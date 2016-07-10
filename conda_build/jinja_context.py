from __future__ import absolute_import, division, print_function

from functools import partial
import json
import os
import re
import subprocess

import jinja2

from conda.compat import PY3
from .environ import get_dict as get_environ
from .metadata import select_lines, ns_cfg
from .source import WORK_DIR

_setuptools_data = None


class UndefinedNeverFail(jinja2.Undefined):
    """
    A class for Undefined jinja variables.
    This is even less strict than the default jinja2.Undefined class,
    because it permits things like {{ MY_UNDEFINED_VAR[:2] }} and
    {{ MY_UNDEFINED_VAR|int }}. This can mask lots of errors in jinja templates, so it
    should only be used for a first-pass parse, when you plan on running a 'strict'
    second pass later.
    """
    all_undefined_names = []

    def __init__(self, hint=None, obj=jinja2.runtime.missing, name=None,
                 exc=jinja2.exceptions.UndefinedError):
        UndefinedNeverFail.all_undefined_names.append(name)
        jinja2.Undefined.__init__(self, hint, obj, name, exc)

    __add__ = __radd__ = __mul__ = __rmul__ = __div__ = __rdiv__ = \
    __truediv__ = __rtruediv__ = __floordiv__ = __rfloordiv__ = \
    __mod__ = __rmod__ = __pos__ = __neg__ = __call__ = \
    __getitem__ = __lt__ = __le__ = __gt__ = __ge__ = \
    __complex__ = __pow__ = __rpow__ = \
        lambda self, *args, **kwargs: UndefinedNeverFail(hint=self._undefined_hint,
                                                         obj=self._undefined_obj,
                                                         name=self._undefined_name,
                                                         exc=self._undefined_exception)

    __str__ = __repr__ = \
        lambda *args, **kwargs: u''

    __int__ = lambda _: 0
    __float__ = lambda _: 0.0

    def __getattr__(self, k):
        try:
            return object.__getattr__(self, k)
        except AttributeError:
            return UndefinedNeverFail(hint=self._undefined_hint,
                                      obj=self._undefined_obj,
                                      name=self._undefined_name + '.' + k,
                                      exc=self._undefined_exception)


class FilteredLoader(jinja2.BaseLoader):
    """
    A pass-through for the given loader, except that the loaded source is
    filtered according to any metadata selectors in the source text.
    """

    def __init__(self, unfiltered_loader):
        self._unfiltered_loader = unfiltered_loader
        self.list_templates = unfiltered_loader.list_templates

    def get_source(self, environment, template):
        contents, filename, uptodate = self._unfiltered_loader.get_source(environment,
                                                                          template)
        return select_lines(contents, ns_cfg()), filename, uptodate


def load_setuptools(setup_file='setup.py', from_recipe_dir=False,
                    recipe_dir=None):
    global _setuptools_data

    if _setuptools_data is None:
        _setuptools_data = {}

        def setup(**kw):
            _setuptools_data.update(kw)

        import setuptools
        import distutils.core
        # Add current directory to path
        import sys
        sys.path.append('.')

        if from_recipe_dir and recipe_dir:
            setup_file = os.path.abspath(os.path.join(recipe_dir, setup_file))

        # Patch setuptools, distutils
        setuptools_setup = setuptools.setup
        distutils_setup = distutils.core.setup
        setuptools.setup = distutils.core.setup = setup
        ns = {
            '__name__': '__main__',
            '__doc__': None,
            '__file__': setup_file,
        }
        code = compile(open(setup_file).read(), setup_file, 'exec',
                       dont_inherit=1)
        exec(code, ns, ns)
        distutils.core.setup = distutils_setup
        setuptools.setup = setuptools_setup
        del sys.path[-1]
    return _setuptools_data


def _get_build_number_from_file(filename):
    # build is always 0 - but may change below.
    number = 0
    if not os.path.isabs(filename):
        filename = os.path.join(os.path.abspath(os.path.join(WORK_DIR, filename)))
    if os.path.isfile(filename):
        try:
            value = open(filename).read()
            number = int(value)
        except ValueError:
            log.warn("Invalid value for build number ({0}) in {1}".format(value, filename))
        except IOError:
            log.warn("Could not open file {0} for reading build number".format(filename))
    return number


def _get_build_number_from_repo(metadata):
    # this is pretty vile, but conda doesn't really have an API for this.
    #    Revisit when conda might have this.
    # Idea is that we need to know which package conda would choose to install, and base our
    #    decision of what to base our build number on using that.
    cmd = "conda create -n noenv --dry-run {0} {1}".format(metadata.name() + "=" + metadata.version(),
                                                           metadata.ms_depends('build'))
    try:
        output = subprocess.check_output(cmd.split())
        # find pattern:
        m = re.search("{packagename}:\s+{version}-?(?:[^-_]*)?[-_]([0-9]{1,4})\s+".format(
            packagename=metadata.name,
            version=metadata.version),
                      output, re.M | re.I)
        number = int(m.groups()[0])
    except subprocess.CalledProcessError:
        # if package does not exist, we are implicitly at build number 0
        number = 0
    return number


def next_build_number(filename=None, metadata=None):
    if filename:
        number = _get_build_number_from_file(filename)
    else:
        number = _get_build_number_from_repo(metadata)
    return number


def load_npm():
    # json module expects bytes in Python 2 and str in Python 3.
    mode_dict = {'mode': 'r', 'encoding': 'utf-8'} if PY3 else {'mode': 'rb'}
    with open('package.json', **mode_dict) as pkg:
        return json.load(pkg)


def context_processor(initial_metadata, recipe_dir):
    """
    Return a dictionary to use as context for jinja templates.

    initial_metadata: Augment the context with values from this MetaData object.
                      Used to bootstrap metadata contents via multiple parsing passes.
    """
    ctx = get_environ(m=initial_metadata)
    environ = dict(os.environ)
    environ.update(get_environ(m=initial_metadata))

    ctx.update(load_setuptools=partial(load_setuptools, recipe_dir=recipe_dir),
               load_npm=load_npm,
               next_build_number=partial(next_build_number, metadata=initial_metadata),
               environ=environ)
    return ctx
