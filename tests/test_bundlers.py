import os

import pytest

from .utils import metadata_dir, put_bad_conda_on_path, get_noarch_python_meta
from conda_build import bundlers


def test_write_about_json_without_conda_on_path(testing_workdir, testing_metadata):
    with put_bad_conda_on_path(testing_workdir):
        # verify that the correct (bad) conda is the one we call
        with pytest.raises(subprocess.CalledProcessError):
            subprocess.check_output('conda -h', env=os.environ, shell=True)
        bundlers.write_about_json(testing_metadata)

    output_file = os.path.join(testing_metadata.config.info_dir, 'about.json')
    assert os.path.isfile(output_file)
    with open(output_file) as f:
        about = json.load(f)
    assert 'conda_version' in about
    assert 'conda_build_version' in about
