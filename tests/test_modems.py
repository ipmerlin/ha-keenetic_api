"""Cellular modem fixtures based on read-only KeeneticOS 5.2 responses.

Identity/account fields in fixtures are synthetic. Never commit device dumps.
"""

import ast
import asyncio
import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

COMPONENT = Path(__file__).resolve().parents[1] / 'custom_components' / 'keenetic_api'
spec = importlib.util.spec_from_file_location('modem_under_test', COMPONENT / 'modem.py')
modem = importlib.util.module_from_spec(spec)
spec.loader.exec_module(modem)

MODEM = {
    'type': 'UsbQmi', 'traits': ['Usb', 'Mobile', 'UsbQmi'],
    'product': 'Qualcomm Snapdragon X5', 'manufacturer': 'Keenetic Ltd.',
    'model': '0011', 'fw': 'test firmware', 'mobile': '4G', 'sim': 'READY',
    'operator': 'Test operator', 'rssi': '-68', 'rsrp': '-96', 'rsrq': '-8',
    'cinr': '0', 'band': '3', 'temperature': 32, 'connected': 'yes',
    'plugged': 'yes', 'roaming': False, 'embedded': True,
    'imei': 'synthetic-imei', 'imsi': 'synthetic-imsi', 'iccid': 'synthetic-iccid',
    'phone-number': 'synthetic-phone', 'serial': 'synthetic-serial',
}
STATS = {'rxbytes': 0, 'txbytes': 232, 'rxspeed': 20_000_000, 'txspeed': 0}


class ModemDataTest(unittest.TestCase):
    def test_local_remote_and_legacy_detection_without_ethernet_false_positive(self):
        for kind in ('UsbQmi', 'UsbLte', 'UsbModem', 'UsbMbim', 'MwsMobile'):
            self.assertTrue(modem.is_modem({'type': kind}))
        self.assertTrue(modem.is_modem({'type': 'future-modem', 'traits': ['Mobile']}))
        for row in (None, {}, {'type': 'Ethernet', 'description': 'USB modem uplink'}, {'type':'WifiMaster'}):
            self.assertFalse(modem.is_modem(row))

    def test_real_response_shape_and_zero_values(self):
        expected = {'info': 'Qualcomm Snapdragon X5', 'operator': 'Test operator',
                    'network': '4G', 'sim': 'READY', 'rssi': -68, 'rsrp': -96,
                    'rsrq': -8, 'sinr': 0, 'band': '3', 'temperature': 32,
                    'connected': True, 'roaming': False, 'rxbytes': 0, 'txbytes': 232,
                    'rxspeed': 20, 'txspeed': 0}
        for metric, value in expected.items():
            self.assertEqual(modem.modem_value(MODEM, STATS, metric), value, metric)

    def test_missing_and_invalid_metrics_are_unknown(self):
        for value in ('', '--', 'unknown', None, True, 'nan', 'inf', float('nan')):
            self.assertIsNone(modem.modem_value({'cinr': value}, {}, 'sinr'))
        self.assertIsNone(modem.modem_value({}, {}, 'sim'))
        self.assertIsNone(modem.modem_value({}, {}, 'connected'))
        self.assertIsNone(modem.modem_value({}, {'rxbytes': -1}, 'rxbytes'))
        self.assertFalse(modem.modem_value({'plugged':'no', 'connected':'yes'}, {}, 'connected'))
        self.assertEqual(modem.modem_value({'sinr':'3.5'}, {}, 'sinr'), 3.5)
        self.assertEqual(modem.modem_value({}, {'rxbytes':'9007199254740993'}, 'rxbytes'), 9007199254740993)

    def test_allowlist_does_not_expose_sim_or_account_identifiers(self):
        attrs = modem.modem_attributes(MODEM, 'UsbQmi0')
        self.assertEqual(attrs['firmware'], 'test firmware')
        self.assertFalse(attrs['remote'])
        self.assertFalse(any('synthetic-' in str(value) for value in attrs.values()))
        self.assertTrue(modem.modem_attributes({'type':'MwsMobile'}, 'MwsMobile10')['remote'])

    def test_discovery_after_start_without_duplicates_and_new_optional_fields(self):
        seen = set()
        self.assertEqual(modem.discover_modem_metrics({}, {}, seen, ['info','sinr','rxbytes']), [])
        interfaces = {'UsbQmi0': MODEM, 'GigabitEthernet0': {'type':'Ethernet'}}
        stats = {'UsbQmi0': STATS}
        expected = [('UsbQmi0','info'), ('UsbQmi0','sinr'), ('UsbQmi0','rxbytes')]
        self.assertEqual(modem.discover_modem_metrics(interfaces, stats, seen, ['info','sinr','rxbytes']), expected)
        self.assertEqual(modem.discover_modem_metrics(interfaces, stats, seen, ['info','sinr','rxbytes']), [])
        self.assertEqual(modem.discover_modem_metrics({}, {}, seen, ['info']), [])
        self.assertEqual(modem.discover_modem_metrics(interfaces, stats, seen, ['info']), [])
        self.assertEqual(modem.discover_modem_metrics(interfaces, stats, seen, ['temperature']), [('UsbQmi0','temperature')])


class CoordinatorBase:
    @classmethod
    def __class_getitem__(cls, _):
        return cls
    def __init__(self, coordinator):
        self.coordinator = coordinator
    @property
    def available(self):
        return self.coordinator.last_update_success

class SensorBase:
    pass


def load_definitions(filename, names, namespace):
    tree = ast.parse((COMPONENT / filename).read_text(encoding='utf-8'))
    selected = [node for node in tree.body if isinstance(node,(ast.ClassDef,ast.AsyncFunctionDef,ast.FunctionDef)) and node.name in names]
    module = ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[]))
    exec(compile(module,str(COMPONENT/filename),'exec',flags=__import__('__future__').annotations.compiler_flag), namespace)
    return namespace


class ModemEntityTest(unittest.IsolatedAsyncioTestCase):
    async def test_sensor_setup_hotplug_loss_and_recovery(self):
        listeners = []
        cleanups = []
        data = SimpleNamespace(show_interface={}, stat_interface={})
        coordinator = SimpleNamespace(data=data, unique_id='router_full', device_info={'identifiers':{('keenetic_api','test')}},
                                      last_update_success=True, router=SimpleNamespace(request_interface={}))
        coordinator.async_add_listener = lambda fn: (listeners.append(fn), lambda: listeners.remove(fn))[1]
        descriptions = [SimpleNamespace(key='modem_'+key) for key in ('info','sinr','rxspeed')]
        ns = {'KeeneticRouterCoordinator':object, 'CoordinatorEntity':CoordinatorBase, 'SensorEntity':SensorBase,
              'discover_modem_metrics':modem.discover_modem_metrics, 'modem_value':modem.modem_value,
              'modem_attributes':modem.modem_attributes, 'is_modem':modem.is_modem,
              'callback':lambda fn:fn, 'SENSOR_TYPES':(), 'SENSORS_STAT_INTERFACE':(),
              'MODEM_SENSORS':descriptions, 'DOMAIN':'keenetic_api', 'COORD_FULL':'full'}
        ns = load_definitions('sensor.py', {'async_setup_entry','KeeneticModemSensor'}, ns)
        entities=[]
        await ns['async_setup_entry'](SimpleNamespace(data={'keenetic_api':{'entry':{'full':coordinator}}}),
                                      SimpleNamespace(entry_id='entry',async_on_unload=cleanups.append),
                                      lambda items,*args:entities.extend(items))
        self.assertEqual(entities, [])
        data.show_interface={'UsbQmi0':copy.deepcopy(MODEM)}
        data.stat_interface={'UsbQmi0':dict(STATS)}
        listeners[0]()
        self.assertEqual(len(entities),3)
        self.assertEqual([entity.native_value for entity in entities],['Qualcomm Snapdragon X5',0,20])
        self.assertTrue(all(entity.available for entity in entities))
        listeners[0]()
        self.assertEqual(len(entities),3)
        data.show_interface={}
        data.stat_interface={}
        self.assertTrue(all(not entity.available for entity in entities))
        self.assertTrue(all(entity.native_value is None for entity in entities))
        data.show_interface={'UsbQmi0':dict(MODEM,cinr='8')}
        listeners[0]()
        self.assertEqual(len(entities),3)
        self.assertEqual(entities[1].native_value,8)
        coordinator.last_update_success=False
        self.assertFalse(entities[0].available)
        cleanups[0]()
        self.assertEqual(listeners,[])

    async def test_binary_modem_connectivity_roaming_and_disappearance(self):
        class Entity(SensorBase):
            pass
        class Connectivity:
            CONNECTIVITY='connectivity'
        class Category:
            DIAGNOSTIC='diagnostic'
        ns=load_definitions('binary_sensor.py',{'KeeneticModemBinarySensor'}, {
            'KeeneticRouterCoordinator':object, 'CoordinatorEntity':CoordinatorBase,'BinarySensorEntity':Entity,
            'BinarySensorDeviceClass':Connectivity,'EntityCategory':Category,
            'is_modem':modem.is_modem,'modem_value':modem.modem_value})
        data=SimpleNamespace(show_interface={'UsbQmi0':dict(MODEM)})
        coordinator=SimpleNamespace(data=data,unique_id='test',device_info={},last_update_success=True)
        connected=ns['KeeneticModemBinarySensor'](coordinator,'UsbQmi0','connected')
        roaming=ns['KeeneticModemBinarySensor'](coordinator,'UsbQmi0','roaming')
        self.assertTrue(connected.is_on)
        self.assertFalse(roaming.is_on)
        data.show_interface['UsbQmi0']['connected']='no'
        self.assertFalse(connected.is_on)
        self.assertTrue(connected.available)
        data.show_interface={}
        self.assertFalse(connected.available)
        self.assertIsNone(connected.is_on)


class ModemPollingTest(unittest.IsolatedAsyncioTestCase):
    async def test_extender_modem_stats_are_read_without_exposing_controls(self):
        from test_token_transport import client
        router=client.Router(object())
        router._hw_type='extender'
        base=[{'show':{'system':{}}}, {'show':{'interface':{'UsbQmi0':MODEM}}},
              {'show':{'associations':{}}}, {'show':{'rc':{'system':{}}}},
              {'show':{'rc':{'ip':{'http':{}}}}}]
        router.api=AsyncMock(side_effect=[base,[{'show':{'interface':{'stat':STATS}}}]])
        data=await router.custom_request()
        self.assertEqual(data.stat_interface,{'UsbQmi0':STATS})
        self.assertEqual(router.request_interface,{})
        self.assertEqual(router.api.await_args_list[1].kwargs['json'],[{'show':{'interface':{'stat':{'name':'UsbQmi0'}}}}])

    async def test_one_unsupported_stat_response_does_not_break_other_modems(self):
        from test_token_transport import client
        router=client.Router(object())
        router.api=AsyncMock(return_value=[{'error':'interface unavailable'}, {'show':{'interface':{'stat':STATS}}}])
        self.assertEqual(await router.show_stat_interface(['UsbLte0','UsbQmi0']),{'UsbQmi0':STATS})
        router.api=AsyncMock(return_value={'error':'unsupported'})
        self.assertEqual(await router.show_stat_interface(['UsbQmi0']),{})

if __name__=='__main__':
    unittest.main()
