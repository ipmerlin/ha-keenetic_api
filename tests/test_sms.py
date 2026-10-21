"""SMS protocol fixtures follow the published Keenetic CLI/RCI reference.

All numbers and messages are synthetic. No SMS is sent to a real modem.
"""
import asyncio
import ast
import importlib.util
import logging
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

ROOT = Path(__file__).resolve().parents[1] / 'custom_components/keenetic_api'
spec = importlib.util.spec_from_file_location('sms_under_test', ROOT/'sms.py')
sms = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sms)


def payload(ident='nv-1', text='synthetic SMS', parts=1, total=1, read=False, timestamp='2026-10-21T10:00:00Z'):
    return {'messages': {ident: {'read':read,'from':'Synthetic sender','timestamp':timestamp,
                                 'parts':parts,'total-parts':total,'text':text}}}


def rows(**kwargs):
    return sms.normalize_messages(payload(**kwargs))


def definitions(file, names, ns):
    tree=ast.parse((ROOT/file).read_text(encoding='utf-8-sig'))
    nodes=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)) and n.name in names]
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes,type_ignores=[])),str(ROOT/file),'exec'),ns)
    return ns


class SmsParserTest(unittest.TestCase):
    def test_mapping_list_and_wrapped_response(self):
        expected=rows()
        row=dict(payload()['messages']['nv-1'],id='nv-1')
        self.assertEqual(sms.normalize_messages({'messages':[row]}),expected)
        self.assertEqual(sms.normalize_messages({'sms':{'list':payload()}}),expected)
        self.assertEqual(set(expected[0]),{'id','from','timestamp','read','parts','total_parts','complete','text'})

    def test_empty_supported_modem(self):
        for p in ({'messages-count':0},{'messages':{}},{'messages':[]}):
            self.assertEqual(sms.normalize_messages(p),[])

    def test_partial_text_is_always_hidden(self):
        self.assertIsNone(rows(parts=1,total=2)[0]['text'])
        for bad in (None,0,True,'n/a',-1,1.5):
            row=rows(parts=bad,total=2)[0]
            self.assertFalse(row['complete'])
            self.assertIsNone(row['text'])
        self.assertFalse(rows(parts=3,total=2)[0]['complete'])

    def test_complete_unicode_and_empty_text_preserved(self):
        for text in ('Привет, мир 👋',''):
            self.assertEqual(rows(text=text,parts=2,total=2)[0]['text'],text)

    def test_invalid_list_does_not_create_fake_sms_capability(self):
        for p in (None,[],{},{'sms':[]},{'messages':{'bad':None}},Mock(status=404),{'status':[{'status':'error'}]}, {'messages':'bad'}, {'messages':[{'text':'secret'}]}):
            with self.assertRaises(sms.SmsError): sms.normalize_messages(p)

    def test_unknown_read_flag_is_not_unread(self):
        self.assertIsNone(rows(read='not known')[0]['read'])
        self.assertTrue(rows(read='yes')[0]['read'])
        self.assertFalse(rows(read='no')[0]['read'])

    def test_duplicate_ids_are_rejected(self):
        p=payload()['messages']['nv-1']
        with self.assertRaises(sms.SmsError): sms.normalize_messages({'messages':[dict(p,id='a'),dict(p,id='a')]})

    def test_latest_message_timestamp_handles_cli_and_iso(self):
        older=rows(timestamp='Wed Oct 21 09:00:00 2026')[0]
        newer=rows(timestamp='2026-10-21T10:00:00Z')[0]
        self.assertLess(sms.timestamp_key(older),sms.timestamp_key(newer))


class SmsTrackerTest(unittest.TestCase):
    def test_startup_complete_and_partial_messages_never_emit(self):
        tracker=sms.SmsTracker()
        self.assertEqual(tracker.update(rows()),[])
        self.assertEqual(tracker.update(rows(read=True)),[])
        tracker=sms.SmsTracker()
        self.assertEqual(tracker.update(rows(parts=1,total=2)),[])
        self.assertEqual(tracker.update(rows(parts=2,total=2)),[])

    def test_new_multipart_emits_only_after_completion_once(self):
        tracker=sms.SmsTracker();tracker.update([])
        self.assertEqual(tracker.update(rows(parts=1,total=3)),[])
        self.assertEqual(tracker.update(rows(parts=2,total=3)),[])
        self.assertEqual(len(tracker.update(rows(parts=3,total=3))),1)
        self.assertEqual(tracker.update(rows(parts=3,total=3,read=True)),[])
        self.assertEqual(tracker.update([]),[])
        self.assertEqual(tracker.update(rows(parts=3,total=3)),[])

    def test_reused_storage_slot_with_new_timestamp_emits(self):
        tracker=sms.SmsTracker();tracker.update(rows())
        self.assertEqual(len(tracker.update(rows(timestamp='2026-10-22T10:00:00Z'))),1)

    def test_per_modem_baselines_are_independent(self):
        first=sms.SmsTracker();second=sms.SmsTracker();first.update([])
        self.assertEqual(second.update(rows()),[])
        self.assertEqual(len(first.update(rows())),1)

    def test_tracker_history_is_bounded(self):
        tracker=sms.SmsTracker();tracker.update([])
        for n in range(4200): tracker.update(rows(ident=str(n)))
        self.assertLessEqual(len(tracker.seen),4096)


class SmsGatewayTest(unittest.IsolatedAsyncioTestCase):
    async def test_list_uses_only_readonly_endpoint(self):
        router=SimpleNamespace(api=AsyncMock(return_value=payload()))
        self.assertEqual(await sms.SmsGateway(router).list_messages('UsbQmi0'),rows())
        router.api.assert_awaited_once_with('GET','/rci/sms/list?interface=UsbQmi0')

    async def test_mark_read_is_explicit_and_scoped(self):
        router=SimpleNamespace(api=AsyncMock(return_value=[{'status':'message'}]))
        await sms.SmsGateway(router).mark_read('UsbQmi0','nv-1')
        router.api.assert_awaited_once_with('POST','/rci/sms/read',{'interface':'UsbQmi0','id':'nv-1'})

    async def test_send_exactly_once_and_preserve_unicode(self):
        router=SimpleNamespace(api=AsyncMock(return_value=[{'status':'message'}]))
        await sms.SmsGateway(router).send('UsbQmi0','+79990000000','Тест 👋')
        router.api.assert_awaited_once_with('POST','/rci/sms/send',{'interface':'UsbQmi0','to':'+79990000000','message':'Тест 👋'})

    async def test_timeout_never_retries_or_leaks_content(self):
        router=SimpleNamespace(api=AsyncMock(side_effect=TimeoutError('private phone and text')))
        with self.assertRaisesRegex(sms.SmsError,'not retried') as err:
            await sms.SmsGateway(router).send('UsbQmi0','+79990000000','private body')
        self.assertNotIn('private',str(err.exception))
        self.assertEqual(router.api.await_count,1)

    async def test_invalid_recipient_or_message_sends_nothing(self):
        router=SimpleNamespace(api=AsyncMock())
        for phone,message in [('', 'x'),('12','x'),('1;reboot','x'),('+79990000000',''),('+79990000000','x'*10001)]:
            with self.assertRaises(sms.SmsError): await sms.SmsGateway(router).send('UsbQmi0',phone,message)
        router.api.assert_not_awaited()

    async def test_cli_errors_and_unknown_outcomes_are_not_success(self):
        for response in (None,{},[],Mock(status=500),{'foo':'bar'},[{'status':'error','message':'private'}],{'sms':{'send':{'status':[{'status':'error'}]}}}):
            router=SimpleNamespace(api=AsyncMock(return_value=response))
            with self.assertRaises(sms.SmsError): await sms.SmsGateway(router).send('UsbQmi0','+79990000000','body')
            self.assertEqual(router.api.await_count,1)


class CoordinatorBase:
    def __init__(self,hass,logger,**kwargs):
        self.hass=hass;self.data={};self.last_update_success=True
    def async_set_updated_data(self,data): self.data=data
    async def async_request_refresh(self): self.data=await self._async_update_data()


def make_coordinator():
    events=[];services={}
    class Registry:
        def has_service(self,domain,name): return (domain,name) in services
        def async_register(self,domain,name,fn,**kwargs): services[domain,name]=fn
        def async_remove(self,domain,name): services.pop((domain,name),None)
    hass=SimpleNamespace(data={},bus=SimpleNamespace(async_fire=lambda e,d:events.append((e,d))),services=Registry())
    entry=SimpleNamespace(entry_id='entry',unique_id='router',options={'sms_default_target':'+79990000000'})
    full=SimpleNamespace(last_update_success=True,data=SimpleNamespace(show_interface={'UsbQmi0':{'type':'UsbQmi'}}),device_info={},router=SimpleNamespace(api=AsyncMock(return_value=payload())))
    ns=definitions('sms_coordinator.py',{'KeeneticSmsCoordinator'},{
        'DataUpdateCoordinator':CoordinatorBase,'timedelta':timedelta,'_LOGGER':logging.getLogger(__name__),
        'DOMAIN':'keenetic_api','CONF_SMS_TARGET':'sms_default_target','SmsGateway':sms.SmsGateway,
        'SmsTracker':sms.SmsTracker,'SmsError':sms.SmsError,'ServiceValidationError':ValueError,
        'asyncio':asyncio,'callback':lambda fn:fn,'slugify':lambda v:v.lower(),
        'modem_interfaces':lambda d:{k:v for k,v in d.items() if v['type'] in ('UsbQmi','UsbLte')},
        'async_setup_component':AsyncMock(),'NOTIFY_SCHEMA':None,
    })
    coordinator=ns['KeeneticSmsCoordinator'](hass,entry,full)
    return coordinator,events,services


class SmsRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_events_and_nonmutating_list(self):
        c,events,services=make_coordinator()
        await c.async_request_refresh()
        self.assertEqual(events,[])
        self.assertEqual(c.supported,{'UsbQmi0'})
        self.assertEqual(set(services),{('notify','keenetic_sms_router'),('notify','keenetic_sms_router_usbqmi0')})
        c.full.router.api.return_value=payload(ident='nv-2')
        result=await c.list_messages()
        self.assertEqual(len(events),1)
        self.assertEqual(set(events[0][1]),{'entry_id','interface','sms_id','from','timestamp','message'})
        self.assertEqual(result['messages'][0]['id'],'nv-2')
        self.assertTrue(all(call.args[0]=='GET' for call in c.full.router.api.await_args_list))
        await c.list_messages();self.assertEqual(len(events),1)

    async def test_poll_failure_unavailable_and_recovery_no_replay(self):
        c,events,_=make_coordinator();await c.async_request_refresh()
        c.full.router.api.side_effect=TimeoutError('private')
        await c.async_request_refresh()
        self.assertEqual(c.available_interfaces,set())
        c.full.router.api.side_effect=None
        await c.async_request_refresh()
        self.assertEqual(events,[])
        self.assertEqual(c.available_interfaces,{'UsbQmi0'})

    async def test_unsupported_modem_creates_no_notify(self):
        c,_,services=make_coordinator();c.full.router.api.return_value={'status':[{'status':'error'}]}
        await c.async_request_refresh()
        self.assertEqual(c.supported,set());self.assertEqual(services,{})

    async def test_bound_notify_and_default_override(self):
        c,_,services=make_coordinator();await c.async_request_refresh()
        c.gateway.send=AsyncMock()
        bound=services['notify','keenetic_sms_router_usbqmi0']
        await bound(SimpleNamespace(data={'message':'test'}))
        c.gateway.send.assert_awaited_with('UsbQmi0','+79990000000','test')
        await bound(SimpleNamespace(data={'message':'test','target':['+79990000001']}))
        c.gateway.send.assert_awaited_with('UsbQmi0','+79990000001','test')
        with self.assertRaises(ValueError):
            await bound(SimpleNamespace(data={'message':'x','data':{'interface':'UsbLte0'}}))

    async def test_multiple_modems_require_explicit_interface(self):
        c,_,services=make_coordinator();c.full.data.show_interface['UsbLte0']={'type':'UsbLte'}
        await c.async_request_refresh();c.gateway.send=AsyncMock()
        legacy=services['notify','keenetic_sms_router']
        with self.assertRaises(ValueError): await legacy(SimpleNamespace(data={'message':'test'}))
        await legacy(SimpleNamespace(data={'message':'test','data':{'interface':'UsbLte0'}}))
        c.gateway.send.assert_awaited_with('UsbLte0','+79990000000','test')
        self.assertIn(('notify','keenetic_sms_router_usblte0'),services)

    async def test_unload_removes_only_owned_notify_actions(self):
        c,_,services=make_coordinator();await c.async_request_refresh()
        services['notify','other']=Mock();old=services['notify','keenetic_sms_router']
        c.close();self.assertEqual(set(services),{('notify','other')})
        with self.assertRaises(ValueError): await old(SimpleNamespace(data={'message':'test'}))
        await c._register_notify();self.assertEqual(len(services),1)

    async def test_mark_read_checks_id_and_issues_one_write(self):
        c,_,_=make_coordinator();await c.async_request_refresh()
        c.gateway.mark_read=AsyncMock()
        with self.assertRaises(ValueError): await c.mark_read('missing')
        c.gateway.mark_read.assert_not_awaited()
        await c.mark_read('nv-1');c.gateway.mark_read.assert_awaited_once_with('UsbQmi0','nv-1')

    async def test_new_modem_late_discovery_gets_its_own_baseline(self):
        c,events,services=make_coordinator();await c.async_request_refresh()
        c.full.data.show_interface['UsbLte0']={'type':'UsbLte'}
        await c.async_request_refresh()
        self.assertEqual(events,[]);self.assertIn(('notify','keenetic_sms_router_usblte0'),services)



class SensorCoordinatorEntity:
    def __init__(self,coordinator): self.coordinator=coordinator
    @property
    def available(self): return self.coordinator.last_update_success


class SmsSensorTest(unittest.TestCase):
    def sensor(self,messages):
        ns=definitions('sms_sensor.py',{'KeeneticSmsSensor'},{
            'CoordinatorEntity':SensorCoordinatorEntity,'SensorEntity':type('SensorEntity',(),{}),
            'timestamp_key':sms.timestamp_key})
        coordinator=SimpleNamespace(entry=SimpleNamespace(unique_id='router'),device_info={},
            data={'UsbQmi0':messages},last_update_success=True,available_interfaces={'UsbQmi0'})
        return ns['KeeneticSmsSensor'](coordinator,'UsbQmi0')

    def test_only_latest_complete_message_in_attributes(self):
        messages=rows(read=False)+rows(ident='nv-2',read=True,timestamp='2026-10-21T11:00:00Z',text='latest')
        messages+=rows(ident='nv-3',parts=1,total=2,timestamp='2026-10-21T12:00:00Z',text='private partial')
        sensor=self.sensor(messages)
        self.assertEqual(sensor.native_value,2)
        self.assertEqual(sensor.extra_state_attributes,{'sms_id':'nv-2','from':'Synthetic sender',
            'timestamp':'2026-10-21T11:00:00Z','message':'latest'})
        self.assertNotIn('private partial',str(sensor.extra_state_attributes))

    def test_empty_partial_and_unavailable_have_no_text_attributes(self):
        for messages in ([],rows(parts=1,total=2)):
            self.assertEqual(self.sensor(messages).extra_state_attributes,{})
        sensor=self.sensor(rows())
        sensor.coordinator.available_interfaces.clear()
        self.assertFalse(sensor.available);self.assertEqual(sensor.extra_state_attributes,{})

    def test_sms_entity_identity_is_scoped_to_router_and_modem(self):
        sensor=self.sensor(rows())
        self.assertEqual(sensor._attr_unique_id,'router_sms_unread_UsbQmi0')


class SmsLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_unloading_one_entry_keeps_shared_services(self):
        unload_services=Mock()
        ns=definitions('__init__.py',{'async_unload_entry'}, {'DOMAIN':'keenetic_api','PLATFORMS':[],
            'async_unload_services':unload_services,'HomeAssistant':object,'ConfigEntry':object})
        entries={'first':{},'second':{}}
        hass=SimpleNamespace(data={'keenetic_api':entries},config_entries=SimpleNamespace(async_unload_platforms=AsyncMock(return_value=True)))
        await ns['async_unload_entry'](hass,SimpleNamespace(entry_id='first'))
        unload_services.assert_not_called()
        await ns['async_unload_entry'](hass,SimpleNamespace(entry_id='second'))
        unload_services.assert_called_once_with(hass)

    async def test_notify_name_collision_does_not_overwrite_other_action(self):
        c,_,services=make_coordinator();foreign=Mock()
        services['notify','keenetic_sms_router']=foreign
        await c.async_request_refresh()
        self.assertIs(services['notify','keenetic_sms_router'],foreign)
        c.close();self.assertIs(services['notify','keenetic_sms_router'],foreign)

    async def test_failed_router_snapshot_stops_sms_polling(self):
        c,_,_=make_coordinator();c.full.last_update_success=False
        await c.async_request_refresh();c.full.router.api.assert_not_awaited()
        self.assertEqual(c.available_interfaces,set())


if __name__ == '__main__':
    unittest.main()
