"""Unread SMS counts with only the most recent complete message as attributes."""
from homeassistant.components.sensor import SensorEntity
from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .sms import timestamp_key
from .sms_coordinator import SMS_COORDINATOR
from .const import DOMAIN


async def async_setup_sms_sensors(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id][SMS_COORDINATOR]
    seen = set()

    @callback
    def discover():
        added = sorted(coordinator.supported - seen)
        seen.update(added)
        if added:
            async_add_entities([KeeneticSmsSensor(coordinator, interface) for interface in added])

    discover()
    entry.async_on_unload(coordinator.async_add_listener(discover))


class KeeneticSmsSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "unread_sms"
    _attr_icon = "mdi:message-text-outline"

    def __init__(self, coordinator, interface):
        super().__init__(coordinator)
        self.interface = interface
        self._attr_unique_id = f"{coordinator.entry.unique_id}_sms_unread_{interface}"
        self._attr_translation_placeholders = {"name": interface}
        self._attr_device_info = coordinator.device_info

    @property
    def available(self):
        return super().available and self.interface in self.coordinator.available_interfaces

    @property
    def native_value(self):
        return sum(row["read"] is False for row in self.coordinator.data.get(self.interface, []))

    @property
    def extra_state_attributes(self):
        if not self.available:
            return {}
        complete = [row for row in self.coordinator.data.get(self.interface, []) if row["complete"]]
        if not complete:
            return {}
        last = max(complete, key=timestamp_key)
        return {"sms_id": last["id"], "from": last["from"], "timestamp": last["timestamp"], "message": last["text"]}
