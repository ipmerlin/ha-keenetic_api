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

class ModemDeduplicationTest(unittest.TestCase):
    def test_controller_keeps_legacy_metrics_but_extender_keeps_new_ones(self):
        interfaces={'MwsMobile10':dict(MODEM,type='MwsMobile'),'UsbQmi0':dict(MODEM)}
        metrics=('info','operator','rssi','rxbytes','txbytes','rxspeed','txspeed','connected','roaming')
        stats={key:dict(STATS) for key in interfaces}
        new=modem.discover_modem_metrics(interfaces,stats,set(),metrics,{'MwsMobile10':'MTS 4G'})
        for metric in ('rxbytes','txbytes','rxspeed','txspeed','connected'):
            self.assertNotIn(('MwsMobile10',metric),new)
            self.assertIn(('UsbQmi0',metric),new)
        for metric in ('info','operator','rssi','roaming'):
            self.assertIn(('MwsMobile10',metric),new)

    def test_exact_duplicate_identities_only_for_legacy_modem_interfaces(self):
        pairs=list(modem.duplicate_modem_entities('router_full',
                   {'MwsMobile10':dict(MODEM,type='MwsMobile'),'UsbQmi0':MODEM,'Ethernet0':{'type':'Ethernet'}},
                   {'MwsMobile10':'MTS','Ethernet0':'WAN'}))
        self.assertEqual(len(pairs),5)
        self.assertIn(('sensor','router_full_rxbytes_MwsMobile10','router_full_modem_rxbytes_MwsMobile10'),pairs)
        self.assertIn(('binary_sensor','router_full_connected_to_interface_MwsMobile10','router_full_modem_connected_MwsMobile10'),pairs)
        self.assertFalse(any('UsbQmi0' in value or 'Ethernet0' in value for pair in pairs for value in pair))

    def test_registry_cleanup_preserves_originals_orphans_other_entries_and_extender(self):
        rows={}
        objects={}
        def add(domain,key,entity_id,entry='entry'):
            rows[(domain,'keenetic_api',key)]=entity_id
            objects[entity_id]=SimpleNamespace(config_entry_id=entry,name='user customized')
        add('sensor','router_full_rxbytes_MwsMobile10','sensor.original')
        add('sensor','router_full_modem_rxbytes_MwsMobile10','sensor.duplicate')
        add('sensor','router_full_modem_txbytes_MwsMobile10','sensor.orphan_without_original')
        add('sensor','router_full_txspeed_MwsMobile10','sensor.foreign_original','other-entry')
        add('sensor','router_full_modem_txspeed_MwsMobile10','sensor.local_new')
        add('sensor','router_full_modem_info_MwsMobile10','sensor.model')
        add('sensor','router_full_modem_rxbytes_UsbQmi0','sensor.extender_counter')
        add('binary_sensor','router_full_connected_to_interface_MwsMobile10','binary_sensor.original')
        add('binary_sensor','router_full_modem_connected_MwsMobile10','binary_sensor.duplicate')
        removed=[]
        def remove(entity_id):
            removed.append(entity_id)
            for key in list(rows):
                if rows[key]==entity_id:
                    del rows[key]
        registry=SimpleNamespace(async_get_entity_id=lambda *key:rows.get(key),
                                 async_get=objects.get,async_remove=remove)
        ns=load_definitions('__init__.py',{'remove_duplicate_modem_entities'},{
            'callback':lambda fn:fn,'er':SimpleNamespace(async_get=lambda hass:registry),
            'DOMAIN':'keenetic_api','duplicate_modem_entities':modem.duplicate_modem_entities})
        coordinator=SimpleNamespace(unique_id='router_full',data=SimpleNamespace(
            show_interface={'MwsMobile10':dict(MODEM,type='MwsMobile'),'UsbQmi0':MODEM}),
            router=SimpleNamespace(request_interface={'MwsMobile10':'MTS'}))
        for _ in range(2):
            ns['remove_duplicate_modem_entities'](object(),SimpleNamespace(entry_id='entry'),coordinator)
        self.assertEqual(set(removed),{'sensor.duplicate','binary_sensor.duplicate'})
        self.assertEqual(len(removed),2)
        self.assertEqual(rows[('sensor','keenetic_api','router_full_rxbytes_MwsMobile10')],'sensor.original')
        self.assertEqual(objects['sensor.original'].name,'user customized')


class ModemIconTest(unittest.TestCase):
    def test_signal_thresholds_include_boundary_values(self):
        cases={
            'rssi':[(-69,'signal-cellular-3'),(-70,'signal-cellular-2'),(-84,'signal-cellular-2'),
                    (-85,'signal-cellular-1'),(-99,'signal-cellular-1'),(-100,'signal-cellular-outline')],
            'rsrp':[(-84,'antenna'),(-85,'antenna'),(-99,'antenna'),(-100,'signal-cellular-outline')],
            'sinr':[(14,'speedometer'),(13,'speedometer-medium'),(10,'speedometer-medium'),
                    (9,'speedometer-medium'),(1,'speedometer-medium'),(0,'speedometer-slow'),(-2,'speedometer-slow')],
            'rsrq':[(-9,'quality-high'),(-10,'quality-medium'),(-14,'quality-medium'),(-15,'quality-low')],
        }
        for metric,points in cases.items():
            for value,icon in points:
                self.assertEqual(modem.modem_signal_icon(metric,value),'mdi:'+icon,(metric,value))

    def test_missing_values_are_not_shown_as_a_valid_zero_reading(self):
        for metric in modem.SIGNAL_METRICS:
            for value in (None,'',True,'nan','unknown'):
                self.assertEqual(modem.modem_signal_icon(metric,value),'mdi:signal-off')
        self.assertEqual(modem.modem_signal_icon('sinr','0'),'mdi:speedometer-slow')

    def test_entity_icon_tracks_new_value_without_changing_identity(self):
        ns=load_definitions('sensor.py',{'KeeneticModemSensor'},{
            'CoordinatorEntity':CoordinatorBase,'KeeneticRouterCoordinator':object,'SensorEntity':SensorBase,
            'modem_value':modem.modem_value,'is_modem':modem.is_modem,
            'modem_signal_icon':modem.modem_signal_icon,'SIGNAL_METRICS':modem.SIGNAL_METRICS,
        })
        data=SimpleNamespace(show_interface={'UsbQmi0':dict(MODEM)},stat_interface={})
        coordinator=SimpleNamespace(data=data,unique_id='test_full',device_info={},last_update_success=True)
        entity=ns['KeeneticModemSensor'](coordinator,SimpleNamespace(key='modem_rssi',icon=None),'UsbQmi0')
        unique_id=entity._attr_unique_id
        self.assertEqual(entity.icon,'mdi:signal-cellular-3')
        data.show_interface['UsbQmi0']['rssi']='-90'
        self.assertEqual(entity.icon,'mdi:signal-cellular-1')
        self.assertEqual(entity._attr_unique_id,unique_id)
        coordinator.last_update_success=False
        self.assertEqual(entity.icon,'mdi:signal-off')
        coordinator.last_update_success=True
        data.show_interface={}
        self.assertEqual(entity.icon,'mdi:signal-off')


if __name__=='__main__':
    unittest.main()
