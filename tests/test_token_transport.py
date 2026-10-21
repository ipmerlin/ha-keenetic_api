"""Network-free tests of the real router client with scripted HTTP responses."""

import asyncio
import importlib.util
import logging
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / 'custom_components' / 'keenetic_api' if (ROOT / 'custom_components').is_dir() else ROOT / 'keenetic_api'
aiofiles = types.ModuleType('aiofiles')
aiofiles_os = types.ModuleType('aiofiles.os')
aiofiles.os = aiofiles_os
spec = importlib.util.spec_from_file_location('router_under_test', COMPONENT / 'keenetic.py')
client = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {
    'aiohttp': types.ModuleType('aiohttp'), 'aiofiles': aiofiles,
    'aiofiles.os': aiofiles_os, 'router_under_test': client,
}):
    spec.loader.exec_module(client)


class Response:
    def __init__(self, status=200, data=None, headers=None, content_type='application/json'):
        self.status = status
        self.data = data
        self.headers = headers or {}
        self.content_type = content_type

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        return self.data


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)

    def get(self, url, **kwargs):
        return self.request(method='get', url=url, **kwargs)


class TokenTransportTest(unittest.IsolatedAsyncioTestCase):
    async def test_token_probe_and_requests_skip_password_auth(self):
        session = Session(Response(data={'model': 'Viva'}), Response(data={'model': 'Viva'}))
        router = client.Router(session, host='http://192.168.31.1', access_token=' synthetic-test-token ')
        self.assertTrue(await router.auth())
        self.assertEqual(await router.show_version(), {'model': 'Viva'})
        self.assertEqual(len(session.calls), 2)
        for call in session.calls:
            self.assertEqual(call['url'], 'http://192.168.31.1:80/rci/show/version')
            self.assertEqual(call['headers'], {'X-NDMA-TKN': 'synthetic-test-token'})
            self.assertFalse(call['allow_redirects'])

    async def test_rejected_token_never_falls_back_or_leaks(self):
        for status in (401, 403):
            with self.subTest(status=status):
                session = Session(Response(status))
                router = client.Router(session, access_token='synthetic-secret')
                with self.assertRaises(client.RouterAuthError) as error:
                    await router.show_version()
                self.assertNotIn('synthetic-secret', str(error.exception))
                self.assertEqual(len(session.calls), 1)

    async def test_html_or_redirect_is_not_successful_token_probe(self):
        for status in (200, 302):
            session = Session(Response(status, content_type='text/html'))
            with self.assertRaises(client.RouterAuthError):
                await client.Router(session, access_token='synthetic-token').auth()

    async def test_old_password_challenge_is_preserved(self):
        from hashlib import md5, sha256
        session = Session(
            Response(401, headers={'X-NDM-Realm': 'test-realm', 'X-NDM-Challenge': 'challenge'}),
            Response(content_type='text/plain'), Response(data={'model': 'old-router'}),
        )
        router = client.Router(session, username='test-user', password='test-password')
        self.assertEqual(await router.show_version(), {'model': 'old-router'})
        expected = sha256(('challenge' + md5(b'test-user:test-realm:test-password').hexdigest()).encode()).hexdigest()
        self.assertEqual(session.calls[1]['json'], {'login': 'test-user', 'password': expected})
        self.assertTrue(all('X-NDMA-TKN' not in call['headers'] for call in session.calls))

    async def test_missing_legacy_challenge_has_actionable_error(self):
        router = client.Router(Session(Response(401)))
        with self.assertRaisesRegex(client.RouterAuthError, 'access token'):
            await router.auth()

    async def test_token_download_includes_header_without_redirect(self):
        session = Session(Response(401))
        router = client.Router(session, host='http://192.168.31.1', access_token='synthetic-token')
        with patch.object(aiofiles_os, 'makedirs', AsyncMock(), create=True):
            with self.assertRaises(client.RouterAuthError):
                await router.async_download_file('http://192.168.31.1:80/ci/firmware', 'unused')
        self.assertEqual(session.calls[0]['headers'], {'X-NDMA-TKN': 'synthetic-token'})
        self.assertFalse(session.calls[0]['allow_redirects'])

    async def test_download_does_not_send_token_to_other_host(self):
        session = Session()
        router = client.Router(session, host='http://192.168.31.1', access_token='synthetic-token')
        with self.assertRaises(ValueError):
            await router.async_download_file('http://other-host/file', 'unused')
        self.assertEqual(session.calls, [])

    async def test_reject_header_injection(self):
        with self.assertRaises(client.RouterAuthError):
            client.Router(Session(), access_token='token\r\nInjected: value')

    async def test_request_debug_log_omits_payload(self):
        router = client.Router(Session(Response(data={})), access_token='synthetic-secret')
        with self.assertLogs(client._LOGGER, logging.DEBUG) as logs:
            await router.api('post', '/rci/', {'password': 'synthetic-password'})
        self.assertNotIn('synthetic-secret', str(logs.output))
        self.assertNotIn('synthetic-password', str(logs.output))


if __name__ == '__main__':
    unittest.main()
