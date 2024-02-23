from __future__ import annotations
from typing import Any, Callable, Dict, List, Optional, Tuple

import asyncio
import logging
import random
_LOGGER = logging.getLogger(__name__)
import math
from enum import IntEnum
import time
import bleak_retry_connector

from bleak import BleakClient, BleakScanner, BLEDevice
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
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback


from .const import DOMAIN
from .models import LedCommand, LedMode, ControlMode, ModelInfo
from .kelvin_rgb import kelvin_to_rgb

UUID_CONTROL_CHARACTERISTIC = '00010203-0405-0607-0809-0a0b0c0d2b11'

PARALLEL_UPDATES = 1

def clamp(value, min_value, max_value):
    return max(min(value, max_value), min_value)

async def async_setup_entry(
        hass: HomeAssistant,
        config_entry: ConfigEntry, 
        async_add_entities: AddEntitiesCallback,
    ) -> None:
    """Set up the light from a config entry."""
    light = hass.data[DOMAIN][config_entry.entry_id]
    #bluetooth setup
    ble_device = bluetooth.async_ble_device_from_address(hass, light.address.upper(), False)
    async_add_entities([GoveeBluetoothLight(light, ble_device, config_entry)])

class GoveeBluetoothLight(LightEntity):
    MAX_RECONNECT_ATTEMPTS = 1
    INITIAL_RECONNECT_DELAY = 1 # seconds

    ble_device: Optional[BLEDevice] = None

    _attr_has_entity_name = True
    _attr_name = None
    _attr_color_mode = ColorMode.RGB
    _attr_min_color_temp_kelvin = 2000
    _attr_max_color_temp_kelvin = 9000
    _attr_supported_color_modes = {
            ColorMode.COLOR_TEMP,
            ColorMode.RGB,
            ColorMode.BRIGHTNESS,
        }
    


    def __init__(self, light, ble_device, config_entry: ConfigEntry) -> None:
        """Initialize an bluetooth light."""
        self._mac = light.address
        _LOGGER.debug("Config entry data: %s", config_entry.data)
        self._model = config_entry.data.get("model", "default")
        self._name = config_entry.data.get("name", self._model + "-" + self._mac.replace(":", "")[-4:])
        self._ble_device = ble_device
        self._state = None
        self._is_on = False
        self._brightness = 0
        self._temperature = 4000
        self._rgb_color = [255,255,255]
        self._client = None
        self._control_mode = ColorMode.RGB
        
        self._attr_extra_state_attributes = {}

        # dirty attributes to be updated
        self._dirty_state = False
        self._dirty_brightness = False
        self._dirty_color = False

        self._temp_rgb_color = [255,255,255]
        self._temp_brightness = 255
        self._temp_state = False


        self._power_data = 0x0
        self._brightness_data = 0x0
        self._rgb_color_data = [0,0,0]

        self._reconnect = 0
        self._last_update = time.time()
        self._ping_roll = 0
        self._keep_alive_task = None

    @property
    def name(self):
        return self._name

    @property
    def model(self):
        return self._model

    @property
    def unique_id(self) -> str:
        """Return a unique, Home Assistant friendly identifier for this entity."""
        return self._mac.replace(":", "")

    @property
    def brightness(self):
        return self._brightness
    
    @property
    def rgb_color(self):
        return self._rgb_color
    
    @property
    def color_temp(self):
        return int(self._temperature)

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
            name=self._name,
            manufacturer="Govee",
            model=self._model,
        )

    async def async_added_to_hass(self):
        """Run when entity about to be added to hass."""
        _LOGGER.debug("Adding %s", self.name)
        await self._connect()
        _LOGGER.debug("Connected to %s", self.name)

    async def async_will_remove_from_hass(self):
        """Run when entity will be removed from hass."""
        _LOGGER.debug("Removing %s", self.name)
        if self._keep_alive_task:
            self._keep_alive_task.cancel()
            _LOGGER.debug("Cancelled keep alive task for %s", self.name)

    async def async_turn_on(self, **kwargs) -> None:
        _LOGGER.debug(
            "async turn on %s %s with %s",
            self.name,
            self.model,
            kwargs,
        )

        if self._keep_alive_task:
            self._keep_alive_task.cancel()
            _LOGGER.debug("Cancelled keep alive task for %s", self.name)


        self._temp_state = True
        self._dirty_state = True
        self._attr_extra_state_attributes["dirty_state"] = self._dirty_state


        if ATTR_BRIGHTNESS in kwargs:
            brightness = kwargs.get(ATTR_BRIGHTNESS, 255)

            self._temp_brightness = brightness
            self._dirty_brightness = True
            self._attr_extra_state_attributes["dirty_brightness"] = self._dirty_brightness
        elif ATTR_BRIGHTNESS_PCT in kwargs:
            brightness_pct = kwargs.get(ATTR_BRIGHTNESS_PCT, 100)

            self._temp_brightness = brightness_pct * 255 / 100
            self._dirty_brightness = True
            self._attr_extra_state_attributes["dirty_brightness"] = self._dirty_brightness

        if ATTR_RGB_COLOR in kwargs:
            red, green, blue = kwargs.get(ATTR_RGB_COLOR)

            self._control_mode = ControlMode.COLOR
            self._temp_rgb_color = [red, green, blue]
            self._dirty_color = True
            self._attr_extra_state_attributes["dirty_rgb_color"] = self._dirty_color
        elif ATTR_COLOR_TEMP in kwargs:
            color_temp = kwargs.get(ATTR_COLOR_TEMP)
            kelvin = (1000000 / color_temp)

            kelvin = clamp(kelvin, self._attr_max_color_temp_kelvin, self._attr_min_color_temp_kelvin)
            self._control_mode = ControlMode.TEMPERATURE
            self._temperature = kelvin
            self._dirty_color = True
            red, green, blue = kelvin_to_rgb(kelvin)
            self._temp_rgb_color = [red, green, blue]
            self._attr_extra_state_attributes["dirty_rgb_color"] = self._dirty_rgb_color

        self._keep_alive_task = asyncio.create_task(self._send_packets_thread())
        # if self.client:
            # await self._disconnect()
        

    async def async_turn_off(self, **kwargs) -> None:
        if self._keep_alive_task:
            self._keep_alive_task.cancel()
            _LOGGER.debug("Cancelled keep alive task for %s", self.name)

        self._temp_state = False
        self._dirty_state = True
        self._attr_extra_state_attributes["dirty_state"] = self._dirty_state

        self._keep_alive_task = asyncio.create_task(self._send_packets_thread())


    async def _send_power(self, power):
        """Send the power state to the device."""
        self._attr_extra_state_attributes["power_data"] = 0x1 if power else 0x0
        try:
            return await self._send_bluetooth_data(LedCommand.POWER, [0x1 if power else 0x0])
        
        except Exception as exception:
            _LOGGER.error("Error sending power to %s: %s", self.name, exception)

        return False


    async def _send_brightness(self, brightness):
        """Send the brightness to the device."""
        max_brightness = ModelInfo.get_brightness_max(self.model)
        brightness = math.floor(brightness / 255 * max_brightness)
        self._attr_extra_state_attributes["brightness_data"] = brightness
        _packet = [brightness]
        try:
            return await self._send_bluetooth_data(LedCommand.BRIGHTNESS, _packet)
    
        except Exception as exception:
            _LOGGER.error("Error sending brightness to %s: %s", self.name, exception)

        return False
    

    async def _send_rgb_color(self):

        _R = self._temp_rgb_color[0];
        _G = self._temp_rgb_color[1];
        _B = self._temp_rgb_color[2];

        _TK = 0;
        _WR = 0;
        _WG = 0;
        _WB = 0;

        if self._control_mode == ControlMode.TEMPERATURE:
            # _R = _G = _B = 0xFF;
            # _TK = int(self._temperature);
            pass
        self._attr_extra_state_attributes["rgb_color_data"] = [_R,_G,_B]
        self._attr_extra_state_attributes["control_mode"] = self._control_mode
        self._attr_extra_state_attributes["temperature"] = self._temperature
        
        if not isinstance(_R, int) or _R < 0 or _R > 255:
            return ValueError("Invalid r");
        if not isinstance(_G, int) or _G < 0 or _G > 255:
            return ValueError("Invalid g");
        if not isinstance(_B, int) or _B < 0 or _B > 255:
            return ValueError("Invalid b");
        

        try:
            _payload = [ModelInfo.get_led_mode(self.model)]

            if ModelInfo.get_led_mode(self.model) == LedMode.MODE_1501:
                    _payload.extend([
                        0x01,
                        _R,
                        _G,
                        _B,
                        (_TK >> 8) & 0xFF,
                        _TK & 0xFF,
                        _WR,
                        _WG,
                        _WB,
                        0xFF,
                        0x74,
                    ])
                    """
                        proven to work,  but let's see if the new method is better
                        _payload.extend([
                        0x01,
                        self.R,
                        self.G,
                        self.B,
                        0x00,
                        0x00,
                        0x00,
                        0x00,
                        0x00,
                        0xFF,
                        0x74,
                    ]) """
            else: #MODE_D and MODE_2
                _payload.extend([
                    _R,
                    _G,
                    _B,
                    (_TK >> 8) & 0xFF,
                    _TK & 0xFF,
                    _WR,
                    _WG,
                    _WB,
                ])
                #_payload.extend([red, green, blue])
            
                return await self._send_bluetooth_data(LedCommand.COLOR, _payload)
            
        except Exception as exception:
            _LOGGER.error("Error sending color to %s: %s", self.name, exception)

        return False

    def is_dirty(self):
        return self._dirty_state or self._dirty_brightness or self._dirty_color

    async def _send_packets_thread(self):
        """Send the packets to the device."""
        task_running = True

        while self.is_dirty() and task_running:
            """Connect to the device and send the packets."""
            try:
                """Connect to the device."""
                if not await self._connect():

                    await asyncio.sleep(2)
                    continue

                _changed = True # send mqtt packet once mqtt is implemented
            
                """Send the packets."""
                if self._dirty_state:
                    if not await self._send_power(self._temp_state):
                        await asyncio.sleep(1);
                        continue

                    self._dirty_state = False
                    self._attr_extra_state_attributes["dirty_state"] = self._dirty_state
                    self._state = self._temp_state

                
                if self._dirty_brightness:
                    if not await self._send_brightness(self._temp_brightness):
                        await asyncio.sleep(1);
                        continue

                    self._dirty_brightness = False
                    self._attr_extra_state_attributes["dirty_brightness"] = self._dirty_brightness
                    self._brightness = self._temp_brightness
                elif self._dirty_color:
                    if not await self._send_rgb_color():
                        await asyncio.sleep(1);
                        continue

                    self._dirty_color = False
                    self._attr_extra_state_attributes["dirty_rgb_color"] = self._dirty_color
                    self._rgb_color = self._temp_rgb_color
                else:
                    """Keep alive, send a packet every 1 second."""
                    _changed = False # no mqtt packet if no change


                    if (time.time() - self._last_update) >= 1:
                        _async_res = False
                        self._ping_roll += 1

                        if self._ping_roll % 3 == 0 or self._state == 0:
                            _async_res = await self._send_power(self._state);
                        elif self._ping_roll % 3 == 1:
                            _async_res = await self._send_brightness(self._brightness);
                        elif self._ping_roll % 3 == 2:
                            _async_res = await self._send_rgb_color(*self._rgb_color);
                        
                        
                        if self._ping_roll > 3:
                            task_running = False
                            self._ping_roll = 0
                            if self._client is not None:
                                await self._client.disconnect()

                    jitter = random.uniform(0.3, 0.6)
                    await asyncio.sleep(0.1)
                    continue
                
                if _changed:
                    # send mqtt packet
                    pass
                
                await asyncio.sleep(0.01)


            except Exception as exception:
                _LOGGER.error("Error sending packets to %s: %s", self.name, exception)
                task_running = False
                try:
                    if self._client is not None:
                        await self._client.disconnect()
                except Exception as exception:
                    _LOGGER.error("Error disconnecting from %s: %s", self.name, exception)

                self._client = None

                await asyncio.sleep(2)




    async def _disconnect(self):
        if self._client:
            _LOGGER.debug("Disconnecting from %s", self.name)
            self._attr_extra_state_attributes["connection_status"] = "Disconnecting"
            await self._client.disconnect()
            self._client = None




    async def _connect(self):

        if self._client != None and self._client.is_connected:
            return self._client
        
        try:
            if self._client is not None:
                _LOGGER.debug("Disconnecting from %s", self.name)
                await self._client.disconnect()
        except Exception as exception:
            _LOGGER.error("Error disconnecting from %s: %s", exception)

        self._client = None

        def disconnected_callback(client):
            if self._client == client:
                self.hass.loop.create_task(self._handle_disconnect())

        try:
            client = await bleak_retry_connector.establish_connection(
                BleakClient,
                self._ble_device, 
                self.unique_id,
                disconnected_callback=disconnected_callback,
                timeout=10.0  # Adjust the timeout as needed
            )
            self._client = client
            self._reconnect = 0
            self._attr_extra_state_attributes["connection_status"] = "Connected"

            return self._client.is_connected
        except Exception as exception:
            _LOGGER.error("Failed to connect to %s: %s", self.name, exception)
            self._client = None

        return None
    
    async def _handle_disconnect(self):
        """Handle the device's disconnection."""
        # self.async_write_ha_state()
    

    async def _send_bluetooth_data(self, cmd, payload):
        if not isinstance(cmd, int):
            raise ValueError('Invalid command')
        if not isinstance(payload, bytes) and not (isinstance(payload, list) and all(isinstance(x, int) for x in payload)):
            raise ValueError('Invalid payload')
        if len(payload) > 17:
            raise ValueError('Payload too long')

        _LOGGER.debug("Sending command %s with payload %s to %s", cmd, payload, self.name)
        self._attr_extra_state_attributes["last_command"] = cmd
        # if ModelInfo.get_led_mode(self.model) != LedMode.MODE_1501:
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


        try:
            await self._client.write_gatt_char(UUID_CONTROL_CHARACTERISTIC, frame, False)
            self._last_update = time.time()
            _LOGGER.debug("Sent data to %s: %s", self.name, frame)
            return True
        except Exception as exception:
            _LOGGER.error("Error sending data to %s: %s", self.name, exception)
            try:
                if self._client is not None:
                    _LOGGER.debug("Disconnecting from %s", self.name)
                    await self._client.disconnect()
            except Exception as exception:
                _LOGGER.error("Error disconnecting from %s: %s", self.name, exception)

            self._reconnect += 1
            self._client = None

        return False