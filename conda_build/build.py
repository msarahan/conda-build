'''
Module that does most of the heavy lifting for the ``conda build`` command.
'''
from __future__ import absolute_import, division, print_function

from collections import deque, OrderedDict
from glob import glob
import json
import os
import random
import re
import shutil
import string
import subprocess
import sys
import time

# this is to compensate for a requests idna encoding error.  Conda is a better place to fix,
#   eventually
# exception is raises: "LookupError: unknown encoding: idna"
#    http://stackoverflow.com/a/13057751/1170370
import encodings.idna  # NOQA

try:
    from conda.base.constants import CONDA_TARBALL_EXTENSIONS
except Exception:
    from conda.base.constants import CONDA_TARBALL_EXTENSION
    CONDA_TARBALL_EXTENSIONS = (CONDA_TARBALL_EXTENSION,)

# used to get version
from .conda_interface import env_path_backup_var_exists, conda_45
from .conda_interface import TemporaryDirectory
from .conda_interface import VersionOrder
from .conda_interface import get_rc_urls
from .conda_interface import url_path
from .conda_interface import MatchSpec
from .conda_interface import reset_context
from .conda_interface import context
from .conda_interface import UnsatisfiableError
from .conda_interface import NoPackagesFoundError
from .conda_interface import CondaError
from .conda_interface import pkgs_dirs
from .utils import env_var

from conda_build import environ, utils
from conda_build import bundlers
from conda_build.index import get_build_index, update_index
from conda_build.render import (bldpkg_path, render_recipe, reparse, finalize_metadata,
                                distribute_variants, expand_outputs, try_download,
                                add_upstream_pins, execute_download_actions)
import conda_build.os_utils.external as external
from conda_build.metadata import MetaData
from conda_build.post import get_build_metadata

from conda_build.exceptions import DependencyNeedsBuildingError, CondaBuildException
from conda_build.variants import (set_language_env_vars, dict_of_lists_to_list_of_dicts,
                                  get_package_variants)
from conda_build.create_test import create_all_test_files

from conda import __version__ as conda_version

if sys.platform == 'win32':
    import conda_build.windows as windows

if 'bsd' in sys.platform:
    shell_path = '/bin/sh'
elif utils.on_win:
    shell_path = 'bash'
else:
    shell_path = '/bin/bash'


def scan_metadata(path):
    '''
    Scan all json files in 'path' and return a dictionary with their contents.
    Files are assumed to be in 'index.json' format.
    '''
    installed = dict()
    for filename in glob(os.path.join(path, '*.json')):
        with open(filename) as file:
            data = json.load(file)
            installed[data['name']] = data
    return installed


bundlers = {
    'conda_v2': bundlers.bundle_conda,
    'conda': bundlers.bundle_conda_tarball,
    'wheel': bundlers.bundle_wheel,
}


def create_build_envs(m, notest):
    build_ms_deps = m.ms_depends('build')
    build_ms_deps = [utils.ensure_valid_spec(spec) for spec in build_ms_deps]
    host_ms_deps = m.ms_depends('host')
    host_ms_deps = [utils.ensure_valid_spec(spec) for spec in host_ms_deps]

    m.config._merge_build_host = m.build_is_host

    if m.is_cross and not m.build_is_host:
        if VersionOrder(conda_version) < VersionOrder('4.3.2'):
            raise RuntimeError("Non-native subdir support only in conda >= 4.3.2")

        host_actions = environ.get_install_actions(m.config.host_prefix,
                                                    tuple(host_ms_deps), 'host',
                                                    subdir=m.config.host_subdir,
                                                    debug=m.config.debug,
                                                    verbose=m.config.verbose,
                                                    locking=m.config.locking,
                                                    bldpkgs_dirs=tuple(m.config.bldpkgs_dirs),
                                                    timeout=m.config.timeout,
                                                    disable_pip=m.config.disable_pip,
                                                    max_env_retry=m.config.max_env_retry,
                                                    output_folder=m.config.output_folder,
                                                    channel_urls=tuple(m.config.channel_urls))
        environ.create_env(m.config.host_prefix, host_actions, env='host', config=m.config,
                            subdir=m.config.host_subdir, is_cross=m.is_cross,
                            is_conda=m.name() == 'conda')
    if m.build_is_host:
        build_ms_deps.extend(host_ms_deps)
    build_actions = environ.get_install_actions(m.config.build_prefix,
                                                tuple(build_ms_deps), 'build',
                                                subdir=m.config.build_subdir,
                                                debug=m.config.debug,
                                                verbose=m.config.verbose,
                                                locking=m.config.locking,
                                                bldpkgs_dirs=tuple(m.config.bldpkgs_dirs),
                                                timeout=m.config.timeout,
                                                disable_pip=m.config.disable_pip,
                                                max_env_retry=m.config.max_env_retry,
                                                output_folder=m.config.output_folder,
                                                channel_urls=tuple(m.config.channel_urls))

    try:
        if not notest:
            utils.insert_variant_versions(m.meta.get('requirements', {}),
                                            m.config.variant, 'run')
            test_run_ms_deps = utils.ensure_list(m.get_value('test/requires', [])) + \
                                utils.ensure_list(m.get_value('requirements/run', []))
            # make sure test deps are available before taking time to create build env
            environ.get_install_actions(m.config.test_prefix,
                                        tuple(test_run_ms_deps), 'test',
                                        subdir=m.config.host_subdir,
                                        debug=m.config.debug,
                                        verbose=m.config.verbose,
                                        locking=m.config.locking,
                                        bldpkgs_dirs=tuple(m.config.bldpkgs_dirs),
                                        timeout=m.config.timeout,
                                        disable_pip=m.config.disable_pip,
                                        max_env_retry=m.config.max_env_retry,
                                        output_folder=m.config.output_folder,
                                        channel_urls=tuple(m.config.channel_urls))
    except DependencyNeedsBuildingError as e:
        # subpackages are not actually missing.  We just haven't built them yet.
        from .conda_interface import MatchSpec

        other_outputs = (m.other_outputs.values() if hasattr(m, 'other_outputs') else
                         m.get_output_metadata_set(permit_undefined_jinja=True))
        missing_deps = set(MatchSpec(pkg).name for pkg in e.packages) - set(out.name() for _, out in other_outputs)
        if missing_deps:
            e.packages = missing_deps
            raise e
    if (not m.config.dirty or not os.path.isdir(m.config.build_prefix) or not os.listdir(m.config.build_prefix)):
        environ.create_env(m.config.build_prefix, build_actions, env='build',
                            config=m.config, subdir=m.config.build_subdir,
                            is_cross=m.is_cross, is_conda=m.name() == 'conda')


def build(m, stats, post=None, need_source_download=True, need_reparse_in_env=False,
          built_packages=None, notest=False, provision_only=False):
    '''
    Build the package with the specified metadata.

    :param m: Package metadata
    :type m: Metadata
    :type post: bool or None. None means run the whole build. True means run
    post only. False means stop just before the post.
    :type need_source_download: bool: if rendering failed to download source
    (due to missing tools), retry here after build env is populated
    '''
    default_return = {}
    if not built_packages:
        built_packages = {}

    if m.skip():
        print(utils.get_skip_message(m))
        return default_return

    log = utils.get_logger(__name__)
    host_actions = []
    build_actions = []
    output_metas = []

    with utils.path_prepended(m.config.build_prefix):
        env = environ.get_dict(m=m)
    env["CONDA_BUILD_STATE"] = "BUILD"
    if env_path_backup_var_exists:
        env["CONDA_PATH_BACKUP"] = os.environ["CONDA_PATH_BACKUP"]

    # this should be a no-op if source is already here
    if m.needs_source_for_render:
        try_download(m, no_download_source=False)

    if post in [False, None]:
        output_metas = expand_outputs([(m, need_source_download, need_reparse_in_env)])

        skipped = []
        package_locations = []
        # TODO: should we check both host and build envs?  These are the same, except when
        #    cross compiling.
        for _, om in output_metas:
            if om.skip() or (m.config.skip_existing and is_package_built(om, 'host')):
                skipped.append(bldpkg_path(om))
            else:
                package_locations.append(bldpkg_path(om))
        if not package_locations:
            print("Packages for ", m.path or m.name(), "with variant {} "
                  "are already built and available from your configured channels "
                  "(including local) or are otherwise specified to be skipped."
                  .format(m.get_hash_contents()))
            return default_return

        if not provision_only:
            printed_fns = []
            for pkg in package_locations:
                if (os.path.splitext(pkg)[1] and any(
                        os.path.splitext(pkg)[1] in ext for ext in CONDA_TARBALL_EXTENSIONS)):
                    printed_fns.append(os.path.basename(pkg))
                else:
                    printed_fns.append(pkg)
            print("BUILD START:", printed_fns)

        environ.remove_existing_packages([m.config.bldpkgs_dir],
                [pkg for pkg in package_locations if pkg not in built_packages], m.config)

        specs = [ms.spec for ms in m.ms_depends('build')]
        if any(out.get('type') == 'wheel' for out in m.meta.get('outputs', [])):
            specs.extend(['pip', 'wheel'])

        vcs_source = m.uses_vcs_in_build
        if vcs_source and vcs_source not in specs:
            vcs_executable = "hg" if vcs_source == "mercurial" else vcs_source
            has_vcs_available = os.path.isfile(external.find_executable(vcs_executable,
                                                                m.config.build_prefix) or "")
            if not has_vcs_available:
                if (vcs_source != "mercurial" or not any(spec.startswith('python') and "3." in spec for spec in specs)):
                    specs.append(vcs_source)

                    log.warn("Your recipe depends on %s at build time (for templates), "
                             "but you have not listed it as a build dependency.  Doing "
                             "so for this build.", vcs_source)
                else:
                    raise ValueError("Your recipe uses mercurial in build, but mercurial"
                                    " does not yet support Python 3.  Please handle all of "
                                    "your mercurial actions outside of your build script.")

        exclude_pattern = None
        excludes = set(m.config.variant.get('ignore_version', []))

        for key in m.config.variant.get('pin_run_as_build', {}).keys():
            if key in excludes:
                excludes.remove(key)

        output_excludes = set()
        if hasattr(m, 'other_outputs'):
            output_excludes = set(name for (name, variant) in m.other_outputs.keys())

        if excludes or output_excludes:
            exclude_pattern = re.compile(r'|'.join(r'(?:^{}(?:\s|$|\Z))'.format(exc)
                                            for exc in excludes | output_excludes))

        # this metadata object may not be finalized - the outputs are, but this is the
        #     top-level metadata.  We need to make sure that the variant pins are added in.
        utils.insert_variant_versions(m.meta.get('requirements', {}), m.config.variant, 'build')
        utils.insert_variant_versions(m.meta.get('requirements', {}), m.config.variant, 'host')
        add_upstream_pins(m, False, exclude_pattern)

        create_build_envs(m, notest)

        # this check happens for the sake of tests, but let's do it before the build so we don't
        #     make people wait longer only to see an error
        warn_on_use_of_SRC_DIR(m)

        # Execute any commands fetching the source (e.g., git) in the _build environment.
        # This makes it possible to provide source fetchers (eg. git, hg, svn) as build
        # dependencies.
        with utils.path_prepended(m.config.build_prefix):
            try_download(m, no_download_source=False, raise_error=True)
        if need_source_download and not m.final:
            m.parse_until_resolved(allow_no_other_outputs=True)

        elif need_reparse_in_env:
            m = reparse(m)

        # get_dir here might be just work, or it might be one level deeper,
        #    dependening on the source.
        src_dir = m.config.work_dir
        if os.path.isdir(src_dir):
            if m.config.verbose:
                print("source tree in:", src_dir)
        else:
            if m.config.verbose:
                print("no source - creating empty work folder")
            os.makedirs(src_dir)

        utils.rm_rf(m.config.info_dir)
        files1 = utils.prefix_files(prefix=m.config.host_prefix)
        with open(os.path.join(m.config.build_folder, 'prefix_files.txt'), 'w') as f:
            f.write(u'\n'.join(sorted(list(files1))))
            f.write(u'\n')

        # Use script from recipe?
        script = utils.ensure_list(m.get_value('build/script', None))
        if script:
            script = '\n'.join(script)

        if os.path.isdir(src_dir):
            build_stats = {}
            if utils.on_win:
                build_file = os.path.join(m.path, 'bld.bat')
                if script:
                    build_file = os.path.join(src_dir, 'bld.bat')
                    with open(build_file, 'w') as bf:
                        bf.write(script)
                windows.build(m, build_file, stats=build_stats, provision_only=provision_only)
            else:
                build_file = os.path.join(m.path, 'build.sh')
                if os.path.isfile(build_file) and script:
                    raise CondaBuildException("Found a build.sh script and a build/script section"
                                              "inside meta.yaml. Either remove the build.sh script "
                                              "or remove the build/script section in meta.yaml.")
                # There is no sense in trying to run an empty build script.
                if os.path.isfile(build_file) or script:
                    work_file, _ = write_build_scripts(m, script, build_file)
                    if not provision_only:
                        cmd = [shell_path] + (['-x'] if m.config.debug else []) + ['-e', work_file]

                        # rewrite long paths in stdout back to their env variables
                        if m.config.debug or m.config.no_rewrite_stdout_env:
                            rewrite_env = None
                        else:
                            rewrite_vars = ['PREFIX', 'SRC_DIR']
                            if not m.build_is_host:
                                rewrite_vars.insert(1, 'BUILD_PREFIX')
                            rewrite_env = {
                                k: env[k]
                                for k in rewrite_vars if k in env
                            }
                            for k, v in rewrite_env.items():
                                print('{0} {1}={2}'
                                        .format('set' if build_file.endswith('.bat') else 'export', k, v))

                        # clear this, so that the activate script will get run as necessary
                        del env['CONDA_BUILD']

                        # this should raise if any problems occur while building
                        utils.check_call_env(cmd, env=env, rewrite_stdout_env=rewrite_env,
                                            cwd=src_dir, stats=build_stats)
                        utils.remove_pycache_from_scripts(m.config.host_prefix)
            if build_stats and not provision_only:
                utils.log_stats(build_stats, "building {}".format(m.name()))
                if stats is not None:
                    stats[utils.stats_key(m, 'build')] = build_stats

    prefix_file_list = os.path.join(m.config.build_folder, 'prefix_files.txt')
    initial_files = set()
    if os.path.isfile(prefix_file_list):
        with open(prefix_file_list) as f:
            initial_files = set(f.read().splitlines())
    new_prefix_files = utils.prefix_files(prefix=m.config.host_prefix) - initial_files

    new_pkgs = default_return
    if not provision_only and post in [True, None]:
        outputs = output_metas or m.get_output_metadata_set(permit_unsatisfiable_variants=False)
        top_level_meta = m

        # this is the old, default behavior: conda package, with difference between start
        #    set of files and end set of files
        prefix_file_list = os.path.join(m.config.build_folder, 'prefix_files.txt')
        if os.path.isfile(prefix_file_list):
            with open(prefix_file_list) as f:
                initial_files = set(f.read().splitlines())
        else:
            initial_files = set()

        # subdir needs to always be some real platform - so ignore noarch.
        subdir = (m.config.host_subdir if m.config.host_subdir != 'noarch' else
                    m.config.subdir)

        with TemporaryDirectory() as prefix_files_backup:
            # back up new prefix files, because we wipe the prefix before each output build
            for f in new_prefix_files:
                utils.copy_into(os.path.join(m.config.host_prefix, f),
                                os.path.join(prefix_files_backup, f),
                                symlinks=True)

            # this is the inner loop, where we loop over any vars used only by
            # outputs (not those used by the top-level recipe). The metadata
            # objects here are created by the m.get_output_metadata_set, which
            # is distributing the matrix of used variables.

            for (output_d, m) in outputs:
                if m.skip():
                    print(utils.get_skip_message(m))
                    continue

                # TODO: should we check both host and build envs?  These are the same, except when
                #    cross compiling
                if m.config.skip_existing and is_package_built(m, 'host'):
                    print(utils.get_skip_message(m))
                    new_pkgs[bldpkg_path(m)] = output_d, m
                    continue

                if (top_level_meta.name() == output_d.get('name') and not (output_d.get('files') or
                                                                           output_d.get('script'))):
                    output_d['files'] = (utils.prefix_files(prefix=m.config.host_prefix) -
                                         initial_files)

                # ensure that packaging scripts are copied over into the workdir
                if 'script' in output_d:
                    utils.copy_into(os.path.join(m.path, output_d['script']), m.config.work_dir)

                # same thing, for test scripts
                test_script = output_d.get('test', {}).get('script')
                if test_script:
                    if not os.path.isfile(os.path.join(m.path, test_script)):
                        raise ValueError("test script specified as {} does not exist.  Please "
                                         "check for typos or create the file and try again."
                                         .format(test_script))
                    utils.copy_into(os.path.join(m.path, test_script),
                                    os.path.join(m.config.work_dir, test_script))

                assert output_d.get('type') != 'conda' or m.final, (
                    "output metadata for {} is not finalized".format(m.dist()))
                pkg_path = bldpkg_path(m)
                if pkg_path not in built_packages and pkg_path not in new_pkgs:
                    log.info("Packaging {}".format(m.name()))
                    # for more than one output, we clear and rebuild the environment before each
                    #    package.  We also do this for single outputs that present their own
                    #    build reqs.
                    if not (m.is_output or
                            (os.path.isdir(m.config.host_prefix) and
                             len(os.listdir(m.config.host_prefix)) <= 1)):
                        log.debug('Not creating new env for output - already exists from top-level')
                    else:
                        m.config._merge_build_host = m.build_is_host

                        utils.rm_rf(m.config.host_prefix)
                        utils.rm_rf(m.config.build_prefix)
                        utils.rm_rf(m.config.test_prefix)

                        host_ms_deps = m.ms_depends('host')
                        sub_build_ms_deps = m.ms_depends('build')
                        if m.is_cross and not m.build_is_host:
                            host_actions = environ.get_install_actions(m.config.host_prefix,
                                                    tuple(host_ms_deps), 'host',
                                                    subdir=m.config.host_subdir,
                                                    debug=m.config.debug,
                                                    verbose=m.config.verbose,
                                                    locking=m.config.locking,
                                                    bldpkgs_dirs=tuple(m.config.bldpkgs_dirs),
                                                    timeout=m.config.timeout,
                                                    disable_pip=m.config.disable_pip,
                                                    max_env_retry=m.config.max_env_retry,
                                                    output_folder=m.config.output_folder,
                                                    channel_urls=tuple(m.config.channel_urls))
                            environ.create_env(m.config.host_prefix, host_actions, env='host',
                                               config=m.config, subdir=subdir, is_cross=m.is_cross,
                                               is_conda=m.name() == 'conda')
                        else:
                            # When not cross-compiling, the build deps aggregate 'build' and 'host'.
                            sub_build_ms_deps.extend(host_ms_deps)
                        build_actions = environ.get_install_actions(m.config.build_prefix,
                                                    tuple(sub_build_ms_deps), 'build',
                                                    subdir=m.config.build_subdir,
                                                    debug=m.config.debug,
                                                    verbose=m.config.verbose,
                                                    locking=m.config.locking,
                                                    bldpkgs_dirs=tuple(m.config.bldpkgs_dirs),
                                                    timeout=m.config.timeout,
                                                    disable_pip=m.config.disable_pip,
                                                    max_env_retry=m.config.max_env_retry,
                                                    output_folder=m.config.output_folder,
                                                    channel_urls=tuple(m.config.channel_urls))
                        environ.create_env(m.config.build_prefix, build_actions, env='build',
                                           config=m.config, subdir=m.config.build_subdir,
                                           is_cross=m.is_cross,
                                           is_conda=m.name() == 'conda')

                    to_remove = set()
                    for f in output_d.get('files', []):
                        if f.startswith('conda-meta'):
                            to_remove.add(f)

                    if 'files' in output_d:
                        output_d['files'] = set(output_d['files']) - to_remove

                    # copies the backed-up new prefix files into the newly created host env
                    for f in new_prefix_files:
                        utils.copy_into(os.path.join(prefix_files_backup, f),
                                        os.path.join(m.config.host_prefix, f),
                                        symlinks=True)

                    # we must refresh the environment variables because our env for each package
                    #    can be different from the env for the top level build.
                    with utils.path_prepended(m.config.build_prefix):
                        env = environ.get_dict(m=m)
                    if not hasattr(m, 'type'):
                        if m.config.conda_pkg_format == "2":
                            pkg_type = "conda_v2"
                        else:
                            pkg_type = "conda"
                    else:
                        pkg_type = m.type
                    newly_built_packages = bundlers[pkg_type](output_d, m, env, stats)
                    # warn about overlapping files.
                    if 'checksums' in output_d:
                        for file, csum in output_d['checksums'].items():
                            for _, prev_om in new_pkgs.items():
                                prev_output_d, _ = prev_om
                                if file in prev_output_d.get('checksums', {}):
                                    prev_csum = prev_output_d['checksums'][file]
                                    nature = 'Exact' if csum == prev_csum else 'Inexact'
                                    log.warn("{} overlap between {} in packages {} and {}"
                                             .format(nature, file, output_d['name'],
                                                     prev_output_d['name']))
                    for built_package in newly_built_packages:
                        new_pkgs[built_package] = (output_d, m)

                    # must rebuild index because conda has no way to incrementally add our last
                    #    package to the index.

                    subdir = ('noarch' if (m.noarch or m.noarch_python)
                              else m.config.host_subdir)
                    if m.is_cross:
                        get_build_index(subdir=subdir, bldpkgs_dir=m.config.bldpkgs_dir,
                                        output_folder=m.config.output_folder, channel_urls=m.config.channel_urls,
                                        debug=m.config.debug, verbose=m.config.verbose, locking=m.config.locking,
                                        timeout=m.config.timeout, clear_cache=True)
                    get_build_index(subdir=subdir, bldpkgs_dir=m.config.bldpkgs_dir,
                                    output_folder=m.config.output_folder, channel_urls=m.config.channel_urls,
                                    debug=m.config.debug, verbose=m.config.verbose, locking=m.config.locking,
                                    timeout=m.config.timeout, clear_cache=True)
    else:
        if not provision_only:
            print("STOPPING BUILD BEFORE POST:", m.dist())

    # return list of all package files emitted by this build
    return new_pkgs


def warn_on_use_of_SRC_DIR(metadata):
    test_files = glob(os.path.join(metadata.path, 'run_test*'))
    for f in test_files:
        with open(f) as _f:
            contents = _f.read()
        if ("SRC_DIR" in contents and 'source_files' not in metadata.get_section('test') and
                metadata.config.remove_work_dir):
            raise ValueError("In conda-build 2.1+, the work dir is removed by default before the "
                             "test scripts run.  You are using the SRC_DIR variable in your test "
                             "script, but these files have been deleted.  Please see the "
                             " documentation regarding the test/source_files meta.yaml section, "
                             "or pass the --no-remove-work-dir flag.")


def _construct_metadata_for_test_from_recipe(recipe_dir, config):
    config.need_cleanup = False
    config.recipe_dir = None
    hash_input = {}
    metadata = expand_outputs(render_recipe(recipe_dir, config=config, reset_build_id=False))[0][1]
    log = utils.get_logger(__name__)
    log.warn("Testing based on recipes is deprecated as of conda-build 3.16.0.  Please adjust "
             "your code to pass your desired conda package to test instead.")

    utils.rm_rf(metadata.config.test_dir)

    if metadata.meta.get('test', {}).get('source_files'):
        if not metadata.source_provided:
            try_download(metadata, no_download_source=False)

    if not metadata.final:
        metadata = finalize_metadata(metadata)
    return metadata, hash_input


def _construct_metadata_for_test_from_package(package, config):
    recipe_dir, need_cleanup = utils.get_recipe_abspath(package)
    config.need_cleanup = need_cleanup
    config.recipe_dir = recipe_dir
    hash_input = {}

    info_dir = os.path.normpath(os.path.join(recipe_dir, 'info'))
    with open(os.path.join(info_dir, 'index.json')) as f:
        package_data = json.load(f)

    if package_data['subdir'] != 'noarch':
        config.host_subdir = package_data['subdir']
    # We may be testing an (old) package built without filename hashing.
    hash_input = os.path.join(info_dir, 'hash_input.json')
    if os.path.isfile(hash_input):
        with open(os.path.join(info_dir, 'hash_input.json')) as f:
            hash_input = json.load(f)
    else:
        config.filename_hashing = False
        hash_input = {}
    # not actually used as a variant, since metadata will have been finalized.
    #    This is still necessary for computing the hash correctly though
    config.variant = hash_input

    log = utils.get_logger(__name__)

    # get absolute file location
    local_pkg_location = os.path.normpath(os.path.abspath(os.path.dirname(package)))

    # get last part of the path
    last_element = os.path.basename(local_pkg_location)
    is_channel = False
    for platform in ('win-', 'linux-', 'osx-', 'noarch'):
        if last_element.startswith(platform):
            is_channel = True

    if not is_channel:
        log.warn("Copying package to conda-build croot.  No packages otherwise alongside yours will"
                 " be available unless you specify -c local.  To avoid this warning, your package "
                 "must reside in a channel structure with platform-subfolders.  See more info on "
                 "what a valid channel is at "
                 "https://conda.io/docs/user-guide/tasks/create-custom-channels.html")

        local_dir = os.path.join(config.croot, config.host_subdir)
        try:
            os.makedirs(local_dir)
        except:
            pass
        local_pkg_location = os.path.join(local_dir, os.path.basename(package))
        utils.copy_into(package, local_pkg_location)
        local_pkg_location = local_dir

    local_channel = os.path.dirname(local_pkg_location)

    # update indices in the channel
    update_index(local_channel, verbose=config.debug)

    try:
        metadata = render_recipe(os.path.join(info_dir, 'recipe'), config=config,
                                        reset_build_id=False)[0][0]

    # no recipe in package.  Fudge metadata
    except (IOError, SystemExit, OSError):
        # force the build string to line up - recomputing it would
        #    yield a different result
        metadata = MetaData.fromdict({'package': {'name': package_data['name'],
                                                  'version': package_data['version']},
                                      'build': {'number': int(package_data['build_number']),
                                                'string': package_data['build']},
                                      'requirements': {'run': package_data['depends']}
                                      }, config=config)
    # HACK: because the recipe is fully baked, detecting "used" variables no longer works.  The set
    #     of variables in the hash_input suffices, though.

    if metadata.noarch:
        metadata.config.variant['target_platform'] = "noarch"

    metadata.config.used_vars = list(hash_input.keys())
    urls = list(utils.ensure_list(metadata.config.channel_urls))
    local_path = url_path(local_channel)
    # replace local with the appropriate real channel.  Order is maintained.
    urls = [url if url != 'local' else local_path for url in urls]
    if local_path not in urls:
        urls.insert(0, local_path)
    metadata.config.channel_urls = urls
    utils.rm_rf(metadata.config.test_dir)
    return metadata, hash_input


def _extract_test_files_from_package(metadata):
    recipe_dir = metadata.config.recipe_dir if hasattr(metadata.config, "recipe_dir") else metadata.path
    if recipe_dir:
        info_dir = os.path.normpath(os.path.join(recipe_dir, 'info'))
        test_files = os.path.join(info_dir, 'test')
        if os.path.exists(test_files) and os.path.isdir(test_files):
            # things are re-extracted into the test dir because that's cwd when tests are run,
            #    and provides the most intuitive experience. This is a little
            #    tricky, because SRC_DIR still needs to point at the original
            #    work_dir, for legacy behavior where people aren't using
            #    test/source_files. It would be better to change SRC_DIR in
            #    test phase to always point to test_dir. Maybe one day.
            utils.copy_into(test_files, metadata.config.test_dir,
                            metadata.config.timeout, symlinks=True,
                            locking=metadata.config.locking, clobber=True)
            dependencies_file = os.path.join(test_files, 'test_time_dependencies.json')
            test_deps = []
            if os.path.isfile(dependencies_file):
                with open(dependencies_file) as f:
                    test_deps = json.load(f)
            test_section = metadata.meta.get('test', {})
            test_section['requires'] = test_deps
            metadata.meta['test'] = test_section

        else:
            if metadata.meta.get('test', {}).get('source_files'):
                if not metadata.source_provided:
                    try_download(metadata, no_download_source=False)


def construct_metadata_for_test(recipedir_or_package, config):
    if os.path.isdir(recipedir_or_package) or os.path.basename(recipedir_or_package) == 'meta.yaml':
        m, hash_input = _construct_metadata_for_test_from_recipe(recipedir_or_package, config)
    else:
        m, hash_input = _construct_metadata_for_test_from_package(recipedir_or_package, config)
    return m, hash_input


def write_build_scripts(m, script, build_file):
    with utils.path_prepended(m.config.host_prefix):
        with utils.path_prepended(m.config.build_prefix):
            env = environ.get_dict(m=m)
    env["CONDA_BUILD_STATE"] = "BUILD"

    # hard-code this because we never want pip's build isolation
    #    https://github.com/conda/conda-build/pull/2972#discussion_r198290241
    #
    # Note that pip env "NO" variables are inverted logic.
    #      PIP_NO_BUILD_ISOLATION=False means don't use build isolation.
    #
    env["PIP_NO_BUILD_ISOLATION"] = 'False'
    # some other env vars to have pip ignore dependencies.
    # we supply them ourselves instead.
    env["PIP_NO_DEPENDENCIES"] = True
    env["PIP_IGNORE_INSTALLED"] = True
    # pip's cache directory (PIP_NO_CACHE_DIR) should not be
    # disabled as this results in .egg-info rather than
    # .dist-info directories being created, see gh-3094

    # set PIP_CACHE_DIR to a path in the work dir that does not exist.
    env['PIP_CACHE_DIR'] = m.config.pip_cache_dir

    # tell pip to not get anything from PyPI, please.  We have everything we need
    # locally, and if we don't, it's a problem.
    env["PIP_NO_INDEX"] = True

    if m.noarch == "python":
        env["PYTHONDONTWRITEBYTECODE"] = True

    work_file = os.path.join(m.config.work_dir, 'conda_build.sh')
    env_file = os.path.join(m.config.work_dir, 'build_env_setup.sh')

    with open(env_file, 'w') as bf:
        for k, v in env.items():
            if v != '' and v is not None:
                bf.write('export {0}="{1}"\n'.format(k, v))

        if m.activate_build_script:
            utils._write_sh_activation_text(bf, m)
    with open(work_file, 'w') as bf:
        # bf.write('set -ex\n')
        bf.write('if [ -z ${CONDA_BUILD+x} ]; then\n')
        bf.write("\tsource {}\n".format(env_file))
        bf.write("fi\n")
        if script:
                bf.write(script)
        if os.path.isfile(build_file) and not script:
            bf.write(open(build_file).read())

    os.chmod(work_file, 0o766)
    return work_file, env_file


def _write_test_run_script(metadata, test_run_script, test_env_script, py_files, pl_files,
                           lua_files, r_files, shell_files, trace):
    log = utils.get_logger(__name__)
    with open(test_run_script, 'w') as tf:
        tf.write('{source} "{test_env_script}"\n'.format(
            source="call" if utils.on_win else "source",
            test_env_script=test_env_script))
        if utils.on_win:
            tf.write("IF %ERRORLEVEL% NEQ 0 exit 1\n")
        if py_files:
            test_python = metadata.config.test_python
            # use pythonw for import tests when osx_is_app is set
            if metadata.get_value('build/osx_is_app') and sys.platform == 'darwin':
                test_python = test_python + 'w'
            tf.write('"{python}" -s "{test_file}"\n'.format(
                python=test_python,
                test_file=os.path.join(metadata.config.test_dir, 'run_test.py')))
            if utils.on_win:
                tf.write("IF %ERRORLEVEL% NEQ 0 exit 1\n")
        if pl_files:
            tf.write('"{perl}" "{test_file}"\n'.format(
                perl=metadata.config.perl_bin(metadata.config.test_prefix,
                                              metadata.config.host_platform),
                test_file=os.path.join(metadata.config.test_dir, 'run_test.pl')))
            if utils.on_win:
                tf.write("IF %ERRORLEVEL% NEQ 0 exit 1\n")
        if lua_files:
            tf.write('"{lua}" "{test_file}"\n'.format(
                lua=metadata.config.lua_bin(metadata.config.test_prefix,
                                            metadata.config.host_platform),
                test_file=os.path.join(metadata.config.test_dir, 'run_test.lua')))
            if utils.on_win:
                tf.write("IF %ERRORLEVEL% NEQ 0 exit 1\n")
        if r_files:
            tf.write('"{r}" "{test_file}"\n'.format(
                r=metadata.config.rscript_bin(metadata.config.test_prefix,
                                              metadata.config.host_platform),
                test_file=os.path.join(metadata.config.test_dir, 'run_test.r')))
            if utils.on_win:
                tf.write("IF %ERRORLEVEL% NEQ 0 exit 1\n")
        if shell_files:
            for shell_file in shell_files:
                if utils.on_win:
                    if os.path.splitext(shell_file)[1] == ".bat":
                        tf.write('call "{test_file}"\n'.format(test_file=shell_file))
                        tf.write("IF %ERRORLEVEL% NEQ 0 exit 1\n")
                    else:
                        log.warn("Found sh test file on windows.  Ignoring this for now (PRs welcome)")
                elif os.path.splitext(shell_file)[1] == ".sh":
                    # TODO: Run the test/commands here instead of in run_test.py
                    tf.write('"{shell_path}" {trace}-e "{test_file}"\n'.format(shell_path=shell_path,
                                                                            test_file=shell_file,
                                                                            trace=trace))


def write_test_scripts(metadata, env_vars, py_files, pl_files, lua_files, r_files, shell_files, trace=""):
    if not metadata.config.activate or metadata.name() == 'conda':
        # prepend bin (or Scripts) directory
        env_vars = utils.prepend_bin_path(env_vars, metadata.config.test_prefix, prepend_prefix=True)
        if utils.on_win:
            env_vars['PATH'] = metadata.config.test_prefix + os.pathsep + env_vars['PATH']

    # set variables like CONDA_PY in the test environment
    env_vars.update(set_language_env_vars(metadata.config.variant))

    # Python 2 Windows requires that envs variables be string, not unicode
    env_vars = {str(key): str(value) for key, value in env_vars.items()}
    suffix = "bat" if utils.on_win else "sh"
    test_env_script = os.path.join(metadata.config.test_dir,
                           "conda_test_env_vars.{suffix}".format(suffix=suffix))
    test_run_script = os.path.join(metadata.config.test_dir,
                           "conda_test_runner.{suffix}".format(suffix=suffix))

    with open(test_env_script, 'w') as tf:
        if not utils.on_win:
            tf.write('set {trace}-e\n'.format(trace=trace))
        if metadata.config.activate and not metadata.name() == 'conda':
            ext = ".bat" if utils.on_win else ""
            tf.write('{source} "{conda_root}activate{ext}" "{test_env}"\n'.format(
                conda_root=utils.root_script_dir + os.path.sep,
                source="call" if utils.on_win else "source",
                ext=ext,
                test_env=metadata.config.test_prefix))
            if utils.on_win:
                tf.write("IF %ERRORLEVEL% NEQ 0 exit 1\n")

    _write_test_run_script(metadata, test_run_script, test_env_script, py_files, pl_files,
                           lua_files, r_files, shell_files, trace)
    return test_run_script, test_env_script


def test(recipedir_or_package_or_metadata, config, stats, move_broken=True, provision_only=False):
    '''
    Execute any test scripts for the given package.

    :param m: Package's metadata.
    :type m: Metadata
    '''
    log = utils.get_logger(__name__)
    # we want to know if we're dealing with package input.  If so, we can move the input on success.
    hash_input = {}

    # store this name to keep it consistent.  By changing files, we change the hash later.
    #    It matches the build hash now, so let's keep it around.
    test_package_name = (recipedir_or_package_or_metadata.dist()
                        if hasattr(recipedir_or_package_or_metadata, 'dist')
                        else recipedir_or_package_or_metadata)

    if not provision_only:
        print("TEST START:", test_package_name)

    if hasattr(recipedir_or_package_or_metadata, 'config'):
        metadata = recipedir_or_package_or_metadata
        utils.rm_rf(metadata.config.test_dir)
    else:
        metadata, hash_input = construct_metadata_for_test(recipedir_or_package_or_metadata,
                                                                  config)

    trace = '-x ' if metadata.config.debug else ''

    # Must download *after* computing build id, or else computing build id will change
    #     folder destination
    _extract_test_files_from_package(metadata)

    # When testing a .tar.bz2 in the pkgs dir, clean_pkg_cache() will remove it.
    # Prevent this. When https://github.com/conda/conda/issues/5708 gets fixed
    # I think we can remove this call to clean_pkg_cache().
    in_pkg_cache = (not hasattr(recipedir_or_package_or_metadata, 'config') and
                    os.path.isfile(recipedir_or_package_or_metadata) and
                    recipedir_or_package_or_metadata.endswith(CONDA_TARBALL_EXTENSIONS) and
                    os.path.dirname(recipedir_or_package_or_metadata) in pkgs_dirs[0])
    if not in_pkg_cache:
        environ.clean_pkg_cache(metadata.dist(), metadata.config)

    utils.copy_test_source_files(metadata, metadata.config.test_dir)
    # this is also copying tests/source_files from work_dir to testing workdir

    _, pl_files, py_files, r_files, lua_files, shell_files = create_all_test_files(metadata)
    if not any([py_files, shell_files, pl_files, lua_files, r_files]):
        print("Nothing to test for:", test_package_name)
        return True

    if metadata.config.remove_work_dir:
        for name, prefix in (('host', metadata.config.host_prefix),
                             ('build', metadata.config.build_prefix)):
            if os.path.isdir(prefix):
                # move host folder to force hardcoded paths to host env to break during tests
                #    (so that they can be properly addressed by recipe author)
                dest = os.path.join(os.path.dirname(prefix),
                            '_'.join(('%s_prefix_moved' % name, metadata.dist(),
                                      getattr(metadata.config, '%s_subdir' % name))))
                # Needs to come after create_files in case there's test/source_files
                print("Renaming %s prefix directory, " % name, prefix, " to ", dest)
                shutil.move(prefix, dest)

        # nested if so that there's no warning when we just leave the empty workdir in place
        if metadata.source_provided:
            dest = os.path.join(os.path.dirname(metadata.config.work_dir),
                                '_'.join(('work_moved', metadata.dist(),
                                          metadata.config.host_subdir)))
            # Needs to come after create_files in case there's test/source_files
            print("Renaming work directory, ", metadata.config.work_dir, " to ", dest)
            shutil.move(config.work_dir, dest)
    else:
        log.warn("Not moving work directory after build.  Your package may depend on files "
                    "in the work directory that are not included with your package")

    get_build_metadata(metadata)

    specs = metadata.get_test_deps(py_files, pl_files, lua_files, r_files)

    with utils.path_prepended(metadata.config.test_prefix):
        env = dict(os.environ.copy())
        env.update(environ.get_dict(m=metadata, prefix=config.test_prefix))
        env["CONDA_BUILD_STATE"] = "TEST"
        if env_path_backup_var_exists:
            env["CONDA_PATH_BACKUP"] = os.environ["CONDA_PATH_BACKUP"]

    if not metadata.config.activate or metadata.name() == 'conda':
        # prepend bin (or Scripts) directory
        env = utils.prepend_bin_path(env, metadata.config.test_prefix, prepend_prefix=True)

    if utils.on_win:
        env['PATH'] = metadata.config.test_prefix + os.pathsep + env['PATH']

    env['PREFIX'] = metadata.config.test_prefix
    if 'BUILD_PREFIX' in env:
        del env['BUILD_PREFIX']

    # In the future, we will need to support testing cross compiled
    #     packages on physical hardware. until then it is expected that
    #     something like QEMU or Wine will be used on the build machine,
    #     therefore, for now, we use host_subdir.

    subdir = ('noarch' if (metadata.noarch or metadata.noarch_python)
                else metadata.config.host_subdir)
    # ensure that the test prefix isn't kept between variants
    utils.rm_rf(metadata.config.test_prefix)

    try:
        actions = environ.get_install_actions(metadata.config.test_prefix,
                                                tuple(specs), 'host',
                                                subdir=subdir,
                                                debug=metadata.config.debug,
                                                verbose=metadata.config.verbose,
                                                locking=metadata.config.locking,
                                                bldpkgs_dirs=tuple(metadata.config.bldpkgs_dirs),
                                                timeout=metadata.config.timeout,
                                                disable_pip=metadata.config.disable_pip,
                                                max_env_retry=metadata.config.max_env_retry,
                                                output_folder=metadata.config.output_folder,
                                                channel_urls=tuple(metadata.config.channel_urls))
    except (DependencyNeedsBuildingError, NoPackagesFoundError, UnsatisfiableError,
            CondaError, AssertionError) as exc:
        log.warn("failed to get install actions, retrying.  exception was: %s",
                  str(exc))
        tests_failed(metadata, move_broken=move_broken, broken_dir=metadata.config.broken_dir,
                        config=metadata.config)
        raise
    # upgrade the warning from silently clobbering to warning.  If it is preventing, just
    #     keep it that way.
    conflict_verbosity = ('warn' if str(context.path_conflict) == 'clobber' else
                          str(context.path_conflict))
    with env_var('CONDA_PATH_CONFLICT', conflict_verbosity, reset_context):
        environ.create_env(metadata.config.test_prefix, actions, config=metadata.config,
                           env='host', subdir=subdir, is_cross=metadata.is_cross,
                           is_conda=metadata.name() == 'conda')

    with utils.path_prepended(metadata.config.test_prefix):
        env = dict(os.environ.copy())
        env.update(environ.get_dict(m=metadata, prefix=metadata.config.test_prefix))
        env["CONDA_BUILD_STATE"] = "TEST"
        if env_path_backup_var_exists:
            env["CONDA_PATH_BACKUP"] = os.environ["CONDA_PATH_BACKUP"]

    # when workdir is removed, the source files are unavailable.  There's the test/source_files
    #    entry that lets people keep these files around.  The files are copied into test_dir for
    #    intuitive relative path behavior, though, not work_dir, so we need to adjust where
    #    SRC_DIR points.  The initial CWD during tests is test_dir.
    if metadata.config.remove_work_dir:
        env['SRC_DIR'] = metadata.config.test_dir

    test_script, _ = write_test_scripts(metadata, env, py_files, pl_files, lua_files, r_files, shell_files, trace)

    if utils.on_win:
        cmd = [os.environ.get('COMSPEC', 'cmd.exe'), "/d", "/c", test_script]
    else:
        cmd = [shell_path] + (['-x'] if metadata.config.debug else []) + ['-e', test_script]
    try:
        test_stats = {}
        if not provision_only:
            # rewrite long paths in stdout back to their env variables
            if metadata.config.debug or metadata.config.no_rewrite_stdout_env:
                rewrite_env = None
            else:
                rewrite_env = {
                    k: env[k]
                    for k in ['PREFIX', 'SRC_DIR'] if k in env
                }
                if metadata.config.verbose:
                    for k, v in rewrite_env.items():
                        print('{0} {1}={2}'
                            .format('set' if test_script.endswith('.bat') else 'export', k, v))
            utils.check_call_env(cmd, env=env, cwd=metadata.config.test_dir, stats=test_stats, rewrite_stdout_env=rewrite_env)
            utils.log_stats(test_stats, "testing {}".format(metadata.name()))
            if stats is not None and metadata.config.variants:
                stats[utils.stats_key(metadata, 'test_{}'.format(metadata.name()))] = test_stats
            print("TEST END:", test_package_name)
    except subprocess.CalledProcessError:
        tests_failed(metadata, move_broken=move_broken, broken_dir=metadata.config.broken_dir,
                        config=metadata.config)
        raise

    if config.need_cleanup and config.recipe_dir is not None and not provision_only:
        utils.rm_rf(config.recipe_dir)

    return True


def tests_failed(package_or_metadata, move_broken, broken_dir, config):
    '''
    Causes conda to exit if any of the given package's tests failed.

    :param m: Package's metadata
    :type m: Metadata
    '''
    if not os.path.isdir(broken_dir):
        os.makedirs(broken_dir)

    if hasattr(package_or_metadata, 'config'):
        pkg = bldpkg_path(package_or_metadata)
    else:
        pkg = package_or_metadata
    dest = os.path.join(broken_dir, os.path.basename(pkg))

    if move_broken:
        log = utils.get_logger(__name__)
        try:
            shutil.move(pkg, dest)
            log.warn('Tests failed for %s - moving package to %s' % (os.path.basename(pkg),
                    broken_dir))
        except OSError:
            pass
        update_index(os.path.dirname(os.path.dirname(pkg)), verbose=config.debug)
    sys.exit("TESTS FAILED: " + os.path.basename(pkg))


def check_external():
    if sys.platform.startswith('linux'):
        patchelf = external.find_executable('patchelf')
        if patchelf is None:
            sys.exit("""\
Error:
    Did not find 'patchelf' in: %s
    'patchelf' is necessary for building conda packages on Linux with
    relocatable ELF libraries.  You can install patchelf using conda install
    patchelf.
""" % (os.pathsep.join(external.dir_paths)))


def build_tree(recipe_list, config, stats, build_only=False, post=False, notest=False,
               need_source_download=True, need_reparse_in_env=False, variants=None):

    to_build_recursive = []
    recipe_list = deque(recipe_list)

    if utils.on_win:
        trash_dir = os.path.join(os.path.dirname(sys.executable), 'pkgs', '.trash')
        if os.path.isdir(trash_dir):
            # We don't really care if this does a complete job.
            #    Cleaning up some files is better than none.
            subprocess.call('del /s /q "{0}\\*.*" >nul 2>&1'.format(trash_dir), shell=True)
        # delete_trash(None)

    extra_help = ""
    built_packages = OrderedDict()
    retried_recipes = []
    initial_time = time.time()
    stats_file = config.stats_file

    # this is primarily for exception handling.  It's OK that it gets clobbered by
    #     the loop below.
    metadata = None

    while recipe_list:
        # This loop recursively builds dependencies if recipes exist
        if build_only:
            post = False
            notest = True
            config.anaconda_upload = False
        elif post:
            post = True
            config.anaconda_upload = False
        else:
            post = None

        try:
            recipe = recipe_list.popleft()
            name = recipe.name() if hasattr(recipe, 'name') else recipe
            if hasattr(recipe, 'config'):
                metadata = recipe
                metadata.config.anaconda_upload = config.anaconda_upload
                config = metadata.config
                # this code is duplicated below because we need to be sure that the build id is set
                #    before downloading happens - or else we lose where downloads are
                if config.set_build_id and metadata.name() not in config.build_id:
                    config.compute_build_id(metadata.name(), reset=True)
                recipe_parent_dir = os.path.dirname(metadata.path)
                to_build_recursive.append(metadata.name())

                if not metadata.final:
                    variants_ = (dict_of_lists_to_list_of_dicts(variants) if variants else
                                get_package_variants(metadata))

                    # This is where reparsing happens - we need to re-evaluate the meta.yaml for any
                    #    jinja2 templating
                    metadata_tuples = distribute_variants(metadata, variants_,
                                                        permit_unsatisfiable_variants=False)
                else:
                    metadata_tuples = ((metadata, False, False), )
            else:
                recipe_parent_dir = os.path.dirname(recipe)
                recipe = recipe.rstrip("/").rstrip("\\")
                to_build_recursive.append(os.path.basename(recipe))

                # each tuple is:
                #    metadata, need_source_download, need_reparse_in_env =
                # We get one tuple per variant
                metadata_tuples = render_recipe(recipe, config=config, variants=variants,
                                                permit_unsatisfiable_variants=False,
                                                reset_build_id=not config.dirty,
                                                bypass_env_check=True)
            # restrict to building only one variant for bdist_conda.  The way it splits the build
            #    job breaks variants horribly.
            if post in (True, False):
                metadata_tuples = metadata_tuples[:1]

            # This is the "TOP LEVEL" loop. Only vars used in the top-level
            # recipe are looped over here.

            for (metadata, need_source_download, need_reparse_in_env) in metadata_tuples:
                if post is None:
                    utils.rm_rf(metadata.config.host_prefix)
                    utils.rm_rf(metadata.config.build_prefix)
                    utils.rm_rf(metadata.config.test_prefix)
                if metadata.name() not in metadata.config.build_folder:
                    metadata.config.compute_build_id(metadata.name(), reset=True)

                packages_from_this = build(metadata, stats,
                                           post=post,
                                           need_source_download=need_source_download,
                                           need_reparse_in_env=need_reparse_in_env,
                                           built_packages=built_packages,
                                           notest=notest,
                                           )
                if not notest:
                    for pkg, dict_and_meta in packages_from_this.items():
                        if pkg.endswith(CONDA_TARBALL_EXTENSIONS) and os.path.isfile(pkg):
                            # we only know how to test conda packages
                            test(pkg, config=metadata.config.copy(), stats=stats)
                        _, meta = dict_and_meta
                        downstreams = meta.meta.get('test', {}).get('downstreams')
                        if downstreams:
                            channel_urls = tuple(utils.ensure_list(metadata.config.channel_urls) +
                                                 [utils.path2url(os.path.abspath(os.path.dirname(
                                                                 os.path.dirname(pkg))))])
                            log = utils.get_logger(__name__)
                            # downstreams can be a dict, for adding capability for worker labels
                            if hasattr(downstreams, 'keys'):
                                downstreams = list(downstreams.keys())
                                log.warn("Dictionary keys for downstreams are being "
                                         "ignored right now.  Coming soon...")
                            else:
                                downstreams = utils.ensure_list(downstreams)
                            for dep in downstreams:
                                log.info("Testing downstream package: {}".format(dep))
                                # resolve downstream packages to a known package

                                r_string = ''.join(random.choice(
                                    string.ascii_uppercase + string.digits) for _ in range(10))
                                specs = meta.ms_depends('run') + [MatchSpec(dep),
                                                    MatchSpec(' '.join(meta.dist().rsplit('-', 2)))]
                                specs = [utils.ensure_valid_spec(spec) for spec in specs]
                                try:
                                    with TemporaryDirectory(prefix="_", suffix=r_string) as tmpdir:
                                        actions = environ.get_install_actions(
                                            tmpdir, specs, env='run',
                                            subdir=meta.config.host_subdir,
                                            bldpkgs_dirs=meta.config.bldpkgs_dirs,
                                            channel_urls=channel_urls)
                                except (UnsatisfiableError, DependencyNeedsBuildingError) as e:
                                    log.warn("Skipping downstream test for spec {}; was "
                                             "unsatisfiable.  Error was {}".format(dep, e))
                                    continue
                                # make sure to download that package to the local cache if not there
                                local_file = execute_download_actions(meta, actions, 'host',
                                                                      package_subset=dep,
                                                                      require_files=True)
                                # test that package, using the local channel so that our new
                                #    upstream dep gets used
                                test(list(local_file.values())[0][0],
                                     config=meta.config.copy(), stats=stats)

                        built_packages.update({pkg: dict_and_meta})
                else:
                    built_packages.update(packages_from_this)

                if (os.path.exists(metadata.config.work_dir) and not
                        (metadata.config.dirty or metadata.config.keep_old_work or
                         metadata.get_value('build/no_move_top_level_workdir_loops'))):
                    # force the build string to include hashes as necessary
                    metadata.final = True
                    dest = os.path.join(os.path.dirname(metadata.config.work_dir),
                                        '_'.join(('work_moved', metadata.dist(),
                                                  metadata.config.host_subdir, "main_build_loop")))
                    # Needs to come after create_files in case there's test/source_files
                    print("Renaming work directory, ", metadata.config.work_dir, " to ", dest)
                    try:
                        shutil.move(metadata.config.work_dir, dest)
                    except shutil.Error:
                        utils.rm_rf(dest)
                        shutil.move(metadata.config.work_dir, dest)

            # each metadata element here comes from one recipe, thus it will share one build id
            #    cleaning on the last metadata in the loop should take care of all of the stuff.
            metadata.clean()
        except DependencyNeedsBuildingError as e:
            skip_names = ['python', 'r', 'r-base', 'mro-base', 'perl', 'lua']
            built_package_paths = [entry[1][1].path for entry in built_packages.items()]
            add_recipes = []
            # add the failed one back in at the beginning - but its deps may come before it
            recipe_list.extendleft([recipe])
            for pkg, matchspec in zip(e.packages, e.matchspecs):
                pkg_name = pkg.split(' ')[0].split('=')[0]
                # if we hit missing dependencies at test time, the error we get says that our
                #    package that we just built needs to be built.  Very confusing.  Bomb out
                #    if any of our output metadatas are in the exception list of pkgs.
                if metadata and any(pkg_name == output_meta.name() for (_, output_meta) in
                       metadata.get_output_metadata_set(permit_undefined_jinja=True)):
                    raise
                if pkg in to_build_recursive:
                    config.clean(remove_folders=False)
                    raise RuntimeError("Can't build {0} due to environment creation error:\n"
                                       .format(recipe) + str(e.message) + "\n" + extra_help)

                if pkg in skip_names:
                    to_build_recursive.append(pkg)
                    extra_help = """Typically if a conflict is with the Python or R
packages, the other package or one of its dependencies
needs to be rebuilt (e.g., a conflict with 'python 3.5*'
and 'x' means 'x' or one of 'x' dependencies isn't built
for Python 3.5 and needs to be rebuilt."""

                recipe_glob = glob(os.path.join(recipe_parent_dir, pkg_name))
                # conda-forge style.  meta.yaml lives one level deeper.
                if not recipe_glob:
                    recipe_glob = glob(os.path.join(recipe_parent_dir, '..', pkg_name))
                feedstock_glob = glob(os.path.join(recipe_parent_dir, pkg_name + '-feedstock'))
                if not feedstock_glob:
                    feedstock_glob = glob(os.path.join(recipe_parent_dir, '..',
                                                       pkg_name + '-feedstock'))
                available = False
                if recipe_glob or feedstock_glob:
                    for recipe_dir in recipe_glob + feedstock_glob:
                        if not any(path.startswith(recipe_dir) for path in built_package_paths):
                            dep_metas = render_recipe(recipe_dir, config=metadata.config)
                            for dep_meta in dep_metas:
                                if utils.match_peer_job(MatchSpec(matchspec), dep_meta[0],
                                                        metadata):
                                    print(("Missing dependency {0}, but found" +
                                        " recipe directory, so building " +
                                        "{0} first").format(pkg))
                                    add_recipes.append(recipe_dir)
                                    available = True
                if not available:
                    config.clean(remove_folders=False)
                    raise
            # if we failed to render due to unsatisfiable dependencies, we should only bail out
            #    if we've already retried this recipe.
            if (not metadata and retried_recipes.count(recipe) and
                    retried_recipes.count(recipe) >= len(metadata.ms_depends('build'))):
                config.clean(remove_folders=False)
                raise RuntimeError("Can't build {0} due to environment creation error:\n"
                                    .format(recipe) + str(e.message) + "\n" + extra_help)
            retried_recipes.append(os.path.basename(name))
            recipe_list.extendleft(add_recipes)

    if post in [True, None]:
        # TODO: could probably use a better check for pkg type than this...
        tarballs = [f for f in built_packages if f.endswith(CONDA_TARBALL_EXTENSIONS)]
        wheels = [f for f in built_packages if f.endswith('.whl')]
        handle_anaconda_upload(tarballs, config=config)
        handle_pypi_upload(wheels, config=config)

    total_time = time.time() - initial_time
    max_memory_used = max([step.get('rss') for step in stats.values()] or [0])
    total_disk = sum([step.get('disk') for step in stats.values()] or [0])
    total_cpu_sys = sum([step.get('cpu_sys') for step in stats.values()] or [0])
    total_cpu_user = sum([step.get('cpu_user') for step in stats.values()] or [0])

    print('#' * 84)
    print("Resource usage summary:")
    print("\nTotal time: {}".format(utils.seconds_to_text(total_time)))
    print("CPU usage: sys={}, user={}".format(utils.seconds_to_text(total_cpu_sys),
                                              utils.seconds_to_text(total_cpu_user)))
    print("Maximum memory usage observed: {}".format(utils.bytes2human(max_memory_used)))
    print("Total disk usage observed (not including envs): {}".format(
        utils.bytes2human(total_disk)))
    stats['total'] = {'time': total_time,
                      'memory': max_memory_used,
                      'disk': total_disk}
    if stats_file:
        with open(stats_file, 'w') as f:
            json.dump(stats, f)

    return list(built_packages.keys())


def handle_anaconda_upload(paths, config):
    from conda_build.os_utils.external import find_executable

    paths = utils.ensure_list(paths)

    upload = False
    # this is the default, for no explicit argument.
    # remember that anaconda_upload takes defaults from condarc
    if config.token or config.user:
        upload = True
    # rc file has uploading explicitly turned off
    elif not config.anaconda_upload:
        print("# Automatic uploading is disabled")
    else:
        upload = True

    no_upload_message = """\
# If you want to upload package(s) to anaconda.org later, type:

"""
    for package in paths:
        no_upload_message += "anaconda upload {}\n".format(package)

    no_upload_message += """\

# To have conda build upload to anaconda.org automatically, use
# $ conda config --set anaconda_upload yes
"""
    if not upload:
        print(no_upload_message)
        return

    anaconda = find_executable('anaconda')
    if anaconda is None:
        print(no_upload_message)
        sys.exit('''
Error: cannot locate anaconda command (required for upload)
# Try:
# $ conda install anaconda-client
''')
    cmd = [anaconda, ]

    if config.token:
        cmd.extend(['--token', config.token])
    cmd.append('upload')
    if config.force_upload:
        cmd.append('--force')
    if config.user:
        cmd.extend(['--user', config.user])
    for label in config.labels:
        cmd.extend(['--label', label])
    for package in paths:
        try:
            print("Uploading {} to anaconda.org".format(os.path.basename(package)))
            subprocess.call(cmd + [package])
        except subprocess.CalledProcessError:
            print(no_upload_message)
            raise


def handle_pypi_upload(wheels, config):
    args = ['twine', 'upload', '--sign-with', config.sign_with, '--repository', config.repository]
    if config.user:
        args.extend(['--user', config.user])
    if config.password:
        args.extend(['--password', config.password])
    if config.sign:
        args.extend(['--sign'])
    if config.identity:
        args.extend(['--identity', config.identity])
    if config.config_file:
        args.extend(['--config-file', config.config_file])
    if config.repository:
        args.extend(['--repository', config.repository])

    wheels = utils.ensure_list(wheels)

    if config.anaconda_upload:
        for f in wheels:
            print("Uploading {}".format(f))
            try:
                utils.check_call_env(args + [f])
            except:
                utils.get_logger(__name__).warn("wheel upload failed - is twine installed?"
                                                "  Is this package registered?")
                utils.get_logger(__name__).warn("Wheel file left in {}".format(f))

    else:
        print("anaconda_upload is not set.  Not uploading wheels: {}".format(wheels))


def print_build_intermediate_warning(config):
    print("\n")
    print('#' * 84)
    print("Source and build intermediates have been left in " + config.croot + ".")
    build_folders = utils.get_build_folders(config.croot)
    print("There are currently {num_builds} accumulated.".format(num_builds=len(build_folders)))
    print("To remove them, you can run the ```conda build purge``` command")


def clean_build(config, folders=None):
    if not folders:
        folders = utils.get_build_folders(config.croot)
    for folder in folders:
        utils.rm_rf(folder)


def is_package_built(metadata, env, include_local=True):
    for d in metadata.config.bldpkgs_dirs:
        if not os.path.isdir(d):
            os.makedirs(d)
        update_index(d, verbose=metadata.config.debug, warn=False)
    subdir = getattr(metadata.config, '{}_subdir'.format(env))

    urls = [url_path(metadata.config.output_folder), 'local'] if include_local else []
    urls += get_rc_urls()
    if metadata.config.channel_urls:
        urls.extend(metadata.config.channel_urls)

    spec = MatchSpec(name=metadata.name(), version=metadata.version(), build=metadata.build_id())

    if conda_45:
        from conda.api import SubdirData
        return bool(SubdirData.query_all(spec, channels=urls, subdirs=(subdir, "noarch")))
    else:
        index, _, _ = get_build_index(subdir=subdir, bldpkgs_dir=metadata.config.bldpkgs_dir,
                                      output_folder=metadata.config.output_folder, channel_urls=urls,
                                      debug=metadata.config.debug, verbose=metadata.config.verbose,
                                      locking=metadata.config.locking, timeout=metadata.config.timeout,
                                      clear_cache=True)
        return any(spec.match(prec) for prec in index.values())
