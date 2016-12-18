# (c) Continuum Analytics, Inc. / http://continuum.io
# All Rights Reserved
#
# conda is distributed under the terms of the BSD 3-clause license.
# Consult LICENSE.txt or http://opensource.org/licenses/BSD-3-Clause.

from __future__ import absolute_import, division, print_function

import copy
import functools
from glob import glob
import json
from locale import getpreferredencoding
import os
from os.path import isdir, isfile, abspath
import subprocess
import sys
import tarfile
import tempfile

import yaml

from .conda_interface import PY3, envs_dirs

from conda_build import exceptions, utils
from conda_build.metadata import MetaData, parse
import conda_build.source as source
from conda_build.config import get_or_merge_config
from conda_build.completers import all_versions, conda_version
from conda_build.utils import rm_rf
from conda_build.variants import get_package_variants


def set_language_env_vars(args, parser, config, execute=None):
    """Given args passed into conda command, set language env vars"""
    for lang in all_versions:
        versions = getattr(args, lang)
        if not versions:
            continue
        if versions == ['all']:
            if all_versions[lang]:
                versions = all_versions[lang]
            else:
                parser.error("'all' is not supported for --%s" % lang)
        if len(versions) > 1:
            for ver in versions[:]:
                setattr(args, lang, [str(ver)])
                if execute:
                    execute(args, parser, config)
                # This is necessary to make all combinations build.
                setattr(args, lang, versions)
            return
        else:
            version = versions[0]
            if lang in ('python', 'numpy'):
                version = int(version.replace('.', ''))
            setattr(config, conda_version[lang], version)
        if not len(str(version)) in (2, 3) and lang in ['python', 'numpy']:
            if all_versions[lang]:
                raise RuntimeError("%s must be major.minor, like %s, not %s" %
                    (conda_version[lang], all_versions[lang][-1] / 10, version))
            else:
                raise RuntimeError("%s must be major.minor, not %s" %
                    (conda_version[lang], version))

    # Using --python, --numpy etc. is equivalent to using CONDA_PY, CONDA_NPY, etc.
    # Auto-set those env variables
    for var in conda_version.values():
        if hasattr(config, var) and getattr(config, var):
            # Set the env variable.
            os.environ[var] = str(getattr(config, var))


def bldpkg_path(m):
    '''
    Returns path to built package's tarball given its ``Metadata``.
    '''
    output_dir = m.info_index()['subdir']
    return os.path.join(os.path.dirname(m.config.bldpkgs_dir), output_dir, '%s.tar.bz2' % m.dist())


def _scan_metadata(path):
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


# This really belongs in conda, and it is int conda.cli.common,
#   but we don't presently have an API there.
def _get_env_path(env_name_or_path):
    if not os.path.isdir(env_name_or_path):
        for envs_dir in envs_dirs + [os.getcwd()]:
            path = os.path.join(envs_dir, env_name_or_path)
            if os.path.isdir(path):
                env_name_or_path = path
                break
    bootstrap_metadir = os.path.join(env_name_or_path, 'conda-meta')
    if not isdir(bootstrap_metadir):
        print("Bootstrap environment '%s' not found" % env_name_or_path)
        sys.exit(1)
    return env_name_or_path


def get_dependencies_from_environment(env_name_or_path):
    path = _get_env_path(env_name_or_path)
    # construct build requirements that replicate the given bootstrap environment
    # and concatenate them to the build requirements from the recipe
    bootstrap_metadata = _scan_metadata(path)
    bootstrap_requirements = []
    for package, data in bootstrap_metadata.items():
        bootstrap_requirements.append("%s %s %s" % (package, data['version'], data['build']))
    return {'requirements': {'build': bootstrap_requirements}}


def _jinja_config(config, jinja_env):
    # make all metadata from build_prefix/conda-meta/*.json available to
    # jinja in a dictionary 'installed'
    jinja_env.globals['installed'] = _scan_metadata(os.path.join(config.build_prefix, 'conda-meta'))


def parse_or_try_download(metadata, no_download_source, config,
                          force_download=False):

    need_reparse_in_env = False
    if (force_download or (not no_download_source and metadata.needs_source_for_render)):
        # this try/catch is for when the tool to download source is actually in
        #    meta.yaml, and not previously installed in builder env.
        try:
            if not config.dirty:
                if len(os.listdir(config.work_dir)) == 0:
                    source.provide(metadata.path, metadata.get_section('source'), config=config)
                need_source_download = False
            try:
                metadata.parse_again(permit_undefined_jinja=False,
                                     jinja_config=functools.partial(_jinja_config, config=config))
            except (ImportError, exceptions.UnableToParseMissingSetuptoolsDependencies):
                need_reparse_in_env = True
        except subprocess.CalledProcessError as error:
            print("Warning: failed to download source.  If building, will try "
                "again after downloading recipe dependencies.")
            print("Error was: ")
            print(error)
            need_source_download = True

    elif not metadata.get_section('source'):
        need_source_download = False
    else:
        # we have not downloaded source in the render phase.  Download it in
        #     the build phase
        need_source_download = not no_download_source

    if metadata.get_value('build/noarch'):
        config.noarch = True

    output = []
    variants = get_package_variants(metadata, config.variant_config_files,
                                    config.ignore_system_variants)
    for variant in variants:
        metadata = copy.deepcopy(metadata)
        try:
            metadata.parse_until_resolved(config=config, variant=variant)
        except exceptions.UnableToParseMissingSetuptoolsDependencies:
            need_reparse_in_env = True
        output.append((metadata, need_source_download, need_reparse_in_env))
    return output


def reparse(metadata, config):
    """Some things need to be parsed again after the build environment has been created
    and activated."""
    sys.path.insert(0, config.build_prefix)
    sys.path.insert(0, utils.get_site_packages(config.build_prefix))
    metadata.parse_again(config=config, permit_undefined_jinja=False)


def render_recipe(recipe_path, config, no_download_source=False):
    arg = recipe_path
    # Don't use byte literals for paths in Python 2
    if not PY3:
        arg = arg.decode(getpreferredencoding() or 'utf-8')
    if isfile(arg):
        if arg.endswith(('.tar', '.tar.gz', '.tgz', '.tar.bz2')):
            recipe_dir = tempfile.mkdtemp()
            t = tarfile.open(arg, 'r:*')
            t.extractall(path=recipe_dir)
            t.close()
            need_cleanup = True
        elif arg.endswith('.yaml'):
            recipe_dir = os.path.dirname(arg)
            need_cleanup = False
        else:
            print("Ignoring non-recipe: %s" % arg)
            return
    else:
        recipe_dir = abspath(arg)
        need_cleanup = False

    if not isdir(recipe_dir):
        sys.exit("Error: no such directory: %s" % recipe_dir)

    if config.set_build_id:
        # updates a unique build id if not already computed
        config.compute_build_id(os.path.basename(recipe_dir))
    try:
        m = MetaData(recipe_dir, config=config)
    except exceptions.YamlParsingError as e:
        sys.stderr.write(e.error_msg())
        sys.exit(1)

    # each tuple item is a tuple of 3 items:
    #    metadata, need_download, need_reparse_in_env
    rendered_metadata = parse_or_try_download(m,
                                              no_download_source=no_download_source,
                                              config=config)
    config.noarch = bool(m.get_value('build/noarch'))

    if need_cleanup:
        rm_rf(recipe_dir)

    return rendered_metadata


# Next bit of stuff is to support YAML output in the order we expect.
# http://stackoverflow.com/a/17310199/1170370
class _MetaYaml(dict):
    fields = ["package", "source", "build", "requirements", "test", "outputs", "about", "extra"]

    def to_omap(self):
        return [(field, self[field]) for field in _MetaYaml.fields if field in self]


def _represent_omap(dumper, data):
    return dumper.represent_mapping(u'tag:yaml.org,2002:map', data.to_omap())


def _unicode_representer(dumper, uni):
    node = yaml.ScalarNode(tag=u'tag:yaml.org,2002:str', value=uni)
    return node


class _IndentDumper(yaml.Dumper):
    def increase_indent(self, flow=False, indentless=False):
        return super(_IndentDumper, self).increase_indent(flow, False)

yaml.add_representer(_MetaYaml, _represent_omap)
if PY3:
    yaml.add_representer(str, _unicode_representer)
    unicode = None  # silence pyflakes about unicode not existing in py3
else:
    yaml.add_representer(unicode, _unicode_representer)


def output_yaml(metadata, filename=None):
    output = yaml.dump(_MetaYaml(metadata.meta), Dumper=_IndentDumper,
                       default_flow_style=False, indent=4)
    if filename:
        with open(filename, "w") as f:
            f.write(output)
        return "Wrote yaml to %s" % filename
    else:
        return output
