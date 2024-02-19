from __future__ import annotations
from typing import Any

import logging
_LOGGER = logging.getLogger(__name__)

from enum import IntEnum
import time
import bleak_retry_connector

from bleak import BleakClient
from homeassistant.core import HomeAssistant
from homeassistant.components import bluetooth
from homeassistant.components.light import (
    ATTR_BRIGHTNESS_PCT,
    ATTR_BRIGHTNESS, 
    ATTR_RGB_COLOR, 
    ATTR_COLOR_TEMP,
    ATTR_COLOR_TEMP_KELVIN,
    ColorMode, 
    LightEntity)
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity import DeviceInfo, Entity

from .const import DOMAIN
from .models import LedCommand, LedMode, ControlMode, ModelInfo
from .kelvin_rgb import kelvin_to_rgb

UUID_CONTROL_CHARACTERISTIC = '00010203-0405-0607-0809-0a0b0c0d2b11'



async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities):
    light = hass.data[DOMAIN][config_entry.entry_id]
    #bluetooth setup
    ble_device = bluetooth.async_ble_device_from_address(hass, light.address.upper(), False)
    async_add_entities([GoveeBluetoothLight(light, ble_device, config_entry)])

class GoveeBluetoothLight(LightEntity):
    _attr_color_mode = ColorMode.RGB
    _attr_min_color_temp_kelvin = 2000
    _attr_max_color_temp_kelvin = 9000
    _attr_supported_color_modes = {
            ColorMode.BRIGHTNESS,
            ColorMode.COLOR_TEMP,
            ColorMode.RGB,
        }

    def __init__(self, light, ble_device, config_entry: ConfigEntry) -> None:
        """Initialize an bluetooth light."""
        self._mac = light.address
        _LOGGER.debug("Config entry data: %s", config_entry.data)
        self._model = config_entry.data.get("model", "default")
        self._name = config_entry.data.get("CONF_NAME", self._model + "-" + self._mac.replace(":", "")[-4:])
        self._ble_device = ble_device
        self._state = None
        self._brightness = None
        self.client = None
        self._attr_extra_state_attributes = {}

    @property
    def name(self) -> str:
        """Return the name of the switch."""
        return self._name# self._model + "-" + self._mac.replace(":", "")[-4:]
    
    @property
    def model(self) -> str:
        """Return the model of the switch."""
        return self._model

    @property
    def unique_id(self) -> str:
        """Return a unique, Home Assistant friendly identifier for this entity."""
        return self._mac.replace(":", "")

    @property
    def brightness(self):
        return self._brightness

    @property
    def is_on(self) -> bool | None:
        """Return true if light is on."""
        return self._state
    
    @property
    def device_info(self) -> DeviceInfo:
        """Return the device info."""
        return DeviceInfo(
            identifiers={
                # Serial numbers are unique identifiers within a specific domain
                (DOMAIN, self.unique_id)
            },
            name=self.name,
            manufacturer="Govee",
            model=self.model,
        )

    async def async_turn_on(self, **kwargs) -> None:
        _LOGGER.debug(
            "turn on %s %s with %s",
            self.name,
            self.model,
            kwargs,
        )

        await self._sendBluetoothData(LedCommand.POWER, [0x1])
        self._state = True


        if ATTR_BRIGHTNESS_PCT in kwargs:
            brightness_pct = kwargs.get(ATTR_BRIGHTNESS_PCT)
            max_brightness = ModelInfo.get_brightness_max(self.model)
            brightness = int(brightness_pct / 100 * max_brightness) if max_brightness else brightness_pct
            await self._sendBluetoothData(LedCommand.BRIGHTNESS, [brightness])
            self._brightness = brightness
        elif ATTR_BRIGHTNESS in kwargs:
            brightness = kwargs.get(ATTR_BRIGHTNESS, 255)
            max_brightness = ModelInfo.get_brightness_max(self.model)
            brightness = int(brightness/ max_brightness * 255) if max_brightness else brightness
            await self._sendBluetoothData(LedCommand.BRIGHTNESS, [brightness])
            self._brightness = brightness


        if ATTR_RGB_COLOR in kwargs:
            red, green, blue = kwargs.get(ATTR_RGB_COLOR)
            await self._sendBluetoothData(LedCommand.COLOR, [ModelInfo.get_led_mode(self.model), red, green, blue])


        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            color_temp_kelvin = kwargs.get(ATTR_COLOR_TEMP_KELVIN)
            color_temp_kelvin = max(
                min(color_temp_kelvin, self._attr_max_color_temp_kelvin),
                self._attr_min_color_temp_kelvin,
            )
            red, green, blue = kelvin_to_rgb(color_temp_kelvin)
            await self._sendBluetoothData(LedCommand.COLOR, [ModelInfo.get_led_mode(self.model), red, green, blue])
        elif ATTR_COLOR_TEMP in kwargs:
            color_temp = kwargs.get(ATTR_COLOR_TEMP)
            color_temp_kelvin = int(1000000 / color_temp)
            color_temp_kelvin = max(
                min(color_temp_kelvin, self._attr_max_color_temp_kelvin),
                self._attr_min_color_temp_kelvin,
            )
            red, green, blue = kelvin_to_rgb(color_temp_kelvin)
            await self._sendBluetoothData(LedCommand.COLOR, [ModelInfo.get_led_mode(self.model), red, green, blue])

        if self.client:
            self._disconnect()
        

    async def async_turn_off(self, **kwargs) -> None:
        await self._sendBluetoothData(LedCommand.POWER, [0x0])
        self._state = False

    async def _disconnect(self):
        if self.client:
            _LOGGER.debug("Disconnecting from %s", self.name)
            await self.client.disconnect()
            self.client = None

    async def _connectBluetooth(self) -> BleakClient:



        async def disconnected_callback(client):
            """Callback for when the client disconnects."""
            self.client = None
            self._attr_extra_state_attributes["connection_status"] = "Disconnected"
            self.async_write_ha_state()  # Update HA state immediately

        client = await bleak_retry_connector.establish_connection(
            BleakClient,
            self._ble_device, 
            self.unique_id,
            disconnected_callback=disconnected_callback,
            timeout=10.0  # Adjust the timeout as needed
        )
        self.client = client

        return client
    

    async def _sendBluetoothData(self, cmd, payload):
        if not isinstance(cmd, int):
            raise ValueError('Invalid command')
        if not isinstance(payload, bytes) and not (isinstance(payload, list) and all(isinstance(x, int) for x in payload)):
            raise ValueError('Invalid payload')
        if len(payload) > 17:
            raise ValueError('Payload too long')

        cmd = cmd & 0xFF
        payload = bytes(payload)

        frame = bytes([0x33, cmd]) + bytes(payload)
        # pad frame data to 19 bytes (plus checksum)
        frame += bytes([0] * (19 - len(frame)))
        
        # The checksum is calculated by XORing all data bytes
        checksum = 0
        for b in frame:
            checksum ^= b
        
        frame += bytes([checksum & 0xFF])
        client = await self._connectBluetooth()
        if client.is_connected:
            await client.write_gatt_char(UUID_CONTROL_CHARACTERISTIC, frame, False)
            self._attr_extra_state_attributes["connection_status"] = "Connected"
        else:
            self._attr_extra_state_attributes["connection_status"] = "Disconnected"
        
        current_time_string = time.strftime("%c")
        self._attr_extra_state_attributes["update_status"] = f"updated: {current_time_string}"
        self.async_update_ha_state()  # Reflect the attribute changes immediately