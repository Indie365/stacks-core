#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
    Blockstack-client
    ~~~~~
    copyright: (c) 2014-2015 by Halfmoon Labs, Inc.
    copyright: (c) 2016-2017 by Blockstack.org

    This file is part of Blockstack-client.

    Blockstack-client is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    Blockstack-client is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.
    You should have received a copy of the GNU General Public License
    along with Blockstack-client. If not, see <http://www.gnu.org/licenses/>.
"""

import json
import os
import sys
import time
import jsontokens
import urllib
import virtualchain
import posixpath
import uuid
import errno
import hashlib
import jsontokens
import collections
import threading
import functools
import traceback
import sqlite3

from ..logger import get_logger
from ..proxy import get_default_proxy
from ..config import get_config, get_local_device_id
from ..constants import BLOCKSTACK_TEST, BLOCKSTACK_DEBUG, DEFAULT_DEVICE_ID, CONFIG_PATH
from ..storage import sign_data_payload, make_data_tombstone, make_fq_data_id, sign_data_tombstone, parse_data_tombstone, verify_data_tombstone, parse_fq_data_id, \
        hash_data_payload, sign_data_payload, serialize_mutable_data, get_storage_handlers, verify_data_payload, get_mutable_data

from .blob import datastore_get_id
from .directory import get_root_directory
from .mutable import put_raw_data
from .policy import prioritize_read_drivers

log = get_logger('gaia-file')

def get_file_data_from_header(datastore_id, file_name, file_header, drivers, config_path=CONFIG_PATH, blockchain_id=None):
    """
    Get file data from a header

    This is a server-side method

    Return {'status': True, 'data': the actual data}
    return {'error': ..., 'errno': ...} on error
    """

    urls = file_header['urls']
    data_hash = file_header['data_hash']

    # optimization: try local drivers before non-local drivers
    drivers = prioritize_read_drivers(config_path, drivers)

    # go get it 
    # NOTE: fq_file_name isn't needed since we have URLs, but include it anyway for logging's sake.
    fq_file_name = '{}/{}'.format(datastore_id, file_name)
    file_data = get_mutable_data(fq_file_name, None, urls=urls, data_hash=data_hash, blockchain_id=blockchain_id, drivers=drivers, decode=False)
    if file_data is None:
        return {'error': 'Failed to load {}'.format(fq_file_name), 'errno': "ENODATA"}
    
    return {'status': True, 'data': file_data}


def get_file_data(datastore_id, file_name, data_pubkeys, this_device_id=None, device_ids=None, force=False, timestamp=0, config_path=CONFIG_PATH, proxy=None, blockchain_id=None, full_app_name=None): 
    """
    Get file data

    This is a server-side method.

    Return {'status': True, 'data': the actual data}
    Return {'error': ..., 'errno': ...} on error
    """
    from .datastore import get_datastore_info

    # look up the datastore
    device_ids = None
    if data_pubkeys is not None:
        device_ids = [dpk['device_id'] for dpk in data_pubkeys]

    if not this_device_id:
        this_device_id = get_local_device_id(config_dir=os.path.dirname(config_path))
     
    res = get_datastore_info(blockchain_id=blockchain_id, full_app_name=full_app_name, datastore_id=datastore_id, device_ids=device_ids, config_path=config_path)
    if 'error' in res:
        log.error("Failed to get datastore info: {}".format(res['error']))
        return res

    datastore = res['datastore']

    root_uuid = datastore['root_uuid']
    drivers = datastore['drivers']
    datastore_id = datastore_get_id(datastore['pubkey'])

    res = get_file_info(datastore_id, file_name, data_pubkeys, this_device_id, datastore=datastore, force=force, timestamp=timestamp, config_path=config_path, proxy=proxy, blockchain_id=blockchain_id, full_app_name=full_app_name)
    if 'error' in res:
        return res

    file_header = res['file_info']
    return get_file_data_from_header(datastore_id, file_name, file_header, datastore['drivers'], config_path=config_path, blockchain_id=blockchain_id)


def get_file_info( datastore_id, file_name, data_pubkeys, this_device_id, datastore=None, drivers=None, device_ids=None, force=False, timestamp=0, config_path=CONFIG_PATH, proxy=None, blockchain_id=None, full_app_name=None ):
    """
    Look up all the inodes along the given fully-qualified path, verifying them and ensuring that they're fresh along the way.

    This is a server-side method.
    
    Return {'status': True, 'device_root_page': device_root_dir, 'file_info': header}
    Return {'error': ..., 'errno': ...} on error
    """
    from .datastore import get_datastore_info

    if proxy is None:
        proxy = get_default_proxy(config_path)

    if datastore is None:
        # look up the datastore
        if device_ids is None and data_pubkeys is not None:
            device_ids = [dpk['device_id'] for dpk in data_pubkeys]
     
        res = get_datastore_info(blockchain_id=blockchain_id, full_app_name=full_app_name, datastore_id=datastore_id, device_ids=device_ids, config_path=config_path)
        if 'error' in res:
            log.error("Failed to get datastore info: {}".format(res['error']))
            return res

        datastore = res['datastore']

    root_uuid = datastore['root_uuid']
    drivers = datastore['drivers']
    datastore_id = datastore_get_id(datastore['pubkey'])

    log.debug("Lookup {}/{}".format(datastore_id, file_name))

    # fetch all device-specific versions of this directory
    res = get_root_directory(datastore_id, root_uuid, data_pubkeys, drivers=drivers, timestamp=timestamp, force=force, config_path=config_path, proxy=proxy, blockchain_id=blockchain_id, full_app_name=full_app_name)
    if 'error' in res:
        log.error("Failed to get root directory for datastore {}: {}".format(datastore_id, res['error']))
        return {'error': 'Failed to get root directory: {}'.format(res['error']), 'errno': res['errno']}

    # NOTE: this is possibly None
    device_root = res['device_root_pages'].get(this_device_id)

    # find the file header
    root_files = res['root']
    if file_name not in root_files.keys():
        log.error("Not found: {}".format(file_name))
        return {'error': 'No such file: {}'.format(file_name), 'errno': "ENOENT"}

    file_header = root_files[file_name]
    ret = {
        'status': True,
        'file_info': file_header,
        'device_root_page': device_root
    }

    return ret


def put_file_data(datastore_id, device_id, file_name, file_bytes, drivers, config_path=CONFIG_PATH, blockchain_id=None):
    """
    Store file data to a set of drivers.
    
    This is a server-side method

    Return {'status': True, 'urls': [...]} on success
    Return {'error': ...} on failure
    """

    fq_data_id = make_fq_data_id(device_id, '{}/{}'.format(datastore_id, file_name))
    res = put_raw_data(fq_data_id, file_bytes, drivers, config_path=config_path, blockchain_id=blockchain_id)
    return res
