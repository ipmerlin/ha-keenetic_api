"""Verify the real HTTP client against an independent local SCRAM-like server.

The fixture verifies recovered client keys instead of echoing client proofs.
Exchange format reference: hoaxisr/awg-manager (MIT; see LICENSES).
"""

import base64
import hashlib
import hmac
import json
import unittest

import aiohttp
from aiohttp import web
from argon2.low_level import Type, hash_secret_raw
from multidict import CIMultiDict

from test_token_transport import client


def encode_data(data):
    return base64.b64encode(json.dumps(data).encode()).decode()


class NDW4HTTPTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.phases = []
        self.password = 'synthetic-password'
        self.salt = b'0123456789abcdef'
        self.salt_text = base64.b64encode(self.salt).decode()
        self.nonce = 'server-nonce@abc%def'
        self.iterations = 2
        self.memory = 8
        self.bad_signature = False
        self.missing_data_phase = None
        self.malformed_params = False
        self.final_status = 200
        key = hash_secret_raw(self.password.encode(), self.salt, 2, 8, 1, 64, Type.ID)
        self.client_key = hmac.digest(key, b'NDW4 Interactive Client Key', 'sha3_512')
        self.stored_key = hashlib.sha3_512(self.client_key).digest()
        self.server_key = hmac.digest(key, b'NDW4 Interactive Server Key', 'sha3_512')
        app = web.Application()
        app.router.add_route('*', '/auth', self.handle_auth)
        app.router.add_get('/rci/show/version', self.handle_version)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await self.site.start()
        port = self.site._server.sockets[0].getsockname()[1]
        self.session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        self.router = client.Router(self.session, host='http://127.0.0.1', port=port, password=self.password)

    async def asyncTearDown(self):
        await self.session.close()
        await self.runner.cleanup()

    def data(self, payload, phase):
        headers = {} if self.missing_data_phase == phase else {'X-NDM-Data': encode_data(payload)}
        return web.Response(status=401, headers=headers)

    def message(self):
        return (
            f'login1=admin,nonce1={self.client_nonce};iter2=2,memcost2=8,'
            f'nonce2={self.nonce},salt2={self.salt_text};login3=admin,nonce3={self.nonce}'
        )

    def verifies(self, proof_text, message):
        proof = base64.b64decode(proof_text)
        mask = hmac.digest(self.stored_key, message.encode(), 'sha3_512')
        recovered_key = bytes(a ^ b for a, b in zip(proof, mask))
        return len(proof) == 64 and hmac.compare_digest(hashlib.sha3_512(recovered_key).digest(), self.stored_key)

    async def handle_version(self, request):
        if request.cookies.get('sid') != 'authenticated':
            return web.Response(status=401)
        return web.json_response({'model': 'Viva'})

    async def handle_auth(self, request):
        if request.method == 'GET':
            if request.cookies.get('sid') == 'authenticated':
                return web.json_response({'authenticated': True})
            self.phases.append('GET')
            headers = CIMultiDict([
                ('WWW-Authenticate', 'x-ndw2-interactive endpoint="/auth"'),
                ('WWW-Authenticate', 'X-NDW4-Interactive endpoint="/auth"'),
                ('X-NDM-Realm', 'old-realm'), ('X-NDM-Challenge', 'old-challenge'),
            ])
            response = web.Response(status=401, headers=headers)
            response.set_cookie('sid', 'challenge')
            return response
        self.assertEqual(request.cookies.get('sid'), 'challenge')
        body = await request.json()
        self.assertNotIn('password', body)
        if 'signature-proof' in body:
            self.phases.append('P3')
            signature = base64.b64encode(hmac.digest(self.server_key, self.message().encode(), 'sha3_512')).decode()
            self.assertTrue(self.verifies(body['signature-proof'], self.message() + ';signature4=' + signature))
            response = web.json_response({'authenticated': True}, status=self.final_status)
            if self.final_status == 200:
                response.set_cookie('sid', 'authenticated')
            return response
        if 'proof' in body:
            self.phases.append('P2')
            valid = self.verifies(body['proof'], self.message())
            key = self.server_key if valid and not self.bad_signature else b'wrong-server-key'
            signature = base64.b64encode(hmac.digest(key, self.message().encode(), 'sha3_512')).decode()
            return self.data({'signature': signature}, 2)
        self.phases.append('P1')
        if body['login'] != 'admin':
            return web.Response(status=401)
        self.client_nonce = body['nonce']
        self.assertEqual(len(base64.b64decode(self.client_nonce)), 16)
        return self.data({
            'salt': self.salt_text, 'nonce': self.nonce,
            'iter': 2, 'memcost': 999999999 if self.malformed_params else 8,
        }, 1)

    async def test_success_uses_three_phases_cookie_and_new_protocol(self):
        self.assertEqual(await self.router.show_version(), {'model': 'Viva'})
        self.assertEqual(self.phases, ['GET', 'P1', 'P2', 'P3'])
        self.assertEqual(await self.router.show_version(), {'model': 'Viva'})
        self.assertEqual(self.phases, ['GET', 'P1', 'P2', 'P3'])

    async def test_wrong_password_stops_before_final_phase(self):
        self.router._password = 'wrong-password'
        with self.assertRaises(client.RouterAuthError) as error:
            await self.router.auth()
        self.assertEqual(error.exception.code, 'invalid_auth')
        self.assertEqual(self.phases, ['GET', 'P1', 'P2'])

    async def test_unknown_login_does_not_try_legacy_auth(self):
        self.router._username = 'unknown'
        with self.assertRaises(client.RouterAuthError) as error:
            await self.router.auth()
        self.assertEqual(error.exception.code, 'invalid_auth')
        self.assertEqual(self.phases, ['GET', 'P1'])

    async def test_bad_server_signature_stops_exchange(self):
        self.bad_signature = True
        with self.assertRaises(client.RouterAuthError):
            await self.router.auth()
        self.assertEqual(self.phases, ['GET', 'P1', 'P2'])

    async def test_missing_phase_two_data_is_protocol_error(self):
        self.missing_data_phase = 2
        with self.assertRaises(client.RouterAuthError) as error:
            await self.router.auth()
        self.assertEqual(error.exception.code, 'auth_protocol_error')
        self.assertEqual(self.phases, ['GET', 'P1', 'P2'])

    async def test_unbounded_key_derivation_parameters_rejected(self):
        self.malformed_params = True
        with self.assertRaises(client.RouterAuthError) as error:
            await self.router.auth()
        self.assertEqual(error.exception.code, 'auth_protocol_error')
        self.assertEqual(self.phases, ['GET', 'P1'])

    async def test_final_rejection_does_not_retry(self):
        self.final_status = 401
        with self.assertRaises(client.RouterAuthError) as error:
            await self.router.auth()
        self.assertEqual(error.exception.code, 'invalid_auth')
        self.assertEqual(self.phases, ['GET', 'P1', 'P2', 'P3'])
