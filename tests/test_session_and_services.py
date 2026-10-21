"""Regression checks for failures observed in the user's HA startup log."""

import ast
import __future__
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

import aiohttp
from aiohttp import web


COMPONENT = Path(__file__).resolve().parents[1] / 'custom_components' / 'keenetic_api'


def load_function(filename, name, namespace):
    tree = ast.parse((COMPONENT / filename).read_text(encoding='utf-8'))
    fn = next(node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)
    module = ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[]))
    exec(compile(module, str(COMPONENT / filename), 'exec', flags=__future__.annotations.compiler_flag), namespace)
    return namespace[name]


class SessionLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_failed_setup_detaches_without_closing_shared_connector(self):
        app = web.Application()
        async def ok(request):
            return web.Response(text='other router still works')
        app.router.add_get('/', ok)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        connector = aiohttp.TCPConnector()
        failed = aiohttp.ClientSession(connector=connector)
        other = aiohttp.ClientSession(connector=connector)
        ns = {
            'CONF_USERNAME': 'username', 'CONF_PASSWORD': 'password', 'CONF_ACCESS_TOKEN': 'access_token',
            'CONF_HOST': 'host', 'CONF_PORT': 'port', 'CONF_SSL': 'ssl', 'REQUEST_TIMEOUT': 30,
            'ClientTimeout': aiohttp.ClientTimeout, 'CookieJar': aiohttp.CookieJar,
            'aiohttp_client': SimpleNamespace(async_create_clientsession=lambda *a, **kw: failed),
            'Router': lambda **kw: SimpleNamespace(async_setup_obj=AsyncMock(side_effect=ValueError('setup failed'))),
        }
        try:
            get_api = load_function('__init__.py', 'get_api', ns)
            with self.assertRaisesRegex(ValueError, 'setup failed'):
                await get_api(object(), {'host': 'http://127.0.0.1', 'port': port, 'ssl': False})
            self.assertTrue(failed.closed)
            self.assertFalse(connector.closed)
            async with other.get(f'http://127.0.0.1:{port}/') as response:
                self.assertEqual(await response.text(), 'other router still works')
        finally:
            failed.detach()
            await other.close()
            await runner.cleanup()


class ServiceGuardTest(unittest.TestCase):
    def test_unloaded_entry_is_reported_without_keyerror(self):
        class ValidationError(Exception):
            pass
        router_for_entry = load_function('services.py', '_router_for_entry', {
            'DOMAIN': 'keenetic_api', 'CROUTER': 'client_router', 'ServiceValidationError': ValidationError,
        })
        for data in ({}, {'keenetic_api': {}}, {'keenetic_api': {'missing-router': {}}}):
            with self.assertRaises(ValidationError):
                router_for_entry(SimpleNamespace(data=data), 'missing-router')
        router = Mock()
        self.assertIs(router_for_entry(SimpleNamespace(data={'keenetic_api': {'loaded': {'client_router': router}}}), 'loaded'), router)
