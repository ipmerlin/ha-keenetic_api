"""Independent SMS polling and per-modem notify actions."""
import asyncio
from datetime import timedelta
import logging

import voluptuous as vol
from homeassistant.core import callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.setup import async_setup_component
from homeassistant.util import slugify

from .const import DOMAIN
from .modem import modem_interfaces
from .sms import SmsError, SmsGateway, SmsTracker

SMS_COORDINATOR = "sms_coordinator"
CONF_SMS_TARGET = "sms_default_target"
_LOGGER = logging.getLogger(__name__)
NOTIFY_SCHEMA = vol.Schema({
    vol.Required("message"): cv.string,
    vol.Optional("target"): vol.Any(cv.string, [cv.string]),
    vol.Optional("title"): cv.string,
    vol.Optional("data", default={}): dict,
})


class KeeneticSmsCoordinator(DataUpdateCoordinator):
    """SMS failures never interrupt normal router sensors or expose message text."""
    def __init__(self, hass, entry, full):
        super().__init__(hass, _LOGGER, name=f"{DOMAIN}-sms-{entry.entry_id}",
                         update_interval=timedelta(seconds=max(30, entry.options.get("scan_interval", 30))))
        self.entry = entry
        self.full = full
        self.gateway = SmsGateway(full.router)
        self.trackers = {}
        self.supported = set()
        self.available_interfaces = set()
        self.locks = {}
        self.notify_names = set()
        self.closed = False
        self.notify_ready = False

    @property
    def device_info(self):
        return self.full.device_info

    async def _read(self, interface):
        lock = self.locks.setdefault(interface, asyncio.Lock())
        async with lock:
            messages = await self.gateway.list_messages(interface)
            events = self.trackers.setdefault(interface, SmsTracker()).update(messages)
            for row in events if not self.closed else []:
                self.hass.bus.async_fire("keenetic_api_sms", {
                    "entry_id": self.entry.entry_id, "interface": interface, "sms_id": row["id"],
                    "from": row["from"], "timestamp": row["timestamp"], "message": row["text"],
                })
            return messages

    async def _async_update_data(self):
        data = dict(self.data or {})
        available = set()
        if not self.full.last_update_success:
            self.available_interfaces = available
            return data
        candidates = modem_interfaces(self.full.data.show_interface)
        for interface in candidates:
            try:
                messages = await self._read(interface)
            except Exception:
                # CLI/transport exceptions may contain private response text.
                continue
            data[interface] = messages
            available.add(interface)
            self.supported.add(interface)
        self.available_interfaces = available
        if self.supported and not self.closed:
            await self._register_notify()
        return data

    def select_interface(self, interface=None):
        current = self.supported & set(modem_interfaces(self.full.data.show_interface))
        if interface is None:
            if len(current) != 1:
                raise ServiceValidationError("Specify interface: expected exactly one SMS modem")
            interface = next(iter(current))
        if interface not in current or interface not in self.available_interfaces:
            raise ServiceValidationError("SMS is unavailable on the selected interface")
        return interface

    async def list_messages(self, interface=None):
        interface = self.select_interface(interface)
        try:
            rows = await self._read(interface)
        except Exception:
            raise ServiceValidationError("Unable to retrieve SMS list") from None
        self.async_set_updated_data({**(self.data or {}), interface: rows})
        return {"messages": rows}

    async def mark_read(self, sms_id, interface=None):
        interface = self.select_interface(interface)
        try:
            # Validate ID against the current modem list, including recycled IDs.
            rows = await self._read(interface)
            if not any(row["id"] == sms_id for row in rows):
                raise SmsError("SMS not found")
            async with self.locks[interface]:
                await self.gateway.mark_read(interface, sms_id)
        except Exception:
            raise ServiceValidationError("Unable to mark SMS as read; refresh the list and check sms_id") from None
        await self.async_request_refresh()
        return {"success": True}

    async def send(self, message, target=None, interface=None):
        interface = self.select_interface(interface)
        if isinstance(target, list):
            if len(target) != 1:
                raise ServiceValidationError("Specify exactly one SMS recipient")
            target = target[0]
        if target is None:
            target = self.entry.options.get(CONF_SMS_TARGET, "")
        try:
            # Send is executed once. There is no retry or post-send polling.
            await self.gateway.send(interface, target, message)
        except SmsError as err:
            raise ServiceValidationError(str(err)) from None

    async def _register_notify(self):
        if not self.notify_ready:
            self.notify_ready = await async_setup_component(self.hass, "notify", {})
            if not self.notify_ready:
                return
        if self.closed:
            return
        prefix = f"keenetic_sms_{slugify(self.entry.unique_id or self.entry.entry_id)}"
        owners = self.hass.data.setdefault(f"{DOMAIN}_sms_notify_owners", {})
        for interface in [None, *sorted(self.supported)]:
            name = prefix if interface is None else f"{prefix}_{slugify(interface)}"
            if name in self.notify_names:
                continue
            if self.hass.services.has_service("notify", name):
                if owners.get(name) is not self:
                    _LOGGER.warning("SMS notify name collision; action was not replaced")
                continue

            async def notify(call, bound=interface):
                if self.closed:
                    raise ServiceValidationError("Keenetic SMS integration is unloaded")
                selected = call.data.get("data", {}).get("interface")
                if bound is not None and selected not in (None, bound):
                    raise ServiceValidationError("This action is bound to another SMS interface")
                await self.send(call.data["message"], call.data.get("target"), bound or selected)

            self.hass.services.async_register("notify", name, notify, schema=NOTIFY_SCHEMA)
            self.notify_names.add(name)
            owners[name] = self

    @callback
    def close(self):
        self.closed = True
        owners = self.hass.data.get(f"{DOMAIN}_sms_notify_owners", {})
        for name in self.notify_names:
            if owners.get(name) is self:
                self.hass.services.async_remove("notify", name)
                owners.pop(name, None)
        self.notify_names.clear()
