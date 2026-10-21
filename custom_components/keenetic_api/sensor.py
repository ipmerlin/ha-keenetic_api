"""The Keenetic API sensor entities."""

from dataclasses import dataclass
from collections.abc import Callable
from typing import Any
from datetime import UTC, datetime, timedelta
import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorStateClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    UnitOfInformation, 
    EntityCategory, 
    UnitOfDataRate,
    UnitOfTemperature,
    UnitOfInformation,
    UnitOfTime,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import device_registry as dr
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import KeeneticRouterCoordinator
from .keenetic import KeeneticFullData
from .modem import discover_modem_metrics, is_modem, modem_attributes, modem_value, modem_signal_icon, modem_network_icon, SIGNAL_METRICS
from .const import (
    DOMAIN,
    COORD_FULL,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class KeeneticRouterSensorEntityDescription(SensorEntityDescription):
    """A class that describes sensor entities."""
    value: Callable[[KeeneticFullData, Any], Any] = (
        lambda coordinator, key: coordinator.data.show_system[key] if coordinator.data.show_system[key] is not None else None
    )
    attributes_fn: Callable[[KeeneticFullData], dict[str, Any]] | None = None


def convert_uptime(uptime: int) -> datetime:
    """Convert uptime."""
    if uptime != None:
        return (datetime.now(tz=UTC) - timedelta(seconds=int(uptime))).replace(second=0, microsecond=0)
    else:
        return None

def convert_data_size(data_size: int = 0) -> float:
    """Convert data_size."""
    return round(data_size/1024/1024, 3)

def ind_wan_ip_adress(fdata: KeeneticFullData):
    """Определение внешнего IP адреса."""
    try:
        data_p_i = fdata.priority_interface
        show_interface = fdata.show_interface
        priority_interface = sorted(data_p_i, key=lambda x: data_p_i[x]['order'])
        for row in priority_interface:
            if show_interface[row]["connected"] == "yes":
                if row.startswith('Wireguard'):
                    return show_interface[row]["wireguard"]["peer"][0].get("remote-endpoint-address") or show_interface[row]["wireguard"]["peer"][0].get("remote") or "-"
                else:
                    return show_interface[row]["address"]
    except Exception as ex:
        _LOGGER.debug(f'Not ind_wan_ip_adress - {ex}')
        return None


SENSOR_TYPES: tuple[KeeneticRouterSensorEntityDescription, ...] = (
    KeeneticRouterSensorEntityDescription(
        key="cpuload",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
    ),
    KeeneticRouterSensorEntityDescription(
        key="memory",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        value=lambda coordinator, key: int(float(coordinator.data.show_system[key].split('/')[0])/float(coordinator.data.show_system[key].split('/')[1])*100),
    ),
    KeeneticRouterSensorEntityDescription(
        key="uptime",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda coordinator, key: convert_uptime(coordinator.data.show_system[key]),
    ),
    KeeneticRouterSensorEntityDescription(
        key="wan_ip_adress",
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda coordinator, key: ind_wan_ip_adress(coordinator.data),
    ),
    KeeneticRouterSensorEntityDescription(
        key="temperature_2_4g",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda coordinator, key: coordinator.data.show_interface['WifiMaster0']['temperature'],
    ),
    KeeneticRouterSensorEntityDescription(
        key="temperature_5g",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda coordinator, key: coordinator.data.show_interface['WifiMaster1']['temperature'],
    ),
    KeeneticRouterSensorEntityDescription(
        key="clients_wifi",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda coordinator, key: len(coordinator.data.show_associations.get("station", [])),
    ),
)

SENSORS_STAT_INTERFACE: tuple[KeeneticRouterSensorEntityDescription, ...] = (
    KeeneticRouterSensorEntityDescription(
        key="rxbytes",
        state_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.MEGABYTES,
        value=lambda coordinator, obj_id: convert_data_size(coordinator.data.stat_interface[obj_id].get('rxbytes')),
    ),
    KeeneticRouterSensorEntityDescription(
        key="txbytes",
        state_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.MEGABYTES,
        value=lambda coordinator, obj_id: convert_data_size(coordinator.data.stat_interface[obj_id].get('txbytes')),
    ),
    KeeneticRouterSensorEntityDescription(
        key="timestamp",
        device_class=SensorDeviceClass.TIMESTAMP,
        value=lambda coordinator, obj_id: convert_uptime(coordinator.data.show_interface[obj_id].get('uptime')),
    ),
    KeeneticRouterSensorEntityDescription(
        key="rxspeed",
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND,
        value=lambda coordinator, obj_id: convert_data_size(coordinator.data.stat_interface[obj_id].get('rxspeed')),
    ),
    KeeneticRouterSensorEntityDescription(
        key="txspeed",
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND,
        value=lambda coordinator, obj_id: convert_data_size(coordinator.data.stat_interface[obj_id].get('txspeed')),
    ),
)

MODEM_SENSORS = (
    SensorEntityDescription(key="modem_info", icon="mdi:router-wireless"),
    SensorEntityDescription(key="modem_operator", icon="mdi:radio-tower"),
    SensorEntityDescription(key="modem_network", icon="mdi:network-strength-4"),
    SensorEntityDescription(key="modem_sim", icon="mdi:sim", entity_category=EntityCategory.DIAGNOSTIC),
    SensorEntityDescription(key="modem_band", icon="mdi:radio-tower", entity_category=EntityCategory.DIAGNOSTIC),
    *(SensorEntityDescription(
        key=f"modem_{metric}", device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
    ) for metric in ("rssi", "rsrp")),
    *(SensorEntityDescription(
        key=f"modem_{metric}", native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS,
        state_class=SensorStateClass.MEASUREMENT, icon="mdi:signal",
    ) for metric in ("rsrq", "sinr")),
    SensorEntityDescription(
        key="modem_temperature", device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT, entity_category=EntityCategory.DIAGNOSTIC,
    ),
    *(SensorEntityDescription(
        key=f"modem_{metric}", device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ) for metric in ("rxbytes", "txbytes")),
    *(SensorEntityDescription(
        key=f"modem_{metric}", device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
    ) for metric in ("rxspeed", "txspeed")),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry, 
    async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id][COORD_FULL]

    sensors = []
    for description in SENSOR_TYPES:
        try:
            if description.value(coordinator, description.key) is not None:
                sensors.append(KeeneticRouterSensor(coordinator, description, description.key, description.key))
        except Exception as err:
            _LOGGER.debug(f'async_setup_entry sensor SENSOR_TYPES {description} err - {err}')

    for interface, data_interface in coordinator.data.show_interface.items():
        if interface in coordinator.router.request_interface:
            new_name = coordinator.router.request_interface[interface]
            for description in SENSORS_STAT_INTERFACE:
                try:
                    sensors.append(KeeneticRouterSensor(coordinator, description, interface, new_name))
                except Exception as err:
                    _LOGGER.debug(f'async_setup_entry sensor SENSORS_STAT_INTERFACE {description} err - {err}')

    async_add_entities(sensors, False)

    seen_modems = set()
    descriptions = {description.key.removeprefix("modem_"): description for description in MODEM_SENSORS}

    @callback
    def discover_modems():
        new_metrics = discover_modem_metrics(
            coordinator.data.show_interface, coordinator.data.stat_interface, seen_modems, descriptions,
            coordinator.router.request_interface,
        )
        if new_metrics:
            async_add_entities([
                KeeneticModemSensor(coordinator, descriptions[metric], interface_id)
                for interface_id, metric in new_metrics
            ])

    discover_modems()
    entry.async_on_unload(coordinator.async_add_listener(discover_modems))

class KeeneticRouterSensor(CoordinatorEntity[KeeneticRouterCoordinator], SensorEntity):
    _attr_has_entity_name = True
    entity_description: KeeneticRouterSensorEntityDescription

    def __init__(
            self,
            coordinator: KeeneticRouterCoordinator,
            description: KeeneticRouterSensorEntityDescription,
            obj_id: str,
            obj_name: str,
    ) -> None:
        super().__init__(coordinator)

        self._attr_device_info = coordinator.device_info
        self.obj_id = obj_id
        self._attr_unique_id = f"{coordinator.unique_id}_{description.key}_{self.obj_id}"
        self.entity_description = description
        self._attr_translation_key = description.key
        self._attr_translation_placeholders = {"name": f"{obj_name}"}

    @property
    def native_value(self) -> StateType:
        """Sensor value."""
        return self.entity_description.value(self.coordinator, self.obj_id)

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        """Return the state attributes of the sensor."""
        if self.entity_description.attributes_fn is not None:
            return self.entity_description.attributes_fn(self.coordinator.data)
        else:
            return None


class KeeneticModemSensor(CoordinatorEntity[KeeneticRouterCoordinator], SensorEntity):
    """One read-only metric of a local or Wi-Fi-system cellular modem."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, description, interface_id):
        super().__init__(coordinator)
        self.interface_id = interface_id
        self.entity_description = description
        self._metric = description.key.removeprefix("modem_")
        self._attr_unique_id = f"{coordinator.unique_id}_{description.key}_{interface_id}"
        self._attr_translation_key = description.key
        self._attr_translation_placeholders = {"name": interface_id}
        self._attr_device_info = coordinator.device_info

    @property
    def available(self):
        return super().available and is_modem(self.coordinator.data.show_interface.get(self.interface_id))

    @property
    def native_value(self):
        return modem_value(
            self.coordinator.data.show_interface.get(self.interface_id, {}),
            self.coordinator.data.stat_interface.get(self.interface_id, {}), self._metric,
        )

    @property
    def icon(self):
        if self._metric in SIGNAL_METRICS:
            return modem_signal_icon(self._metric, self.native_value if self.available else None)
        if self._metric == "network":
            return modem_network_icon(self.native_value if self.available else None)
        return self.entity_description.icon

    @property
    def extra_state_attributes(self):
        if self._metric == "info":
            return modem_attributes(self.coordinator.data.show_interface.get(self.interface_id, {}), self.interface_id)
        return None
