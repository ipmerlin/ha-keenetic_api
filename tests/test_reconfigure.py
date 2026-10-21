"""Exercise the actual reconfigure step with minimal Home Assistant flow doubles."""

import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
import logging
from unittest.mock import AsyncMock

from test_token_transport import client


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / 'custom_components' / 'keenetic_api' if (ROOT / 'custom_components').is_dir() else ROOT / 'keenetic_api'
tree = ast.parse((COMPONENT / 'config_flow.py').read_text(encoding='utf-8'))
flow_class = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name in ('ConfigFlow', 'KeeneticConfigFlow'))
step = next(n for n in flow_class.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'async_step_reconfigure')
module = ast.fix_missing_locations(ast.Module(body=[step], type_ignores=[]))


class Flow:
    def __init__(self):
        self.entry = SimpleNamespace(data={
            'host': 'http://router', 'password': 'old-password', 'access_token': 'old-token',
        }, unique_id='Keenetic Viva bbccdd', options={'scan_interval': 60})
        self.updated = None
        self.unique_id = None

    def _get_reconfigure_entry(self):
        return self.entry

    async def async_set_unique_id(self, value):
        self.unique_id = value

    def _abort_if_unique_id_mismatch(self):
        if self.unique_id != self.entry.unique_id:
            raise ValueError('unique_id_mismatch')

    def async_update_reload_and_abort(self, entry, data_updates):
        self.updated = data_updates
        return {'type': 'abort', 'reason': 'reconfigure_successful'}

    def async_show_form(self, **kwargs):
        return {'type': 'form', **kwargs}


class ReconfigureTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.api = AsyncMock(return_value=SimpleNamespace(
            mac='00:11:aa:bb:cc:dd',
            show_version=AsyncMock(return_value={'vendor': 'Keenetic', 'device': 'Viva'}),
        ))
        self.namespace = {
            'CONF_ACCESS_TOKEN': 'access_token', 'get_api': self.api,
            'CONF_USERNAME': 'username', 'CONF_PASSWORD': 'password',
            '_LOGGER': logging.getLogger('test_reconfigure'),
            'cv': SimpleNamespace(string=str),
            'RouterAuthError': client.RouterAuthError, 'ClientError': ConnectionError,
            'format_mac': lambda value: value,
            'vol': SimpleNamespace(Optional=lambda key, **kw: key, Schema=lambda data: data),
            'selector': SimpleNamespace(TextSelector=lambda config: 'masked-input', TextSelectorConfig=lambda **kw: kw,
                                        TextSelectorType=SimpleNamespace(PASSWORD='password')),
        }
        exec(compile(module, str(ROOT / 'keenetic_api' / 'config_flow.py'), 'exec'), self.namespace)
        self.flow = Flow()
        self.flow.hass = object()

    async def invoke(self, user_input):
        return await self.namespace['async_step_reconfigure'](self.flow, user_input)

    async def test_form_available_without_loaded_integration_and_no_secret_defaults(self):
        result = await self.invoke(None)
        self.assertEqual(result['type'], 'form')
        self.assertNotIn('old-token', repr(result))
        self.api.assert_not_called()

    async def test_success_updates_credentials_and_preserves_entry_settings(self):
        result = await self.invoke({'access_token': ' new-token '})
        self.assertEqual(result['reason'], 'reconfigure_successful')
        self.assertEqual(self.flow.updated['access_token'], 'new-token')
        self.assertEqual(self.flow.updated['password'], 'old-password')
        self.assertEqual(self.flow.updated['host'], 'http://router')
        self.assertEqual(self.flow.entry.options, {'scan_interval': 60})
        self.assertEqual(self.flow.entry.data['access_token'], 'old-token')

    async def test_blank_keeps_current_token(self):
        await self.invoke({'access_token': ''})
        self.assertEqual(self.flow.updated['access_token'], 'old-token')

    async def test_explicit_clear_restores_password_mode(self):
        await self.invoke({'access_token': '', 'clear_access_token': True})
        self.assertEqual(self.flow.updated['access_token'], '')
        self.assertEqual(self.flow.updated['password'], 'old-password')

    async def test_rejected_token_is_not_saved(self):
        self.api.side_effect = client.RouterAuthError('rejected')
        result = await self.invoke({'access_token': 'bad-token'})
        self.assertEqual(result['errors'], {'base': 'invalid_auth'})
        self.assertIsNone(self.flow.updated)
        self.assertEqual(self.flow.entry.data['access_token'], 'old-token')

    async def test_wrong_router_is_not_saved(self):
        self.api.return_value.mac = '00:11:22:33:44:55'
        with self.assertRaisesRegex(ValueError, 'unique_id_mismatch'):
            await self.invoke({'access_token': 'new-token'})
        self.assertIsNone(self.flow.updated)

    async def test_timeout_is_not_saved(self):
        self.api.side_effect = TimeoutError()
        result = await self.invoke({'access_token': 'new-token'})
        self.assertEqual(result['errors'], {'base': 'cannot_connect'})
        self.assertIsNone(self.flow.updated)

    async def test_password_mode_can_be_selected_and_credentials_replaced(self):
        await self.invoke({'username': 'ha', 'password': 'new-password', 'clear_access_token': True})
        self.assertEqual(self.flow.updated['username'], 'ha')
        self.assertEqual(self.flow.updated['password'], 'new-password')
        self.assertEqual(self.flow.updated['access_token'], '')


if __name__ == '__main__':
    unittest.main()
