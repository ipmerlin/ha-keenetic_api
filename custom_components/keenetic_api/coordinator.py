"""The Keenetic API coordinator."""

from __future__ import annotations
from datetime import timedelta
import logging
import asyncio

from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.const import CONF_HOST

from .keenetic import Router
from .const import (
    DOMAIN,
    FW_SANDBOX,
    COORD_FIREWARE,
    SCAN_INTERVAL_FIREWARE,
    COUNT_REPEATED_REQUEST_FIREWARE,
    TIMER_REPEATED_REQUEST_FIREWARE,
)

_LOGGER = logging.getLogger(__name__)


class KeeneticRouterCoordinator(DataUpdateCoordinator):
    def __init__(
            self,
            hass: HomeAssistant,
            router: Router,
            update_interval: int,
            entry: ConfigEntry
    ) -> None:
        self.router = router
        self.entry = entry
        self._host = entry.data[CONF_HOST]
        self.unique_id = f"{entry.unique_id}_full"
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}-{self._host}-full",
            update_interval=timedelta(seconds=update_interval),
        )

    async def _async_update_data(self):
        """Asynchronous update of all data."""
        _errr = None
        try:
            full_data = await self.router.custom_request()
            _LOGGER.debug(f"{self.router.mac} _async_update_data full_data {full_data}")
        except Exception as err:
            _LOGGER.debug(f"{self.router.mac} UpdateFailed _async_update_data (err {err})")
            _errr = err
        try:
            coordinator_firmware = self.hass.data[DOMAIN][self.entry.entry_id][COORD_FIREWARE]
            if (not coordinator_firmware.last_update_success) or _errr != None:
                await coordinator_firmware.async_refresh()
        except Exception:
                pass
        if _errr != None:
            raise UpdateFailed(f"{self.router.mac} UpdateFailed (err {_errr})")
        return full_data

    @property
    def device_info(self) -> DeviceInfo:
        """Set device info."""
        return DeviceInfo(
            configuration_url=self.router.url_router,
            connections={(CONNECTION_NETWORK_MAC, self.router.mac)},
            identifiers={(DOMAIN, self.router.mac)},
            manufacturer="Keenetic",
            name=self.entry.unique_id,
            model=self.router.model,
            hw_version=self.router.hw_type,
        )


class KeeneticRouterFirmwareCoordinator(DataUpdateCoordinator):
    def __init__(
            self,
            hass: HomeAssistant,
            router: Router,
            update_interval: int,
            entry: ConfigEntry
    ) -> None:
        self.router = router
        self.entry = entry
        self.unique_id = f"{entry.unique_id}_fw"
        self._host = entry.data[CONF_HOST]
        self._version_firmware = {}
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}-{self._host}-fw",
            update_interval=timedelta(seconds=update_interval),
        )

    async def _async_update_data(self):
        for _ in range(COUNT_REPEATED_REQUEST_FIREWARE):
            data_components_list = await self.router.components_list()
            if not isinstance(data_components_list, dict) or not data_components_list.get('continued', False):
                break
            await asyncio.sleep(TIMER_REPEATED_REQUEST_FIREWARE)
        if not isinstance(data_components_list, dict):
            data_components_list = {}

        firmware = {
            'new': data_components_list.get('firmware'),
            'current': data_components_list.get('local'),
            'sandbox': data_components_list.get('sandbox')
        }

        # Безопасный парсинг release_notes (строка 127)
        firmware['release_notes'] = None
        firmware['channel'] = None

        # Если есть данные о новой прошивке, пытаемся получить заметки
        new_v = firmware['new'].get('version') if isinstance(firmware.get('new'), dict) else None
        if new_v:
            try:
                for _ in range(COUNT_REPEATED_REQUEST_FIREWARE):
                    data_notes = await self.router.release_notes(new_v, FW_SANDBOX.get(firmware.get('sandbox'), "main"))
                    if not isinstance(data_notes, dict) or not data_notes.get('continued', False):
                        break
                    await asyncio.sleep(TIMER_REPEATED_REQUEST_FIREWARE)
                if isinstance(data_notes, dict):
                    webhelp = data_notes.get('webhelp')
                    ru_notes = webhelp.get('ru', []) if isinstance(webhelp, dict) else []
                    if isinstance(ru_notes, list) and ru_notes and isinstance(ru_notes[0], dict):
                        firmware['release_notes'] = ru_notes[0].get('href')
                        firmware['channel'] = ru_notes[0].get('title')
            except Exception:
                _LOGGER.debug("Could not fetch release notes")

        self._version_firmware = firmware
        return self._version_firmware


    @property
    def device_info(self) -> DeviceInfo:
        vfw = self._version_firmware.get("current")
        sw_version = vfw.get("title") if isinstance(vfw, dict) else None
        return DeviceInfo(connections={(CONNECTION_NETWORK_MAC, self.router.mac)}, sw_version=sw_version)



class KeeneticRouterRcInterfaceCoordinator(DataUpdateCoordinator):
    def __init__(
            self,
            hass: HomeAssistant,
            router: Router,
            update_interval: int,
            entry: ConfigEntry
    ) -> None:
        self.router = router
        self.entry = entry
        self._host = entry.data[CONF_HOST]
        self.unique_id = f"{entry.unique_id}_rc_interface"
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}-{self._host}-rc-interface",
            update_interval=timedelta(seconds=update_interval),
        )

    async def _async_update_data(self):
        """Asynchronous update of all data."""
        try:
            return await self.router.show_rc_interface()
        except Exception as err:
            _LOGGER.debug(f"{self.router.mac} UpdateFailed _async_update_data (err {err})")
            raise UpdateFailed(f"{self.router.mac} UpdateFailed {err}")

    @property
    def device_info(self) -> DeviceInfo:
        """Set device info."""
        return DeviceInfo(
            connections={(CONNECTION_NETWORK_MAC, self.router.mac)}
        )
