"""
This file tests the build.py module.  It sits lower in the stack than the API tests,
and is more unit-test oriented.
"""

import json
import os
import subprocess
import sys

import pytest

from conda_build import build, api

from .utils import metadata_dir, put_bad_conda_on_path, get_noarch_python_meta


def test_build_preserves_PATH(testing_workdir, testing_config):
    m = api.render(os.path.join(metadata_dir, 'source_git'), config=testing_config)[0][0]
    ref_path = os.environ['PATH']
    build.build(m, stats=None)
    assert os.environ['PATH'] == ref_path


def test_rewrite_output(testing_workdir, testing_config, capsys):
    api.build(os.path.join(metadata_dir, "_rewrite_env"), config=testing_config)
    captured = capsys.readouterr()
    stdout = captured.out
    if sys.platform == 'win32':
        assert "PREFIX=%PREFIX%" in stdout
        assert "LIBDIR=%PREFIX%\\lib" in stdout
        assert "PWD=%SRC_DIR%" in stdout
        assert "BUILD_PREFIX=%BUILD_PREFIX%" in stdout
    else:
        assert "PREFIX=$PREFIX" in stdout
        assert "LIBDIR=$PREFIX/lib" in stdout
        assert "PWD=$SRC_DIR" in stdout
        assert "BUILD_PREFIX=$BUILD_PREFIX" in stdout
