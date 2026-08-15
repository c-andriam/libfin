import json
import logging
import os

from libfin.config import config as pkg_config
from libfin import __version__, LibfinError
from libfin.vendor.hexdump import hexdump

LOGGER = logging.getLogger(__name__)


def print_banner(command_name, parms):
    message = f'{command_name} (libfin {__version__})'
    print(message)
    print('parameters:')
    for parm_key in parms:
        if parms[parm_key]:
            print(f' -{parm_key}:{parms[parm_key]}')


def print_exception_details(err: LibfinError):
    print("*** ERROR - processing has stopped ***")
    if err.record_number:
        print(f'Error detected in record {err.record_number}')
    print(err)
    if err.ex:  # another exception caused this, so print details
        print(err.ex)
    if err.binary_context_data:
        hexdump(err.binary_context_data)


def add_version(parser):
    version_text = f'%(prog)s (libfin {__version__})\n'

    parser.add_argument('--version', action='version', version=version_text)


def get_config(config_filename, envvar='LIBFIN_CONFIG', cli_filename=None):
    # check for json config file provided from command line argument
    if cli_filename:
        if os.path.isfile(cli_filename):
            LOGGER.info('Using cli config at {}'.format(cli_filename))
            with open(cli_filename, 'r') as f:
                config = f.read()
            return json.loads(config)

    # check for json config file from ENVVAR directory
    config_dir = os.environ.get(envvar)
    LOGGER.debug('config_dir={}'.format(config_dir))
    if config_dir:
        config_dir = os.path.abspath(config_dir)
        LOGGER.debug(os.path.join(config_dir, config_filename))
        if os.path.isfile(os.path.join(config_dir, config_filename)):
            LOGGER.info('Using config at {}'.format(config_dir))
            config_filename = os.path.join(config_dir, config_filename)
            with open(config_filename, 'r') as f:
                config = f.read()
            return json.loads(config)

    # Use package config
    return pkg_config
