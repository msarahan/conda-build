from collections import OrderedDict
from fnmatch import fnmatch
from glob import glob
import io
import json
import os
import re
import sys

from bs4 import UnicodeDammit
import yaml
from conda import __version__ as conda_version
from conda_package_handling import api as cph

from conda_build.conda_interface import prefix_placeholder
from conda_build.conda_interface import PY3
from conda_build.conda_interface import text_type
from conda_build.conda_interface import root_dir
from conda_build.conda_interface import conda_private
from conda_build.conda_interface import get_rc_urls
from conda_build.conda_interface import CrossPlatformStLink
from conda_build.conda_interface import EntityEncoder
from conda_build.conda_interface import PathType, FileMode

from conda_build.conda_interface import TemporaryDirectory
from conda_build import __version__ as conda_build_version
from conda_build import environ
from conda_build import source
from conda_build import tarcheck
from conda_build import utils
import conda_build.noarch_python as noarch_python
import conda_build.os_utils.external as external
from conda_build.exceptions import indent, CondaBuildException
from conda_build.post import post_process, post_build
from conda_build.post import fix_permissions, get_build_metadata
from conda_build.metadata import FIELDS, default_structs
from conda_build.render import output_yaml
from conda_build.create_test import create_all_test_files

CONDA_PACKAGE_FORMAT_VERSION = 2


def _sanitize_channel(channel):
    return re.sub(r'\/t\/[a-zA-Z0-9\-]*\/', '/t/<TOKEN>/', channel)


def _rewrite_file_with_new_prefix(path, data, old_prefix, new_prefix):
    # Old and new prefix should be bytes

    st = os.stat(path)
    data = data.replace(old_prefix, new_prefix)
    # Save as
    with open(path, 'wb') as fo:
        fo.write(data)
    os.chmod(path, stat.S_IMODE(st.st_mode) | stat.S_IWUSR)  # chmod u+w
    return data


def _have_prefix_files(files, prefix):
    '''
    Yields files that contain the current prefix in them, and modifies them
    to replace the prefix with a placeholder.

    :param files: Filenames to check for instances of prefix
    :type files: list of tuples containing strings (prefix, mode, filename)
    '''

    prefix_bytes = prefix.encode(utils.codec)
    prefix_placeholder_bytes = prefix_placeholder.encode(utils.codec)
    if utils.on_win:
        forward_slash_prefix = prefix.replace('\\', '/')
        forward_slash_prefix_bytes = forward_slash_prefix.encode(utils.codec)
        double_backslash_prefix = prefix.replace('\\', '\\\\')
        double_backslash_prefix_bytes = double_backslash_prefix.encode(utils.codec)

    for f in files:
        if f.endswith(('.pyc', '.pyo')):
            continue
        path = os.path.join(prefix, f)
        if not os.path.isfile(path):
            continue
        if sys.platform != 'darwin' and os.path.islink(path):
            # OSX does not allow hard-linking symbolic links, so we cannot
            # skip symbolic links (as we can on Linux)
            continue

        # dont try to mmap an empty file
        if os.stat(path).st_size == 0:
            continue

        try:
            fi = open(path, 'rb+')
        except IOError:
            log = utils.get_logger(__name__)
            log.warn("failed to open %s for detecting prefix.  Skipping it." % f)
            continue
        try:
            mm = utils.mmap_mmap(fi.fileno(), 0, tagname=None, flags=utils.mmap_MAP_PRIVATE)
        except OSError:
            mm = fi.read()

        mode = 'binary' if mm.find(b'\x00') != -1 else 'text'
        if mode == 'text':
            if not utils.on_win and mm.find(prefix_bytes) != -1:
                # Use the placeholder for maximal backwards compatibility, and
                # to minimize the occurrences of usernames appearing in built
                # packages.
                data = mm[:]
                mm.close()
                fi.close()
                _rewrite_file_with_new_prefix(path, data, prefix_bytes, prefix_placeholder_bytes)
                fi = open(path, 'rb+')
                mm = utils.mmap_mmap(fi.fileno(), 0, tagname=None, flags=utils.mmap_MAP_PRIVATE)
        if mm.find(prefix_bytes) != -1:
            yield (prefix, mode, f)
        if utils.on_win and mm.find(forward_slash_prefix_bytes) != -1:
            # some windows libraries use unix-style path separators
            yield (forward_slash_prefix, mode, f)
        elif utils.on_win and mm.find(double_backslash_prefix_bytes) != -1:
            # some windows libraries have double backslashes as escaping
            yield (double_backslash_prefix, mode, f)
        if mm.find(prefix_placeholder_bytes) != -1:
            yield (prefix_placeholder, mode, f)
        mm.close()
        fi.close()


def _has_prefix(short_path, files_with_prefix):
    for prefix, mode, filename in files_with_prefix:
        if short_path == filename:
            return prefix, mode
    return None, None


def _get_files_with_prefix(m, files, prefix):
    files_with_prefix = sorted(_have_prefix_files(files, prefix))

    ignore_files = m.ignore_prefix_files()
    ignore_types = set()
    if not hasattr(ignore_files, "__iter__"):
        if ignore_files is True:
            ignore_types.update((FileMode.text.name, FileMode.binary.name))
        ignore_files = []
    if not m.get_value('build/detect_binary_files_with_prefix', True):
        ignore_types.update((FileMode.binary.name,))
    # files_with_prefix is a list of tuples containing (prefix_placeholder, file_type, file_path)
    ignore_files.extend(
        f[2] for f in files_with_prefix if f[1] in ignore_types and f[2] not in ignore_files)
    files_with_prefix = [f for f in files_with_prefix if f[2] not in ignore_files]
    return files_with_prefix


def _detect_and_record_prefix_files(m, files, prefix):
    files_with_prefix = _get_files_with_prefix(m, files, prefix)
    binary_has_prefix_files = m.binary_has_prefix_files()
    text_has_prefix_files = m.has_prefix_files()

    if files_with_prefix and not m.noarch:
        if utils.on_win:
            # Paths on Windows can contain spaces, so we need to quote the
            # paths. Fortunately they can't contain quotes, so we don't have
            # to worry about nested quotes.
            fmt_str = '"%s" %s "%s"\n'
        else:
            # Don't do it everywhere because paths on Unix can contain quotes,
            # and we don't have a good method of escaping, and because older
            # versions of conda don't support quotes in has_prefix
            fmt_str = '%s %s %s\n'

        with open(os.path.join(m.config.info_dir, 'has_prefix'), 'w') as fo:
            for pfix, mode, fn in files_with_prefix:
                print("Detected hard-coded path in %s file %s" % (mode, fn))
                fo.write(fmt_str % (pfix, mode, fn))

                if mode == 'binary' and fn in binary_has_prefix_files:
                    binary_has_prefix_files.remove(fn)
                elif mode == 'text' and fn in text_has_prefix_files:
                    text_has_prefix_files.remove(fn)

    # make sure we found all of the files expected
    errstr = ""
    for f in text_has_prefix_files:
        errstr += "Did not detect hard-coded path in %s from has_prefix_files\n" % f
    for f in binary_has_prefix_files:
        errstr += "Did not detect hard-coded path in %s from binary_has_prefix_files\n" % f
    if errstr:
        raise RuntimeError(errstr)


def _get_short_path(m, target_file):
    entry_point_script_names = utils.get_entry_point_script_names(m.get_value('build/entry_points'))
    if m.noarch == 'python':
        if target_file.find("site-packages") >= 0:
            return target_file[target_file.find("site-packages"):]
        elif target_file.startswith("bin") and (target_file not in entry_point_script_names):
            return target_file.replace("bin", "python-scripts")
        elif target_file.startswith("Scripts") and (target_file not in entry_point_script_names):
            return target_file.replace("Scripts", "python-scripts")
        else:
            return target_file
    elif m.get_value('build/noarch_python', None):
        return None
    else:
        return target_file


def _is_no_link(no_link, short_path):
    no_link = utils.ensure_list(no_link)
    if any(fnmatch.fnmatch(short_path, p) for p in no_link):
        return True


def _get_inode_paths(files, target_short_path, prefix):
    utils.ensure_list(files)
    target_short_path_inode = os.lstat(os.path.join(prefix, target_short_path)).st_ino
    hardlinked_files = [sp for sp in files
                        if os.lstat(os.path.join(prefix, sp)).st_ino == target_short_path_inode]
    return sorted(hardlinked_files)


def _path_type(path):
    return PathType.softlink if os.path.islink(path) else PathType.hardlink


def _build_info_files_json_v1(m, prefix, files, files_with_prefix):
    no_link_files = m.get_value('build/no_link')
    files_json = []
    for fi in sorted(files):
        prefix_placeholder, file_mode = _has_prefix(fi, files_with_prefix)
        path = os.path.join(prefix, fi)
        short_path = _get_short_path(m, fi)
        if short_path:
            short_path = short_path.replace('\\', '/').replace('\\\\', '/')
        file_info = {
            "_path": short_path,
            "sha256": utils.sha256_checksum(path),
            "size_in_bytes": os.path.getsize(path),
            "path_type": _path_type(path),
        }
        no_link = _is_no_link(no_link_files, fi)
        if no_link:
            file_info["no_link"] = no_link
        if prefix_placeholder and file_mode:
            file_info["prefix_placeholder"] = prefix_placeholder
            file_info["file_mode"] = file_mode
        if file_info.get("path_type") == PathType.hardlink and CrossPlatformStLink.st_nlink(
                os.path.join(prefix, fi)) > 1:
            inode_paths = _get_inode_paths(files, fi, prefix)
            file_info["inode_paths"] = inode_paths
        files_json.append(file_info)
    return files_json


def _create_info_files_json_v1(m, info_dir, prefix, files, files_with_prefix):
    # fields: "_path", "sha256", "size_in_bytes", "path_type", "file_mode",
    #         "prefix_placeholder", "no_link", "inode_paths"
    files_json_files = _build_info_files_json_v1(m, prefix, files, files_with_prefix)
    files_json_info = {
        "paths_version": 1,
        "paths": files_json_files,
    }

    # don't create info/paths.json file if this is an old noarch package
    if not m.noarch_python:
        with open(os.path.join(info_dir, 'paths.json'), "w") as files_json:
            json.dump(files_json_info, files_json, sort_keys=True, indent=2, separators=(',', ': '),
                    cls=EntityEncoder)
    # Return a dict of file: sha1sum. We could (but currently do not)
    # use this to detect overlap and mutated overlap.
    checksums = dict()
    for file in files_json_files:
        checksums[file['_path']] = file['sha256']

    return checksums


def _write_info_files_file(m, files):
    entry_point_scripts = m.get_value('build/entry_points')
    entry_point_script_names = utils.get_entry_point_script_names(entry_point_scripts)

    mode_dict = {'mode': 'w', 'encoding': 'utf-8'} if PY3 else {'mode': 'wb'}
    with open(os.path.join(m.config.info_dir, 'files'), **mode_dict) as fo:
        if m.noarch == 'python':
            for f in sorted(files):
                if f.find("site-packages") >= 0:
                    fo.write(f[f.find("site-packages"):] + '\n')
                elif f.startswith("bin") and (f not in entry_point_script_names):
                    fo.write(f.replace("bin", "python-scripts") + '\n')
                elif f.startswith("Scripts") and (f not in entry_point_script_names):
                    fo.write(f.replace("Scripts", "python-scripts") + '\n')
                else:
                    fo.write(f + '\n')
        else:
            for f in sorted(files):
                fo.write(f + '\n')


def _write_link_json(m):
    package_metadata = OrderedDict()
    noarch_type = m.get_value('build/noarch')
    if noarch_type:
        noarch_dict = OrderedDict(type=text_type(noarch_type))
        if text_type(noarch_type).lower() == "python":
            entry_points = m.get_value('build/entry_points')
            if entry_points:
                noarch_dict['entry_points'] = entry_points
        package_metadata['noarch'] = noarch_dict

    preferred_env = m.get_value("build/preferred_env")
    if preferred_env:
        preferred_env_dict = OrderedDict(name=text_type(preferred_env))
        executable_paths = m.get_value("build/preferred_env_executable_paths")
        if executable_paths:
            preferred_env_dict["executable_paths"] = executable_paths
        package_metadata["preferred_env"] = preferred_env_dict
    if package_metadata:
        # The original name of this file was info/package_metadata_version.json, but we've
        #   now changed it to info/link.json.  Still, we must indefinitely keep the key name
        #   package_metadata_version, or we break conda.
        package_metadata["package_metadata_version"] = 1
        with open(os.path.join(m.config.info_dir, "link.json"), 'w') as fh:
            fh.write(json.dumps(package_metadata, sort_keys=True, indent=2, separators=(',', ': ')))


def _write_about_json(m):
    with open(os.path.join(m.config.info_dir, 'about.json'), 'w') as fo:
        d = {}
        for key in FIELDS["about"]:
            value = m.get_value('about/%s' % key)
            if value:
                d[key] = value
            if default_structs.get('about/%s' % key) == list:
                d[key] = utils.ensure_list(value)

        # for sake of reproducibility, record some conda info
        d['conda_version'] = conda_version
        d['conda_build_version'] = conda_build_version
        # conda env will be in most, but not necessarily all installations.
        #    Don't die if we don't see it.
        stripped_channels = []
        for channel in get_rc_urls() + list(m.config.channel_urls):
            stripped_channels.append(_sanitize_channel(channel))
        d['channels'] = stripped_channels
        evars = ['CIO_TEST']

        d['env_vars'] = {ev: os.getenv(ev, '<not set>') for ev in evars}
        # this information will only be present in conda 4.2.10+
        try:
            d['conda_private'] = conda_private
        except (KeyError, AttributeError):
            pass
        env = environ.Environment(root_dir)
        d['root_pkgs'] = env.package_specs()
        # Include the extra section of the metadata in the about.json
        d['extra'] = m.get_section('extra')
        json.dump(d, fo, indent=2, sort_keys=True)


def _write_info_json(m):
    info_index = m.info_index()
    if m.pin_depends:
        # Wtih 'strict' depends, we will have pinned run deps during rendering
        if m.pin_depends == 'strict':
            runtime_deps = m.meta.get('requirements', {}).get('run', [])
            info_index['depends'] = runtime_deps
        else:
            runtime_deps = environ.get_pinned_deps(m, 'run')
        with open(os.path.join(m.config.info_dir, 'requires'), 'w') as fo:
            fo.write("""\
# This file as created when building:
#
#     %s.tar.bz2  (on '%s')
#
# It can be used to create the runtime environment of this package using:
# $ conda create --name <env> --file <this file>
""" % (m.dist(), m.config.build_subdir))
            for dist in sorted(runtime_deps + [' '.join(m.dist().rsplit('-', 2))]):
                fo.write('%s\n' % '='.join(dist.split()))

    # Deal with Python 2 and 3's different json module type reqs
    mode_dict = {'mode': 'w', 'encoding': 'utf-8'} if PY3 else {'mode': 'wb'}
    with open(os.path.join(m.config.info_dir, 'index.json'), **mode_dict) as fo:
        json.dump(info_index, fo, indent=2, sort_keys=True)


def _write_no_link(m, files):
    no_link = m.get_value('build/no_link')
    if no_link:
        if not isinstance(no_link, list):
            no_link = [no_link]
        with open(os.path.join(m.config.info_dir, 'no_link'), 'w') as fo:
            for f in files:
                if any(fnmatch.fnmatch(f, p) for p in no_link):
                    fo.write(f + '\n')


def _write_run_exports(m):
    run_exports = m.meta.get('build', {}).get('run_exports', {})
    if run_exports:
        with open(os.path.join(m.config.info_dir, 'run_exports.json'), 'w') as f:
            if not hasattr(run_exports, 'keys'):
                run_exports = {'weak': run_exports}
            for k in ('weak', 'strong'):
                if k in run_exports:
                    run_exports[k] = utils.ensure_list(run_exports[k])
            json.dump(run_exports, f)


def _copy_top_level_recipe(path, config, dest_dir, destination_subdir=None):
    files = utils.rec_glob(path, "*")
    file_paths = sorted([f.replace(path + os.sep, '') for f in files])

    # when this actually has a value, we're copying the top-level recipe into a subdirectory,
    #    so that we have record of what parent recipe produced subpackages.
    if destination_subdir:
        dest_dir = os.path.join(dest_dir, destination_subdir)
    else:
        # exclude meta.yaml because the json dictionary captures its content
        file_paths = [f for f in file_paths if not (f == 'meta.yaml' or
                                                    f == 'conda_build_config.yaml')]
    file_paths = utils.filter_files(file_paths, path)
    for f in file_paths:
        utils.copy_into(os.path.join(path, f), os.path.join(dest_dir, f),
                        timeout=config.timeout,
                        locking=config.locking, clobber=True)


def _copy_output_recipe(m, dest_dir):
    _copy_top_level_recipe(m.path, m.config, dest_dir, 'parent')

    this_output = m.get_rendered_output(m.name()) or {}
    install_script = this_output.get('script')
    build_inputs = []
    inputs = [install_script] + build_inputs
    file_paths = [script for script in inputs if script]
    file_paths = utils.filter_files(file_paths, m.path)

    for f in file_paths:
        utils.copy_into(os.path.join(m.path, f), os.path.join(dest_dir, f),
                        timeout=m.config.timeout,
                        locking=m.config.locking, clobber=True)


def _copy_recipe(m):
    if m.config.include_recipe and m.include_recipe():
        # store the rendered meta.yaml file, plus information about where it came from
        #    and what version of conda-build created it
        recipe_dir = os.path.join(m.config.info_dir, 'recipe')
        try:
            os.makedirs(recipe_dir)
        except:
            pass

        original_recipe = ""

        if m.is_output:
            _copy_output_recipe(m, recipe_dir)
        else:
            _copy_top_level_recipe(m.path, m.config, recipe_dir)
            original_recipe = m.meta_path

        output_metadata = m.copy()
        # hard code the build string, so that tests don't get it mixed up
        build = output_metadata.meta.get('build', {})
        build['string'] = output_metadata.build_id()
        output_metadata.meta['build'] = build

        # just for lack of confusion, don't show outputs in final rendered recipes
        if 'outputs' in output_metadata.meta:
            del output_metadata.meta['outputs']
        if 'parent_recipe' in output_metadata.meta.get('extra', {}):
            del output_metadata.meta['extra']['parent_recipe']

        utils.sort_list_in_nested_structure(output_metadata.meta,
                                            ('build/script', 'test/commands'))

        rendered = output_yaml(output_metadata)

        if original_recipe:
            with open(original_recipe, 'rb') as f:
                original_recipe_text = UnicodeDammit(f.read()).unicode_markup

        if not original_recipe or not original_recipe_text == rendered:
            with open(os.path.join(recipe_dir, "meta.yaml"), 'w') as f:
                f.write("# This file created by conda-build {}\n".format(conda_build_version))
                if original_recipe:
                    f.write("# meta.yaml template originally from:\n")
                    f.write("# " + source.get_repository_info(m.path) + "\n")
                f.write("# ------------------------------------------------\n\n")
                f.write(rendered)
            if original_recipe:
                utils.copy_into(original_recipe, os.path.join(recipe_dir, 'meta.yaml.template'),
                                timeout=m.config.timeout, locking=m.config.locking, clobber=True)

        # dump the full variant in use for this package to the recipe folder
        with open(os.path.join(recipe_dir, 'conda_build_config.yaml'), 'w') as f:
            yaml.dump(m.config.variant, f)


def _copy_readme(m):
    readme = m.get_value('about/readme')
    if readme:
        src = os.path.join(m.config.work_dir, readme)
        if not os.path.isfile(src):
            sys.exit("Error: no readme file: %s" % readme)
        dst = os.path.join(m.config.info_dir, readme)
        utils.copy_into(src, dst, m.config.timeout, locking=m.config.locking)
        if os.path.split(readme)[1] not in {"README.md", "README.rst", "README"}:
            print("WARNING: anaconda.org only recognizes about/readme "
                  "as README.md and README.rst", file=sys.stderr)


def _copy_license(m):
    license_file = m.get_value('about/license_file')
    if license_file:
        src_file = os.path.join(m.config.work_dir, license_file)
        if not os.path.isfile(src_file):
            src_file = os.path.join(m.path, license_file)
        if os.path.isfile(src_file):
            utils.copy_into(src_file,
                            os.path.join(m.config.info_dir, 'LICENSE.txt'), m.config.timeout,
                            locking=m.config.locking)
            print("Packaged license file.")
        else:
            raise ValueError("License file given in about/license_file ({}) does not exist in "
                             "source root dir or in recipe root dir (with meta.yaml)".format(src_file))


def _copy_recipe_log(m):
    # the purpose of this file is to capture some change history metadata that may tell people
    #    why a given build was changed the way that it was
    log_file = m.get_value('about/recipe_log_file') or "recipe_log.json"
    # look in recipe folder first
    src_file = os.path.join(m.path, log_file)
    if not os.path.isfile(src_file):
        src_file = os.path.join(m.config.work_dir, log_file)
    if os.path.isfile(src_file):
        utils.copy_into(src_file,
                        os.path.join(m.config.info_dir, 'recipe_log.json'), m.config.timeout,
                        locking=m.config.locking)


def _write_hash_input(m):
    recipe_input = m.get_hash_contents()
    with open(os.path.join(m.config.info_dir, 'hash_input.json'), 'w') as f:
        json.dump(recipe_input, f, indent=2)


def _create_info_files(m, files, prefix):
    '''
    Creates the metadata files that will be stored in the built package.

    :param m: Package metadata
    :type m: Metadata
    :param files: Paths to files to include in package
    :type files: list of str
    '''
    if utils.on_win:
        # make sure we use '/' path separators in metadata
        files = [_f.replace('\\', '/') for _f in files]

    if m.config.filename_hashing:
        _write_hash_input(m)
    _write_info_json(m)  # actually index.json
    _write_about_json(m)
    _write_link_json(m)
    _write_run_exports(m)

    _copy_recipe(m)
    _copy_readme(m)
    _copy_license(m)
    _copy_recipe_log(m)

    create_all_test_files(m, test_dir=os.path.join(m.config.info_dir, 'test'))
    if m.config.copy_test_source_files:
        utils.copy_test_source_files(m, os.path.join(m.config.info_dir, 'test'))

    _write_info_files_file(m, files)

    files_with_prefix = _get_files_with_prefix(m, files, prefix)
    checksums = _create_info_files_json_v1(m, m.config.info_dir, prefix, files, files_with_prefix)

    _detect_and_record_prefix_files(m, files, prefix)
    _write_no_link(m, files)

    sources = m.get_section('source')
    if hasattr(sources, 'keys'):
        sources = [sources]

    with io.open(os.path.join(m.config.info_dir, 'git'), 'w', encoding='utf-8') as fo:
        for src in sources:
            if src.get('git_url'):
                source.git_info(os.path.join(m.config.work_dir, src.get('folder', '')),
                                verbose=m.config.verbose, fo=fo)

    if m.get_value('app/icon'):
        utils.copy_into(os.path.join(m.path, m.get_value('app/icon')),
                        os.path.join(m.config.info_dir, 'icon.png'),
                        m.config.timeout, locking=m.config.locking)
    return checksums


def _create_post_scripts(m):
    '''
    Create scripts to run after build step
    '''
    ext = '.bat' if utils.on_win else '.sh'
    for tp in 'pre-link', 'post-link', 'pre-unlink':
        # To have per-output link scripts they must be prefixed by the output name or be explicitly
        #    specified in the build section
        is_output = 'package:' not in m.get_recipe_text()
        scriptname = tp
        if is_output:
            if m.meta.get('build', {}).get(tp, ''):
                scriptname = m.meta['build'][tp]
            else:
                scriptname = m.name() + '-' + tp
        scriptname += ext
        dst_name = '.' + m.name() + '-' + tp + ext
        src = os.path.join(m.path, scriptname)
        if os.path.isfile(src):
            dst_dir = os.path.join(m.config.host_prefix,
                           'Scripts' if m.config.host_subdir.startswith('win-') else 'bin')
            if not os.path.isdir(dst_dir):
                os.makedirs(dst_dir, 0o775)
            dst = os.path.join(dst_dir, dst_name)
            utils.copy_into(src, dst, m.config.timeout, locking=m.config.locking)
            os.chmod(dst, 0o775)


def _post_process_files(m, initial_prefix_files):
    get_build_metadata(m)
    _create_post_scripts(m)

    # this is new-style noarch, with a value of 'python'
    if m.noarch != 'python':
        utils.create_entry_points(m.get_value('build/entry_points'), config=m.config)
    current_prefix_files = utils.prefix_files(prefix=m.config.host_prefix)

    python = (m.config.build_python if os.path.isfile(m.config.build_python) else
              m.config.host_python)
    post_process(m.get_value('package/name'), m.get_value('package/version'),
                 sorted(current_prefix_files - initial_prefix_files),
                 prefix=m.config.host_prefix,
                 config=m.config,
                 preserve_egg_dir=bool(m.get_value('build/preserve_egg_dir')),
                 noarch=m.get_value('build/noarch'),
                 skip_compile_pyc=m.get_value('build/skip_compile_pyc'))

    # The post processing may have deleted some files (like easy-install.pth)
    current_prefix_files = utils.prefix_files(prefix=m.config.host_prefix)
    new_files = sorted(current_prefix_files - initial_prefix_files)
    new_files = utils.filter_files(new_files, prefix=m.config.host_prefix)

    if any(m.config.meta_dir in os.path.join(m.config.host_prefix, f) for f in new_files):
        meta_files = (tuple(f for f in new_files if m.config.meta_dir in
                os.path.join(m.config.host_prefix, f)),)
        sys.exit(indent("""Error: Untracked file(s) %s found in conda-meta directory.
This error usually comes from using conda in the build script.  Avoid doing this, as it
can lead to packages that include their dependencies.""" % meta_files))
    post_build(m, new_files, build_python=python)

    entry_point_script_names = utils.get_entry_point_script_names(m.get_value('build/entry_points'))
    if m.noarch == 'python':
        pkg_files = [fi for fi in new_files if fi not in entry_point_script_names]
    else:
        pkg_files = new_files

    # the legacy noarch
    if m.get_value('build/noarch_python'):
        noarch_python.transform(m, new_files, m.config.host_prefix)
    # new way: build/noarch: python
    elif m.noarch == 'python':
        noarch_python.populate_files(m, pkg_files, m.config.host_prefix, entry_point_script_names)

    current_prefix_files = utils.prefix_files(prefix=m.config.host_prefix)
    new_files = current_prefix_files - initial_prefix_files
    fix_permissions(new_files, m.config.host_prefix)

    return new_files


def _guess_interpreter(script_filename):
    # -l is needed for MSYS2 as the login scripts set some env. vars (TMP, TEMP)
    # Since the MSYS2 installation is probably a set of conda packages we do not
    # need to worry about system environmental pollution here. For that reason I
    # do not pass -l on other OSes.
    extensions_to_run_commands = {'.sh': ['bash{}'.format('.exe' if utils.on_win else '')],
                                  '.bat': [os.environ.get('COMSPEC', 'cmd.exe'), '/d', '/c'],
                                  '.ps1': ['powershell', '-executionpolicy', 'bypass', '-File'],
                                  '.py': ['python']}
    file_ext = os.path.splitext(script_filename)[1]
    for ext, command in extensions_to_run_commands.items():
        if file_ext.lower().startswith(ext):
            interpreter_command = command
            break
    else:
        raise NotImplementedError("Don't know how to run {0} file.   Please specify "
                                  "script_interpreter for {1} output".format(file_ext,
                                                                             script_filename))
    return interpreter_command


def _write_activation_text(script_path, m):
    with open(script_path, 'r+') as fh:
        data = fh.read()
        fh.seek(0)
        if os.path.splitext(script_path)[1].lower() == ".bat":
            utils._write_bat_activation_text(fh, m)
        elif os.path.splitext(script_path)[1].lower() == ".sh":
            utils._write_sh_activation_text(fh, m)
        else:
            log = utils.get_logger(__name__)
            log.warn("not adding activation to {} - I don't know how to do so for "
                        "this file type".format(script_path))
        fh.write(data)


def _get_conda_package_file_list(output, metadata, env, stats, **kw):
    log = utils.get_logger(__name__)
    log.info('Packaging %s', metadata.dist())

    files = output.get('files', [])

    # this is because without any requirements at all, we still need to have the host prefix exist
    try:
        os.makedirs(metadata.config.host_prefix)
    except OSError:
        pass

    # Use script from recipe?
    script = utils.ensure_list(metadata.get_value('build/script', None))

    # need to treat top-level stuff specially.  build/script in top-level stuff should not be
    #     re-run for an output with a similar name to the top-level recipe
    is_output = 'package:' not in metadata.get_recipe_text()
    top_build = metadata.get_top_level_recipe_without_outputs().get('build', {}) or {}
    activate_script = metadata.activate_build_script
    if (script and not output.get('script')) and (is_output or not top_build.get('script')):
        # do add in activation, but only if it's not disabled
        activate_script = metadata.config.activate
        script = '\n'.join(script)
        suffix = "bat" if utils.on_win else "sh"
        script_fn = output.get('script') or 'output_script.{}'.format(suffix)
        with open(os.path.join(metadata.config.work_dir, script_fn), 'w') as f:
            f.write('\n')
            f.write(script)
            f.write('\n')
        output['script'] = script_fn

    if output.get('script'):
        env = environ.get_dict(m=metadata)

        interpreter = output.get('script_interpreter')
        if not interpreter:
            interpreter_and_args = _guess_interpreter(output['script'])
            interpreter_and_args[0] = external.find_executable(interpreter_and_args[0],
                                                               metadata.config.build_prefix)
            if not interpreter_and_args[0]:
                log.error("Did not find an interpreter to run {}, looked for {}".format(
                    output['script'], interpreter_and_args[0]))
        else:
            interpreter_and_args = interpreter.split(' ')

        initial_files = utils.prefix_files(metadata.config.host_prefix)
        env_output = env.copy()
        env_output['TOP_PKG_NAME'] = env['PKG_NAME']
        env_output['TOP_PKG_VERSION'] = env['PKG_VERSION']
        env_output['PKG_VERSION'] = metadata.version()
        env_output['PKG_NAME'] = metadata.get_value('package/name')
        for var in utils.ensure_list(metadata.get_value('build/script_env')):
            if var not in os.environ:
                raise ValueError("env var '{}' specified in script_env, but is not set."
                                    .format(var))
            env_output[var] = os.environ[var]
        dest_file = os.path.join(metadata.config.work_dir, output['script'])
        utils.copy_into(os.path.join(metadata.path, output['script']), dest_file)
        if activate_script:
            _write_activation_text(dest_file, metadata)

        bundle_stats = {}
        utils.check_call_env(interpreter_and_args + [dest_file],
                             cwd=metadata.config.work_dir, env=env_output, stats=bundle_stats)
        utils.log_stats(bundle_stats, "bundling {}".format(metadata.name()))
        if stats is not None:
            stats[utils.stats_key(metadata, 'bundle_{}'.format(metadata.name()))] = bundle_stats

    elif files:
        # Files is specified by the output
        # we exclude the list of files that we want to keep, so post-process picks them up as "new"
        keep_files = set(os.path.normpath(pth)
                         for pth in utils.expand_globs(files, metadata.config.host_prefix))
        pfx_files = set(utils.prefix_files(metadata.config.host_prefix))
        initial_files = set(item for item in (pfx_files - keep_files)
                            if not any(keep_file.startswith(item + os.path.sep)
                                       for keep_file in keep_files))
        initial_files = set(item for item in (pfx_files - keep_files)
                            if not any(keep_file.startswith(item + os.path.sep)
                                       for keep_file in keep_files))
    else:
        if not metadata.always_include_files():
            log.warn("No files or script found for output {}".format(output.get('name')))
            build_deps = metadata.get_value('requirements/build')
            host_deps = metadata.get_value('requirements/host')
            build_pkgs = [pkg.split()[0] for pkg in build_deps]
            host_pkgs = [pkg.split()[0] for pkg in host_deps]
            dangerous_double_deps = {'python': 'PYTHON', 'r-base': 'R'}
            for dep, env_var_name in dangerous_double_deps.items():
                if all(dep in pkgs_list for pkgs_list in (build_pkgs, host_pkgs)):
                    raise CondaBuildException("Empty package; {0} present in build and host deps.  "
                                              "You probably picked up the build environment's {0} "
                                              " executable.  You need to alter your recipe to "
                                              " use the {1} env var in your recipe to "
                                              "run that executable.".format(dep, env_var_name))
                elif (dep in build_pkgs and metadata.uses_new_style_compiler_activation):
                    link = ("https://conda.io/docs/user-guide/tasks/build-packages/"
                            "define-metadata.html#host")
                    raise CondaBuildException("Empty package; {0} dep present in build but not "
                                              "host requirements.  You need to move your {0} dep "
                                              "to the host requirements section.  See {1} for more "
                                              "info." .format(dep, link))
        initial_files = set(utils.prefix_files(metadata.config.host_prefix))

    for pat in metadata.always_include_files():
        has_matches = False
        for f in set(initial_files):
            if fnmatch(f, pat):
                print("Including in package existing file", f)
                initial_files.remove(f)
                has_matches = True
        if not has_matches:
            log.warn("Glob %s from always_include_files does not match any files", pat)
    files = _post_process_files(metadata, initial_files)

    if output.get('name') and output.get('name') != 'conda':
        assert 'bin/conda' not in files and 'Scripts/conda.exe' not in files, ("Bug in conda-build "
            "has included conda binary in package. Please report this on the conda-build issue "
            "tracker.")

    # first filter is so that info_files does not pick up ignored files
    files = utils.filter_files(files, prefix=metadata.config.host_prefix)
    # this is also copying things like run_test.sh into info/recipe
    utils.rm_rf(os.path.join(metadata.config.info_dir, 'test'))

    with utils.tmp_chdir(metadata.config.host_prefix):
        output['checksums'] = _create_info_files(metadata, files, prefix=metadata.config.host_prefix)

    # here we add the info files into the prefix, so we want to re-collect the files list
    prefix_files = set(utils.prefix_files(metadata.config.host_prefix))
    files = utils.filter_files(prefix_files - initial_files, prefix=metadata.config.host_prefix)
    if kw.get("new_pkg_split"):
        # files are split into paths starting with "info" (metadata) and the actual package content
        #    This is used by the new package format.
        info_files = {f for f in files if f.startswith('info')}
        files = {'info': info_files,
                 'package': set(files) - info_files}
    return files


def _sort_file_order(prefix, files):
    """Sort by filesize or by binsort, to optimize compression"""
    def order(f):
        # we don't care about empty files so send them back via 100000
        fsize = os.stat(os.path.join(prefix, f)).st_size or 100000
        # info/* records will be False == 0, others will be 1.
        info_order = int(os.path.dirname(f) != 'info')
        if info_order:
            _, ext = os.path.splitext(f)
            # Strip any .dylib.* and .so.* and rename .dylib to .so
            ext = re.sub(r'(\.dylib|\.so).*$', r'.so', ext)
            if not ext:
                # Files without extensions should be sorted by dirname
                info_order = 1 + hash(os.path.dirname(f)) % (10 ** 8)
            else:
                info_order = 1 + abs(hash(ext)) % (10 ** 8)
        return info_order, fsize

    binsort = os.path.join(sys.prefix, 'bin', 'binsort')
    if os.path.exists(binsort):
        with NamedTemporaryFile(mode='w', suffix='.filelist', delete=False) as fl:
            with utils.tmp_chdir(prefix):
                fl.writelines(map(lambda x: '.' + os.sep + x + '\n', files))
                fl.close()
                cmd = binsort + ' -t 1 -q -d -o 1000 {}'.format(fl.name)
                out, _ = subprocess.Popen(cmd, shell=True,
                                            stdout=subprocess.PIPE).communicate()
                files_list = out.decode('utf-8').strip().split('\n')
                # binsort returns the absolute paths.
                files_list = [f.split(prefix + os.sep, 1)[-1]
                                for f in files_list]
                os.unlink(fl.name)
    else:
        files_list = list(f for f in sorted(files, key=order))
    return files_list


def _create_compressed_tarball(prefix, files, tmpdir, basename, ext, compression_filter, filter_opts=''):
    tmp_path = os.path.join(tmpdir, basename)
    files = _sort_file_order(prefix, files)

    # add files in order of a) in info directory, b) increasing size so
    # we can access small manifest or json files without decompressing
    # possible large binary or data files
    fullpath = tmp_path + ext
    print("Compressing to {}".format(fullpath))
    with utils.tmp_chdir(prefix):
        with libarchive.file_writer(fullpath, 'gnutar', filter_name=compression_filter,
                                    options=filter_opts) as archive:
            archive.add_files(*files)
    return fullpath


def _verify_artifacts(metadata, tmp_archive_paths):
    log = utils.get_logger(__name__)
    for artifact in utils.ensure_list(tmp_archive_paths):
        # we're done building, perform some checks
        if artifact.endswith('.tar.bz2'):
            tarcheck.check_all(artifact, metadata.config)
        # we do the import here because we want to respect logger level context
        try:
            from conda_verify.verify import Verify
        except ImportError:
            Verify = None
            log.warn("Importing conda-verify failed.  Please be sure to test your packages.  "
                "conda install conda-verify to make this message go away.")
        if getattr(metadata.config, "verify", False) and Verify:
            verifier = Verify()
            checks_to_ignore = (utils.ensure_list(metadata.config.ignore_verify_codes) +
                                metadata.ignore_verify_codes())
            try:
                verifier.verify_package(path_to_package=artifact, checks_to_ignore=checks_to_ignore,
                                        exit_on_error=metadata.config.exit_on_verify_error)
            except KeyError as e:
                log.warn("Package doesn't have necessary files.  It might be too old to inspect."
                            "Legacy noarch packages are known to fail.  Full message was {}".format(e))


def _move_artifacts_to_output_dir(metadata, artifacts):
    final_outputs = []
    try:
        crossed_subdir = metadata.config.target_subdir
    except AttributeError:
        crossed_subdir = metadata.config.host_subdir
    subdir = ('noarch' if (metadata.noarch or metadata.noarch_python)
            else crossed_subdir)
    if metadata.config.output_folder:
        output_folder = os.path.join(metadata.config.output_folder, subdir)
    else:
        output_folder = os.path.join(os.path.dirname(metadata.config.bldpkgs_dir), subdir)
    for artifact in utils.ensure_list(artifacts):
        fn = os.path.basename(artifact)
        final_output = os.path.join(output_folder, fn)
        if os.path.isfile(final_output):
            utils.rm_rf(final_output)

        # disable locking here.  It's just a temp folder getting locked.  Removing it proved to be
        #    a major bottleneck.
        utils.copy_into(artifact, final_output, metadata.config.timeout, locking=False)
        final_outputs.append(final_output)
    return final_outputs


def _bundle_conda_impl(output, metadata, env, stats, ext, **kw):
    basename = metadata.dist()
    final_outputs = []
    pkg_fn = basename + ext
    file_list = _get_conda_package_file_list(output, metadata, env, stats, **kw)
    tmpdir = kw.get('tmpdir', TemporaryDirectory())
    tmpdir_name = tmpdir.name if hasattr(tmpdir, 'name') else tmpdir
    cph.create(prefix=metadata.config.host_prefix, file_list=file_list,
               out_fn=pkg_fn, out_folder=tmpdir_name)
    tmp_archives = [os.path.join(tmpdir_name, pkg_fn)]
        _verify_artifacts(metadata, tmp_archives)
        final_outputs = _move_artifacts_to_output_dir(metadata, tmp_archives)
    return final_outputs


def bundle_conda_tarball(output, metadata, env, stats, **kw):
    """The 'old' conda format, .tar.bz2 files, where metadata is in the info folder inside the .tar.bz2.
    Also repurposed to produce the inner compressed container for the 'new' conda format"""
    return _bundle_conda_impl(output, metadata, env, stats, ".tar.bz2", **kw)


def _write_conda_pkg_version_spec(tmpdir):
    metadata_file = os.path.join(tmpdir, 'metadata.json')
    pkg_metadata = {'conda_pkg_format_version': CONDA_PACKAGE_FORMAT_VERSION}
    with open(metadata_file, 'w') as f:
        json.dump(pkg_metadata, f)


def bundle_conda(output, metadata, env, stats, **kw):
    """The 'new' conda format, introduced in late 2018/early 2019.  Spec at
    https://anaconda.atlassian.net/wiki/spaces/AD/pages/90210540/Conda+package+format+v2"""
    return _bundle_conda_impl(output, metadata, env, stats, ".conda", **kw)


def bundle_wheel(output, metadata, env, stats):
    ext = ".bat" if utils.on_win else ".sh"
    with TemporaryDirectory() as tmpdir, utils.tmp_chdir(metadata.config.work_dir):
        dest_file = os.path.join(metadata.config.work_dir, 'wheel_output' + ext)
        with open(dest_file, 'w') as f:
            f.write('\n')
            f.write('pip wheel --wheel-dir {} --no-deps .'.format(tmpdir))
            f.write('\n')
        if metadata.config.activate:
            _write_activation_text(dest_file, metadata)

        # run the appropriate script
        env = environ.get_dict(m=metadata).copy()
        env['TOP_PKG_NAME'] = env['PKG_NAME']
        env['TOP_PKG_VERSION'] = env['PKG_VERSION']
        env['PKG_VERSION'] = metadata.version()
        env['PKG_NAME'] = metadata.get_value('package/name')
        interpreter_and_args = _guess_interpreter(dest_file)

        bundle_stats = {}
        utils.check_call_env(interpreter_and_args + [dest_file],
                             cwd=metadata.config.work_dir, env=env, stats=bundle_stats)
        utils.log_stats(bundle_stats, "bundling wheel {}".format(metadata.name()))
        if stats is not None:
            stats[utils.stats_key(metadata, 'bundle_wheel_{}'.format(metadata.name()))] = bundle_stats

        wheel_files = glob(os.path.join(tmpdir, "*.whl"))
        if not wheel_files:
            raise RuntimeError("Wheel creation failed.  Please see output above to debug.")
        wheel_file = wheel_files[0]
        if metadata.config.output_folder:
            output_folder = os.path.join(metadata.config.output_folder, metadata.config.subdir)
        else:
            output_folder = metadata.config.bldpkgs_dir
        utils.copy_into(wheel_file, output_folder, locking=metadata.config.locking)
    return os.path.join(output_folder, os.path.basename(wheel_file))
