# (c) Continuum Analytics, Inc. / http://continuum.io
# All Rights Reserved
#
# conda is distributed under the terms of the BSD 3-clause license.
# Consult LICENSE.txt or http://opensource.org/licenses/BSD-3-Clause.

import logging

from ._version import get_versions
__version__ = get_versions()['version']
del get_versions

# Sub commands added by conda-build to the conda command
sub_commands = [
    'build',
    'convert',
    'develop',
    'index',
    'inspect',
    'metapackage',
    'pipbuild',
    'sign',
    'skeleton',
]

logging.basicConfig(level=logging.INFO)
# This squelches a ton of conda output that is not hugely relevant
logging.getLogger("conda.install").setLevel(logging.ERROR)
logging.getLogger("fetch").setLevel(logging.WARN)
logging.getLogger("print").setLevel(logging.WARN)
logging.getLogger("progress").setLevel(logging.WARN)
logging.getLogger("dotupdate").setLevel(logging.WARN)
logging.getLogger("requests.packages.urllib3.connectionpool").setLevel(logging.WARN)
