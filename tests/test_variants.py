import os
import subprocess
import tempfile

from conda_build import variants
from conda_build import render
from .utils import testing_workdir, test_config

import yaml

global_specs = {"python": ["2.7", "3.5"],
                "numpy": ["1.10", "1.11"]}

single_version = {"python": "2.7",
                  "numpy": "1.10"}

no_numpy_version = {"python": ["2.7", "3.5"]}

thisdir = os.path.dirname(__file__)


def test_later_spec_priority():
    # override a single key
    combined_spec = variants.combine_specs([global_specs, single_version])
    assert len(combined_spec) == 2
    assert combined_spec["python"] == "2.7"

    # keep keys that are not overwritten
    combined_spec = variants.combine_specs([single_version, no_numpy_version])
    assert len(combined_spec) == 2
    assert len(combined_spec["python"]) == 2


def test_get_package_variants(test_config):
    with tempfile.NamedTemporaryFile() as f:
        test_config.variant_config_files = [f.name]
        test_config.ignore_system_config = True
        yaml.dump(global_specs, f)
        metadata = render.render_recipe(os.path.join(thisdir, "variant_recipe"),
                                        no_download_source=False, config=test_config)
    # one for each Python version
    assert len(metadata) == 2
    assert 'python 2.7' in metadata[0][0].meta['requirements']['run'][0]
    assert 'python 3.5' in metadata[1][0].meta['requirements']['run'][0]


def test_build_config_file(test_config):
    metadata = render.render_recipe(os.path.join(thisdir, "variant_recipe"),
                                    no_download_source=False, config=test_config)
    assert not any("git" in pkg for pkg in metadata.meta["requirements"]["build"]), metadata.meta["requirements"]["build"]
    metadata = render.add_build_config(metadata, os.path.join(thisdir, "variant_recipe", "build_config.yaml"))
    assert any("git" in pkg for pkg in metadata.meta["requirements"]["build"]), metadata.meta["requirements"]["build"]


def test_build_bootstrap_env_by_name(test_config):
    metadata = render.render_recipe(os.path.join(thisdir, "variant_recipe"),
                                    no_download_source=False, config=test_config)
    assert not any("git" in pkg for pkg in metadata.meta["requirements"]["build"]), metadata.meta["requirements"]["build"]
    try:
        cmd = "conda create -y -n conda_build_bootstrap_test git"
        subprocess.check_call(cmd.split())
        metadata = render.add_build_config(metadata, "conda_build_bootstrap_test")
        assert any("git" in pkg for pkg in metadata.meta["requirements"]["build"]), metadata.meta["requirements"]["build"]
    finally:
        cmd = "conda remove -y -n conda_build_bootstrap_test --all"
        subprocess.check_call(cmd.split())


def test_build_bootstrap_env_by_path(test_config):
    metadata = render.render_recipe(os.path.join(thisdir, "variant_recipe"), no_download_source=False, config=test_config)
    assert not any("git" in pkg for pkg in metadata.meta["requirements"]["build"]), metadata.meta["requirements"]["build"]
    path = os.path.join(thisdir, "conda_build_bootstrap_test")
    try:
        cmd = "conda create -y -p {} git".format(path)
        subprocess.check_call(cmd.split())
        metadata = render.add_build_config(metadata, path)
        assert any("git" in pkg for pkg in metadata.meta["requirements"]["build"]), metadata.meta["requirements"]["build"]
    finally:
        cmd = "conda remove -y -p {} --all".format(path)
        subprocess.check_call(cmd.split())
