"""The Keenetic API binary sensor entities."""

from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    COORD_FULL,
)
from .coordinator import KeeneticRouterCoordinator
from .keenetic import KeeneticFullData
from .modem import discover_modem_metrics, is_modem, modem_value

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class KeeneticBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describes Keenetic sensor entity."""
    value_fn: Callable[[KeeneticRouterCoordinator, str], bool]
    attributes_fn: Callable[[KeeneticRouterCoordinator, str], bool] | None = None
    available_fn: Callable[[KeeneticFullData, str], bool] = lambda data, _: True


BINARY_SENSOR_TYPES: dict[str, KeeneticBinarySensorEntityDescription] = {
    "connected_to_router": KeeneticBinarySensorEntityDescription(
        key="connected_to_router",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn= lambda coordinator, obj_id: coordinator.last_update_success,
    ),
    "connected_to_interface": KeeneticBinarySensorEntityDescription(
        key="connected_to_interface",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn= lambda coordinator, obj_id: coordinator.data.show_interface[obj_id].get('connected', "no") == "yes",
        available_fn=lambda data, obj_id: obj_id in data.show_interface,
    ),
    "connected_to_media": KeeneticBinarySensorEntityDescription(
        key="connected_to_media",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn= lambda coordinator, obj_id: coordinator.data.show_media.get(obj_id, False),
        attributes_fn=lambda coordinator, obj_id: {
            "media": coordinator.data.show_media.get(obj_id, None),
        },
    ),
    "interface_pingcheck": KeeneticBinarySensorEntityDescription(
        key="interface_pingcheck",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn= lambda coordinator, obj_id: coordinator.data.show_pingcheck.get(obj_id, {}).status == "pass",
        available_fn=lambda data, obj_id: obj_id in data.show_pingcheck,
    ),
}


async def async_setup_entry(
    hass: HomeAssistant, 
    entry: ConfigEntry, 
    async_add_entities: AddEntitiesCallback
) -> None:

    coordinator = hass.data[DOMAIN][entry.entry_id][COORD_FULL]
    binary_sensors: list[BinarySensorEntity] = []

    binary_sensors.append(KeeneticBinarySensorEntity(coordinator, BINARY_SENSOR_TYPES["connected_to_router"], "connected_to_router", coordinator.router.name_device))

    for interface, data_interface in coordinator.data.show_interface.items():
        if interface in coordinator.router.request_interface:
            new_name = coordinator.router.request_interface[interface]
            binary_sensors.append(
                KeeneticBinarySensorEntity(
                    coordinator,
                    BINARY_SENSOR_TYPES["connected_to_interface"],
                    interface,
                    new_name,
                )
            )
    for pc_interface, pc_data_interface in coordinator.data.show_pingcheck.items():
            binary_sensors.append(
                KeeneticBinarySensorEntity(
                    coordinator,
                    BINARY_SENSOR_TYPES["interface_pingcheck"],
                    pc_interface,
                    pc_data_interface.interface_name,
                )
            )

    for usb in coordinator.data.show_rc_system_usb:
        name_media = f"Media{int(usb['port'])-1}"
        binary_sensors.append(KeeneticBinarySensorEntity(coordinator, BINARY_SENSOR_TYPES["connected_to_media"], name_media, name_media))

    async_add_entities(binary_sensors, False)

    seen_modems = set()

    @callback
    def discover_modems():
        new_metrics = discover_modem_metrics(
            coordinator.data.show_interface, coordinator.data.stat_interface, seen_modems,
            ("connected", "roaming"),
        )
        if new_metrics:
            async_add_entities([
                KeeneticModemBinarySensor(coordinator, interface_id, metric)
                for interface_id, metric in new_metrics
            ])

    discover_modems()
    entry.async_on_unload(coordinator.async_add_listener(discover_modems))



class KeeneticBinarySensorEntity(CoordinatorEntity[KeeneticRouterCoordinator], BinarySensorEntity):

    _attr_has_entity_name = True
    entity_description: KeeneticRouterSensorEntityDescription

    def __init__(
        self,
        coordinator: KeeneticRouterCoordinator,
        description: KeeneticBinarySensorEntityDescription,
        obj_id: str,
        obj_name: str = "",
    ) -> None:
        super().__init__(coordinator)
        self._obj_id = obj_id
        self._attr_key = description.key
        self.entity_description = description
        self._attr_device_info = coordinator.device_info
        self._attr_unique_id = f"{coordinator.unique_id}_{self._attr_key}_{self._obj_id}"
        self._attr_translation_key = self._attr_key
        self._attr_translation_placeholders = {"name": f"{obj_name}"}

    @property
    def is_on(self) -> bool:
        return self.entity_description.value_fn(self.coordinator, self._obj_id)

    @property
    def available(self) -> bool:
        if self.entity_description.key == "connected_to_router":
            return True
        else:
            return (super().available and self.entity_description.available_fn(self.coordinator.data, self._obj_id))

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        if self.entity_description.attributes_fn is not None:
            return self.entity_description.attributes_fn(self.coordinator, self._obj_id)
        else:
            return None


class KeeneticModemBinarySensor(CoordinatorEntity[KeeneticRouterCoordinator], BinarySensorEntity):
    """Connectivity/roaming of a modem, including one attached to an extender."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, interface_id, metric):
        super().__init__(coordinator)
        self.interface_id = interface_id
        self._metric = metric
        self._attr_unique_id = f"{coordinator.unique_id}_modem_{metric}_{interface_id}"
        self._attr_translation_key = f"modem_{metric}"
        self._attr_translation_placeholders = {"name": interface_id}
        self._attr_device_info = coordinator.device_info
        self._attr_device_class = BinarySensorDeviceClass.CONNECTIVITY if metric == "connected" else None
        self._attr_icon = "mdi:signal" if metric == "roaming" else None
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def available(self):
        return super().available and is_modem(self.coordinator.data.show_interface.get(self.interface_id))

    @property
    def is_on(self):
        return modem_value(self.coordinator.data.show_interface.get(self.interface_id, {}), {}, self._metric)
