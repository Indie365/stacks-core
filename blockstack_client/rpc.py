#!/usr/bin/env python2
# -*- coding: utf-8 -*-

from __future__ import print_function

"""
    Blockstack-client
    ~~~~~
    copyright: (c) 2014-2015 by Halfmoon Labs, Inc.
    copyright: (c) 2016 by Blockstack.org

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

import os
import sys
import errno
import time
import atexit
import socket
import requests
import random
import posixpath
import SocketServer
from SimpleHTTPServer import SimpleHTTPRequestHandler
import urllib
import urllib2
import re
import base58
import base64
import jsonschema
import jsontokens
import subprocess
import platform
import shutil
import urlparse
from jsonschema import ValidationError
import client as bsk_client
from schemas import (
    APP_SESSION_REQUEST_SCHEMA,
    APP_SESSION_REQUEST_SCHEMA_OLD,
    OP_NAME_PATTERN,
    OP_BASE58CHECK_PATTERN,
    OP_ADDRESS_PATTERN,
    OP_ZONEFILE_HASH_PATTERN,
    PRIVKEY_INFO_SCHEMA,
    CREATE_DATASTORE_REQUEST_SCHEMA,
    DELETE_DATASTORE_REQUEST_SCHEMA,
    OP_BASE64_PATTERN,
    WALLET_SCHEMA_CURRENT,
    OP_HEX_PATTERN,
    LENGTH_MAX_NAME,
    LENGTH_MAX_NAMESPACE_ID,
    OP_URLENCODED_NOSLASH_COLON_CLASS,
    OP_NAME_CLASS,
    OP_NAMESPACE_CLASS,
    OP_BASE58CHECK_CLASS,
    PUT_DATASTORE_RESPONSE,
    DATASTORE_SCHEMA,
    GET_DEVICE_ROOT_RESPONSE,
    GET_ROOT_RESPONSE,
    FILE_LOOKUP_RESPONSE,
    PUT_DATA_RESPONSE,
    GET_PROFILE_RESPONSE,
)

import keylib
from keylib import ECPrivateKey

import signal
import json
import config as blockstack_config
import config as blockstack_constants
import backend
import backend.blockchain as backend_blockchain
import backend.drivers as backend_drivers
import proxy
from proxy import json_is_error, json_is_exception
import scripts

import subdomains

DEFAULT_UI_PORT = 8888
DEVELOPMENT_UI_PORT = 3000

from .constants import (
    CONFIG_FILENAME, serialize_secrets, WALLET_FILENAME,
    BLOCKSTACK_DEBUG, BLOCKSTACK_TEST, RPC_MAX_ZONEFILE_LEN, CONFIG_PATH,
    WALLET_FILENAME, TX_MIN_CONFIRMATIONS, DEFAULT_API_PORT, SERIES_VERSION,
    DEFAULT_SESSION_LIFETIME, FIRST_BLOCK_MAINNET,
    TX_MAX_FEE, set_secret, get_secret, DEFAULT_TIMEOUT)
from .method_parser import parse_methods
from .wallet import make_wallet

import app
import gaia
import zonefile
import wallet
import keys
import storage
import key_file
from utils import daemonize, streq_constant

from profile import get_profile

import virtualchain
from virtualchain.lib.ecdsalib import get_pubkey_hex, verify_raw_data

import blockstack_profiles

log = blockstack_config.get_logger()

running = False


class CLIRPCArgs(object):
    """
    Argument holder for CLI arguments,
    as part of the RPCInternalProxy wrapper
    methods.
    """
    pass


def run_cli_api(command_info, argv, config_path=CONFIG_PATH, check_rpc=True, **kw):
    """
    Run a CLI method, given its parsed command information and its list of arguments.
    The caller will be the API server; this method takes the extra step of inspecting the
    CLI method docstring to verify that it is allowed to be called from the API server.

    Return the result of the CLI command on success
    Return {'error': ...} on failure
    """
    # do sanity checks.
    if command_info is None:
        return {'error': 'No such method'}

    num_argv = len(argv)
    num_args = len(command_info['args'])
    num_opts = len(command_info['opts'])
    pragmas = command_info['pragmas']

    if num_argv > num_args + num_opts:
        msg = 'Invalid number of arguments (need at most {}, got {})'
        return {'error': msg.format(num_args + num_opts, num_argv)}

    if num_argv < num_args:
        msg = 'Invalid number of arguments (need at least {})'
        return {'error': msg.format(num_args)}

    if check_rpc and 'rpc' not in command_info['pragmas']:
        return {'error': 'This method is not available via RPC'}

    arg_infos = command_info['args'] + command_info['opts']
    args = CLIRPCArgs()

    for i, arg in enumerate(argv):
        arg_info = arg_infos[i]
        arg_name = arg_info['name']
        arg_type = arg_info['type']

        if arg is not None:
            # type-check...
            try:
                arg = arg_type(arg)
            except:
                return {'error': 'Type error: {} must be {}'.format(arg_name, arg_type)}

        setattr(args, arg_name, arg)

    if 'config_path' in kw:
        config_path = kw.pop('config_path')

    res = command_info['method'](args, config_path=config_path, **kw)

    return res


# need to wrap CLI methods to capture arguments
def api_cli_wrapper(method_info, config_path, check_rpc=True, include_kw=False):
    """
    Factory for generating a method
    """
    def argwrapper(*args, **kw):
        cf = config_path
        if kw.has_key('config_path'):
            cf = kw.pop('config_path')

        if include_kw:
            result = run_cli_api(method_info, list(args), config_path=cf, check_rpc=check_rpc, **kw)
        else:
            result = run_cli_api(method_info, list(args), config_path=cf, check_rpc=check_rpc)

        return result

    argwrapper.__doc__ = method_info['method'].__doc__
    argwrapper.__name__ = method_info['method'].__name__
    return argwrapper

JSONRPC_MAX_SIZE = 1024 * 1024

class BlockstackAPIEndpointHandler(SimpleHTTPRequestHandler):
    '''
    Blockstack RESTful API endpoint.
    '''

    http_errors = {
        "ENOENT": 404,
        "EINVAL": 401,
        "EPERM": 400,
        "EACCES": 403,
        "EEXIST": 409,
    }

    def _send_headers(self, status_code=200, content_type='application/json', more_headers={}):
        """
        Generate and reply headers
        """
        self.send_response(status_code)
        self.send_header('content-type', content_type)
        self.send_header('Access-Control-Allow-Origin', '*')    # CORS
        for (hdr, val) in more_headers.items():
            self.send_header(hdr, val)

        self.end_headers()


    def _reply_json(self, json_payload, status_code=200):
        """
        Return a JSON-serializable data structure
        """
        self._send_headers(status_code=status_code)
        json_str = json.dumps(json_payload)
        self.wfile.write(json_str)


    def _read_payload(self, maxlen=None):
        """
        Read raw uploaded data.
        Return the data on success
        Return None on I/O error, or if maxlen is not None and the number of bytes read is too big
        """

        client_address_str = "{}:{}".format(self.client_address[0], self.client_address[1])

        # check length
        read_len = self.headers.get('content-length', None)
        if read_len is None:
            log.error("No content-length given from {}".format(client_address_str))
            return None

        try:
            read_len = int(read_len)
        except:
            log.error("Invalid content-length")
            return None

        if maxlen is not None and read_len >= maxlen:
            log.error("Request from {} is too long ({} >= {})".format(client_address_str, read_len, maxlen))
            return None

        # get the payload
        request_str = self.rfile.read(read_len)
        return request_str


    def _read_json(self, schema=None, maxlen=JSONRPC_MAX_SIZE):
        """
        Read a JSON payload from the requester
        Return the parsed payload on success
        Return None on error
        """
        # JSON post?
        request_type = self.headers.get('content-type', None)
        client_address_str = "{}:{}".format(self.client_address[0], self.client_address[1])

        if request_type != 'application/json':
            log.error("Invalid request of type {} from {}".format(request_type, client_address_str))
            return None

        request_str = self._read_payload(maxlen=maxlen)
        if request_str is None:
            log.error("Failed to read request")
            return None

        # parse the payload
        request = None
        try:
            request = json.loads( request_str )
            if schema is not None:
                jsonschema.validate( request, schema )

        except ValidationError as ve:
            if BLOCKSTACK_DEBUG:
                log.exception(ve)
            log.error("Validation error on request {}...".format(
                request_str[:15]))
            if ve.validator == "maxLength":
                return {"error" : "maxLength"}
        except (TypeError, ValueError) as ve:
            if BLOCKSTACK_DEBUG:
                log.exception(ve)

            return None

        return request


    def parse_qs(self, qs):
        """
        Parse query string, but enforce one instance of each variable.
        Return a dict with the variables on success
        Return None on parse error
        """
        qs_state = urllib2.urlparse.parse_qs(qs)
        ret = {}
        for qs_var, qs_value_list in qs_state.items():
            if len(qs_value_list) > 1:
                return None

            ret[qs_var] = qs_value_list[0]

        return ret


    def verify_origin(self, origin_header, allowed):
        """
        Verify that the Origin: header is present and listed
        in our list of allowed origins
        Return True if so
        Return False if not
        """
        allowed_urlparse = [urlparse.urlparse(a) for a in allowed]
        origin_info = urlparse.urlparse(origin_header)

        for allowed in allowed_urlparse:
            if allowed.scheme == origin_info.scheme and allowed.netloc == origin_info.netloc:
                return True

        return False


    def verify_session(self, qs_values):
        """
        Verify and return the application's session.
        Return the decoded session token on success.
        Return None on error
        """
        session = None
        auth_header = self.headers.get('authorization', None)
        if auth_header is not None:
            # must be a 'bearer' type
            auth_parts = auth_header.split(" ", 1)
            if auth_parts[0].lower() == 'bearer':
                # valid JWT?
                session_token = auth_parts[1]
                session = app.app_verify_session(session_token, self.server.master_data_pubkey)

        else:
            # possibly given as a qs argument
            session_token = qs_values.get('session', None)
            if session_token is not None:
                session = app.app_verify_session(session_token, self.server.master_data_pubkey)

        return session


    def verify_password(self):
        """
        Verify that the caller submitted the right
        RPC authorization header.

        Only call this once we're sure that the caller
        didn't give us a valid session.

        Return True if we got the right header
        Return False if not
        """
        auth_header = self.headers.get('authorization', None)
        if auth_header is None:
            log.debug("No authorization header")
            return False

        auth_parts = auth_header.split(" ", 1)
        if auth_parts[0].lower() != 'bearer':
            # must be bearer
            log.debug("Not 'bearer' auth")
            return False

        if not streq_constant(auth_parts[1], self.server.api_pass):
            # wrong token
            log.debug("Wrong API password")
            return False

        return True


    def get_path_and_qs(self):
        """
        Parse and obtain the path and query values.
        We don't care about fragments.

        Return {'path': ..., 'qs_values': ...} on success
        Return {'error': ...} on error
        """
        path_parts = self.path.split("?", 1)

        if len(path_parts) > 1:
            qs = path_parts[1].split("#", 1)[0]
        else:
            qs = ""

        path = path_parts[0].split("#", 1)[0]
        path = posixpath.normpath(urllib.unquote(path))

        qs_values = self.parse_qs( qs )
        if qs_values is None:
            return {'error': 'Failed to parse query string'}

        parts = path.strip('/').split('/')

        return {'path': path, 'qs_values': qs_values, 'parts': parts}


    def _route_match( self, method_name, path_info, route_table ):
        """
        Look up the method to call
        Return the route info and its arguments on success:
        Return None on error
        """
        path = path_info['path']

        for route_path, route_info in route_table.items():
            if method_name not in route_info['routes'].keys():
                continue

            grps = re.match(route_path, path)
            if grps is None:
                continue

            groups = grps.groups()
            whitelist = route_info['whitelist']

            assert method_name in whitelist.keys()
            whitelist_info = whitelist[method_name]

            return {
                'route': route_info,
                'whitelist': whitelist_info,
                'method': route_info['routes'][method_name],
                'args': groups,
            }

        return None


    def OPTIONS_preflight( self, ses, path_info ):
        """
        Give back CORS preflight check headers
        """
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')    # CORS
        self.send_header('Access-Control-Allow-Methods', 'GET, PUT, POST, DELETE')
        self.send_header('Access-Control-Allow-Headers', 'content-type, authorization, range')
        self.send_header('Access-Control-Expose-Headers', 'content-length, content-range')
        self.send_header('Access-Control-Max-Age', 21600)
        self.end_headers()
        return


    def GET_auth( self, ses, path_info ):
        """
        Get a session token, given a signed request in the query string.
        Return 200 on success with {'token': token}
        Return 401 if the authRequest argument is missing
        Return 404 if we failed to sign in
        """

        token = path_info['qs_values'].get('authRequest')
        if token is None:
            log.debug("missing authRequest")
            return self._reply_json({'error': 'No authRequest= query argument given'}, status_code=401)

        if len(token) > 4096:
            # no way no how
            log.debug("token too long")
            return self._reply_json({'error': 'Invalid authRequest token: too big'}, status_code=401)

        decoded_token = jsontokens.decode_token(token)
        legacy = False
        decode_err = False
        try:
            assert isinstance(decoded_token, dict)
            assert decoded_token.has_key('payload')
            try:
                jsonschema.validate(decoded_token['payload'], APP_SESSION_REQUEST_SCHEMA )
            except ValidationError as ve2:
                decode_err = ve2
                log.debug("Authentication request is not current; trying legacy")
                jsonschema.validate(decoded_token['payload'], APP_SESSION_REQUEST_SCHEMA_OLD )
                legacy = True

        except ValidationError as ve:
            if BLOCKSTACK_TEST or BLOCKSTACK_DEBUG:
                if BLOCKSTACK_TEST:
                    log.debug("Invalid decoded token: {}".format(decoded_token['payload']))

            log.error('Invalid authRequest token, tried legacy and current decode paths.')
            if decode_err:
                log.error('Current decode error:')
                log.exception(decode_err)

            log.error('Legacy decode error:')
            log.exception(ve)
            return self._reply_json({'error': 'Invalid authRequest token: does not match any known request schemas'}, status_code=401)
        
        # NOTE: we want the scheme:// part here
        app_domain = str(decoded_token['payload']['app_domain'])
        methods = [str(m) for m in decoded_token['payload']['methods'] if len(m) > 0]
        blockchain_id = None
        app_private_key = None
        app_public_key = None
        app_public_keys = None
        requester_device_id = None
        requester_public_key = None

        if legacy:
            # legacy; fill in defaults
            app_public_key = str(decoded_token['payload']['app_public_key'])
            app_private_key = '0000000000000000000000000000000000000000000000000000000000000001'
            app_public_keys = []
            requester_device_id = '0'
            blockchain_id = None

        else:
            # current
            blockchain_id = decoded_token['payload']['blockchain_id']
            app_private_key = str(decoded_token['payload']['app_private_key'])
            app_public_key = get_pubkey_hex(app_private_key)
            app_public_keys = decoded_token['payload']['app_public_keys']
            requester_device_id = decoded_token['payload']['device_id']

            # must be listed in app_public_keys
            requester_public_key = None
            for apk in app_public_keys:
                if apk['device_id'] == requester_device_id:
                    requester_public_key = apk['public_key']
                    break

            if requester_public_key is None:
                return self._reply_json({'error': 'Invalid authRequest token: requesting device "{}" does not have a public key listed'.format(requester_device_id)}, status_code=401)

            # must match the app private key
            if keylib.key_formatting.compress(requester_public_key) != keylib.key_formatting.compress(app_public_key):
                log.error("Device public key mismatch: {} != {}".format(requester_public_key, app_public_key))
                return self._reply_json({'error': 'Invalid authRequest token: app private key does not match the device\'s listed public key'}, status_code=401)

        session_lifetime = DEFAULT_SESSION_LIFETIME
        log.debug("app_public_key: {}".format(app_public_key))
        log.debug("device_id: {}".format(requester_device_id))

        # it needs to be at least self-signed
        try:
            verifier = jsontokens.TokenVerifier()
            log.debug("Verify with {}".format(app_public_key))
            assert verifier.verify(token, app_public_key)
        except AssertionError as ae:
            if BLOCKSTACK_TEST:
                log.exception(ae)

            log.debug("Invalid token: wrong signature")
            return self._reply_json({'error': 'Invalid authRequest token: invalid signature'}, status_code=401)

        # make the token
        res = app.app_make_session( blockchain_id, app_private_key, app_domain, methods, app_public_keys, requester_device_id, self.server.master_data_privkey,
                                    session_lifetime=session_lifetime, config_path=self.server.config_path)

        if 'error' in res:
            return self._reply_json({'error': 'Failed to create session: {}'.format(res['error'])}, status_code=500)

        return self._reply_json({'token': res['session_token']})


    def GET_names_owned_by_address( self, ses, path_info, blockchain, address ):
        """
        Get all names owned by an address
        Returns the list on success
        Return 401 on unsupported blockchain
        Returns 500 on failure to get names
        """
        if blockchain != 'bitcoin':
            return self._reply_json({'error': 'Invalid blockchain'}, status_code=401)

        address = str(address)
        subdomain_names = subdomains.get_subdomains_owned_by_address(address)
        if json_is_error(subdomain_names):
            log.error("Failed to fetch subdomains owned by address")
            log.error(subdomain_names)
            subdomain_names = []

        # make sure we have the right encoding
        new_addr = virtualchain.address_reencode(address)
        if new_addr != address:
            log.debug("Re-encode {} to {}".format(new_addr, address))
            address = new_addr

        res = proxy.get_names_owned_by_address(address)
        if json_is_error(res):
            log.error("Failed to get names owned by address")
            self._reply_json({'error': 'Failed to list names by address'}, status_code=500)
            return

        self._reply_json({'names': res + subdomain_names})
        return


    def GET_names( self, ses, path_info ):
        """
        Get all names in existence
        Returns the list on success
        Returns 401 on invalid arguments
        Returns 500 on failure to get names
        """

        qs_values = path_info['qs_values']
        page = qs_values.get('page', None)
        if page is None:
            log.error("Page required")
            return self._reply_json({'error': 'page= argument required'}, status_code=401)

        try:
            page = int(page)
        except ValueError:
            log.error("Invalid page")
            return self._reply_json({'error': 'Invalid page= value'}, status_code=401)

        offset = page * 100
        count = 100

        res = proxy.get_all_names(offset, count)
        if json_is_error(res):
            log.error("Failed to list all names (offset={}, count={}): {}".format(offset, count, res['error']))
            self._reply_json({'error': 'Failed to list all names'}, status_code=500)
            return

        self._reply_json(res)
        return


    def POST_names( self, ses, path_info ):
        """
        Register or renew a name.
        Takes {'name': name to register}
        Reply 202 with a txid on success
        Reply 401 for invalid payload
        Reply 500 on failure to register
        """
        request_schema = {
            'type': 'object',
            'properties': {
                "name": {
                    'type': 'string',
                    'pattern': OP_NAME_PATTERN
                },
                "zonefile": {
                    'type': 'string',
                    'maxLength': RPC_MAX_ZONEFILE_LEN,
                },
                "owner_address": {
                    'type': 'string',
                    'pattern': OP_BASE58CHECK_PATTERN,
                },
                'min_confs': {
                    'type': 'integer',
                    'minimum': 0,
                },
                'tx_fee': {
                    'type': 'integer',
                    'minimum': 0,
                    'maximum': TX_MAX_FEE,
                },
                'cost_satoshis': {
                    'type': 'integer',
                    'minimum': 0,
                },
                'make_key_file': {
                    'type' : 'boolean'
                },
                'unsafe': {
                    'type': 'boolean'
                },
                'owner_key': PRIVKEY_INFO_SCHEMA,
                'payment_key': PRIVKEY_INFO_SCHEMA
            },
            'required': [
                'name'
            ],
            'additionalProperties': False,
        }

        qs_values = path_info['qs_values']
        internal = self.server.get_internal_proxy()

        request = self._read_json(schema=request_schema)
        if request is None or 'error' in request:
            return self._reply_json({"error": 'Invalid request'}, status_code=401)

        name = request['name']
        zonefile_txt = request.get('zonefile', None)
        recipient_address = request.get('owner_address', None)
        min_confs = request.get('min_confs', TX_MIN_CONFIRMATIONS)
        cost_satoshis = request.get('cost_satoshis', None)
        unsafe_reg = request.get('unsafe', False)
        make_key_file = request.get('make_key_file', False)

        owner_key = request.get('owner_key', None)
        payment_key = request.get('payment_key', None)

        if unsafe_reg:
            unsafe_reg = 'true'
        else:
            unsafe_reg = 'false'

        if min_confs < 0:
            min_confs = 0

        if min_confs != TX_MIN_CONFIRMATIONS:
            log.warning("Using payment UTXOs with as few as {} confirmations".format(min_confs))

        # make sure we have the right encoding
        if recipient_address:
            new_addr = virtualchain.address_reencode(str(recipient_address))
            if new_addr != recipient_address:
                log.debug("Re-encode {} to {}".format(new_addr, recipient_address))
                recipient_address = new_addr

        # do we own this name already?
        # i.e. do we need to renew?
        if owner_key is None:
            check_already_owned_by = self.server.wallet_keys['owner_addresses'][0]
        else:
            check_already_owned_by = virtualchain.get_privkey_address(owner_key)
        res = proxy.get_names_owned_by_address(check_already_owned_by)
        if json_is_error(res):
            log.error("Failed to get names owned by address")
            self._reply_json({'error': 'Failed to list names by address'}, status_code=500)
            return

        op = None
        if name in res:
            # renew
            for prop in request_schema['properties'].keys():
                if prop in request.keys() and prop not in ['name', 'owner_key', 'payment_key']:
                    log.debug("Invalid renewal argument {}".format(prop))
                    return self._reply_json(
                        {'error': 'Name already owned by this wallet and ' +
                         '`{}` is an invalid argument for renewal'.format(prop)}, status_code=401)

            op = 'renew'
            log.debug("renew {}".format(name))
            res = internal.cli_renew(name, owner_key, payment_key, interactive=False,
                                     cost_satoshis=cost_satoshis)

        else:
            # register
            op = 'register'
            log.debug("register {}".format(name))
            res = internal.cli_register(name, zonefile_txt, recipient_address, min_confs,
                                        unsafe_reg, owner_key, payment_key, interactive=False, force_data=True,
                                        cost_satoshis=cost_satoshis, make_key_file=make_key_file)

        if 'error' in res:
            log.error("Failed to {} {}".format(op, name))
            return self._reply_json({"error": "{} failed.\n{}".format(op, res['error'])}, status_code=500)

        resp = {
            'success': True,
            'transaction_hash': res['transaction_hash'],
            'message': 'Name queued for registration.  The process takes several hours.  You can check the status with `blockstack info`.',
        }

        if 'tx' in res:
            resp['tx'] = res['tx']

        return self._reply_json(resp, status_code=202)


    def GET_name_info( self, ses, path_info, name ):
        """
        Look up a name's zonefile, address, and last TXID
        Reply status, zonefile, zonefile hash, address, and last TXID.
        'status' can be 'available', 'registered', 'revoked', or 'pending'
        """
        # are there any pending operations on this name
        internal = self.server.get_internal_proxy()
        registrar_info = internal.cli_get_registrar_info()
        if 'error' in registrar_info:
            log.error("Failed to connect to backend")
            self._reply_json({'error': 'Failed to connect to backend'}, status_code=500)
            return

        # if the name has pending operations, return the pending status
        for queue_type in registrar_info.keys():
            for pending_entry in registrar_info[queue_type]:
                if pending_entry['name'] == name:
                    # pending
                    log.debug("{} is pending".format(name))
                    ret = {
                        'status': 'pending',
                        'operation': queue_type,
                        'txid': pending_entry['tx_hash'],
                        'confirmations': pending_entry['confirmations'],
                    }
                    self._reply_json(ret)
                    return

        # not pending. get name
        # is this a subdomain?
        res = subdomains.is_address_subdomain(name)
        if res:
            subdomain, domain = res[1]
            try:
                subdomain_obj = subdomains.get_subdomain_info(subdomain, domain)
            except subdomains.SubdomainNotFound:
                self._reply_json({"status" : "available"}, status_code=404)
                return

            ret = {
                'status' : 'registered_subdomain',
                'zonefile_txt' : subdomain_obj.zonefile_str,
                'zonefile_hash' : storage.get_zonefile_data_hash(subdomain_obj.zonefile_str),
                'address' : subdomain_obj.address,
                'blockchain' : 'bitcoin',
                'last_txid' : subdomain_obj.last_txid,
            }

            self._reply_json(ret)
            return

        name_rec = proxy.get_name_blockchain_record(name)
        if json_is_error(name_rec):
            # does it exist?
            if name_rec['error'] == 'Not found.':
                ret = {
                    'status': 'available'
                }
                self._reply_json(ret, status_code=404)
                return

            else:
                # some other error
                log.error("Failed to look up {}: {}".format(name, name_rec['error']))
                self._reply_json({'error': 'Failed to lookup name'}, status_code=500)
                return

        zonefile_res = zonefile.get_name_zonefile(name, raw_zonefile=True, name_record=name_rec)
        zonefile_txt = None
        if 'error' in zonefile_res:
            error = "No zonefile for name"
            if zonefile_res is not None:
                error = zonefile_res['error']

            log.error("Failed to get name zonefile for {}: {}".format(name, error))
            zonefile_txt = {'error': 'No zone file loaded'}

        else:
            zonefile_txt = zonefile_res.pop("zonefile")
            
            # make sure it's well-formed
            res = zonefile.decode_name_zonefile(name, zonefile_txt, allow_legacy=True)
            if res is None:
                log.error("Failed to parse zone file for {}".format(name))
                zonefile_txt = {'error': 'Non-standard zone file'}

        status = 'revoked' if name_rec['revoked'] else 'registered'

        address = name_rec['address']
        if address:
            address = virtualchain.address_reencode(str(address), network='mainnet')

        log.debug("{} is {}".format(name, status))
        ret = {
            'status': status,
            'zonefile': zonefile_txt,
            'zonefile_hash': name_rec['value_hash'],
            'address': address,
            'last_txid': name_rec['txid'],
            'blockchain': 'bitcoin',
            'expire_block': name_rec['expire_block'],
        }

        self._reply_json(ret)
        return


    def GET_name_history(self, ses, path_info, name ):
        """
        Get the history of a name.
        Takes `start_block` and `end_block` in the query string.
        return the history on success
        return 401 on invalid start_block or end_block
        return 500 on failure to query blockstack server
        """
        qs_values = path_info['qs_values']
        start_block = qs_values.get('start_block', None)
        end_block = qs_values.get('end_block', None)

        try:
            if start_block is None:
                start_block = FIRST_BLOCK_MAINNET
            else:
                start_block = int(start_block)

            if end_block is None:
                end_block = 2**32   # hope we never get this many blocks!
            else:
                end_block = int(end_block)
        except:
            log.error("Invalid start_block or end_block")
            self._reply_json({'error': 'Invalid start_block or end_block'}, status_code=401)
            return

        res = proxy.get_name_blockchain_history(name, start_block, end_block)
        if json_is_error(res):
            self._reply_json({'error': res['error']}, status_code=500)
            return

        self._reply_json(res)
        return


    def PUT_name_transfer( self, ses, path_info, name ):
        """
        Transfer a name to a new owner
        Return 202 and a txid on success, with {'transaction_hash': txid}
        Return 401 on invalid recipient address
        Return 500 on failure to broadcast tx
        """
        request_schema = {
            'type': 'object',
            'properties': {
                "owner": {
                    'type': 'string',
                    'pattern': OP_ADDRESS_PATTERN
                },
                'tx_fee': {
                    'type': 'integer',
                    'minimum': 0,
                    'maximum': TX_MAX_FEE
                },
                'owner_key': PRIVKEY_INFO_SCHEMA,
                'payment_key': PRIVKEY_INFO_SCHEMA
            },
            'required': [
                'owner'
            ],
            'additionalProperties': False,
        }

        qs_values = path_info['qs_values']
        internal = self.server.get_internal_proxy()

        request = self._read_json(schema=request_schema)
        if request is None or 'error' in request:
            self._reply_json({"error": 'Invalid request'}, status_code=401)
            return

        recipient_address = request['owner']
        try:
            base58.b58decode_check(recipient_address)
        except ValueError:
            self._reply_json({"error": 'Invalid owner address'}, status_code=401)
            return

        # make sure we have the right encoding
        new_addr = virtualchain.address_reencode(str(recipient_address))
        if new_addr != recipient_address:
            log.debug("Re-encode {} to {}".format(new_addr, recipient_address))
            recipient_address = new_addr

        owner_key = request.get('owner_key', None)
        payment_key = request.get('payment_key', None)

        res = internal.cli_transfer(name, recipient_address, owner_key, payment_key, interactive=False)
        if 'error' in res:
            log.error("Failed to transfer {}: {}".format(name, res['error']))
            self._reply_json({"error": 'Transfer failed.\n{}'.format(res['error'])}, status_code=500)
            return

        resp = {
            'success': True,
            'transaction_hash': res['transaction_hash'],
            'message': 'Name queued for transfer.  The process takes ~1 hour.  You can check the status with `blockstack info`.',
        }

        if 'tx' in res:
            resp['tx'] = res['tx']

        self._reply_json(resp, status_code=202)
        return


    def PUT_name_zonefile( self, ses, path_info, name ):
        """
        Set a new name zonefile
        Return 202 with a txid on success, with {'transaction_hash': txid}
        Return 401 on invalid zonefile payload
        Return 500 on failure to broadcast tx
        """
        request_schema = {
            'type': 'object',
            'properties': {
                "zonefile": {
                    'type': 'string',
                    'maxLength': RPC_MAX_ZONEFILE_LEN,
                },
                'zonefile_b64': {
                    'type': 'string',
                    'maxLength': (RPC_MAX_ZONEFILE_LEN * 4) / 3 + 1,
                },
                'zonefile_hash': {
                    'type': 'string',
                    'pattern': OP_ZONEFILE_HASH_PATTERN,
                },
                'key_file_txt': {
                    'type': 'string',
                },
                'tx_fee': {
                    'type': 'integer',
                    'minimum': 0,
                    'maximum': TX_MAX_FEE
                },
                'owner_key': PRIVKEY_INFO_SCHEMA,
                'payment_key': PRIVKEY_INFO_SCHEMA
            },
            'additionalProperties': False,
        }

        qs_values = path_info['qs_values']
        internal = self.server.get_internal_proxy()

        request = self._read_json(schema=request_schema)
        if request is None:
            self._reply_json({"error": 'Invalid request'}, status_code=401)
            return
        elif 'error' in request:
            self._reply_json({"error": request["error"]}, status_code=401)
            return

        zonefile_hash = request.get('zonefile_hash')
        zonefile_str = request.get('zonefile')
        zonefile_str_b64 = request.get('zonefile_b64')
        key_file_txt = request.get('key_file_txt')

        if zonefile_hash is None and zonefile_str is None and zonefile_str_b64 is None:
            log.error("No zonefile or zonefile hash received")
            self._reply_json({'error': 'Invalid request'}, status_code=401)
            return

        if zonefile_str is not None and zonefile_str_b64 is not None:
            log.error("Got both 'zonefile' and 'zonefile_b64'")
            self._reply_json({'error': 'Invalid request'}, status_code=401)
            return

        if zonefile_str_b64 is not None:
            try:
                zonefile_str = base64.b64decode(zonefile_str_b64)
            except:
                self._reply_json({'error': 'Invalid base64-encoded zonefile'}, status_code=401)
                return

        if zonefile_hash is not None and zonefile_str is not None:
            log.error("Got both zonefile and zonefile hash")
            self._reply_json({'error': 'Invalid request'}, status_code=401)
            return

        res = None
        owner_key = request.get('owner_key', None)
        payment_key = request.get('payment_key', None)

        if zonefile_str is not None:
            res = internal.cli_update(name, str(zonefile_str), "false", owner_key, payment_key, interactive=False,
                                      nonstandard=True, force_data=True)
        else:
            res = internal.cli_set_zonefile_hash(name, str(zonefile_hash), owner_key, payment_key)

        if 'error' in res:
            log.error("Failed to update {}: {}".format(name, res['error']))
            self._reply_json({"error": "Update failed.\n{}".format(res['error'])}, status_code=503)
            return

        resp = {
            'success': True,
            'zonefile_hash': res['zonefile_hash'],
            'transaction_hash': res['transaction_hash'],
            'message': 'Name queued for update.  The process takes ~1 hour.  You can check the status with `blockstack info`.',
        }

        if 'tx' in res:
            resp['tx'] = res['tx']

        self._reply_json(resp, status_code=202)
        return


    def DELETE_name( self, ses, path_info, name ):
        """
        Revoke a name.
        Reply 202 on success, with {'transaction_hash': txid}
        Reply 401 on invalid payload
        Reply 500 on failure to revoke
        """
        internal = self.server.get_internal_proxy()
        res = internal.cli_revoke(name, interactive=False)
        if 'error' in res:
            log.error("Failed to revoke {}: {}".format(name, res['error']))
            self._reply_json({"error": "Revoke failed.\n{}".format(res['error'])}, status_code=500)
            return

        resp = {
            'success': True,
            'transaction_hash': res['transaction_hash'],
            'message': 'Name queued for revocation.  The process takes ~1 hour.  You can check the status with `blockstack info`.'
        }

        if 'tx' in res:
            resp['tx'] = res['tx']

        self._reply_json(resp, status_code=202)
        return


    def GET_name_public_key( self, ses, path_info, name ):
        """
        Get a name's current zone file public key
        Reply {'public_key': ...} on success
        Reply 404 if the zone file has no public key
        Reply 503 on failure to fetch data
        """
        internal = self.server.get_internal_proxy()
        resp = internal.cli_get_public_key(name)
        if json_is_error(resp):
            if resp.has_key('errno'):
                if resp['errno'] == "EINVAL":
                    # no public key in zone file
                    return self._reply_json({'error': resp['error']}, status_code=404)

                elif resp['errno'] == "ENODATA":
                    # failed to load
                    return self._reply_json({'error': resp['error']}, status_code=503)

            return self._reply_json({'error': resp['error']}, status_code=500)

        return self._reply_json(resp)


    def GET_name_zonefile( self, ses, path_info, name ):
        """
        Get the name's current zonefile data.
        With `raw=1` on the query string, return the raw zone file.

        Reply the {'zonefile': zonefile} on success
        Reply 500 on failure to fetch or parse data
        """
        raw = path_info['qs_values'].get('raw', '')
        raw = (raw.lower() in ['1', 'true'])
        
        internal = self.server.get_internal_proxy()
        resp = internal.cli_get_name_zonefile(name, "true" if not raw else "false", raw=False)
        if json_is_error(resp):
            log.error("Failed to load zone file for {}: {}".format(name, resp['error']))
            self._reply_json({"error": resp['error']}, status_code=500)
            return

        if raw:
            self._send_headers(status_code=200, content_type='application/octet-stream')
            self.wfile.write(resp['zonefile'])

        else:
            self._reply_json({'zonefile': resp['zonefile']})

        return


    def GET_name_zonefile_by_hash( self, ses, path_info, name, zonefile_hash ):
        """
        Get a historic zonefile for a name
        With `raw=1` on the query string, return the raw zone file

        Reply 200 with {'zonefile': zonefile} on success
        Reply 204 with {'error': ...} if the zone file is non-standard
        Reply 404 on not found
        Reply 500 on failure to fetch data
        """
        raw = path_info['qs_values'].get('raw', '')
        raw = (raw.lower() in ['1', 'true'])

        conf = blockstack_config.get_config(self.server.config_path)

        blockstack_server = conf['server']
        blockstack_port = conf['port']
        blockstack_hostport = '{}:{}'.format(blockstack_server, blockstack_port)

        historic_zonefiles = gaia.list_update_history(name)
        if json_is_error(historic_zonefiles):
            self._reply_json({'error': historic_zonefiles['error']}, status_code=500)
            return

        if zonefile_hash not in historic_zonefiles:
            self._reply_json({'error': 'No such zonefile'}, status_code=404)
            return

        resp = proxy.get_zonefiles( blockstack_hostport, [str(zonefile_hash)] )
        if json_is_error(resp):
            self._reply_json({'error': resp['error']}, status_code=500)
            return

        if raw:
            self._send_headers(status_code=200, content_type='application/octet-stream')
            self.wfile.write(resp['zonefiles'][str(zonefile_hash)])

        else:
            # make sure it's valid
            if str(zonefile_hash) not in resp['zonefiles']:
                log.debug('Failed to find zonefile hash {}, possess {}'.format(
                    str(zonefile_hash), resp['zonefiles'].keys()))
                return self._reply_json({'error': 'No such zonefile'}, status_code=404)
            zonefile_txt = resp['zonefiles'][str(zonefile_hash)]
            res = zonefile.decode_name_zonefile(name, zonefile_txt, allow_legacy=True)
            if res is None:
                log.error("Failed to parse zone file for {}".format(name))
                self._reply_json({'error': 'Non-standard zone file for {}'.format(name)}, status_code=204)
                return

            self._reply_json({'zonefile': zonefile_txt})

        return


    def PUT_name_zonefile_hash( self, ses, path_info, name, zonefile_hash ):
        """
        Set a name's zonefile hash directly.
        Reply 202 with txid on success, as {'transaction_hash': txid}
        Reply 500 on internal failure
        """
        internal = self.server.get_internal_proxy()
        resp = internal.cli_set_zonefile_hash( name, zonefile_hash )
        if json_is_error(resp):
            self._reply_json({'error': resp['error']}, status_code=500)
            return

        ret = {
            'status': True,
            'transaction_hash': resp['transaction_hash'],
            'message': 'Name queued for update.  The process takes ~1 hour.  You can check the status with `blockstack info`.'
        }

        if 'tx' in resp:
            ret['tx'] = resp['tx']

        self._reply_json(ret)
        return


    def GET_name_profile( self, ses, path_info, name ):
        """
        Get a profile from a name.
        Reply the profile on success
        Return 404 on failure to load
        """
        res = get_profile(name, use_legacy=True, include_raw_zonefile=True, include_name_record=True)
        if json_is_error(res):
            return self._reply_json({'error': res['error']}, status_code=404)
        
        ret = {
            'status': True,
            'profile': res['profile'],
            'name_record': res['name_record'],
        }

        try:
            json.dumps(res['raw_zonefile'])
            ret['zonefile'] = res['raw_zonefile']
        except:
            ret['zonefile_b64'] = base64.b64encode(res['raw_zonefile'])
        
        return self._reply_json(ret)


    def _store_name_profile(self, ses, path_info, name):
        """
        Receive and store a name profile.
        Takes {'profile_data': signed profile JWT (string)}
        Returns 200 on success
        Returns 404 if the name doesn't exist
        Returns 503 on failure to store
        """
        profile_data_schema = {
            'type': 'object',
            'properties': {
                'profile_data': {
                    'type': 'string'
                },
            },
            'required': [
                'profile_data'
            ],
        }
        
        request = self._read_json()
        if request is None:
            return self._reply_json({'error': 'Failed to read JSON response'}, status_code=401)

        try:
            jsonschema.validate(request, profile_data_schema)
        except ValidationError as ve:
            if BLOCKSTACK_DEBUG:
                log.exception(ve)

            log.error("Failed to validate profile request")
            return self._reply_json({'error': 'Failed to parse profile JSON request'}, status_code=401)

        # authenticate
        name_rec = proxy.get_name_blockchain_record(name)
        if json_is_error(name_rec):
            # error
            status_code = None
            if json_is_exception(name_rec):
                status_code = 500
            else:
                status_code = 404

            return self._reply_json({'error': name_rec['error']}, status_code=status_code)
        
        profile_txt = request['profile_data']

        # verify and validate
        res = key_file.key_file_parse(profile_txt, name_rec['address'])
        if 'error' in res:
            log.error("Failed to parse key file: {}".format(res['error']))
            return self._reply_json({'error': 'Failed to parse key file: {}'.format(res['error'])}, status_code=401)

        # looks good. store it
        res = key_file.key_file_put(name, profile_txt, cache=gaia.GLOBAL_CACHE, config_path=self.server.config_path)
        if 'error' in res:
            log.error("Failed to store key file: {}".format(res['error']))
            return self._reply_json({'error': 'Failed to store key file: {}'.format(res['error'])}, status_code=502)

        return self._reply_json({'status': True})


    def POST_name_profile(self, ses, path_info, name):
        return self._store_name_profile(ses, path_info, name)


    def PUT_name_profile(self, ses, path_info, name):
        return self._store_name_profile(ses, path_info, name)


    def _parse_datastore_id(self, datastore_id_or_app_name):
        """
        Identify whether or not a string is a datastore ID or an app name
        Return {'status': True, 'datastore_id': ..., 'app_name': ...} on success
        Return {'error': ...} on error
        """
        datastore_id = None
        app_name = None

        if not app.is_valid_app_name(datastore_id_or_app_name) and re.match(OP_BASE58CHECK_PATTERN, datastore_id_or_app_name):
            # datastore ID
            datastore_id = datastore_id_or_app_name

        elif app.is_valid_app_name(datastore_id_or_app_name):
            # app name
            app_name = datastore_id_or_app_name

        if not app_name and not datastore_id:
            return {'error': 'Invalid datastore ID or app name'}
        return {'status': True, 'datastore_id': datastore_id, 'app_name': app_name}


    def GET_store( self, ses, path_info, datastore_id_or_app_name ):
        """
        Get the specific data store for this app user account or app domain.
        Takes the following query string arguments:
        * blockchain_id: owner of this datastore
        * device_ids: list of devices that write to this datastore

        Reply 200 on success with {'datastore': ...}
        Reply 401 if we're missing 'device_ids' or 'blockchain_id' query arguments
        Reply 503 on failure to load
        """

        device_ids = ''
        blockchain_id = ''
        datastore_id = ''
        app_name = ''

        blockchain_id = path_info['qs_values'].get('blockchain_id', '')
        device_ids = path_info['qs_values'].get('device_ids', '').split(',')

        if device_ids == ['']:
            device_ids = None

        res = self._parse_datastore_id(datastore_id_or_app_name)
        if 'error' in res:
            self._reply_json({'error': res['error']}, status_code=401)

        datastore_id = res['datastore_id']
        app_name = res['app_name']

        if blockchain_id is None or not scripts.is_name_valid(blockchain_id):
            # need device IDs and datastore_id if blockchain_id isn't given
            if not datastore_id or not device_ids:
                return self._reply_json({'error': 'Datastore ID and device IDs are required if the blockchain ID is not given'}, status_code=401)

        res = gaia.get_datastore_info(blockchain_id=blockchain_id, datastore_id=datastore_id, device_ids=device_ids, full_app_name=app_name, config_path=self.server.config_path)
        if 'error' in res:
            log.debug("Failed to get datastore: {}".format(res['error']))
            if res.has_key('errno'):
                # propagate an error code, if possible
                if res['errno'] in self.http_errors:
                    return self._reply_json(res, status_code=self.http_errors[res['errno']])

            return self._reply_json({'error': res['error']}, status_code=503)

        ret = {
            'datastore': res['datastore']
        }
        return self._reply_json(ret)


    def _store_signed_datastore( self, ses, path_info, datastore_sigs_info ):
        """
        Upload signed datastore information, if it is well-formed
        and signed with the session key.
        Return 200 on success
        Return 403 on invalid signature
        Return 503 on failure to store
        """

        try:
            jsonschema.validate(datastore_sigs_info, CREATE_DATASTORE_REQUEST_SCHEMA)
        except ValidationError as ve:
            if BLOCKSTACK_DEBUG:
                log.exception(ve)

            log.error("Invalid datastore and sig info")
            return False

        qs = path_info['qs_values']

        datastore_info = datastore_sigs_info['datastore_info']
        root_tombstones = datastore_sigs_info['root_tombstones']
        sigs_info = datastore_sigs_info['datastore_sigs']
        datastore_pubkey_hex = None

        if ses is not None:
            datastore_pubkey_hex = app.app_get_datastore_pubkey( ses )
        else:
            if not qs.has_key('datastore_pubkey'):
                log.error("Missing datastore_pubkey")
                return self._reply_json({'error': 'Invalid request: no session and no datastore_pubkey query argument'}, status_code=401)

            datastore_pubkey_hex = qs['datastore_pubkey']
            try:
                keylib.ECPublicKey(datastore_pubkey_hex)
            except:
                log.error("Malformed public key {}".format(datastore_pubkey_hex))
                return self._reply_json({'error': 'Invalid request: malformed public key "{}"'.format(datastore_pubkey_hex)}, status_code=401)

        res = gaia.verify_datastore_info( datastore_info, sigs_info, datastore_pubkey_hex )
        if not res:
            log.error("Failed to verify datastore signature with {}".format(datastore_pubkey_hex))
            return self._reply_json({'error': 'Unable to verify datastore info with {}'.format(datastore_pubkey_hex)}, status_code=403)

        res = gaia.put_datastore_info( datastore_info, sigs_info, root_tombstones, config_path=self.server.config_path )
        if 'error' in res:
            return self._reply_json({'error': 'Failed to store datastore info'}, status_code=503)

        return self._reply_json(res)


    def POST_store( self, ses, path_info ):
        """
        Make a data store for the application identified by the session
        Reply 200 if we either succeded or have an error message
        Reply 401 on invalid request
        Reply 403 on signature verification failure

        Takes a payload describing the datastore and signatures.
        Takes serialized datastore payload and signature
        """

        qs = path_info['qs_values']

        # maybe storing signed datastore?
        request = self._read_json()
        if request and 'error' not in request:
            return self._store_signed_datastore(ses, path_info, request)
        else:
            return self._reply_json({'error': 'Missing signed datastore info', 'errno': "EINVAL"}, status_code=401)


    def PUT_store( self, ses, path_info, app_user_id ):
        """
        Update a data store for the application identified by the session.
        """
        return self._reply_json({'error': 'Not implemented'}, status_code=501)


    def _delete_signed_datastore( self, ses, path_info, tombstone_info, device_ids=None ):
        """
        Given a set of sigend tombstones, go delete the datastore.
        """
        try:
            jsonschema.validate(tombstone_info, DELETE_DATASTORE_REQUEST_SCHEMA)
        except ValidationError as ve:
            if BLOCKSTACK_DEBUG:
                log.exception(ve)

            return self._reply_json({'error': 'Invalid tombstone info', 'errno': "EINVAL"}, status_code=401)
 
        datastore_pubkey = None
        authentic = False

        # app domain must be a valid fully-qualified app domain (i.e. ends in .1 or .x)
        app_domain = urlparse.urlparse(ses['app_domain']).netloc
        log.debug("app domain: {}".format(app_domain))

        if not app.is_valid_app_name(app_domain):
            return self._reply_json({'error': 'Invalid application domain name "{}"'.format(app_domain)}, status_code=401)

        blockchain_id = ses['blockchain_id']

        if device_ids is None:
            device_ids = [apk['device_id'] for apk in ses['app_public_keys']];

        # authenticate, and require qs-given device IDs to be covered by the tombstones.
        # at least one of the session's keys must succeed
        for dpk in ses['app_public_keys']:
            datastore_pubkey = dpk['public_key']
            res = gaia.verify_data_tombstones( tombstone_info['datastore_tombstones'], datastore_pubkey, device_ids=device_ids )
            if not res:
                continue

            res = gaia.verify_data_tombstones( tombstone_info['root_tombstones'], datastore_pubkey, device_ids=device_ids )
            if not res:
                continue
            
            authentic = True
            break
        
        if not authentic:
            log.error("Unable to authenticate tombstones")
            return self._reply_json({'error': 'Failed to authenticate datastore tombstones', 'errno': "EINVAL"})

        # extract datastore ID from tombstones 
        if len(tombstone_info['datastore_tombstones']) == 0:
            return self._reply_json({'error': 'Empty tombstone data', 'errno': "EINVAL"})

        datastore_tombstone = tombstone_info['datastore_tombstones'][0]
        ts_data = storage.parse_signed_data_tombstone(datastore_tombstone)
        if ts_data is None:
            return self._reply_json({'error': 'Invalid tombstone "{}"'.format(datastore_tombstone), 'errno': "EINVAL"})

        fq_data_id = ts_data['id']
        ts_device_id, ts_data_id = storage.parse_fq_data_id(fq_data_id)
        if ts_data_id is None:
            return self._reply_json({'error': 'Invalid fully-qualified data ID in tombstone "{}"'.format(datastore_tombstone), 'errno': "EINVAL"})

        # format: "$datastore_id.datastore"
        p = ts_data_id.split(".", 1)
        if len(p) != 2 or p[1] != 'datastore':
            return self._reply_json({'error': 'Invalid datastore data ID in tombstone "{}"'.format(datastore_tombstone), 'errno': "EINVAL"})

        datastore_id = p[0]

        # delete
        res = gaia.delete_datastore_info( datastore_id, tombstone_info['datastore_tombstones'], tombstone_info['root_tombstones'], ses['app_public_keys'], 
                                          blockchain_id=blockchain_id, full_app_name=app_domain, config_path=self.server.config_path )
        if 'error' in res:
            return self._reply_json({'error': 'Failed to delete datastore info: {}'.format(res['error']), 'errno': res['errno']})

        return self._reply_json({'status': True})


    def DELETE_store( self, ses, path_info ):
        """
        Delete the data store identified by the given session
        Takes a signed payload describing the datastore tombstones.

        Reply 200 on success
        Reply 403 on invalid user ID or invalid signatures
        Reply 503 on (partial) failure to delete all replicas
        """
        qs = path_info['qs_values']
        force = qs.get('force', "0")
        force = (force.lower() in ['1', 'true'])

        device_ids = qs.get('device_ids', None)
        if device_ids:
            device_ids = device_ids.split(',')

        # deleting from signed tombstones?
        request = self._read_json()
        if request and 'error' not in request:
            return self._delete_signed_datastore( ses, path_info, request, device_ids )

        else:
            return self._reply_json({'error': 'Missing signed datastore info', 'errno': "EINVAL"}, status_code=401)


    def _get_request_range(self):
        """
        Get the request's HTTP Range: header values
        TODO: does not yet support multi-range

        Returns (start, end), or (None, None)
        """
        range_header = self.headers.get('range', None)
        if range_header is None:
            return (None, None)
        
        range_header = range_header.strip()
        ranges = re.match("^bytes=([0-9]+)-([0-9]+)$", range_header)
        if not ranges:
            return (None, None)

        start, end = ranges.groups()
        start = int(start)
        end = int(end)

        if start >= end:
            return (None, None)

        return (start, end)


    def _load_device_pubkeys(self, qs, ses, app_name, blockchain_id):
        """
        Get device IDs and data pubkeys from the query string, or from the blockchain ID

        Returns {'status': True, 'data_pubkeys': [{'device_id': ..., 'public_key': ...}]} on success
        Return {'error': ..., 'status_code': ...} on failure
        """

        qs_device_ids = qs.get('device_ids', None)
        qs_device_pubkeys = qs.get('device_pubkeys', None)

        device_ids = None
        app_public_keys = None

        if qs_device_ids is None and qs_device_pubkeys is None:
            # not given; check session
            if (blockchain_id is not None and len(blockchain_id) > 0) and (app_name is not None and len(app_name) > 0):
                key_file_res = key_file.lookup_app_pubkeys(blockchain_id, app_name, cache=gaia.GLOBAL_CACHE)
                if 'error' in key_file_res:
                    return {'error': 'Failed to load key delegation information for "{}"'.format(blockchain_id), 'status_code': 503}
                
                app_pubkeys = key_file_res['pubkeys']
                if len(app_pubkeys.keys()) == 0:
                    return {'error': 'Blockchain ID {} is not registered in application {}'.format(blockchain_id, app_name), 'status_code': 404}

                device_ids = app_pubkeys.keys()
                app_public_keys = [app_pubkeys[device_id] for device_id in device_ids]

            elif ses is not None:
                # load from session
                device_ids = [apk['device_id'] for apk in ses['app_public_keys']]
                app_public_keys = [apk['public_key'] for apk in ses['app_public_keys']]
            
            else:
                # need either blockchain ID or session 
                return {'error': 'Need either a session JWT or both "blockchain_id" query argument and an application name', 'status_code': 401}
        else:
            if qs_device_ids is None or qs_device_pubkeys is None:
                return {'error': 'Missing either device_ids= or device_pubkeys= query arguments', 'status_code': 401}

            device_ids = [urllib.unquote(dev_id) for dev_id in qs_device_ids.split(',')]
            app_public_keys = qs_device_pubkeys.split(',')

            if len(device_ids) != len(app_public_keys):
                return {'error': 'Invalid request: device_ids must have length equal to device_pubkeys', 'status_code': 401}

        return {'status': True, 'data_pubkeys': [{'device_id': dev_id, 'public_key': data_pubk} for (dev_id, data_pubk) in zip(device_ids, app_public_keys)]}


    def GET_store_item( self, ses, path_info, datastore_id_or_app_name, item_type ):
        """
        Get a store item.
        Accepts as query string arguments:
        * blockchain_id
        * force (0, 1)
        * device_ids (list)
        * device_pubkeys (list)
        * this_device_id (str)
        
        Honors Range: headers for files, if given 

        Reply 200 on succes, with the raw data (as application/octet-stream for files, and as application/json for the file list)
        Reply 401 if no path is given
        Reply 403 on invalid user ID
        Reply 404 if the file/datastore does not exist
        Reply 500 if we fail to load the datastore record for some other reason than the above
        Reply 503 on failure to load data from storage providers
        """

        if item_type not in ['files', 'listing', 'device_roots', 'headers']:
            return self._reply_json({'error': 'Invalid request', 'errno': "EINVAL"}, status_code=401)

        qs = path_info['qs_values']
        blockchain_id = qs.get('blockchain_id', '')

        res = self._parse_datastore_id(datastore_id_or_app_name)
        if 'error' in res:
            return self._reply_json({'error': res['error']}, status_code=401)

        datastore_id = res['datastore_id']
        app_name = res['app_name']
        
        force = (qs.get('force', '0') != '0')
        this_device_id = qs.get('this_device_id', None)

        path = qs.get('path', None)

        # make sure we have device IDs and public keys 
        res = self._load_device_pubkeys(qs, ses, app_name, blockchain_id)
        if 'error' in res:
            log.error("Failed to load device keys: {}".format(res['error']))
            return self._reply_json({'error': res['error']}, status_code=res['status_code'])
        
        data_pubkeys = res['data_pubkeys']

        log.debug("Device IDs: {}".format(','.join([dpk['device_id'] for dpk in data_pubkeys])))
        log.debug("Device pubkeys: {}".format(','.join([dpk['public_key'] for dpk in data_pubkeys])))

        res = None
        status_code = 200

        if item_type == 'files':
            # getfile
            if path is None:
                return self._reply_json({'error': 'No path given', 'errno': "EINVAL"}, status_code=401)

            res = gaia.get_file_data(datastore_id, path, data_pubkeys, config_path=self.server.config_path, blockchain_id=blockchain_id, full_app_name=app_name, force=force)

        elif item_type == 'device_roots':
            # get device root listing
            if this_device_id is None:
                return self._reply_json({'error': 'missing "this_device_id" query argument'}, status_code=401)

            # directly do the query
            this_data_pubkey = None
            for dpk in data_pubkeys:
                if this_device_id == dpk['device_id']:
                    this_data_pubkey = dpk['public_key']

            res = gaia.get_device_root_directory(datastore_id, None, this_device_id, this_data_pubkey, config_path=self.server.config_path, blockchain_id=blockchain_id, full_app_name=app_name, force=force)

        elif item_type == 'listing':
            # get listing
            res = gaia.get_root_directory(datastore_id, None, data_pubkeys, config_path=self.server.config_path, blockchain_id=blockchain_id, full_app_name=app_name, force=force)

        elif item_type == 'headers':
            # file header
            if path is None:
                return self._reply_json({'error': 'No path given', 'errno': "EINVAL"}, status_code=401)

            if this_device_id is None:
                return self._reply_json({'error': 'missing "this_device_id" query argument'}, status_code=401)
            
            res = gaia.get_file_info(datastore_id, path, data_pubkeys, this_device_id, blockchain_id=blockchain_id, full_app_name=app_name, config_path=self.server.config_path, force=force)

        if json_is_error(res):
            # propagate an error code, if possible
            if res['errno'] in self.http_errors:
                status_code = self.http_errors[res['errno']]

            else:
                log.error("Failed operation {}: errno = '{}', error = '{}'".format(item_type, res['errno'], res['error']))
                status_code = 502

            return self._reply_json(res, status_code)

        if item_type == 'files':
            # return raw bytes
            res = res['data']
            bytes_range = self._get_request_range()
            if bytes_range == (None, None):
                # no bytes range
                self._send_headers(status_code=200, content_type='application/octet-stream')
                self.wfile.write(res)

            else:
                # serve back only the parts we want
                start, end = bytes_range
                res_len = len(res)
                if len(res) < start:
                    res = ""
                elif len(res) < end+1:
                    res = res[start:]
                else:
                    res = res[start:end+1]

                more_headers = {
                    'content-length': end+1-start,
                    'content-range': 'bytes {}-{}/{}'.format(start, end, res_len)
                }

                self._send_headers(status_code=206, content_type='application/octet-stream', more_headers=more_headers)
                self.wfile.write(res)

        else:
            if BLOCKSTACK_TEST:
                log.debug("Reply {}".format(json.dumps(res)))

            self._reply_json(res)

        return


    def POST_store_item( self, ses, path_info, store_id, item_type ):
        """
        Create a store item.
        Only works with the session's user ID.
        For files, this is putfile.  The payload is the raw data
        Reply 200 on success
        Reply 401 if no path is given, or we can't read the file
        Reply 403 on invalid userID
        Reply 503 on failure to upload data to storage providers
        """
        return self._create_or_update_store_item( ses, path_info, store_id, item_type )


    def PUT_store_item(self, ses, path_info, store_id, item_type ):
        """
        Update a store item.
        Only works with the session's user ID.
        Only works on files.
        Reply 200 on success
        Reply 401 if no path is given, ir we can't read the file
        Reply 403 on invalid userID
        Reply 503 on failre to upload data to storage providers
        """
        return self._create_or_update_store_item( ses, path_info, store_id, item_type )


    def _patch_from_signed_file_info( self, ses, path_info, operation, file_name, file_info, synchronous=False ):
        """
        Given signed file information, store it and act on it.
        Verify that the data is consistent with local versioning information.
        """
        file_info_schema = {
            'type': 'object',
            'properties': {
                'headers': {
                    'type': 'array',
                    'items': {
                        'type': 'string',
                    },
                },
                'payloads': {
                    'type': 'array',
                    'items': {
                        'type': 'string',
                    },
                },
                'signatures': {
                    'type': 'array',
                    'items': {
                        'type': 'string',
                    },
                },
                'tombstones': {
                    'type': 'array',
                    'items': {
                        'type': 'string',
                    },
                },
                'datastore_str': {
                    'type': 'string'
                },
                'datastore_sig': {
                    'type': 'string',
                    'pattern': OP_BASE64_PATTERN,
                },
            },
            'additionalProperties': False,
            'required': [
                'headers',
                'payloads',
                'signatures',
                'tombstones',
                'datastore_str',
                'datastore_sig',
            ],
        }

        try:
            jsonschema.validate(file_info, file_info_schema)
        except ValidationError as ve:
            if BLOCKSTACK_DEBUG:
                log.exception(ve)

            return self._reply_json({'error': 'Invalid request'}, status_code=401)

        # app domain must be a valid fully-qualified app domain (i.e. ends in .1 or .x)
        app_domain = ses['app_domain']
        log.debug("app domain: {}".format(ses['app_domain']))
        if not app.is_valid_app_name(app_domain):
            # try to see if it was a URL format
            app_domain = urlparse.urlparse(app_domain).netloc

        if not app.is_valid_app_name(app_domain):
            return self._reply_json({'error': 'Invalid application domain name "{}"'.format(ses['app_domain'])}, status_code=401)

        # optionally takes a public key
        qs = path_info['qs_values']
        this_device_id = ses['device_id']

        data_pubkey = None
        for dpk in ses['app_public_keys']:
            if this_device_id == dpk['device_id']:
                data_pubkey = dpk['public_key']

        if data_pubkey is None:
            return self._reply_json({'error': 'Invalid session: missing public key for device ID {}'.format(this_device_id)}, status_code=401)

        # verify datastore signature
        # do so by finding the datastore public key
        datastore_str = str(file_info['datastore_str'])
        datastore_sig = str(file_info['datastore_sig'])

        datastore_pubkey = None
        for dpk in ses['app_public_keys']:
            res = gaia.datastore_verify_and_parse(datastore_str, datastore_sig, dpk['public_key'])
            if 'error' in res:
                continue

            datastore_pubkey = dpk['public_key']
            break

        if datastore_pubkey is None:
            return self._reply_json({'error': 'Session does not contain a public key that matches the signature on the datastore'}, status_code=401)

        datastore = res['datastore']

        if operation == 'putfile':
            # NOTE: this can work even if blockchain_id is not given (i.e. the user hasn't claimed a name yet)
            # in which case, data_pubkey is *required*
            if ses['blockchain_id'] is None or len(ses['blockchain_id']) == 0:
                if data_pubkey is None:
                    return self._reply_json({'error': 'No blockchain ID given, and no data_pubkey given'}, status_code=401)

            # client may require blockchain ID, in which case, data_pubkey will be ignored 
            conf = blockstack_config.get_config(path=self.server.config_path)
            if not conf['storage_anonymous_write']:
                if ses['blockchain_id'] is None or len(ses['blockchain_id']) == 0:
                    return self._reply_json({'error': 'This node does not support anonymous writes'}, status_code=403)

                data_pubkey = None

            # payloads will be base64-encoded
            payloads_b64 = file_info['payloads']
            headers = file_info['headers']
            signatures = file_info['signatures']

            if file_name is None:
                return self._reply_json({'error': 'Missing "path" query argument'}, status_code=401)

            if len(headers) != len(payloads_b64):
                return self._reply_json({'error': 'Invalid request: number of payloads must match the number of headers'}, status_code=401)

            if len(payloads_b64) != len(signatures):
                return self._reply_json({'error': 'Invalid request: number of signatures must match the number of headers'}, status_code=401)

            if len(payloads_b64) != 1:
                return self._reply_json({'error': 'Invalid request: can only put one file at a time'}, status_code=401)

            file_header = headers[0]
            signature = signatures[0]
            payload = None
            try:
                payload = base64.b64decode(payloads_b64[0])
            except:
                log.error("putfile: File payloads must be base64-encoded")
                return self._reply_json({'error': 'putfile: file payloads must be base64-encoded'}, status_code=401)

            res = gaia.datastore_put_file_data( app_domain, datastore, file_name, file_header, payload, signature, ses['device_id'],
                                                config_path=self.server.config_path, blockchain_id=ses['blockchain_id'], data_pubkey=data_pubkey )
        elif operation == 'deletefile':
            # NOTE: this can work even if blockchain_id is not given (i.e. the user hasn't claimed a name yet)
            # in which case, data_pubkey is *required*
            if ses['blockchain_id'] is None or len(ses['blockchain_id']) == 0:
                if data_pubkey is None:
                    return self._reply_json({'error': 'No blockchain ID given, and no data_pubkey given'}, status_code=401)

            # client may require blockchain ID, in which case, data_pubkey will be ignored 
            conf = blockstack_config.get_config(path=self.server.config_path)
            if not conf['storage_anonymous_write']:
                if ses['blockchain_id'] is None or len(ses['blockchain_id']) == 0:
                    return self._reply_json({'error': 'This node does not support anonymous writes'}, status_code=403)

                data_pubkey = None

            # tombstone required
            tombstones = file_info['tombstones']

            if len(tombstones) < 1:
                return self._reply_json({'error': 'Invalid request: expected > 1 tombstone (got {})'.format(len(tombstones))}, status_code=401)

            # takes device IDs and public keys 
            res = self._load_device_pubkeys(qs, ses, app_domain, ses['blockchain_id'])
            if 'error' in res:
                return self._reply_json({'error': res['error']}, status_code=res['status_code'])
            
            data_pubkeys = res['data_pubkeys']
            res = gaia.datastore_delete_file_data( datastore, tombstones, blockchain_id=ses['blockchain_id'], full_app_name=app_domain, data_pubkeys=data_pubkeys, config_path=self.server.config_path)

        elif operation == 'device_roots':
            # NOTE: this can work even if blockchain_id is not given (i.e. the user hasn't claimed a name yet)
            # in which case, data_pubkey is *required*
            if ses['blockchain_id'] is None or len(ses['blockchain_id']) == 0:
                if data_pubkey is None:
                    return self._reply_json({'error': 'No blockchain ID given, and no data_pubkey given'}, status_code=401)

            # client may require blockchain ID, in which case, data_pubkey will be ignored 
            conf = blockstack_config.get_config(path=self.server.config_path)
            if not conf['storage_anonymous_write']:
                if ses['blockchain_id'] is None or len(ses['blockchain_id']) == 0:
                    return self._reply_json({'error': 'This node does not support anonymous writes'}, status_code=403)

                data_pubkey = None

            # put new device root
            # payload is the serialized root
            payloads = file_info['payloads']
            signatures = file_info['signatures']

            if len(payloads) != len(signatures):
                return self._reply_json({'error': 'Invalid request: number of payloads must match the number of headers'}, status_code=401)

            device_root_blob = payloads[0]
            device_root_sig = signatures[0]
            
            res = gaia.datastore_put_device_root_data( datastore, device_root_blob, device_root_sig, ses['device_id'], full_app_name=app_domain,
                                                       config_path=self.server.config_path, data_pubkey=data_pubkey, blockchain_id=ses['blockchain_id'], synchronous=synchronous )

        else:
            # indicates a bug
            return self._reply_json({'error': 'Unrecognized operation'}, status_code=500)

        if 'error' in res:
            log.debug("Failed to patch datastore with {}: {}".format(operation, res['error']))
            return self._reply_json({'error': res['error'], 'errno': res['errno']})

        # good to go!
        ret = {'status': True}
        if 'urls' in res:
            ret['urls'] = res['urls']

        return self._reply_json(ret)


    def _create_or_update_store_item( self, ses, path_info, datastore_id, item_type ):
        """
        Create or update a file.
        Implements POST_store_item and PUT_store_item
        Return 200 on successful save
        Return 401 on invalid request
        return 503 on storage failure

        Allows arbitrary sized PUTs/POSTs
        """
        
        # one of ses's public keys must match the datastore 
        valid_session = False
        for dpk in ses['app_public_keys']:
            if datastore_id in [gaia.datastore_get_id(keylib.key_formatting.decompress(str(dpk['public_key']))), gaia.datastore_get_id(keylib.key_formatting.compress(str(dpk['public_key'])))]:
                valid_session = True
                break

        if not valid_session:
            log.error("Invalid session: no public key matches the datastore ID")
            return self._reply_json({'error': 'Invalid session: no public key matches this datastore ID'}, status_code=403)

        if item_type not in ['files', 'device_roots']:
            log.error("Invalid request: unrecognized item type")
            self._reply_json({'error': 'Invalid request'}, status_code=401)
            return

        qs = path_info['qs_values']
        path = qs.get('path', None)
       
        synchronous = False
        if qs.get('sync') and qs.get('sync') != '0':
            synchronous = True

        request = self._read_json(maxlen=None)
        if request and 'error' not in request:
            # sent externally-signed data
            if item_type == 'files':
                if path is None:
                    log.debug("No path given")
                    return self._reply_json({'error': 'Invalid request: missing path'}, status_code=401)

                return self._patch_from_signed_file_info( ses, path_info, 'putfile', path, request )

            elif item_type == 'device_roots':
                return self._patch_from_signed_file_info( ses, path_info, 'device_roots', None, request, synchronous=synchronous )

            else:
                return self._reply_json({'error': 'Unrecognized item type "{}"'.format(item_type)}, status_code=401)

        else:
            return self._reply_json({'error': 'Missing signed file data'}, status_code=401)


    def DELETE_store_item( self, ses, path_info, datastore_id, item_type ):
        """
        Delete a store item.
        Only works with the session's user ID.
        For files, this is deletefile.
        Reply 200 on success
        Reply 401 if no path is given
        Reply 403 on invalid user ID
        Reply 404 if the file does not exist
        Reply 503 on failure to contact remote storage providers
        """

        # one of ses's public keys must match the datastore 
        valid_session = False
        for dpk in ses['app_public_keys']:
            if datastore_id in [gaia.datastore_get_id(keylib.key_formatting.decompress(str(dpk['public_key']))), gaia.datastore_get_id(keylib.key_formatting.compress(str(dpk['public_key'])))]:
                valid_session = True
                break

        if not valid_session:
            log.error("Invalid session: no public key matches the datastore ID")
            return self._reply_json({'error': 'Invalid session: no public key matches this datastore ID'}, status_code=403)

        if item_type not in ['files']:
            return self._reply_json({'error': 'Invalid request', 'errno': "EINVAL"}, status_code=401)

        qs = path_info['qs_values']
        path = qs.get('path', None)
        
        synchronous = False
        if qs.get('sync') and qs.get('sync') != '0':
            synchronous = True

        request = self._read_json()
        if request and 'error' not in request:
            # sent externally-signed data
            if item_type == 'files':
                return self._patch_from_signed_file_info( ses, path_info, 'deletefile', path, request )
            else:
                return self._reply_json({'error': 'Unrecognized operation "{}"'.format(item_type)}, status_code=500)

        else:
            return self._reply_json({'error': 'Missing signed file data'}, status_code=401)


    def GET_app_resource( self, ses, path_info, blockchain_id, app_domain ):
        """
        Get a signed application resource
        qs includes `name=...` for the resource name
        Return 200 on success with `application/octet-stream`
        Return 401 if no path is given
        Return 403 if the session has a whitelist of blockchain IDs, and the given one is not present
        Return 404 on not found
        Return 503 on failure to load
        """

        if ses is not None:
            if ses['app_domain'] != app_domain:
                return self._reply_json({'error': 'Unauthorized app domain'}, status_code=403)

            if blockchain_id != ses.get('blockchain_id', None):
                return self._reply_json({'error': 'Unauthorized blockchain ID'}, status_code=403)

        if not app.is_valid_app_name(app_domain):
            return self._reply_json({'error': 'Invalid application domain name "{}"'.format(app_domain)}, status_code=401)

        qs = path_info['qs_values']
        pubkey = None
        if qs.has_key('pubkey'):
            pubkey = qs['pubkey']

        internal = self.server.get_internal_proxy()
        res_path = qs.get('name', None)
        if res_path is None:
            return self._reply_json({'error': 'No resource name given'}, status_code=401)

        res = internal.cli_app_get_resource(blockchain_id, app_domain, res_path, pubkey, config_path=self.server.config_path)
        if 'error' in res:
            if res.has_key('errno') and res['errno'] in self.http_errors:
                return self._send_headers(status_code=self.http_errors[res['errno']], content_type='text/plain')

            else:
                return self._reply_json({'error': 'Failed to load resource'}, status_code=503)

        self._send_headers(status_code=200, content_type='application/octet-stream')
        self.wfile.write(res['res'])
        return


    def GET_collections( self, ses, path_info, user_id ):
        """
        Get the list of collections
        Reply the list of collections on success.
        """
        return self._reply_json({'error': 'Not implemented'}, status_code=501)


    def POST_collections( self, ses, path_info, user_id ):
        """
        Create a new collection
        """
        return self._reply_json({'error': 'Not implemented'}, status_code=501)


    def GET_collection_info( self, ses, path_info, user_id, collection_id ):
        """
        Get metadata on a user's collection (including the list of items)
        Reply the list of items on success
        Reply 404 on not found
        """
        return self._reply_json({'error': 'Not implemented'}, status_code=501)


    def GET_collection_item( self, ses, path_info, user_id, collection_id, item_id ):
        """
        Get a particular item from a particular collection
        Reply the item requested
        Reply 404 if the collection doesn't exist
        Reply 404 if the item doesn't exist
        """
        return self._reply_json({'error': 'Not implemented'}, status_code=501)


    def POST_collection_item( self, ses, path_info, user_id, collection_id ):
        """
        Add an item to a collection
        """
        return self._reply_json({'error': 'Not implemented'}, status_code=501)


    def GET_prices_namespace( self, ses, path_info, namespace_id ):
        """
        Get the price for a namespace
        Reply the price for the namespace as {'satoshis': price in satoshis}
        Reply 500 if we can't reach the namespace for whatever reason
        """
        price_info = proxy.get_namespace_cost(namespace_id)
        if json_is_error(price_info):
            # error
            status_code = None
            if json_is_exception(price_info):
                status_code = 500
            else:
                status_code = 404

            self._reply_json({'error': price_info['error']}, status_code=status_code)
            return

        ret = {
            'satoshis': price_info['satoshis']
        }
        self._reply_json(ret)
        return


    def GET_prices_name( self, ses, path_info, name ):
        """
        Get the price for a name in a namespace
        Reply the price as {'satoshis': price in satoshis}
        Reply 404 if the namespace doesn't exist
        Reply 500 if we can't reach the server for whatever reason
        """

        internal = self.server.get_internal_proxy()
        res = internal.cli_price(name)
        if json_is_error(res):
            # error
            status_code = None
            if json_is_exception(res):
                status_code = 500
            else:
                status_code = 404

            return self._reply_json({'error': res['error']}, status_code=status_code)

        self._reply_json(res)
        return


    def GET_namespaces( self, ses, path_info ):
        """
        Get the list of all namespaces
        Reply all existing namespaces
        Reply 500 if we can't reach the server for whatever reason
        """

        qs_values = path_info['qs_values']
        offset = qs_values.get('offset', None)
        count = qs_values.get('count', None)

        namespaces = proxy.get_all_namespaces(offset=offset, count=count)
        if json_is_error(namespaces):
            # error
            status_code = None
            if json_is_exception(namespaces):
                status_code = 500
            else:
                status_code = 404

            return self._reply_json({'error': namespaces['error']}, status_code=500)

        self._reply_json(namespaces)
        return


    def GET_namespace_info( self, ses, path_info, namespace_id ):
        """
        Look up a namespace's info
        Reply information about a namespace
        Reply 404 if the namespace doesn't exist
        Reply 500 for any error in talking to the blocksatck server
        """

        namespace_rec = proxy.get_namespace_blockchain_record(namespace_id)
        if json_is_error(namespace_rec):
            # error
            status_code = None
            if json_is_exception(namespace_rec):
                status_code = 500
            else:
                status_code = 404

            self._reply_json({'error': namespace_rec['error']}, status_code=status_code)
            return

        self._reply_json(namespace_rec)
        return


    def GET_namespace_names( self, ses, path_info, namespace_id ):
        """
        Get the list of names in a namespace
        Reply the list of names in a namespace
        Reply 404 if the namespace doesn't exist
        Reply 500 for any error in talking to the blockstack server
        """

        qs_values = path_info['qs_values']
        page = qs_values.get('page', None)
        if page is None:
            log.error("Page required")
            return self._reply_json({'error': 'page= argument required'}, status_code=401)

        try:
            page = int(page)
        except ValueError:
            log.error("Invalid page")
            return self._reply_json({'error': 'Invalid page= value'}, status_code=401)

        offset = page * 100
        count = 100

        namespace_names = proxy.get_names_in_namespace(namespace_id, offset=offset, count=count)
        if json_is_error(namespace_names):
            # error
            status_code = None
            if json_is_exception(namespace_names):
                status_code = 500
            else:
                status_code = 404

            self._reply_json({'error': namespace_names['error']}, status_code=status_code)
            return

        self._reply_json(namespace_names)
        return


    def GET_wallet_payment_address( self, ses, path_info ):
        """
        Get the wallet payment address
        Return 200 with {'address': ...} on success
        Return 500 on failure to read the wallet
        """

        wallet_path = os.path.join( os.path.dirname(self.server.config_path), WALLET_FILENAME )
        if not os.path.exists(wallet_path):
            # shouldn't happen; the API server can't start without a wallet
            return self._reply_json({'error': 'No such wallet'}, status_code=500)

        try:
            payment_address, owner_address, data_pubkey = wallet.get_addresses_from_file(wallet_path=wallet_path)
            self._reply_json({'address': payment_address})
            return

        except Exception as e:
            log.exception(e)
            self._reply_json({'error': 'Failed to read wallet file'}, status_code=500)
            return


    def GET_wallet_owner_address( self, ses, path_info ):
        """
        Get the wallet owner address
        Return 200 with {'address': ...} on success
        Return 500 on failure to read the wallet
        """

        wallet_path = os.path.join( os.path.dirname(self.server.config_path), WALLET_FILENAME )
        if not os.path.exists(wallet_path):
            # shouldn't happen; the API server can't start without a wallet
            return self._reply_json({'error': 'No such wallet'}, status_code=500)

        try:
            payment_address, owner_address, data_pubkey = wallet.get_addresses_from_file(wallet_path=wallet_path)
            self._reply_json({'address': owner_address})
            return

        except Exception as e:
            log.exception(e)
            self._reply_json({'error': 'Failed to read wallet file'}, status_code=500)
            return


    def GET_wallet_data_pubkey( self, ses, path_info ):
        """
        Get the data public key
        Return 200 with {'public_key': ...} on success
        Return 500 on failure to read the wallet
        """
        wallet_path = os.path.join( os.path.dirname(self.server.config_path), WALLET_FILENAME )
        if not os.path.exists(wallet_path):
            # shouldn't happen; the API server can't start without a wallet
            return self._reply_json({'error': 'No such wallet'}, status_code=500)

        try:
            payment_address, owner_address, data_pubkey = wallet.get_addresses_from_file(wallet_path=wallet_path)
            return self._reply_json({'public_key': data_pubkey})

        except Exception as e:
            log.exception(e)
            return self._reply_json({'error': 'Failed to read wallet file'}, status_code=500)


    def GET_wallet_keys( self, ses, path_info ):
        """
        Get the decrypted wallet keys
        Return 200 with wallet info on success
        Return 500 on failure to load the wallet
        """
        res = backend.registrar.get_wallet(config_path=self.server.config_path)
        if 'error' in res:
            # shouldn't happen; the API server can't start without a wallet
            return self._reply_json({'error': res['error']}, status_code=500)

        else:
            return self._reply_json(res)

    def _get_confirmed_balance( self, get_address ):
        satoshis_confirmed = backend_blockchain.get_balance(
            get_address, config_path = self.server.config_path,
            min_confirmations = 1)
        return satoshis_confirmed

    def GET_confirmed_balance_insight( self, ses, path_info, address ):
        """
        Handle GET /insight-api/addr/:address/balance
        """
        # step 0, convert address if needed
        get_address = virtualchain.address_reencode(address)
        if get_address != address:
            log.debug("Re-encode {} to {}".format(address, get_address))
        return self._reply_json( self._get_confirmed_balance(get_address) )

    def GET_unconfirmed_balance_insight( self, ses, path_info, address ):
        """
        Handle GET /insight-api/addr/:address/unconfirmedBalance
        """
        # step 0, convert address if needed
        get_address = virtualchain.address_reencode(address)
        if get_address != address:
            log.debug("Re-encode {} to {}".format(address, get_address))
        # step 1, get the confirmed balance
        satoshis_confirmed = self._get_confirmed_balance(get_address)
        # step 2, get total balance
        satoshis_total = backend_blockchain.get_balance(
            get_address, config_path = self.server.config_path,
            min_confirmations = 0)
        # step 3 -> unconfirmed balance = total - confirmed.
        #   this is replication of kind of silly insight-api behavior
        return self._reply_json( satoshis_total - satoshis_confirmed )

    def GET_wallet_balance( self, ses, path_info, min_confs = None ):
        """
        Get the wallet balance
        Return 200 with the balance
        Return 500 on error
        """
        internal = self.server.get_internal_proxy()
        if min_confs is None:
            res = internal.cli_balance(config_path=self.server.config_path)
        else:
            try:
                min_confs = int(min_confs)
            except:
                log.warn("Couldn't parse argument to min_confs of wallet_balance.")
                min_confs = None
            res = internal.cli_balance(min_confs,
                                       config_path=self.server.config_path)
        if 'error' in res:
            log.debug("Failed to query wallet balance: {}".format(res['error']))
            return self._reply_json({'error': 'Failed to query wallet balance'}, status_code=503)

        return self._reply_json({'balance': res['total_balance']})


    def POST_wallet_balance( self, ses, path_info ):
        """
        Transfer wallet balance.  Takes 'address' 'amount', 'min_confs', 'tx_only', 'message'
        Return 200 with the balance
        Return 500 on failure to contact the blockchain service
        """
        address_schema = {
            'type': 'object',
            'properties': {
                'address': {
                    'type': 'string',
                    'pattern': OP_BASE58CHECK_PATTERN,
                },
                'amount': {
                    'type': 'integer',
                    'minimum': 0,
                },
                'min_confs': {
                    'type': 'integer',
                    'minimum': 0,
                },
                'tx_only': {
                    'type': 'boolean'
                },
                'message': {
                    'type': 'string',
                },
                'payment_key': PRIVKEY_INFO_SCHEMA,
            },
            'required': [
                'address'
            ],
            'additionalProperties': False
        }

        request = self._read_json(schema=address_schema)
        if request is None or 'error' in request:
            return self._reply_json({'error': 'Invalid request'}, status_code=401)

        address = str(request['address'])
        amount = request.get('amount', None)
        min_confs = request.get('min_confs', TX_MIN_CONFIRMATIONS)
        tx_only = request.get('tx_only', False)
        message = request.get('message', None)
        payment_key = request.get('payment_key', None)

        if tx_only:
            tx_only = 'True'
        else:
            tx_only = 'False'

        if min_confs < 0:
            min_confs = 0

        # make sure we have the right encoding
        new_addr = virtualchain.address_reencode(str(address))
        if new_addr != address:
            log.debug("Re-encode {} to {}".format(new_addr, address))
            address = new_addr

        internal = self.server.get_internal_proxy()
        res = internal.cli_withdraw(
            address, amount, message, min_confs, tx_only, payment_key,
            config_path=self.server.config_path, interactive=False,
            wallet_keys=self.server.wallet_keys)
        if 'error' in res:
            log.debug("Failed to transfer balance: {}".format(res['error']))
            error_msg = {'error': 'Failed to transfer balance: {}'.format(res['error'])}

            if res.has_key('errno') and res['errno'] == "EIO":
                # network-level error: failed to broadcast
                return self._reply_json(error_msg, status_code=503)

            else:
                # bad input
                return self._reply_json(error_msg, status_code=400)

        return self._reply_json(res)


    def PUT_wallet_keys( self, ses, path_info ):
        """
        Set wallet keys
        Return 200 on success
        Return 500 on error
        """
        wallet = self._read_json(schema=WALLET_SCHEMA_CURRENT)
        if wallet is None or 'error' in wallet:
            return self._reply_json({'error': 'Failed to validate wallet keys'}, status_code=401)

        res = backend.registrar.set_wallet( (wallet['payment_addresses'][0], wallet['payment_privkey']),
                                            (wallet['owner_addresses'][0], wallet['owner_privkey']),
                                            (wallet['data_pubkeys'][0], wallet['data_privkey']), config_path=self.server.config_path )

        if 'error' in res:
            return self._reply_json({'error': 'Failed to set wallet: {}'.format(res['error'])}, status_code=500)

        self.server.wallet_keys = wallet
        return self._reply_json({'status': True})


    def PUT_wallet_key( self, ses, path_info, key_id ):
        """
        Set an individual wallet key ('owner', 'payment', 'data')
        Return 200 on success
        Return 401 on invalid key
        Return 500 on error
        """

        SET_WALLET_KEY_SCHEMA = {
            'anyOf': [
                PRIVKEY_INFO_SCHEMA,
                {
                    'type': 'object',
                    'properties': {
                        'private_key' : PRIVKEY_INFO_SCHEMA,
                        'persist_change' : {'type' : 'boolean'}
                    },
                    'required' : [
                        'private_key'
                    ]
                }
            ]
        }

        if key_id not in ['owner', 'payment', 'data']:
            return self._reply_json({'error': 'Invalid key ID'}, status_code=401)

        request_data = self._read_json(schema=SET_WALLET_KEY_SCHEMA)

        if isinstance(request_data, dict):
            privkey_info = request_data['private_key']
            persist = request_data.get('persist_change', False)
        else:
            privkey_info = request_data
            persist = False

        if privkey_info is None or 'error' in privkey_info:
            return self._reply_json({'error': 'Failed to validate private key'}, status_code=401)

        old_wallet = backend.registrar.get_wallet(config_path=self.server.config_path)
        if 'error' in old_wallet:
            return self._reply_json({'error': old_wallet['error']}, status_code=500)

        payment_privkey_info = old_wallet['payment_privkey']
        owner_privkey_info = old_wallet['owner_privkey']
        data_privkey_info = old_wallet['data_privkey']

        if key_id == 'owner':
            changed = owner_privkey_info
            owner_privkey_info = privkey_info

        elif key_id == 'payment':
            changed = payment_privkey_info
            payment_privkey_info = privkey_info

        elif key_id == 'data':
            changed = data_privkey_info
            data_privkey_info = privkey_info

        else:
            return self._reply_json({'error': 'Failed to set private key info'}, status_code=401)

        if virtualchain.get_privkey_address(changed) == virtualchain.get_privkey_address(privkey_info):
            return self._reply_json({'status': True,
                                     'message' : 'Supplied key was identical to previous; no change written'})

        # convert...
        new_wallet = make_wallet(None, payment_privkey_info=payment_privkey_info, owner_privkey_info=owner_privkey_info,
                                 data_privkey_info=data_privkey_info, encrypt=False)
        if persist:
            password = get_secret('BLOCKSTACK_CLIENT_WALLET_PASSWORD')
            if not password:
                return self._reply_json({'error' : 'Failed to load encryption password for wallet, refusing to persist key change.'}, status_code=500)

            res = wallet.save_modified_wallet(new_wallet, password, config_path=self.server.config_path)
            if 'error' in res:
                return self._reply_json(res, status_code=500)

        if 'error' in new_wallet:
            return self._reply_json({'error': 'Failed to reinstantiate wallet'}, status_code=500)

        res = backend.registrar.set_wallet( (new_wallet['payment_addresses'][0], new_wallet['payment_privkey']),
                                            (new_wallet['owner_addresses'][0], new_wallet['owner_privkey']),
                                            (new_wallet['data_pubkeys'][0], new_wallet['data_privkey']), config_path=self.server.config_path )

        if 'error' in res:
            return self._reply_json({'error': 'Failed to set wallet: {}'.format(res['error'])}, status_code=500)

        self.server.wallet_keys = new_wallet
        return self._reply_json({'status': True})


    def PUT_wallet_password( self, ses, path_info ):
        """
        Change the wallet password.
        Takes {'password': password, 'new_password': new password}
        Returns 200 on success
        Returns 401 on invalid request
        Returns 500 on failure to change the password
        """
        password_schema = {
            'type': 'object',
            'properties': {
                'password': {
                    'type': 'string'
                },
                'new_password': {
                    'type': 'string',
                },
            },
            'required': [
                'password',
                'new_password'
            ],
            'additionalProperties': False
        }

        request = self._read_json(schema=password_schema)
        if request is None or 'error' in request:
            return self._reply_json({'error': 'Invalid request'}, status_code=401)

        password = str(request['password'])
        new_password = str(request['password'])

        internal = self.server.get_internal_proxy()
        res = internal.cli_wallet_password(password, new_password, config_path=self.server.config_path, interactive=False)
        if 'error' in res:
            log.debug("Failed to change wallet password: {}".format(res['error']))
            return self._reply_json({'error': 'Failed to change password: {}'.format(res['error'])}, status_code=500)

        return self._reply_json({'status': True})


    def GET_registrar_state( self, ses, path_info ):
        """
        Handle GET /v1/node/registrar/state
        Get registrar state
        Return 200 on success
        """
        res = backend.registrar.state()
        return self._reply_json(res)


    def POST_reboot( self, ses, path_info ):
        """
        Reboot the node.
        Requires the caller pass the RPC secret
        Does not return on success
        Return 403 on failure
        """
        return self._reply_json({'error': 'Not implemented'}, status_code=501)


    def GET_node_config( self, ses, path_info ):
        """
        Get node configuration
        Return 200 on success
        Return 500 on failure to read
        """
        conf = None
        try:
            conf = blockstack_config.read_config_file(config_path=self.server.config_path)
            assert conf
        except Exception as e:
            log.exception(e)
            return self._reply_json({'error': 'Failed to read config'}, status_code=500)

        for unneeded_field in ['path', 'dir']:
            if conf.has_key(unneeded_field):
                del conf[unneeded_field]

        return self._reply_json(conf)


    def POST_node_config( self, ses, path_info, section ):
        """
        Set node configuration items (as {name}={value} in the query string)
        Return 200 on success
        Return 500 on failure to write
        """
        conf_items = path_info['qs_values']
        try:
            res = blockstack_config.write_config_section(self.server.config_path, section, conf_items)
            assert res
        except Exception as e:
            if BLOCKSTACK_DEBUG:
                log.exception(e)

            log.error("Failed to set config section {}".format(section))
            return self._reply_json({'error': 'Failed to write config section'}, status_code=500)

        return self._reply_json({'status': True})



    def DELETE_node_config_section( self, ses, path_info, section ):
        """
        Remove a config file section
        Return 200 on success
        Return 500 on failure
        """
        res = blockstack_config.delete_config_section(self.server.config_path, section)
        if not res:
            return self._reply_json({'error': 'Failed to delete section'}, status_code=500)

        else:
            return self._reply_json({'status': True})


    def DELETE_node_config_field( self, ses, path_info, section, field ):
        """
        Delete a specific config item
        Return 200 on success
        Return 500 on failure
        """
        res = blockstack_config.delete_config_field(self.server.config_path, section, field)
        if not res:
            return self._reply_json({'error': 'Failed to delete field'}, status_code=500)

        else:
            return self._reply_json({'status': True})


    def GET_node_logfile(self, ses, path_info):
        """
        Get the node's log file.
        Return 200 on success, and reply "text/plain" log
        Return 500 on failure
        """
        logpath = local_api_logfile_path(config_dir=os.path.dirname(self.server.config_path))
        with open(logpath, 'r') as f:
            logdata = f.read()

        self._send_headers(status_code=200, content_type='text/plain')
        self.wfile.write(logdata)


    def POST_node_logmsg(self, ses, path_info):
        """
        Write a line to the node's logfile
        qs args:
            * level: "debug", "info", "warning", "warn", "error", "critical"
            * name: name of the requester

        Return 200 on success
        Return 401 on invalid
        """
        loglevel = "info"
        name = "unknown"
        qs_values = path_info['qs_values']

        if qs_values.get('name') is not None:
            name = qs_values.get('name')

        if qs_values.get('level') is not None:
            loglevel = qs_values.get('level')

        if loglevel not in ['debug', 'info', 'warning', 'warn', 'error', 'critical']:
            return self._reply_json({'error': 'Invalid log level'}, status_code=401)

        logmsg = self._read_payload(maxlen=4096)
        msg = "{}: {}".format(name, logmsg)

        if loglevel == 'debug':
            log.debug(msg)

        elif loglevel == 'info':
            log.info(msg)

        elif loglevel in ['warn', 'warning']:
            log.warning(msg)

        elif loglevel == 'error':
            log.error(msg)

        else:
            log.critical(msg)

        self._send_headers(status_code=200, content_type='text/plain')
        return


    def GET_node_storage_driver_config( self, ses, path_info, driver_name ):
        """
        Get the system-wide storage driver config and routing information (i.e. index URLs) for one or more drivers.

        Return 200 on success
        Return 500 on failure to query
        """
        driver_config = {}
        index_url = backend_drivers.index_settings_get_index_manifest_url(driver_name, self.server.config_path)

        if index_url:
            driver_config['index_url'] = index_url

        ret = {}
        ret[driver_name] = driver_config

        return self._reply_json(ret)


    def POST_node_storage_driver_config(self, ses, path_info, driver_name):
        """
        DANGEROUS

        Initialize a storage driver explicitly
        qs values:
        * driver (str): the name of the driver

        Reply 200 on success
        Reply 202 on success, if we're still in the process of setting up the driver.
        Reply 401 if the driver is invalid
        Reply 500 if the driver fails to initialize
        """
        qs_values = path_info['qs_values']

        if len(driver_name) == 0:
            return self._reply_json({'error': 'Missing driver name'}, status_code=401)
        
        try:
            driver_mod = bsk_client.load_storage(driver_name)
            if driver_mod is None:
                return self._reply_json({'error': 'Invalid driver'}, status_code=401)

        except ValueError:
            return self._reply_json({'error': 'Invalid driver name'}, status_code=401)

        except Exception as e:
            if BLOCKSTACK_DEBUG:
                log.exception(e)

            return self._reply_json({'error': 'Failed to load driver'}, status_code=500)

        # patch config file
        # expect: { 'driver_config': { '$driver_name': { '$property': '$value', ...} } }
        request_schema = {
            'type': 'object',
            'properties': {
                'driver_config': {
                    'type': 'object',
                    'patternProperties': {
                        '^{}$'.format(driver_name): {
                            'type': 'object',
                            'patternProperties': {
                                '^[^ ]+$': {
                                    'type': 'object',
                                    'patternProperties': {
                                        '.+' : {
                                            'type': 'string'
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            },
            'required': [
                'driver_config',
            ],
        }

        driver_request = self._read_json(request_schema)
        if driver_request is not None and 'error' not in driver_request:
            log.debug("Updating driver config for {}".format(driver_name))
            driver_config = driver_request['driver_config']

            change = False
            log.debug("Set up config for driver {} system-wide".format(driver_name))

            # write storage config
            try:
                rc = blockstack_config.write_config_section(self.server.config_path, driver_name, driver_config)
                assert rc
            except Exception as e:
                if BLOCKSTACK_DEBUG:
                    log.exception(e)

                log.error("Failed to write config file")
                return self._reply_json({'error': 'Failed to update config file'}, status_code=500)

        else:
            log.debug("Will NOT update driver config for {}".format(driver_name))

        # read it back
        conf = None
        try:
            conf = blockstack_config.read_config_file(config_path=self.server.config_path)
            assert conf
        except Exception as e:
            if BLOCKSTACK_DEBUG:
                log.exception(e)

            return self._reply_json({'error': 'Failed to read config file'}, status_code=500)

        # instantiate...
        try:
            res = bsk_client.register_storage(driver_mod, conf)
        except backend_drivers.ConcurrencyViolationException as cve:
            # already running 
            if BLOCKSTACK_DEBUG:
                log.exception(cve)

            return self._reply_json({'warning': 'Operation already in progress'}, status_code=202)

        except Exception as e:
            if BLOCKSTACK_DEBUG:
                log.exception(e)

            return self._reply_json({'error': 'Driver failed to initialize'}, status_code=500)

        if not res:
            return self._reply_json({'error': 'Driver failed to initialize'}, status_code=500)

        ret = {
            'status': True,
        }

        return self._reply_json(ret)


    def GET_blockchain_ops( self, ses, path_info, blockchain_name, blockheight ):
        """
        Get the name's historic name operations
        Reply the list of nameops at the given block height
        Reply 404 for blockchains other than those supported
        Reply 500 for any error we have in talking to the blockstack server
        """
        if blockchain_name != 'bitcoin':
            # not supported
            self._reply_json({'error': 'Unsupported blockchain'}, status_code=401)
            return

        nameops = proxy.get_nameops_at(blockheight)
        if json_is_error(nameops):
            # error
            status_code = None
            if json_is_exception(nameops):
                status_code = 500
            else:
                status_code = 404

            self._reply_json({'error': nameops['error']}, status_code=status_code)
            return

        self._reply_json(nameops)
        return


    def GET_blockchain_name_history( self, ses, path_info, blockchain_name, name ):
        """
        Get the name's blockchain history
        Reply the raw history record on success
        Reply 404 if the name is not found
        Reply 500 if we have an error talking to the server
        """
        if blockchain_name != 'bitcoin':
            # not supported
            self._reply_json({'error': 'Unsupported blockchain'}, status_code=401)
            return

        name_rec = proxy.get_name_blockchain_record(name)
        if json_is_error(name_rec):
            # error
            status_code = None
            if json_is_exception(name_rec):
                status_code = 500
            else:
                status_code = 404

            self._reply_json({'error': name_rec['error']}, status_code=status_code)
            return

        self._reply_json({'error' : 'Unimplemented'}, status_code = 405)

    def GET_blockchain_num_names( self, ses, path_info, blockchain_name ):
        """
        Handle GET /blockchains/:blockchainID/name_count
        Reply with the number of names on this blockchain
        """
        if blockchain_name != 'bitcoin':
            # not supported
            self._reply_json({'error': 'Unsupported blockchain'}, status_code=401)
            return
        num_names = proxy.get_num_names()
        if json_is_error(num_names):
            if json_is_exception(info):
                status_code = 500
            else:
                status_code = 404

            self._reply_json({'error': info['error']}, status_code=status_code)
            return

        self._reply_json({'names_count': num_names})
        return

    def GET_blockchain_consensus( self, ses, path_info, blockchain_name ):
        """
        Handle GET /blockchain/:blockchainID/consensus
        Reply the consensus hash at this blockchain's tip
        Reply 401 for unrecognized blockchain
        Reply 404 for blockchains that we don't support
        Reply 500 for any error we have in talking to the blockstack server
        """
        if blockchain_name != 'bitcoin':
            # not supported
            self._reply_json({'error': 'Unsupported blockchain'}, status_code=401)
            return

        info = proxy.getinfo()
        if json_is_error(info):
            # error
            status_code = None
            if json_is_exception(info):
                status_code = 500
            else:
                status_code = 404

            self._reply_json({'error': info['error']}, status_code=status_code)
            return

        self._reply_json({'consensus_hash': info['consensus']})
        return


    def GET_blockchain_pending( self, ses, path_info, blockchain_name ):
        """
        Handle GET /blockchain/:blockchainID/pending
        Reply the list of pending transactions from our internal registrar queue
        Reply 401 if the blockchain is not known
        Reply 404 if the name cannot be found.
        """
        if blockchain_name != 'bitcoin':
            # not supported
            self._reply_json({'error': 'Unsupported blockchain'}, status_code=401)
            return

        internal = self.server.get_internal_proxy()
        res = internal.cli_get_registrar_info()
        if json_is_error(res):
            # error
            status_code = None
            if json_is_exception(res):
                status_code = 500
            else:
                status_code = 404

            self._reply_json({'error': res['error']}, status_code=status_code)
            return

        self._reply_json({'queues': res})
        return


    def GET_blockchain_unspents( self, ses, path_info, blockchain_name, address ):
        """
        Handle GET /blockchains/:blockchain_name/:address/unspent
        Takes min_confirmations= as a query-string arg.

        Reply 200 and the list of unspent outputs or current address states
        Reply 401 if the blockchain is not known
        Reply 503 on failure to contact the requisite back-end services
        """
        if blockchain_name != 'bitcoin':
            # not supported
            return self._reply_json({'error': 'Unsupported blockchain'}, status_code=401)

        # make sure we have the right encoding
        address = virtualchain.address_reencode(str(address))

        min_confirmations = path_info['qs_values'].get('min_confirmations', '{}'.format(TX_MIN_CONFIRMATIONS))
        try:
            min_confirmations = int(min_confirmations)
        except:
            return self._reply_json({'error': 'Invalid min_confirmations value: expected int'}, status_code=401)

        res = backend_blockchain.get_utxos(address, config_path=self.server.config_path, min_confirmations=min_confirmations)
        if 'error' in res:
            return self._reply_json({'error': 'Failed to query backend UTXO service: {}'.format(res['error'])}, status_code=503)

        return self._reply_json(res)


    def POST_broadcast_tx( self, ses, path_info, blockchain_name ):
        """
        Handle POST /blockchains/:blockchain_name/tx
        Reads {'tx': ...} as JSON from the request.

        Reply 200 and the transaction hash as {'status': True, 'tx_hash': ...} on success
        Reply 401 if the blockchain is not known
        Reply 503 on failure to contact the requisite back-end services
        """
        if blockchain_name != 'bitcoin':
            # not supported
            return self._reply_json({'error': 'Unsupported blockchain'}, status_code=401)

        tx_schema = {
            'type': 'object',
            'properties': {
                'tx': {
                    'type': 'string',
                    'pattern': OP_HEX_PATTERN,
                },
            },
            'additionalProperties': False,
            'required': [
                'tx'
            ],
        }

        tx_req = None
        tx_req = self._read_json(tx_schema)
        if tx_req is None or 'error' in tx_req:
            return self._reply_json({'error': 'Failed to parse request.  Expected {}'.format(json.dumps(tx_schema))}, status_code=401)

        # broadcast!
        res = backend_blockchain.broadcast_tx(tx_req['tx'], config_path=self.server.config_path)
        if 'error' in res:
            return self._reply_json({'error': 'Failed to sent transaction: {}'.format(res['error'])}, status_code=503)

        return self._reply_json(res)


    def GET_ping(self, session, path_info):
        """
        ping
        """
        self._reply_json({'status': 'alive', 'version': SERIES_VERSION})
        return


    def POST_test(self, session, path_info, command):
        """
        Issue a test framework command.  Only works in test mode.
        Return 200 on success
        Return 401 on invalid command or arguments
        """
        if not BLOCKSTACK_TEST:
            return self._send_headers(status_code=404, content_type='text/plain')

        if command == 'envar':
            # set an envar on the qs
            for (key, value) in path_info['qs_values'].items():
                os.environ[key] = value

            return self._send_headers(status_code=200, content_type='text/plain')

        elif command == 'clearcache':
            # clear the cache 
            gaia.cache_evict_all()
            return self._send_headers(status_code=200, content_type='text/plain')

        else:
            return self._send_headers(status_code=401, content_type='text/plain')


    def GET_help(self, session, path_info):
        """
        Top-level GET request
        """
        template = """<html><header></header><body>Blockstack RESTful API<br><a href="https://github.com/blockstack/blockstack-core/tree/master/api">Documentation</a></body></html>"""
        self._send_headers(status_code=200, content_type='text/html')
        self.wfile.write(template)
        return


    def _dispatch(self, method_name):
        """
        Top-level dispatch method
        """

        URLENCODING_CLASS = r'{}+'.format(OP_URLENCODED_NOSLASH_COLON_CLASS)
        NAME_CLASS = OP_NAME_CLASS
        NAMESPACE_CLASS = OP_NAMESPACE_CLASS
        BASE58CHECK_CLASS = r'{}+'.format(OP_BASE58CHECK_CLASS)

        routes = {
            r'^/v1/ping$': {
                'routes': {
                    'GET': self.GET_ping,
                },
                'whitelist': {
                    'GET': {
                        'name': 'ping',
                        'desc': 'Check to see if the server is alive.',
                        'auth_session': False,
                        'auth_pass': False,
                        'need_data_key': False,
                    },
                },
                'need_data_key': False,
            },
            r'^/v1/auth$': {
                'routes': {
                    'GET': self.GET_auth,
                },
                'whitelist': {
                    'GET': {
                        'name': 'auth',
                        'desc': 'get a session token',
                        'auth_session': False,
                        'auth_pass': True,
                        'need_data_key': True,
                    },
                },
            },
            r'^/v1/addresses/({})/({})$'.format(URLENCODING_CLASS, BASE58CHECK_CLASS): {
                'routes': {
                    'GET': self.GET_names_owned_by_address,
                },
                'whitelist': {
                    'GET': {
                        'name': 'names',
                        'desc': 'get names owned by an address on a particular blockchain',
                        'auth_session': False,
                        'auth_pass': False,
                        'need_data_key': False,
                    },
                },
            },
            r'^/v1/blockchains/({})/name_count'.format(URLENCODING_CLASS) : {
                'routes': {
                    'GET': self.GET_blockchain_num_names
                },
                'whitelist': {
                    'GET': {
                        'name': 'blockchain',
                        'desc': 'read blockchain name blocks',
                        'auth_session': False,
                        'auth_pass': False,
                        'need_data_key': False,
                    },
                },
            },
            r'^/v1/blockchains/({})/operations/([0-9]+)$'.format(URLENCODING_CLASS): {
                'routes': {
                    'GET': self.GET_blockchain_ops
                },
                'whitelist': {
                    'GET': {
                        'name': 'blockchain',
                        'desc': 'read blockchain name blocks',
                        'auth_session': False,
                        'auth_pass': False,
                        'need_data_key': False,
                    },
                },
            },
            r'^/v1/blockchains/({})/names/({})/history$'.format(URLENCODING_CLASS, NAME_CLASS): {
                'routes': {
                    'GET': self.GET_blockchain_name_history
                },
                'whitelist': {
                    'GET': {
                        'name': 'blockchain',
                        'desc': 'read blockchain name histories',
                        'auth_session': False,
                        'auth_pass': False,
                        'need_data_key': False,
                    },
                },
            },
            r'^/v1/blockchains/({})/consensus$'.format(URLENCODING_CLASS): {
                'routes': {
                    'GET': self.GET_blockchain_consensus,
                },
                'whitelist': {
                    'GET': {
                        'name': 'blockchain',
                        'desc': 'get current consensus hash',
                        'auth_session': False,
                        'auth_pass': False,
                        'need_data_key': False,
                    },
                },
            },
            r'^/v1/blockchains/({})/pending$'.format(URLENCODING_CLASS): {
                'routes': {
                    'GET': self.GET_blockchain_pending,
                },
                'whitelist': {
                    'GET': {
                        'name': 'blockchain',
                        'desc': 'get pending transactions this node has sent',
                        'auth_session': False,
                        'auth_pass': False,
                        'need_data_key': False,
                    },
                },
            },
            r'^/v1/blockchains/({})/({})/unspent$'.format(URLENCODING_CLASS, BASE58CHECK_CLASS): {
                'routes': {
                    'GET': self.GET_blockchain_unspents,
                },
                'whitelist': {
                    'GET': {
                        'name': 'blockchain',
                        'desc': 'get most-recent state of any address our account',
                        'auth_session': True,
                        'auth_pass': True,
                        'need_data_key': False,
                    },
                },
            },
            r'^/v1/blockchains/({})/txs$'.format(URLENCODING_CLASS): {
                'routes': {
                    'POST': self.POST_broadcast_tx,
                },
                'whitelist': {
                    'POST': {
                        'name': 'blockchain',
                        'desc': 'send a transaction through this node',
                        'auth_session': True,
                        'auth_pass': True,
                        'need_data_key': False,
                    },
                },
            },
            r'^/v1/names$': {
                'routes': {
                    'GET': self.GET_names,
                    'POST': self.POST_names,    # accepts: name, address, zonefile.  Returns: HTTP 202 with txid
                },
                'whitelist': {
                    'GET': {
                        'name': 'names',
                        'desc': 'read all names',
                        'auth_session': False,
                        'auth_pass': False,
                        'need_data_key': False,
                    },
                    'POST': {
                        'name': 'register',
                        'desc': 'register new names',
                        'auth_session': True,
                        'auth_pass': True,
                        'need_data_key': False,
                    },
                },
            },
            r'^/v1/names/({})$'.format(NAME_CLASS): {
                'routes': {
                    'GET': self.GET_name_info,
                    'DELETE': self.DELETE_name,     # revoke
                },
                'whitelist': {
                    'GET': {
                        'name': 'names',
                        'desc': 'read name information',
                        'auth_session': False,
                        'auth_pass': False,
                        'need_data_key': False,
                    },
                    'DELETE': {
                        'name': 'revoke',
                        'desc': 'revoke names',
                        'auth_session': True,
                        'auth_pass': True,
                        'need_data_key': False,
                    },
                },
            },
            r'^/v1/names/({})/history$'.format(NAME_CLASS): {
                'routes': {
                    'GET': self.GET_name_history,
                },
                'whitelist': {
                    'GET': {
                        'name': 'names',
                        'desc': 'read name history',
                        'auth_session': False,
                        'auth_pass': False,
                        'need_data_key': False,
                    },
                },
            },
            r'^/v1/names/({})/owner$'.format(NAME_CLASS): {
                'routes': {
                    'PUT': self.PUT_name_transfer,     # accepts: recipient address.  Returns: HTTP 202 with txid
                },
                'whitelist': {
                    'PUT': {
                        'name': 'transfer',
                        'desc': 'transfer names to new addresses',
                        'auth_session': True,
                        'auth_pass': True,
                        'need_data_key': False,
                    },
                },
            },
            r'^/v1/names/({})/public_key$'.format(NAME_CLASS): {
                'routes': {
                    'GET': self.GET_name_public_key,
                },
                'whitelist': {
                    'GET': {
                        'name': 'zonefiles',
                        'desc': 'read name public key from zone file',
                        'auth_session': False,
                        'auth_pass': False,
                        'need_data_key': False,
                    },
                },
            },
            r'^/v1/names/({})/zonefile$'.format(NAME_CLASS): {
                'routes': {
                    'GET': self.GET_name_zonefile,
                    'PUT': self.PUT_name_zonefile,
                },
                'whitelist': {
                    'GET': {
                        'name': 'zonefiles',
                        'desc': 'read name zonefiles',
                        'auth_session': False,
                        'auth_pass': False,
                        'need_data_key': False,
                    },
                    'PUT': {
                        'name': 'update',
                        'desc': 'set name zonefiles',
                        'auth_session': True,
                        'auth_pass': True,
                        'need_data_key': False,
                    },
                },
            },
            r'^/v1/names/({})/zonefile/([0-9a-fA-F]{{40}})$'.format(NAME_CLASS): {
                'routes': {
                    'GET': self.GET_name_zonefile_by_hash,     # returns a zonefile
                },
                'whitelist': {
                    'GET': {
                        'name': 'zonefiles',
                        'desc': 'get current and historic name zonefiles',
                        'auth_session': False,
                        'auth_pass': False,
                        'need_data_key': False,
                    },
                },
            },
            r'^/v1/names/({})/zonefile/zonefileHash$'.format(NAME_CLASS): {
                'routes': {
                    'PUT': self.PUT_name_zonefile_hash,     # accepts: zonefile hash.  Returns: HTTP 202 with txid
                },
                'whitelist': {
                    'PUT': {
                        'name': 'update',
                        'desc': 'set name zonefile hashes',
                        'auth_session': True,
                        'auth_pass': True,
                        'need_data_key': False
                    },
                },
            },
            r'^/v1/users/({})'.format(NAME_CLASS): {
                'routes': {
                    'GET': self.GET_name_profile,
                },
                'whitelist': {
                    'GET': {
                        'name': 'profile',
                        'desc': 'read name profile',
                        'auth_session': False,
                        'auth_pass': False,
                        'need_data_key': False,
                    },
                },
            },
            r'^/v1/names/({})/profile$'.format(NAME_CLASS): {
                'routes': {
                    'GET': self.GET_name_profile,
                    'POST': self.POST_name_profile,
                    'PUT': self.PUT_name_profile,
                },
                'whitelist': {
                    'GET': {
                        'name': 'profile',
                        'desc': 'read name profile',
                        'auth_session': False,
                        'auth_pass': False,
                        'need_data_key': False,
                    },
                    'POST': {
                        'name': 'profile',
                        'desc': 'update name profile',
                        'auth_session': False,
                        'auth_pass': False,
                        'need_data_key': False,
                    },
                    'PUT': {
                        'name': 'profile',
                        'desc': 'set name profile',
                        'auth_session': True,
                        'auth_pass': True,
                        'need_data_key': False,
                    },
                },
            },
            r'^/v1/namespaces$': {
                'routes': {
                    'GET': self.GET_namespaces,
                },
                'whitelist': {
                    'GET': {
                        'name': 'namespaces',
                        'desc': 'read all namespace IDs',
                        'auth_session': False,
                        'auth_pass': False,
                        'need_data_key': False
                    },
                },
            },
            r'^/v1/namespaces/({})$'.format(NAMESPACE_CLASS): {
                'routes': {
                    'GET': self.GET_namespace_info,
                },
                'whitelist': {
                    'GET': {
                        'name': 'namespaces',
                        'desc': 'read namespace information',
                        'auth_session': False,
                        'auth_pass': False,
                        'need_data_key': False
                    },
                },
            },
            r'^/v1/namespaces/({})/names$'.format(NAMESPACE_CLASS): {
                'routes': {
                    'GET': self.GET_namespace_names,
                },
                'whitelist': {
                    'GET': {
                        'name': 'namespaces',
                        'desc': 'read all names in a namespace',
                        'auth_session': False,
                        'auth_pass': False,
                        'need_data_key': False
                    },
                },
            },
            r'^/v1/wallet/payment_address$': {
                'routes': {
                    'GET': self.GET_wallet_payment_address,
                },
                'whitelist': {
                    'GET': {
                        'name': 'wallet_read',
                        'desc': 'get the node wallet\'s payment address',
                        'auth_session': True,
                        'auth_pass': True,
                        'need_data_key': False
                    },
                },
            },
            r'^/v1/wallet/owner_address$': {
                'routes': {
                    'GET': self.GET_wallet_owner_address,
                },
                'whitelist': {
                    'GET': {
                        'name': 'wallet_read',
                        'desc': 'get the node wallet\'s payment address',
                        'auth_session': True,
                        'auth_pass': True,
                        'need_data_key': False
                    },
                },
            },
            r'^/v1/wallet/data_pubkey$': {
                'routes': {
                    'GET': self.GET_wallet_data_pubkey,
                },
                'whitelist': {
                    'GET': {
                        'name': 'wallet_read',
                        'desc': 'get the node wallet\'s data public key',
                        'auth_session': True,
                        'auth_pass': True,
                        'need_data_key': True,
                    },
                },
            },
            r'^/insight-api/addr/({})/balance$'.format(BASE58CHECK_CLASS): {
                'routes': {
                    'GET': self.GET_confirmed_balance_insight,
                },
                'whitelist': {
                    'GET': {
                        'name': 'wallet_read',
                        'desc': 'get the node wallet\'s balance',
                        'auth_session': True,
                        'auth_pass': True,
                        'need_data_key': False,
                    },
                }
            },
            r'^/insight-api/addr/({})/unconfirmedBalance$'.format(BASE58CHECK_CLASS): {
                'routes': {
                    'GET': self.GET_unconfirmed_balance_insight,
                },
                'whitelist': {
                    'GET': {
                        'name': 'wallet_read',
                        'desc': 'get the node wallet\'s balance',
                        'auth_session': True,
                        'auth_pass': True,
                        'need_data_key': False,
                    },
                }
            },
            r'^/v1/wallet/balance$': {
                'routes': {
                    'GET': self.GET_wallet_balance,
                    'POST': self.POST_wallet_balance,
                },
                'whitelist': {
                    'GET': {
                        'name': 'wallet_read',
                        'desc': 'get the node wallet\'s balance',
                        'auth_session': True,
                        'auth_pass': True,
                        'need_data_key': False,
                    },
                    'POST': {
                        'name': 'wallet_write',
                        'desc': 'transfer the node wallet\'s funds',
                        'auth_session': True,
                        'auth_pass': True,
                        'need_data_key': False,
                    },
                },
            },
            r'^/v1/wallet/balance/([0-9]{1,3})$': {
                'routes': {
                    'GET': self.GET_wallet_balance,
                },
                'whitelist': {
                    'GET': {
                        'name': 'wallet_read',
                        'desc': 'get the node wallet\'s balance',
                        'auth_session': True,
                        'auth_pass': True,
                        'need_data_key': False,
                    },
                },
            },
            r'^/v1/wallet/password$': {
                'routes': {
                    'PUT': self.PUT_wallet_password,
                },
                'whitelist': {
                    'PUT': {
                        'name': 'wallet_write',
                        'desc': 'Change the wallet password',
                        'auth_session': False,
                        'auth_pass': True,
                        'need_data_key': False
                    },
                },
            },
            r'^/v1/wallet/keys/({})$'.format(URLENCODING_CLASS): {
                'routes': {
                    'PUT': self.PUT_wallet_key,
                },
                'whitelist': {
                    'PUT': {
                        'name': 'wallet_write',
                        'desc': 'Set the wallet\'s private key',
                        'auth_session': False,
                        'auth_pass': True,
                        'need_data_key': False
                    },
                },
            },
            r'^/v1/wallet/keys$': {
                'routes': {
                    'GET': self.GET_wallet_keys,
                    'PUT': self.PUT_wallet_keys,
                },
                'whitelist': {
                    'GET': {
                        'name': 'wallet_read',
                        'desc': 'Get the wallet\'s private keys',
                        'auth_session': False,
                        'auth_pass': True,
                        'need_data_key': False,
                    },
                    'PUT': {
                        'name': 'wallet_write',
                        'desc': 'Set the wallet\'s private keys',
                        'auth_session': False,
                        'auth_pass': True,
                        'need_data_key': False
                    },
                },
            },
            r'^/v1/node/ping$': {
                'routes': {
                    'GET': self.GET_ping,
                },
                'whitelist': {
                    'GET': {
                        'name': '',
                        'desc': 'ping the node',
                        'auth_session': False,
                        'auth_pass': False,
                        'need_data_key': False
                    },
                },
            },
            r'^/v1/node/registrar/state$': {
                'routes': {
                    'GET': self.GET_registrar_state,
                },
                'whitelist': {
                    'GET': {
                        'name': '',
                        'desc': 'Get internal registrar state',
                        'auth_session': False,
                        'auth_pass': True,
                        'need_data_key': False,
                    },
                },
            },
            r'^/v1/node/reboot$': {
                'routes': {
                    'POST': self.POST_reboot,
                },
                'whitelist': {
                    'POST': {
                        'name': '',
                        'desc': 'reboot the node',
                        'auth_session': False,
                        'auth_pass': True,
                        'need_data_key': False
                    },
                },
            },
            r'^/v1/node/config$': {
                'routes': {
                    'GET': self.GET_node_config,
                },
                'whitelist': {
                    'GET': {
                        'name': '',
                        'desc': 'get the node config',
                        'auth_session': False,
                        'auth_pass': True,
                        'need_data_key': False,
                    },
                },
            },
            r'^/v1/node/config/({})$'.format(URLENCODING_CLASS): {
                'routes': {
                    'POST': self.POST_node_config,
                    'DELETE': self.DELETE_node_config_section,
                },
                'whitelist': {
                    'POST': {
                        'name': '',
                        'desc': 'set node section fields',
                        'auth_session': False,
                        'auth_pass': True,
                        'need_data_key': False,
                    },
                    'DELETE': {
                        'name': '',
                        'desc': 'delete a config section',
                        'auth_session': False,
                        'auth_pass': True,
                        'need_data_key': False,
                    },
                },
            },
            r'^/v1/node/config/({})/({})$'.format(URLENCODING_CLASS, URLENCODING_CLASS): {
                'routes': {
                    'DELETE': self.DELETE_node_config_field,
                },
                'whitelist': {
                    'DELETE': {
                        'name': '',
                        'desc': 'delete a config section field',
                        'auth_session': False,
                        'auth_pass': True,
                        'need_data_key': False,
                    },
                },
            },
            r'^/v1/node/log$': {
                'routes': {
                    'GET': self.GET_node_logfile,
                    'POST': self.POST_node_logmsg,
                },
                'whitelist': {
                    'GET': {
                        'name': '',
                        'desc': 'Get the node log file',
                        'auth_session': False,
                        'auth_pass': True,
                        'need_data_key': False,
                    },
                    'POST': {
                        'name': '',
                        'desc': 'Append to the log file',
                        'auth_session': False,
                        'auth_pass': True,
                        'need_data_key': False,
                    },
                },
            },
            r'^/v1/node/drivers/storage/({})$'.format(URLENCODING_CLASS): {
                'routes': {
                    'GET': self.GET_node_storage_driver_config,
                    'POST': self.POST_node_storage_driver_config,
                },
                'whitelist': {
                    'POST': {
                        'name': '',
                        'desc': 'Initialize a driver',
                        'auth_session': False,
                        'auth_pass': True,
                        'need_data_key': False,
                    },
                },
            },
            r'^/v1/prices/namespaces/({})$'.format(NAMESPACE_CLASS): {
                'routes': {
                    'GET': self.GET_prices_namespace,
                },
                'whitelist': {
                    'GET': {
                        'name': 'prices',
                        'desc': 'get the price of a namespace',
                        'auth_session': False,
                        'auth_pass': False,
                        'need_data_key': False,
                    },
                },
            },
            r'^/v1/prices/names/({})$'.format(NAME_CLASS): {
                'need_data_key': False,
                'routes': {
                    'GET': self.GET_prices_name,
                },
                'whitelist': {
                    'GET': {
                        'name': 'prices',
                        'desc': 'get the price of a name',
                        'auth_session': False,
                        'auth_pass': False,
                        'need_data_key': False,
                    },
                },
            },
            r'^/v1/collections$': {
                'routes': {
                    'GET': self.GET_collections,
                    'POST': self.POST_collections,
                },
                'whitelist': {
                    'GET': {
                        'name': 'collections',
                        'desc': 'list a user\'s collections',
                        'auth_session': True,
                        'auth_pass': True,
                        'need_data_key': True,
                    },
                    'POST': {
                        'name': 'collections_admin',
                        'desc': 'create new collections',
                        'auth_session': True,
                        'auth_pass': True,
                        'need_data_key': True,
                    },
                },
            },
            r'^/v1/collections/({})$'.format(URLENCODING_CLASS): {
                'routes': {
                    'GET': self.GET_collection_info,
                    'POST': self.POST_collection_item,
                },
                'whitelist': {
                    'GET': {
                        'name': 'collections',
                        'desc': 'list items in a collection',
                        'auth_session': True,
                        'auth_pass': True,
                        'need_data_key': True,
                    },
                    'POST': {
                        'name': 'collections_write',
                        'desc': 'add items to a collection',
                        'auth_session': True,
                        'auth_pass': True,
                        'need_data_key': True,
                    },
                },
            },
            r'^/v1/collections/({})/({})$'.format(URLENCODING_CLASS, URLENCODING_CLASS): {
                'routes': {
                    'GET': self.GET_collection_item,
                },
                'whitelist': {
                    'GET': {
                        'name': 'collections',
                        'desc': 'read collection items',
                        'auth_session': True,
                        'auth_pass': True,
                        'need_data_key': True,
                    },
                },
            },
            r'^/v1/stores$': {
                'routes': {
                    'POST': self.POST_store,
                    'PUT': self.PUT_store,
                    'DELETE': self.DELETE_store,
                },
                'whitelist': {
                    'POST': {
                        'name': 'store_write',
                        'desc': 'create the datastore for the app user',
                        'auth_session': True,
                        'auth_pass': True,
                        'need_data_key': False,
                    },
                    'PUT': {
                        'name': 'store_write',
                        'desc': 'update the app user\'s datastore',
                        'auth_session': True,
                        'auth_pass': True,
                        'need_data_key': False,
                    },
                    'DELETE': {
                        'name': 'store_write',
                        'desc': 'delete the app user\'s datastore',
                        'auth_session': True,
                        'auth_pass': True,     # need app_domain from session
                        'need_data_key': False,
                    },
                },
            },
            r'^/v1/stores/({})$'.format(URLENCODING_CLASS): {
                'routes': {
                    'GET': self.GET_store,
                },
                'whitelist': {
                    'GET': {
                        'name': '',
                        'desc': 'Get an app user\'s datastore metadata',
                        'auth_session': False,
                        'auth_pass': False,
                        'need_data_key': False,
                    },
                },
            },
            r'^/v1/stores/({})/(files|device_roots|listing|headers)$'.format(URLENCODING_CLASS): {
                'routes': {
                    'GET': self.GET_store_item,
                    'POST': self.POST_store_item,
                    'PUT': self.PUT_store_item,
                    'DELETE': self.DELETE_store_item,
                },
                'whitelist': {
                    'GET': {
                        'name': '',
                        'desc': 'read files in the app user\'s data store',
                        'auth_session': False,
                        'auth_pass': False,
                        'need_data_key': False,
                    },
                    'POST': {
                        'name': 'store_write',
                        'desc': 'create files in the app user\'s data store',
                        'auth_session': True,
                        'auth_pass': True,
                        'need_data_key': True,
                    },
                    'PUT': {
                        'name': 'store_write',
                        'desc': 'write files to the app user\'s data store',
                        'auth_session': True,
                        'auth_pass': True,
                        'need_data_key': True,
                    },
                    'DELETE': {
                        'name': 'store_write',
                        'desc': 'delete files in the app user\'s data store',
                        'auth_session': True,
                        'auth_pass': True,
                        'need_data_key': True,
                    },
                },
            },
            r'^/v1/resources/({})/({})$'.format(NAME_CLASS, URLENCODING_CLASS): {
                'routes': {
                    'GET': self.GET_app_resource,
                },
                'whitelist': {
                    'GET': {
                        'name': 'app',
                        'desc': 'get an application resource',
                        'auth_session': False,
                        'auth_pass': False,
                        'need_data_key': False,
                    },
                },
            },
            # test interface (only active if BLOCKSTACK_TEST is set)
            r'^/v1/test/({})$'.format(URLENCODING_CLASS): {
                'routes': {
                    'POST': self.POST_test,
                },
                'whitelist': {
                    'POST': {
                        'name': 'test',
                        'desc': 'issue test framework commands',
                        'auth_session': False,
                        'auth_pass': False,
                        'need_data_key': False,
                    },
                },
            },
            r'^/$': {
                'routes': {
                    'GET': self.GET_help,
                },
                'whitelist': {
                    'GET': {
                        'name': '',
                        'desc': 'help',
                        'auth_session': False,
                        'auth_pass': False,
                        'need_data_key': False,
                    },
                },
            },
            r'^/v1/.*$': {
                'routes': {
                    'OPTIONS': self.OPTIONS_preflight,
                },
                'whitelist': {
                    'OPTIONS': {
                        'name': '',
                        'desc': 'preflight check',
                        'auth_session': False,
                        'auth_pass': False,
                        'need_data_key': False,
                    },
                },
            },
        }
        
        LOCALHOST = []
        for port in [DEFAULT_UI_PORT, DEVELOPMENT_UI_PORT]:
            LOCALHOST += [
                'http://localhost:{}'.format(port),
                'https://{}:{}'.format(socket.gethostname(), port),
                'http://127.0.0.1:{}'.format(port),
                'http://::1:{}'.format(port)
            ]

        path_info = self.get_path_and_qs()
        if 'error' in path_info:
            log.debug('Failed to parse path and qs: {}'.format(path_info['error']))
            self._send_headers(status_code=401, content_type='text/plain')
            return

        qs_values = path_info['qs_values']

        route_info = self._route_match( method_name, path_info, routes )
        if route_info is None:
            log.debug("Unmatched route: {} '{}'".format(method_name, path_info['path']))
            print(json.dumps( routes.keys(), sort_keys=True, indent=4 ))
            self._send_headers(status_code=404, content_type='text/plain')
            return

        route_args = route_info['args']
        route_method = route_info['method']
        route = route_info['route']
        whitelist_info = route_info['whitelist']

        need_data_key = whitelist_info['need_data_key']
        use_session = whitelist_info['auth_session']
        use_password = whitelist_info['auth_pass']

        log.debug("\nfull path: {}\nmethod: {}\npath: {}\nqs: {}\nheaders:\n{}\n".format(self.path, method_name, path_info['path'], qs_values, '\n'.join( '{}: {}'.format(k, v) for (k, v) in self.headers.items() )))

        have_password = False
        auth_valid = True
        session = self.verify_session(qs_values)

        if not session:
            # password authentication
            # don't even try to authenticate with the password unless the Origin is set appropriately 
            origin_header = self.headers.get('origin', None)
            if origin_header is not None:
                if self.verify_origin(origin_header, LOCALHOST):
                    # can authenticate
                    have_password = self.verify_password()
                else:
                    log.warning("Origin is absent or not local")
        
        else:
            # got a session.
            # check origin.
            app_domain = session['app_domain']
            app_name = app_domain
            session_verified = False
            auth_valid = False
            try:
                origin_header = self.headers.get('origin', None)
                if origin_header is not None:
                    scheme = urlparse.urlparse(origin_header).scheme
                    origin_netloc = urlparse.urlparse(origin_header).netloc
                    if not scheme:
                        log.debug("Invalid Origin '{}'".format(origin_header))
                        return self._reply_json({'error': 'Invalid Origin (no scheme)'}, status_code=401);

                    if not app.is_valid_app_name(origin_netloc):
                        origin_header = '{}://{}'.format(scheme, app.app_domain_to_app_name(origin_header))

                    app_name_scheme = urlparse.urlparse(app_name).scheme
                    if app_name_scheme:
                        app_name = urlparse.urlparse(app_name).netloc

                    if not app.is_valid_app_name(app_name):
                        # suffix not given.
                        # assume it's an ICANN name
                        log.debug("{} is not a valid app domain".format(app_name))

                        if not urlparse.urlparse(app_name).netloc:
                            app_name = 'http://' + app_name

                        app_name = app.app_domain_to_app_name(app_name)

                    # convert origin to .1 or .x, since app domains have it
                    session_verified = self.verify_origin(origin_header, ['http://' + app_name, 'https://' + app_name])

            except AssertionError as e:
                log.exception(e)
                session = None
                err = {'error' : e.message}
                return self._reply_json(err, status_code = 403)

            if not session_verified:
                # invalid session
                log.warning("Invalid session: app domain '{}' does not match Origin '{}'".format(app_name, origin_header))
                session = None
            else:
                auth_valid = True
                # set app_domain to corrected app_name
                session['app_domain'] = app_name

        authorized = False

        if self.server.master_data_privkey is None and need_data_key:
            log.debug("No master data private key set")
            self._send_headers(status_code=503, content_type='text/plain')
            return

        if not auth_valid:
            # invalid auth credentials
            log.debug("Invalid authentication credentials")
            authorized = False

        elif not use_session and not use_password:
            # no auth needed
            log.debug("No authentication needed")
            authorized = True

        elif have_password and use_password:
            # password allowed
            log.debug("Authenticated with password")
            authorized = True

        elif session is not None and use_session:
            # session required, but we have one
            # validate session
            allowed_methods = session['methods']

            # is this method allowed?
            if whitelist_info['name'] not in allowed_methods:
                if os.environ.get("BLOCKSTACK_TEST_NOAUTH_SESSION") != '1':
                    # this method is not allowed
                    log.warn("Unauthorized method call to {}".format(path_info['path']))
                    err = { 'error' :
                            "Unauthorized method. Requires '{}', you have permissions for {}".format(
                                whitelist_info['name'], allowed_methods)}
                    return self._reply_json(err, status_code=403)
                else:
                    log.warning("No-session-authentication environment variable set; skipping...")

            authorized = True
            log.debug("Authenticated with session")

        if not authorized:
            log.warn("Failed to authenticate caller")
            if BLOCKSTACK_TEST:
                log.debug("Session was: {}".format(session))

            return self._send_headers(status_code=403, content_type='text/plain')

        # good to go!
        try:
            return route_method( session, path_info, *route_args )
        except Exception as e:
            if BLOCKSTACK_DEBUG:
                log.exception(e)

            return self._send_headers(status_code=500, content_type='text/plain')


    def do_GET(self):
        """
        Top-level GET dispatch
        """
        return self._dispatch("GET")

    def do_POST(self):
        """
        Top-level POST dispatch
        """
        return self._dispatch("POST")

    def do_PUT(self):
        """
        Top-level PUT dispatch
        """
        return self._dispatch("PUT")

    def do_DELETE(self):
        """
        Top-level DELETE dispatch
        """
        return self._dispatch("DELETE")

    def do_HEAD(self):
        """
        Top-level HEAD dispatch
        """
        return self._dispatch("HEAD")

    def do_OPTIONS(self):
        """
        Top-level OPTIONS dispatch
        """
        return self._dispatch("OPTIONS")

    def do_PATCH(self):
        """
        TOp-level PATCH dispatch
        """
        return self._dispatch("PATCH")


class BlockstackAPIEndpoint(SocketServer.ThreadingMixIn, SocketServer.TCPServer):
    """
    Lightweight API endpoint to Blockstack server:
    exposes all of the client methods via a RESTful interface,
    so other local programs (e.g. those that can't use the library)
    can access the Blockstack client functionality.
    """

    @classmethod
    def is_method(cls, method):
        return bool(callable(method) or getattr(method, '__call__', None))


    def register_function(self, func_internal, name=None):
        """
        Register a CLI-wrapper function to our "internal proxy"
        (i.e. a mock module with all of the wrapped CLI methods
        that follow the Python calling convention)
        """
        name = func_internal.__name__ if name is None else name
        assert name

        setattr(self.internal_proxy, name, func_internal)


    def get_internal_proxy(self):
        """
        Get the "internal proxy", which contains wrappers for
        each CLI method that allow Python code to call them easily.
        """
        return self.internal_proxy


    def register_api_functions(self, config_path):
        """
        Register all CLI functions to an "internal proxy" object
        that allows the API server implementation to call them
        via Python calling convention.
        """

        import blockstack_client

        # load methods
        all_methods = blockstack_client.get_cli_methods()
        all_method_infos = parse_methods(all_methods)

        # register the command-line methods (will all start with cli_)
        # methods will be named after their *action*
        for method_info in all_method_infos:
            method_name = 'cli_{}'.format(method_info['command'])
            method = method_info['method']

            msg = 'Register CLI method "{}" as "{}"'
            log.debug(msg.format(method.__name__, method_name))

            self.register_function(
                api_cli_wrapper(method_info, config_path, check_rpc=False, include_kw=True),
                name=method_name,
            )

        return True


    def cache_app_config(self, name, appname, app_config):
        """
        Cache application config for a loaded application
        """
        self.app_configs["{}:{}".format(name, appname)] = app_config


    def get_cached_app_config(self, name, appname):
        """
        Get a cached app config
        """
        return self.app_configs.get("{}:{}".format(name, appname), None)


    def __init__(self, api_pass, wallet_keys, host='localhost', port=blockstack_constants.DEFAULT_API_PORT,
                 handler=BlockstackAPIEndpointHandler, config_path=CONFIG_PATH, server=True):

        """
        wallet_keys is only needed if server=True
        """

        if server:
            assert wallet_keys is not None
            SocketServer.TCPServer.__init__(self, (host, port), handler, bind_and_activate=False)

            log.debug("Set SO_REUSADDR")
            self.socket.setsockopt( socket.SOL_SOCKET, socket.SO_REUSEADDR, 1 )
            
            # we want daemon threads, so we join on abrupt shutdown (applies if multithreaded) 
            self.daemon_threads = True

            self.server_bind()
            self.server_activate()


        # proxy method to all wrapped CLI methods
        class InternalProxy(object):
            pass

        # instantiate
        self.internal_proxy = InternalProxy()
        self.plugin_mods = []
        self.plugin_destructors = []
        self.plugin_prefixes = []
        self.config_path = config_path
        self.funcs = {}
        self.wallet_keys = wallet_keys
        self.master_data_privkey = None
        self.master_data_pubkey = None
        self.port = port
        self.api_pass = api_pass
        self.app_configs = {}   # cached app config state

        conf = blockstack_config.get_config(path=config_path)
        assert conf

        if wallet_keys is not None:
            assert wallet_keys.has_key('data_privkey')

            self.master_data_privkey = ECPrivateKey(wallet_keys['data_privkey']).to_hex()
            self.master_data_pubkey = ECPrivateKey(self.master_data_privkey).public_key().to_hex()

            if keylib.key_formatting.get_pubkey_format(self.master_data_pubkey) == 'hex_compressed':
                self.master_data_pubkey = keylib.key_formatting.decompress(self.master_data_pubkey)

        self.register_api_functions(config_path)


class BlockstackAPIEndpointClient(object):
    """
    Client for blockstack's local API endpoint.
    Usable both by external clients and by the API server itself.
    """
    def __init__(self, server, port, scheme='http', api_pass=None, session=None, config_path=CONFIG_PATH,
                 timeout=DEFAULT_TIMEOUT, debug_timeline=False, **kw):

        self.timeout = timeout
        self.server = server
        self.port = port
        self.debug_timeline = debug_timeline
        self.api_pass = api_pass
        self.session = session
        self.config_path = config_path
        self.config_dir = os.path.dirname(config_path)
        self.remote_version = None


    def log_debug_timeline(self, event, key, r=-1):
        # random ID to match in logs
        r = random.randint(0, 2 ** 16) if r == -1 else r
        if self.debug_timeline:
            log.debug('RPC({}) {} {}'.format(r, event, key))
        return r


    def make_request_headers(self, api_pass=None, need_session=False):
        """
        Make HTTP request headers
        (for local client)
        """

        headers = {
            'content-type': 'application/json'
        }

        assert not need_session or self.session
        if not api_pass:
            api_pass = self.api_pass

        if need_session:
            headers['Authorization'] = 'bearer {}'.format(self.session)

        else:
            if self.api_pass:
                headers['Authorization'] = 'bearer {}'.format(api_pass)

            elif self.session:
                headers['Authorization'] = 'bearer {}'.format(self.session)

        headers['Origin'] = 'http://localhost:{}'.format(DEFAULT_UI_PORT)
        return headers


    def get_response(self, req, schema=None):
        """
        Get the response
        """
        error_schema = {
            'type': 'object',
            'properties': {
                'error': {
                    'type': 'string',
                },
            },
            'required': ['error'],
        }
        
        resp = None
        try:
            resp = req.json()
            if schema:
                try:
                    jsonschema.validate(resp, schema)
                except ValidationError as ve:
                    try:
                        jsonschema.validate(resp, error_schema)
                    except ValidationError as ve2:
                        if BLOCKSTACK_TEST:
                            log.exception(ve)
                            log.exception(ve2)

                        resp = {'error': 'Invalid JSON response; did not match schema'}

        except Exception as e:
            if BLOCKSTACK_DEBUG:
                log.exception(e)

            if BLOCKSTACK_TEST:
                print("==== unparseable text ====")
                print(req.text)
                print("==========================")

            resp = {'error': 'No JSON response', 'http_status': req.status_code}

        if req.status_code == 403:
            resp['error'] = ('Authentication to API service failed. Are you ' +
                             'using the correct API password or do you need to pass the ' +
                             'correct one with --api_password ?')
            
            if 'http_status' in resp:
                del resp['http_status']

        return resp


    def ping(self):
        """
        Ping the endpoint
        """
        assert not is_api_server(self.config_dir), 'API server should not call this method'
        headers = self.make_request_headers()
        req = requests.get( 'http://{}:{}/v1/node/ping'.format(self.server, self.port), timeout=self.timeout, headers=headers)
        return self.get_response(req)


    def check_version(self):
        """
        Verify that the remote server is up-to-date with this client
        """
        if self.remote_version:
            # already did this
            return {'status': True}

        res = self.ping()
        if 'error' in res:
            return {'error': 'Failed to ping server: {}'.format(res['error'])}

        if res.get('version', None) != SERIES_VERSION:
            log.error("Obsolete reply: {}".format(res))
            return {'error': 'Obsolete API server (version {}).  Please restart it with `blockstack api restart`.'.format(res.get('version', '<unknown>'))}

        log.debug("Remote API endpoint is running version {}".format(res['version']))
        self.remote_version = res['version']
        return {'status': True}


    def backend_set_wallet(self, wallet_keys):
        """
        Save wallet keys to memory
        Return {'status': True} on success
        Return {'error': ...} on error
        """
        assert not is_api_server(self.config_dir), 'API server should not call this method'

        res = self.check_version()
        if 'error' in res:
            return res

        headers = self.make_request_headers()
        req = requests.put( 'http://{}:{}/v1/wallet/keys'.format(self.server, self.port), timeout=self.timeout, data=json.dumps(wallet_keys), headers=headers )
        return self.get_response(req)


    def backend_get_wallet(self):
        """
        Get the wallet from the API server
        Return wallet data on success
        Return {'error': ...} on error
        """
        assert not is_api_server(self.config_dir), 'API server should not call this method'

        res = self.check_version()
        if 'error' in res:
            return res

        headers = self.make_request_headers()
        req = requests.get( 'http://{}:{}/v1/wallet/keys'.format(self.server, self.port), timeout=self.timeout, headers=headers )
        return self.get_response(req)


    def backend_state(self):
        """
        Get the backend registrar state
        Return the state on success
        Return {'error': ...} on error
        """
        if is_api_server(self.config_dir):
            # directly invoke
            return backend.registrar.state()

        else:
            res = self.check_version()
            if 'error' in res:
                return res

            # ask API server
            headers = self.make_request_headers()
            req = requests.get( 'http://{}:{}/v1/node/registrar/state'.format(self.server, self.port), timeout=self.timeout, headers=headers)
            return self.get_response(req)


    def backend_preorder(self, fqu, cost_satoshis, user_zonefile, key_file,
                         transfer_address, min_payment_confs, unsafe_reg = False,
                         payment_key = None, owner_key = None):
        """
        Queue up a name for registration.
        """

        if is_api_server(self.config_dir):
            # directly invoke the registrar
            return backend.registrar.preorder(
                fqu, cost_satoshis, user_zonefile, key_file, transfer_address,
                min_payment_confs, config_path = self.config_path, unsafe_reg = unsafe_reg,
                owner_key = owner_key, payment_key = payment_key)

        else:
            res = self.check_version()
            if 'error' in res:
                return res

            # ask API server
            data = {
                'name': fqu,
            }

            if user_zonefile is not None:
                data['zonefile'] = user_zonefile

            if transfer_address is not None:
                data['owner_address'] = transfer_address

            if min_payment_confs is not None:
                data['min_confs'] = min_payment_confs

            if cost_satoshis is not None:
                data['cost_satoshis'] = cost_satoshis

            if key_file:
                log.warning("Will make an empty keyfile")
                data['make_key_file'] = True

            if payment_key is not None:
                data['payment_key'] = payment_key

            if owner_key is not None:
                data['owner_key'] = owner_key

            if unsafe_reg:
                data['unsafe'] = True

            headers = self.make_request_headers()
            req = requests.post( 'http://{}:{}/v1/names'.format(self.server, self.port), data=json.dumps(data), timeout=self.timeout, headers=headers)
            return self.get_response(req)


    def backend_update(self, fqu, zonefile_txt, zonefile_hash,
                       owner_key = None, payment_key = None):
        """
        Queue an update
        """
        if is_api_server(self.config_dir):
            # directly invoke the registrar
            return backend.registrar.update(
                fqu, zonefile_txt, zonefile_hash, None,
                config_path=self.config_path, owner_key=owner_key,
                payment_key=payment_key)

        else:
            res = self.check_version()
            if 'error' in res:
                return res

            # ask the API server
            headers = self.make_request_headers()
            data = {}

            if zonefile_txt is not None:
                try:
                    json.dumps(zonefile_txt)
                    data['zonefile'] = zonefile_txt
                except:
                    # non-standard
                    data['zonefile_b64'] = base64.b64encode(zonefile_txt)

            if zonefile_hash is not None:
                data['zonefile_hash'] = zonefile_hash

            if owner_key is not None:
                data['owner_key'] = owner_key
            if payment_key is not None:
                data['payment_key'] = payment_key

            headers = self.make_request_headers()
            req = requests.put( 'http://{}:{}/v1/names/{}/zonefile'.format(self.server, self.port, fqu), data=json.dumps(data), timeout=self.timeout, headers=headers)
            return self.get_response(req)


    def backend_transfer(self, fqu, recipient_addr, owner_key = None, payment_key = None):
        """
        Queue a transfer
        """
        if is_api_server(self.config_dir):
            # directly invoke the transfer
            return backend.registrar.transfer(
                fqu, recipient_addr, config_path=self.config_path,
                owner_key = owner_key, payment_key = payment_key)

        else:
            res = self.check_version()
            if 'error' in res:
                return res

            # ask the API server
            data = {
                'owner': recipient_addr
            }

            if owner_key is not None:
                data['owner_key'] = owner_key
            if payment_key is not None:
                data['payment_key'] = payment_key

            headers = self.make_request_headers()
            req = requests.put('http://{}:{}/v1/names/{}/owner'.format(self.server, self.port, fqu),
                               data=json.dumps(data), timeout=self.timeout, headers=headers)
            return self.get_response(req)


    def backend_renew(self, fqu, renewal_fee, owner_key = None, payment_key = None):
        """
        Queue a renewal
        """
        if is_api_server(self.config_dir):
            # directly invoke the renew
            return backend.registrar.renew(fqu, renewal_fee, config_path=self.config_path,
                                           owner_key = owner_key, payment_key = payment_key)

        else:
            res = self.check_version()
            if 'error' in res:
                return res

            # ask the API server
            data = {
                'name': fqu,
            }

            if owner_key is not None:
                data['owner_key'] = owner_key
            if payment_key is not None:
                data['payment_key'] = payment_key


            headers = self.make_request_headers()
            req = requests.post( 'http://{}:{}/v1/names'.format(self.server, self.port), data=json.dumps(data), timeout=self.timeout, headers=headers)
            return self.get_response(req)


    def backend_revoke(self, fqu):
        """
        Queue a revoke
        """
        if is_api_server(self.config_dir):
            # directly invoke the revoke
            return backend.registrar.revoke(fqu, config_path=self.config_path)

        else:
            res = self.check_version()
            if 'error' in res:
                return res

            # ask the API server
            headers = self.make_request_headers()
            req = requests.delete( 'http://{}:{}/v1/names/{}'.format(self.server, self.port, fqu), timeout=self.timeout, headers=headers)
            return self.get_response(req)


    def backend_signin(self, blockchain_id, app_privkey, app_domain, app_methods, device_ids, public_keys, this_device_id, api_password=None):
        """
        Sign in and set the session token.
        Cannot be used by the server (nonsensical)
        """
        assert not is_api_server(self.config_dir)

        assert len(device_ids) == len(public_keys)

        res = self.check_version()
        if 'error' in res:
            return res

        headers = self.make_request_headers(api_pass=api_password)

        app_public_keys = [{
            'public_key': pubkey,
            'device_id': dev_id
        } for (pubkey, dev_id) in zip(public_keys, device_ids)]

        request = {
            'version': 1,
            'blockchain_id': blockchain_id,
            'app_private_key': app_privkey,
            'app_domain': app_domain,
            'methods': app_methods,
            'app_public_keys': app_public_keys,
            'device_id': this_device_id,
        }

        signer = jsontokens.TokenSigner()
        authreq = signer.sign(request, app_privkey)

        req = requests.get('http://{}:{}/v1/auth?authRequest={}'.format(self.server, self.port, authreq), timeout=self.timeout, headers=headers)
        res = self.get_response(req)
        if 'error' in res:
            return res

        self.session = res['token']
        return res


    def backend_datastore_create(self, datastore_info, datastore_sigs, root_tombstones ):
        """
        Store signed datastore record.
        Return {'status': True} on success
        Return {'error': ..., 'errno': ...} on error
        """
        assert not is_api_server(self.config_dir)

        res = self.check_version()
        if 'error' in res:
            return res

        # ask the API server
        headers = self.make_request_headers(need_session=True)
        request = {
            'datastore_info': datastore_info,
            'datastore_sigs': datastore_sigs,
            'root_tombstones': root_tombstones,
        }

        url = 'http://{}:{}/v1/stores'.format(self.server, self.port)
        log.debug("create datastore: {}".format(url))

        req = requests.post( url, timeout=self.timeout, data=json.dumps(request), headers=headers)
        
        # expect root_urls and datastore_urls
        return self.get_response(req, schema=PUT_DATASTORE_RESPONSE)


    def backend_datastore_get( self, blockchain_id, full_app_name, datastore_id=None, device_ids=None ):
        """
        Get a datastore from the backend
        Return {'status': True, 'datastore': ...} on success
        Return {'error': ..., 'errno': ...} on error
        """
        assert not is_api_server(self.config_dir), 'BUG: called from API server'
        assert (blockchain_id and full_app_name) or (datastore_id and device_ids), 'Need both blockchain ID and full_app_name or need datastore ID'
        
        res = self.check_version()
        if 'error' in res:
            return res

        # ask the API server
        headers = self.make_request_headers(need_session=False)
        store_id = None
        if datastore_id:
            store_id = datastore_id
        else:
            store_id = full_app_name

            # sanitize
            scheme = urlparse.urlparse(store_id).scheme
            if scheme:
                store_id = store_id[len(scheme) + len('://'):]

        url = 'http://{}:{}/v1/stores/{}'.format(self.server, self.port, urllib.quote(store_id))

        if device_ids:
            if '?' not in url:
                url += '?'
            else:
                url += '&'

            url += 'device_ids={}'.format(','.join(device_ids))

        if blockchain_id:
            if '?' not in url:
                url += '?'
            else:
                url += '&'

            url += 'blockchain_id={}'.format(urllib.quote(blockchain_id))

        log.debug("get datastore: {}".format(url))
        req = requests.get( url, timeout=self.timeout, headers=headers)

        resp_schema = {'type': 'object', 'properties': {'datastore': DATASTORE_SCHEMA}, 'required': ['datastore']}
        return self.get_response(req, schema=resp_schema)


    def backend_datastore_delete(self, datastore_id, datastore_tombstones, root_tombstones, data_pubkeys):
        """
        Delete a datastore.
        Return {'status': True} on success
        Return {'error': ..., 'errno': ...} on error
        """
        assert not is_api_server(self.config_dir)

        res = self.check_version()
        if 'error' in res:
            return res

        # ask the API server
        headers = self.make_request_headers(need_session=True)
        request = {
            'datastore_tombstones': datastore_tombstones,
            'root_tombstones': root_tombstones,
        }
       
        url = 'http://{}:{}/v1/stores'.format(self.server, self.port) 
        log.debug("delete datastore: {}".format(url))

        req = requests.delete( url, data=json.dumps(request), timeout=self.timeout, headers=headers)

        resp_schema = {'type': 'object', 'properties': {'status': {'type': 'boolean'}}, 'requierd': ['status']}
        return self.get_response(req, schema=resp_schema)

    
    @classmethod
    def _get_store_id(cls, blockchain_id, datastore_id, full_app_name):
        """
        Get the datastore ID to use.
        Prefer app name to datastore ID
        """

        store_id = None
        if blockchain_id and full_app_name:
            store_id = full_app_name

            # sanitize
            scheme = urlparse.urlparse(store_id).scheme
            if scheme:
                store_id = store_id[len(scheme) + len('://'):]
        
        else:
            store_id = datastore_id
        
        return store_id


    def backend_datastore_get_device_root(self, device_id, blockchain_id=None, datastore_id=None, full_app_name=None, data_pubkeys=None, force=False):
        """
        Get the device-specific root directory listing.
        Return {'status': True, 'device_root_page': {...}} on success
        Return {'error': ..., 'errno': ...} on failure
        """
        assert not is_api_server(self.config_dir), 'BUG: called as API server'
        assert (datastore_id and data_pubkeys) or (blockchain_id and full_app_name), 'need either both datastore_id and data_pubkeys or full_app_name and blockchain_id'

        # client
        res = self.check_version()
        if 'error' in res:
            return res

        # ask the API server 
        headers = self.make_request_headers(need_session=False)
        
        store_id = self._get_store_id(blockchain_id, datastore_id, full_app_name)

        url = 'http://{}:{}/v1/stores/{}/device_roots?force={}&this_device_id={}'.format(self.server, self.port, store_id, '1' if force else '0', device_id)

        if data_pubkeys:
            device_ids = [dpk['device_id'] for dpk in data_pubkeys]
            device_pubkeys = [dpk['public_key'] for dpk in data_pubkeys]
            device_ids_str = urllib.quote( ','.join(device_ids))
            device_pubkeys_str = urllib.quote( ','.join(device_pubkeys))

            url += '&device_ids={}&device_pubkeys={}'.format(device_ids_str, device_pubkeys_str)
        
        if blockchain_id:
            url += '&blockchain_id={}'.format(urllib.quote(blockchain_id))

        log.debug("get_device_root: {}".format(url))

        req = requests.get(url, timeout=self.timeout, headers=headers)
        res = self.get_response(req, schema=GET_DEVICE_ROOT_RESPONSE)
        return res

    
    def backend_datastore_get_root(self, blockchain_id=None, datastore_id=None, full_app_name=None, data_pubkeys=None, timestamp=0, force=False):
        """
        Get the datastore-wide root directory listing.
        Return {'status'; True, 'root': ...} on success
        Return {'error': ..., 'errno': ...} on failure
        """

        assert not is_api_server(self.config_dir), 'BUG: called as API server'
        assert (blockchain_id and full_app_name) or (datastore_id and data_pubkeys), 'Need either blockchain_id/full_app_name or datastore_id/data_pubkeys'

        # client
        assert (datastore_id and data_pubkeys) or (blockchain_id and full_app_name), 'need either both datastore_id and data_pubkeys or full_app_name and blockchain_id'

        res = self.check_version()
        if 'error' in res:
            return res

        # ask the API server 
        headers = self.make_request_headers(need_session=False)

        store_id = self._get_store_id(blockchain_id, datastore_id, full_app_name)

        url = 'http://{}:{}/v1/stores/{}/listing?force={}'.format(self.server, self.port, store_id, force)
        
        if blockchain_id:
            url += '&blockchain_id={}'.format(urllib.quote(blockchain_id))
        
        if data_pubkeys is not None:
            device_ids = [dpk['device_id'] for dpk in data_pubkeys]
            device_pubkeys = [dpk['public_key'] for dpk in data_pubkeys]
            device_ids_str = urllib.quote( ','.join(device_ids))
            device_pubkeys_str = urllib.quote( ','.join(device_pubkeys))

            url += '&device_ids={}&device_pubkeys={}'.format(device_ids_str, device_pubkeys_str)

        log.debug("get root: {}".format(url))
        req = requests.get(url, timeout=self.timeout, headers=headers)
        res = self.get_response(req, schema=GET_ROOT_RESPONSE)
        return res


    def backend_datastore_lookup(self, path, device_id, blockchain_id=None, datastore_id=None, full_app_name=None, data_pubkeys=None, force=False):
        """
        Look up a file's metadata, and optionally the device-specific root directory page it came from

        Return {'status': True, 'file_info': {...}} on success

        Return {'error': ..., 'errno': ...} on failure.
        """
        assert not is_api_server(self.config_dir), 'BUG: called as API server'
        assert (blockchain_id and full_app_name) or (datastore_id and data_pubkeys), 'Need either blockchain_id/full_app_name or datastore_id/data_pubkeys'

        # client
        res = self.check_version()
        if 'error' in res:
            return res

        # ask the API server
        headers = self.make_request_headers(need_session=False)
        store_id = self._get_store_id(blockchain_id, datastore_id, full_app_name)

        url = 'http://{}:{}/v1/stores/{}/headers?path={}&force={}&this_device_id={}'.format(
                self.server, self.port, store_id, urllib.quote(path), '1' if force else '0', device_id
        )
        if blockchain_id:
            url += '&blockchain_id={}'.format(urllib.quote(blockchain_id))

        if data_pubkeys is not None and len(data_pubkeys) > 0:
            device_ids = [urllib.quote(dk['device_id']) for dk in data_pubkeys]
            device_pubkeys = [dk['public_key'] for dk in data_pubkeys]

            device_ids_str = urllib.quote( ','.join(device_ids))
            device_pubkeys_str = urllib.quote( ','.join(device_pubkeys))

            url += '&device_ids={}&device_pubkeys={}'.format(device_ids_str, device_pubkeys_str)

        log.debug("lookup: {}".format(url))

        req = requests.get( url, timeout=self.timeout, headers=headers)
        res = self.get_response(req, schema=FILE_LOOKUP_RESPONSE)
        return res


    def backend_datastore_getfile(self, file_name, blockchain_id=None, datastore_id=None, data_pubkeys=None, full_app_name=None, force=False, timestamp=0):
        """
        Get file data
        Usually not needed, since files usually have HTTP(S) URLs
        Return {'status': True, 'data': the data} on success
        Return {'error': ..., 'errno': ...} on failure
        """
        
        assert not is_api_server(self.config_dir), 'BUG: called as API server'
        assert (blockchain_id and full_app_name) or (datastore_id and data_pubkeys), 'Need either blockchain_id/full_app_name or datastore_id/data_pubkeys'

        # client
        # get it from the API server
        # (NOTE: most clients can simply `fetch`, `curl`, etc. the file URLs)
        res = self.check_version()
        if 'error' in res:
            return res

        store_id = self._get_store_id(blockchain_id, datastore_id, full_app_name)

        headers = self.make_request_headers(need_session=False)
        url = 'http://{}:{}/v1/stores/{}/files?path={}&force={}'.format(
                self.server, self.port, store_id, urllib.quote(file_name), '1' if force else '0'
        )

        if blockchain_id:
            url += '&blockchain_id={}'.format(blockchain_id)

        if data_pubkeys is not None and len(data_pubkeys) > 0:
            device_ids = [urllib.quote(dk['device_id']) for dk in data_pubkeys]
            device_pubkeys = [dk['public_key'] for dk in data_pubkeys]

            device_ids_str = urllib.quote( ','.join(device_ids))
            device_pubkeys_str = urllib.quote( ','.join(device_pubkeys))

            url += '&device_ids={}&device_pubkeys={}'.format(device_ids_str, device_pubkeys_str)

        log.debug("getfile: {}".format(url))
        req = requests.get(url, timeout=self.timeout, headers=headers)
        res = None

        if req.status_code != 200:
            res = self.get_response(req)
            log.error("Failed to get {}: {}".format(url, res['error']))
            return res
            
        else:
            data = req.content
            res = {'status': True, 'data': req.content}

        return res


    def backend_datastore_putfile(self, datastore_str, datastore_sig, file_name, file_header_blob, payload, signature, device_id, blockchain_id=None, full_app_name=None):
        """
        Put file data.
        Return {'status': True, 'urls': [...]} on success
        Return {'error': ..., 'errno': ...} on failure
        """
        assert not is_api_server(self.config_dir)

        # store via API endpoint 
        res = self.check_version()
        if 'error' in res:
            return res

        headers = self.make_request_headers(need_session=True)
        datastore_id = gaia.datastore_get_id(json.loads(datastore_str)['pubkey'])
        url = 'http://{}:{}/v1/stores/{}/files?path={}'.format(
                self.server, self.port, datastore_id, urllib.quote(file_name)
        )
        
        if blockchain_id is not None:
            url += '&blockchain_id={}'.format(urllib.quote(blockchain_id))

        # request structure
        request_struct = {
            'headers': [file_header_blob],
            'payloads': [base64.b64encode(payload)],
            'signatures': [signature],
            'tombstones': [],
            'datastore_str': datastore_str,
            'datastore_sig': datastore_sig,
        }
        
        url = 'http://{}:{}/v1/stores/{}/files?path={}'.format(self.server, self.port, datastore_id, urllib.quote(file_name))
        log.debug("putfile: {}".format(url))

        req = requests.post( url, data=json.dumps(request_struct), timeout=self.timeout, headers=headers)
        res = self.get_response(req, schema=PUT_DATA_RESPONSE)
        return res


    def backend_datastore_put_device_root(self, datastore_str, datastore_sig, device_root_page_blob, signature, device_id, data_pubkey=None, blockchain_id=None, full_app_name=None, synchronous=False):
        """
        Put a new device-specific root
        Return {'status': True, 'urls': ...} on success
        Return {'error': ...} on failure
        """
        assert not is_api_server(self.config_dir)

        # client
        # store via API endpoint 
        res = self.check_version()
        if 'error' in res:
            return res

        headers = self.make_request_headers(need_session=True)
        datastore_id = gaia.datastore_get_id(json.loads(datastore_str)['pubkey'])
        url = 'http://{}:{}/v1/stores/{}/device_roots?sync={}'.format(
                self.server, self.port, datastore_id, '1' if synchronous else '0'
        )
        
        if blockchain_id is not None:
            url += '&blockchain_id=' + urllib.quote(blockchain_id)

        # request structure 
        request_struct = {
            'headers': [], 
            'payloads': [device_root_page_blob],
            'signatures': [signature],
            'tombstones': [],
            'datastore_str': datastore_str,
            'datastore_sig': datastore_sig
        }
        req = requests.post( url, data=json.dumps(request_struct), timeout=self.timeout, headers=headers)
        res = self.get_response(req, schema=PUT_DATA_RESPONSE)
        return res


    def backend_datastore_deletefile(self, datastore_str, datastore_sig, signed_tombstones, data_pubkeys=None, blockchain_id=None, full_app_name=None):
        """
        Delete file data
        Return {'status': True} on success
        Return {"error': ...} on error
        """
        assert not is_api_server(self.config_dir), "BUG: tried to delete file with API server"

        res = self.check_version()
        if 'error' in res:
            return res

        datastore_id = gaia.datastore_get_id(json.loads(datastore_str)['pubkey'])
        headers = self.make_request_headers(need_session=True)
        url = 'http://{}:{}/v1/stores/{}/files'.format(self.server, self.port, datastore_id)
        sep = '?'

        if blockchain_id:
            url += sep + 'blockchain_id=' + urllib.quote(blockchain_id)
            sep = '&'

        if data_pubkeys is not None and len(data_pubkeys) > 0:
            device_ids = [urllib.quote(dk['device_id']) for dk in data_pubkeys]
            device_pubkeys = [dk['public_key'] for dk in data_pubkeys]

            device_ids_str = urllib.quote( ','.join(device_ids))
            device_pubkeys_str = urllib.quote( ','.join(device_pubkeys))

            url += sep + 'device_ids={}&device_pubkeys={}'.format(device_ids_str, device_pubkeys_str)
            sep = '&'

        request_struct = {
            'headers': [],
            'payloads': [],
            'signatures': [],
            'tombstones': signed_tombstones,
            'datastore_str': datastore_str,
            'datastore_sig': datastore_sig,
        }

        log.debug("deletefile: {}".format(url))
        req = requests.delete(url, data=json.dumps(request_struct), timeout=self.timeout, headers=headers)

        resp_schema = {'type': 'object', 'properties': {'status': {'type': 'boolean'}}, 'required': ['status']}
        res = self.get_response(req, schema=resp_schema)
        return res

    
    def backend_get_name_profile(self, blockchain_id):
        """
        Get a blockchain ID's profile
        Return {'status': True, 'profile': ...} on success
        Return {'error': ...} on error
        """
        assert not is_api_server(self.config_dir), "BUG: tried to get profile with API server"

        res = self.check_version()
        if 'error' in res:
            return res

        headers = self.make_request_headers(need_session=False)
        url = 'http://{}:{}/v1/names/{}/profile'.format(self.server, self.port, blockchain_id)
        
        log.debug("get_profile: {}".format(url))
        req = requests.get(url, timeout=self.timeout, headers=headers)

        res = self.get_response(req, schema=GET_PROFILE_RESPONSE)
        return res


def make_local_api_server(api_pass, portnum, wallet_keys, api_bind=None, config_path=blockstack_constants.CONFIG_PATH, plugins=None):
    """
    Make a local RPC server instance.
    It will be derived from BaseHTTPServer.HTTPServer.
    @plugins can be a list of modules, or a list of strings that
    identify module names to import.

    Returns the global server instance on success.
    """
    conf = blockstack_config.get_config(config_path)
    assert conf

    # arg --> envar --> config
    if api_bind is None:
        api_bind = os.environ.get("BLOCKSTACK_API_BIND", None)
        if api_bind is None:
            api_bind = conf.get('api_endpoint_bind', 'localhost') if api_bind is None else api_bind

    srv = BlockstackAPIEndpoint(api_pass, wallet_keys, host=api_bind, port=portnum, config_path=config_path)
    return srv


def is_api_server(config_dir=blockstack_constants.CONFIG_DIR):
    """
    Is this process running an RPC server?
    Return True if so
    Return False if not
    """
    rpc_pidpath = local_api_pidfile_path(config_dir=config_dir)
    if not os.path.exists(rpc_pidpath):
        return False

    rpc_pid = local_api_read_pidfile(rpc_pidpath)
    if rpc_pid != os.getpid():
        return False

    return True


def local_api_server_run(srv):
    """
    Start running the RPC server, but in a separate thread.
    """
    global running

    srv.timeout = 0.5
    while running:
        srv.handle_request()


def local_api_server_stop(srv):
    """
    Stop a running RPC server
    """
    log.debug("Server shutdown")
    srv.socket.close()

    # stop the registrar
    backend.registrar.registrar_shutdown(srv.config_path)

    # stop gaia 
    gaia.gaia_stop()


def local_api_connect(api_pass=None, api_session=None, config_path=blockstack_constants.CONFIG_PATH, api_host=None, api_port=None):
    """
    Connect to a locally-running API server.
    Return a server proxy object on success.
    Raise on error.

    The API server can safely connect to itself using this method,
    since instead of opening a socket and doing the conventional RPC,
    it will instead use the proxy object to call the request method
    directly.
    """

    conf = blockstack_config.get_config(config_path)
    if conf is None:
        raise Exception('Failed to read conf at "{}"'.format(config_path))

    api_port = conf.get('api_endpoint_port', DEFAULT_API_PORT)  if api_port is None else api_port
    api_host = conf.get('api_endpoint_host', 'localhost') if api_host is None else api_host

    if api_pass is None:
        # try environment
        api_pass = get_secret('BLOCKSTACK_API_PASSWORD')

    if api_pass is None:
        # try config file
        api_pass = conf.get('api_password', None)

    if api_session is None:
        # try environment
        api_session = get_secret('BLOCKSTACK_API_SESSION')

    connect_msg = 'Connect to API at {}:{}'
    log.debug(connect_msg.format(api_host, api_port))

    return BlockstackAPIEndpointClient(api_host, api_port, timeout=3000, config_path=config_path, session=api_session, api_pass=api_pass)


def local_api_action(command, password=None, api_pass=None, config_dir=blockstack_constants.CONFIG_DIR):
    """
    Handle an API endpoint command:
    * start: start up an API endpoint
    * stop: stop a running API endpoint
    * status: see if there's an API endpoint running.
    * restart: stop and start the API endpoint

    Return {'status': True} on success
    Return {'error': ...} on error
    """

    if command not in ['start', 'start-foreground', 'stop', 'status', 'restart']:
        raise ValueError('Invalid command "{}"'.format(command))

    if command in ['start', 'start-foreground', 'restart']:
        assert api_pass, "Need API password for '{}'".format(command)

    if command == 'status':
        rc = local_api_status(config_dir=config_dir)
        if rc:
            return {'status': True}

        else:
            return {'error': 'Failed to check API status'}

    if command == 'stop':
        rc = local_api_stop(config_dir=config_dir)
        if rc:
            return {'status': True}
        else:
            return {'error': 'Failed to stop API endpoint'}

    config_path = os.path.join(config_dir, CONFIG_FILENAME)

    conf = blockstack_config.get_config(config_path)
    if conf is None:
        raise Exception('Failed to read conf at "{}"'.format(config_path))

    api_port = None
    try:
        api_port = int(conf['api_endpoint_port'])
    except:
        raise Exception("Invalid port number {}".format(conf['api_endpoint_port']))

    if command == 'start-foreground':
        # start API server the foreground
        res = local_api_start(port=api_port, config_dir=config_dir, api_pass=api_pass, password=password, foreground=True)
        return res

    else:
        # use the RPC runner
        argv = [sys.executable, '-m', 'blockstack_client.rpc_runner', str(command), str(api_port), str(config_dir)]

        env = {}
        env.update( os.environ )

        api_stdin_buf = serialize_secrets()

        p = subprocess.Popen(argv, cwd=config_dir, stdout=subprocess.PIPE, stdin=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True, env=env)
        out, err = p.communicate(input=api_stdin_buf)
        res = p.wait()
        if res != 0:
            log.error("Failed to {} API endpoint: exit code {}".format(command, res))
            log.error("Error log:\n{}".format("\n".join(["  " + e for e in err.split('\n')])))
            return {'error': 'Failed to {} API endpoint (exit code {})'.format(command, res)}

    return {'status': True}


def local_api_pidfile_path(config_dir=blockstack_constants.CONFIG_DIR):
    """
    Where do we put the PID file?
    """
    return os.path.join(config_dir, 'api_endpoint.pid')


def local_api_logfile_path(config_dir=blockstack_constants.CONFIG_DIR):
    """
    Where do we put logs?
    """
    return os.path.join(config_dir, 'api_endpoint.log')


def backup_api_logfile(config_dir=blockstack_constants.CONFIG_DIR):
    """
    Back up the logfile
    """
    logpath = local_api_logfile_path(config_dir=config_dir)
    if os.path.exists(logpath):
        # back this up
        backup_path = logpath
        while os.path.exists(backup_path):
            now = int(time.time())
            backup_path = '{}.{}'.format(logpath, now)

        shutil.move(logpath, backup_path)

    return True


def local_api_read_pidfile(pidfile_path):
    """
    Read a PID file
    Return None if unable
    """
    try:
        with open(pidfile_path, 'r') as f:
            data = f.read()
            return int(data)
    except:
        return None


def local_api_write_pidfile(pidfile_path):
    """
    Write a PID file
    """
    with open(pidfile_path, 'w') as f:
        f.write(str(os.getpid()))
        f.flush()
        os.fsync(f.fileno())

    return


def local_api_unlink_pidfile(pidfile_path):
    """
    Remove a PID file
    """
    try:
        os.unlink(pidfile_path)
    except:
        pass

# used when running in a separate process
rpc_pidpath = None
rpc_srv = None

def local_api_atexit():
    """
    atexit: clean out PID file
    """
    log.debug("Atexit handler invoked; shutting down")

    global rpc_pidpath, rpc_srv
    local_api_unlink_pidfile(rpc_pidpath)
    if rpc_srv is not None:
        local_api_server_stop(rpc_srv)
        rpc_srv = None


def local_api_exit_handler(sig, frame):
    """
    Fatal signal handler
    """
    local_api_atexit()
    log.debug('Local API exit from signal {}'.format(sig))
    sys.exit(0)


def local_api_start_wait( api_host='localhost', api_port=DEFAULT_API_PORT, config_path=CONFIG_PATH ):
    """
    Wait for the API server to come up
    Return True if we could ping it
    Return False if not.

    Used by the intermediate process when forking a daemon.
    """

    config_dir = os.path.dirname(config_path)

    # ping it
    running = False
    for i in range(1, 10):
        log.debug("Attempt {} to ping API server".format(i))
        try:
            local_proxy = local_api_connect(api_host=api_host, api_port=api_port, config_path=config_path)
            res = local_proxy.check_version()
            if 'error' in res:
                print(res['error'], file=sys.stderr)
                return False

            running = True
            break
        except requests.ConnectionError as ie:
            log.debug('API server is not responding; trying again in {} seconds'.format(i))
            time.sleep(i)
            continue

        except (IOError, OSError) as ie:
            if ie.errno == errno.ECONNREFUSED:
                log.debug('API server not responding; try again in {} seconds'.format(i))
                time.sleep(i)
                continue
            else:
                log.exception(ie)
                return False

        except Exception as e:
            log.exception(e)
            return False

    if not running:
        log.error("API endpoint did not initialize")
    else:
        log.debug("API endpoint is running")

    return running


def local_api_check_alive(config_path, api_host=None, api_port=None):
    """
    Is the local API daemon alive?
    Return True if so
    Return False if not, and clean up stale pidfiles
    """

    alive = False
    config_dir = os.path.dirname(config_path)

    # already running?
    rpc_pidpath = local_api_pidfile_path(config_dir=config_dir)
    if os.path.exists(rpc_pidpath):

        alive = True
        pid = local_api_read_pidfile(rpc_pidpath)
        if pid is None:
            # failure
            raise Exception("Failed to read PID file {}".format(rpc_pidpath))

        # see if it's alive
        try:
            os.kill(pid, 0)
        except OSError as oe:
            if oe.errno == errno.ESRCH:
                alive = False
                log.debug("Stale PID: {}".format(rpc_pidpath))

        if alive:
            # see if we can ping it
            rpcclient = local_api_connect(config_path=config_path, api_port=api_port, api_host=api_host)
            try:
                rpcclient.ping()
                log.debug("Still alive: {}".format(rpc_pidpath))
            except:
                alive = False
                log.debug("Unresponsive process: {}".format(rpc_pidpath))

        if alive:
            return True

    if not alive:
        # remove stale pid file
        if os.path.exists(rpc_pidpath):
            log.debug("Removing stale PID file {}".format(rpc_pidpath))
            local_api_unlink_pidfile(rpc_pidpath)

    return False


def local_api_start( port=None, host=None, config_dir=blockstack_constants.CONFIG_DIR, foreground=False, api_pass=None, password=None):
    """
    Start up an API endpoint
    Return {'status': True} on success
    Return {'error': ...} on failure
    """

    global rpc_pidpath, rpc_srv, running

    if password:
        set_secret("BLOCKSTACK_CLIENT_WALLET_PASSWORD", password)

    p = subprocess.Popen("pip freeze", shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = p.communicate()
    if p.returncode != 0:
        raise Exception("Failed to run `pip freeze`")

    log.info("API server version {} starting...".format(SERIES_VERSION))
    log.info("Machine:   {}".format(platform.machine()))
    log.info("Version:   {}".format(platform.version()))
    log.info("Platform:  {}".format(platform.platform()))
    log.info("uname:     {}".format(" ".join(platform.uname())))
    log.info("System:    {}".format(platform.system()))
    log.info("Processor: {}".format(platform.processor()))
    log.info("pip:\n{}".format(out))

    import blockstack_client
    from blockstack_client.wallet import load_wallet
    from blockstack_client.client import session

    config_path = os.path.join(config_dir, CONFIG_FILENAME)
    wallet_path = os.path.join(config_dir, WALLET_FILENAME)

    conf = blockstack_client.get_config(config_path)
    assert conf

    if port is None:
        port = int(conf['api_endpoint_port'])

    if host is None:
        host = conf.get('api_endpoint_host', 'localhost')

    rpc_pidpath = local_api_pidfile_path(config_dir=config_dir)

    # already running?
    alive = local_api_check_alive(config_path, api_host=host, api_port=port)
    if alive:
        print("API endpoint is already running")
        return {'error': 'API endpoint is already running'}

    if not os.path.exists(wallet_path):
        print("No wallet found at {}".format(wallet_path), file=sys.stderr)
        return {'error': 'No wallet found at {}'.format(wallet_path)}

    if not BLOCKSTACK_TEST:
        signal.signal(signal.SIGINT, local_api_exit_handler)
        signal.signal(signal.SIGQUIT, local_api_exit_handler)
        signal.signal(signal.SIGTERM, local_api_exit_handler)

    wallet = load_wallet(
        password=password, config_path=config_path,
        include_private=True, wallet_path=wallet_path
    )

    if 'error' in wallet:
        log.error('Failed to load wallet: {}'.format(wallet['error']))
        print('Failed to load wallet: {}'.format(wallet['error']), file=sys.stderr)
        return {'error': 'Failed to load wallet: {}'.format(wallet['error'])}

    if wallet['migrated']:
        log.error("Wallet is in legacy format")
        print("Wallet is in legacy format.  Please migrate it first with the `setup_wallet` command.", file=sys.stderr)
        return {'error': 'Wallet is in legacy format.  Please migrate it first with the `setup_wallet` command.'}

    wallet = wallet['wallet']
    if not foreground:
        log.debug('Running in the background')

        backup_api_logfile(config_dir=config_dir)
        logpath = local_api_logfile_path(config_dir=config_dir)

        res = daemonize(logpath, child_wait=lambda: local_api_start_wait(api_port=port, api_host=host, config_path=config_path))
        if res < 0:
            log.error("API server failed to start")
            return {'error': 'API endpoint failed to start'}

        if res > 0:
            # parent
            log.debug("Parent {} forked intermediate child {}".format(os.getpid(), res))
            return {'status': True}

        # now a daemon.
        # we can and should clear out our signal handlers
        log.debug("Clearing old signal handlers")
        signal.signal(signal.SIGINT, local_api_exit_handler)
        signal.signal(signal.SIGQUIT, local_api_exit_handler)
        signal.signal(signal.SIGTERM, local_api_exit_handler)

    # daemon child takes this path...
    atexit.register(local_api_atexit)

    # load drivers
    log.debug("Loading drivers")
    session(conf=conf)
    log.debug("Loaded drivers")

    # make server
    try:
        rpc_srv = make_local_api_server(api_pass, port, wallet, config_path=config_path)
    except socket.error as se:
        if BLOCKSTACK_DEBUG is not None:
            log.exception(se)

        if not foreground:
            msg = 'Failed to open socket (socket errno {}); aborting...'
            log.error(msg.format(se.errno))
            os.abort()
        else:
            msg = 'Failed to open socket (socket errno {})'
            log.error(msg.format(se.errno))
            return {'error': 'Failed to open socket (errno {})'.format(se.errno)}

    assert wallet.has_key('owner_addresses')
    assert wallet.has_key('owner_privkey')
    assert wallet.has_key('payment_addresses')
    assert wallet.has_key('payment_privkey')
    assert wallet.has_key('data_pubkeys')
    assert wallet.has_key('data_privkey')

    log.debug("Initializing registrar...")
    state = backend.registrar.set_registrar_state(config_path=config_path, wallet_keys=wallet)
    if state is None:
        log.error("Failed to initialize registrar: {}".format(res['error']))
        return {'error': 'Failed to initialize registrar: {}'.format(res['error'])}

    log.debug("Initializing Gaia storage...")
    res = gaia.gaia_start(config_path=config_path)
    if 'error' in res:
        log.error("Failed to initialize Gaia: {}".format(res['error']))
        
        backend.registrar.registrar_shutdown(config_path)
        return {'error': 'Failed to initialize Gaia: {}'.format(res['error'])}

    log.debug("Setup finished")

    running = True
    local_api_write_pidfile(rpc_pidpath)
    local_api_server_run(rpc_srv)

    local_api_unlink_pidfile(rpc_pidpath)
    local_api_server_stop(rpc_srv)

    return {'status': True}


def api_kill(pidpath, pid, sig, unlink_pidfile=True):
    """
    Utility function to send signals
    Return True if signal actions were successful
    Return False if signal actions were unsuccessful
    """
    try:
        os.kill(pid, sig)
        if sig == signal.SIGKILL:
            local_api_unlink_pidfile(pidpath)

        return True
    except OSError as oe:
        if oe.errno == errno.ESRCH:
            log.debug('Not running: {} ({})'.format(pid, pidpath))
            if unlink_pidfile:
                local_api_unlink_pidfile(pidpath)
            return True
        elif oe.errno == errno.EPERM:
            log.debug('Not our RPC daemon: {} ({})'.format(pid, pidpath))
            return False
        else:
            raise


def local_api_stop(config_dir=blockstack_constants.CONFIG_DIR):
    """
    Shut down an API endpoint
    Return True if we stopped it
    Return False if it wasn't running, or we couldn't stop it
    """
    # already running?
    pidpath = local_api_pidfile_path(config_dir=config_dir)
    if not os.path.exists(pidpath):
        print('Not running ({})'.format(pidpath), file=sys.stderr)
        return False

    pid = local_api_read_pidfile(pidpath)
    if pid is None:
        print('Failed to read "{}"'.format(pidpath), file=sys.stderr)
        return False

    if not api_kill(pidpath, pid, 0):
        print('API server is not running', file=sys.stderr)
        return False

    # still running. try to terminate
    print('Sending SIGTERM to {}'.format(pid), file=sys.stderr)

    if not api_kill(pidpath, pid, signal.SIGTERM):
        print("Unable to send SIGTERM to {}".format(pid), file=sys.stderr)
        return False

    time.sleep(1)

    for i in xrange(0, 2):
        # still running?
        if not api_kill(pidpath, pid, 0):
            # dead
            return False

        time.sleep(1)

    # still running
    print('Sending SIGKILL to {}'.format(pid), file=sys.stderr)

    # sigkill ensure process will die
    return api_kill(pidpath, pid, signal.SIGKILL)


def local_api_status(config_dir=blockstack_constants.CONFIG_DIR):
    """
    Print the status of an instantiated API endpoint
    Return True if the daemon is running.
    Return False if not, or if unknown.
    """
    # see if it's running
    pidpath = local_api_pidfile_path(config_dir=config_dir)
    if not os.path.exists(pidpath):
        log.debug('No PID file {}'.format(pidpath))
        return False

    pid = local_api_read_pidfile(pidpath)
    if pid is None:
        log.debug('Invalid PID file {}'.format(pidpath))
        return False

    if not api_kill(pidpath, pid, 0, unlink_pidfile=False):
        return False

    log.debug('RPC running ({})'.format(pidpath))

    return True
