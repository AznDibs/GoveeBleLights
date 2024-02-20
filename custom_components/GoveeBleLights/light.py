from __future__ import annotations
from typing import Any, Callable, Dict, List, Optional, Tuple

import asyncio
import logging
_LOGGER = logging.getLogger(__name__)

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

    ble_device: Optional[BLEDevice] = None

    _attr_has_entity_name = True
    _attr_name = None
    _attr_color_mode = ColorMode.RGB
    _attr_min_color_temp_kelvin = 2000
    _attr_max_color_temp_kelvin = 9000
    _attr_supported_color_modes = {
            ColorMode.COLOR_TEMP,
            ColorMode.RGB,
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
        self._brightness = None
        self._rgb_color = [0,0,0]
        self._client = None
        
        self._attr_extra_state_attributes = {}

        # dirty attributes to be updated
        self._dirty_state = False
        self._dirty_brightness = False
        self._dirty_rgb_color = False

        self._reconnect = 0
        self._last_update = time.time()
        self._ping_roll = 0
        self._keep_alive_task = None

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


        self._state = True
        self._dirty_state = True
        self._attr_extra_state_attributes["dirty_state"] = self._dirty_state

        if ATTR_BRIGHTNESS in kwargs:
            brightness = kwargs.get(ATTR_BRIGHTNESS, 255)

            self._brightness = brightness
            self._dirty_brightness = True
            self._attr_extra_state_attributes["dirty_brightness"] = self._dirty_brightness

        if ATTR_RGB_COLOR in kwargs:
            red, green, blue = kwargs.get(ATTR_RGB_COLOR)

            self._rgb_color = [red, green, blue]
            self._dirty_rgb_color = True
            self._attr_extra_state_attributes["dirty_rgb_color"] = self._dirty_rgb_color

        self._keep_alive_task = asyncio.create_task(self._send_packets_thread())
        # if self.client:
            # await self._disconnect()
        

    async def async_turn_off(self, **kwargs) -> None:
        if self._keep_alive_task:
            self._keep_alive_task.cancel()
            _LOGGER.debug("Cancelled keep alive task for %s", self.name)

        self._state = False
        self._dirty_state = True
        self._attr_extra_state_attributes["dirty_state"] = self._dirty_state

        self._keep_alive_task = asyncio.create_task(self._send_packets_thread())


    async def _send_power(self):
        """Send the power state to the device."""
        try:
            return await self._send_bluetooth_data(LedCommand.POWER, [0x1 if self._state else 0x0])
        
        except Exception as exception:
            _LOGGER.error("Error sending power to %s: %s", self.name, exception)
            self._attr_extra_state_attributes["error"] = f"Error sending power to {self.name}: {exception}"
        
        return False


    async def _send_brightness(self, brightness):
        _packet = [ModelInfo.get_led_mode(self.model)]

        max_brightness = ModelInfo.get_brightness_max(self.model)
        brightness = int(brightness/ 255 * max_brightness)
        _packet.append(brightness)
        try:
            return await self._send_bluetooth_data(LedCommand.BRIGHTNESS, _packet)
    
        except Exception as exception:
            _LOGGER.error("Error sending brightness to %s: %s", self.name, exception)
            self._attr_extra_state_attributes["error"] = f"Error sending brightness to {self.name}: {exception}"
        
        return False
    

    async def _send_rgb_color(self, red, green, blue):
        _packet = [ModelInfo.get_led_mode(self.model)]
        if ModelInfo.get_led_mode(self.model) == LedMode.MODE_1501:
                _packet.extend([
                    0x01,
                    red, 
                    green, 
                    blue,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                    0xFF,
                    0x74,
                ])
        else:
            _packet.extend([red, green, blue])
        
        try:
            return await self._send_bluetooth_data(LedCommand.COLOR, _packet)
        
        except Exception as exception:
            _LOGGER.error("Error sending color to %s: %s", self.name, exception)
            self._attr_extra_state_attributes["error"] = f"Error sending color to {self.name}: {exception}"
        
        return False


    async def _send_packets_thread(self):
        """Send the packets to the device."""
        while True:
            """Connect to the device and send the packets."""
            try:
                """Connect to the device."""
                if not await self._connect():
                    time.sleep(2)
                    continue

                _changed = True # send mqtt packet once mqtt is implemented
            
                """Send the packets."""
                if self._dirty_state:
                    if not await self._send_power():
                        asyncio.sleep(1);
                        continue

                    self._dirty_state = False
                elif self._dirty_brightness:
                    if not await self._send_brightness(self._brightness):
                        asyncio.sleep(1);
                        continue

                    self._dirty_brightness = False
                elif self._dirty_rgb_color:
                    if not await self._send_rgb_color(*self._rgb_color):
                        asyncio.sleep(1);
                        continue

                    self._dirty_rgb_color = False
                else:
                    """Keep alive, send a packet every 1 second."""
                    _changed = False # no mqtt packet if no change

                    if (time.time() - self._last_update) >= 1:
                        _async_res = False
                        self._ping_roll += 1

                        if self._ping_roll % 3 == 0 or self._state == 0:
                            _async_res = await self._send_power();
                        elif self._ping_roll % 3 == 1:
                            _async_res = await self._send_brightness(self._brightness);
                        elif self._ping_roll % 3 == 2:
                            _async_res = await self._send_rgb_color(*self._rgb_color);
                    
                    asyncio.sleep(0.1)
                    continue
                
                if _changed:
                    # send mqtt packet
                    pass
                
                asyncio.sleep(0.01)


            except Exception as exception:
                _LOGGER.error("Error sending packets to %s: %s", self.name, exception)

                try:
                    if self._client is not None:
                        await self._client.disconnect()
                except Exception as exception:
                    pass

                self._client = None

                time.sleep(2)




    async def _disconnect(self):
        if self._client:
            _LOGGER.debug("Disconnecting from %s", self.name)
            self._attr_extra_state_attributes["connection_status"] = "Disconnecting"
            await self._client.disconnect()
            self._client = None




    async def _connect(self):

        if self._client != None:
            return self._client

        def disconnected_callback(client):
            if self._client == client:
                self._client = None
                _LOGGER.debug("Disconnected from %s", self.name)
                self._attr_extra_state_attributes["connection_status"] = "Disconnected"
            """Callback for when the client disconnects."""
            # Schedule the coroutine from a synchronous context
            asyncio.run_coroutine_threadsafe(
                self._handle_disconnect(),
                self.hass.loop
            )

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

            return self._client
        except Exception as exception:
            _LOGGER.error("Failed to connect to %s: %s", self.name, exception)
            self._attr_extra_state_attributes["connection_status"] = "Disconnected"
            self._client = None

        return None
    
    async def _handle_disconnect(self):
        """Handle the device's disconnection."""
        self._attr_extra_state_attributes["connection_status"] = "Disconnected"
        # self.async_write_ha_state()
    

    async def _send_bluetooth_data(self, cmd, payload):
        if not isinstance(cmd, int):
            raise ValueError('Invalid command')
        if not isinstance(payload, bytes) and not (isinstance(payload, list) and all(isinstance(x, int) for x in payload)):
            raise ValueError('Invalid payload')
        if len(payload) > 17:
            raise ValueError('Payload too long')

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
            await self._client.write_gatt_char(UUID_CONTROL_CHARACTERISTIC, frame) # , False)
            self._last_update = time.time()
            _LOGGER.debug("Sent data to %s: %s", self.name, frame)
            return True
        except Exception as exception:
            _LOGGER.error("Failed to send data to %s: %s", self.name, exception)
            
            try:
                if self._client is not None:
                    _LOGGER.debug("Disconnecting from %s", self.name)
                    await self._client.disconnect()
            except Exception as exception:
                pass

            self._reconnect += 1
            self._client = None
        return False

        client = await self._connect()
        
        if client and client.is_connected:
            _LOGGER.debug("Connected to %s", self.name)
            
            self._attr_extra_state_attributes["client"] = client
            await client.write_gatt_char(UUID_CONTROL_CHARACTERISTIC, frame, False)
        else:
            _LOGGER.debug("Failed to connect to %s", self.name)
            self._attr_extra_state_attributes["connection_status"] = "Disconnected"
            self._attr_extra_state_attributes["client"] = None
        
        current_time_string = time.strftime("%c")
        self._attr_extra_state_attributes["update_status"] = f"updated: {current_time_string}"
        # self.async_write_ha_state()  # Reflect the attribute changes immediately