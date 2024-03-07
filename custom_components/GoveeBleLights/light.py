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
from homeassistant.util.dt import utcnow


from .const import DOMAIN
from .models import LedCommand, LedMode, ControlMode, ModelInfo
from .kelvin_rgb import kelvin_to_rgb
from .throttled_updater import ThrottledUpdater

UUID_CONTROL_CHARACTERISTIC = '00010203-0405-0607-0809-0a0b0c0d2b11'

PARALLEL_UPDATES = 1

MAX_ACTIVE_DEVICES = 1

# devices trying to connect
queued_devices = {}
# devices connected and sending packets
active_devices = {}
# devices connected but not needed
stale_devices = {}

def clamp(value, min_value, max_value):
    return max(min(value, max_value), min_value)

def is_in_range(value, min_value, max_value):
    return value >= min_value and value <= max_value

async def async_setup_entry(
        hass: HomeAssistant,
        config_entry: ConfigEntry, 
        async_add_entities: AddEntitiesCallback,
    ) -> None:
    """Set up the light from a config entry."""
    light = hass.data[DOMAIN][config_entry.entry_id]
    #bluetooth setup
    ble_device = bluetooth.async_ble_device_from_address(hass, light.address.upper(), False)
    async_add_entities([GoveeBluetoothLight(hass, light, ble_device, config_entry)])

class GoveeBluetoothLight(LightEntity):
    MAX_RECONNECT_ATTEMPTS = 0
    INITIAL_RECONNECT_DELAY = 1 # seconds
    DEVICE_PING_INTERVAL = 30 # seconds

    ble_device: Optional[BLEDevice] = None

    _attr_has_entity_name = True
    _attr_name = None
    _attr_color_mode = ColorMode.RGB
    _attr_min_color_temp_kelvin = ModelInfo.get("default", "min_kelvin")
    _attr_max_color_temp_kelvin = ModelInfo.get("default", "max_kelvin")
    _attr_supported_color_modes = {
            ColorMode.COLOR_TEMP,
            ColorMode.RGB,
        }
    


    def __init__(self, hass, light, ble_device, config_entry: ConfigEntry) -> None:
        """Initialize an bluetooth light."""
        self._mac = light.address
        _LOGGER.debug("Config entry data: %s", config_entry.data)
        self._hass = hass
        self._model = config_entry.data.get("model", "default")
        self._name = config_entry.data.get("name", self._model + "-" + self._mac.replace(":", "")[-4:])
        self._ble_device = ble_device
        self._state = None
        self._is_on = False
        self._brightness = 255
        self._temperature = 4000
        self._rgb_color = [255,255,255]
        self._client = None
        self._control_mode = ColorMode.RGB
        
        self._attr_extra_state_attributes = {}

        self.updater = ThrottledUpdater(self._hass, self.update_light_state)

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
        self._last_device_update = time.time()
        self._ping_roll = 0
        self._keep_alive_task = None

    @property
    def should_poll(self):
        """Return False as this entity should not be polled."""
        return False

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
    
    def update(self):
        """Update the light state."""
        return True

    def set_state_attr(self, key, value):
        self._attr_extra_state_attributes[key] = value
        record_last_device_update = {
            "connection_status" : True,
            "ble_status" : True,
            "command_sent" : True,
        }
        if key in record_last_device_update:
            self._attr_extra_state_attributes["last_" + key] = utcnow().isoformat()

    async def update_light_state(self):
        """
            Update the light state.
            Should be called:
            - after successful ble commands
            - ble connection/disconnection
            - every few keep alive packets
        """
        self._attr_extra_state_attributes["last_state_update"] = utcnow().isoformat()
        self.async_write_ha_state()

    async def async_added_to_hass(self):
        """Run when entity about to be added to hass."""
        _LOGGER.debug("Adding %s", self.name)
        # await self._connect()
        _LOGGER.debug("Initialized %s", self.name)

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
        
        self._temp_state = True
        self._dirty_state = True
        self.set_state_attr("dirty_state", self._dirty_state)


        if ATTR_BRIGHTNESS in kwargs:
            brightness = kwargs.get(ATTR_BRIGHTNESS, 255)

            self._temp_brightness = brightness
            self._dirty_brightness = True
            self.set_state_attr("dirty_brightness", self._dirty_brightness)
        elif ATTR_BRIGHTNESS_PCT in kwargs:
            brightness_pct = kwargs.get(ATTR_BRIGHTNESS_PCT, 100)

            self._temp_brightness = brightness_pct * 255 / 100
            self._dirty_brightness = True
            self.set_state_attr("dirty_brightness", self._dirty_brightness)

        if ATTR_RGB_COLOR in kwargs:
            red, green, blue = kwargs.get(ATTR_RGB_COLOR)

            self._control_mode = ControlMode.COLOR
            self._temp_rgb_color = [red, green, blue]
            self._dirty_color = True
            self.set_state_attr("dirty_rgb_color", self._dirty_color)
        elif ATTR_COLOR_TEMP in kwargs:
            color_temp = kwargs.get(ATTR_COLOR_TEMP)
            kelvin = (1000000 / color_temp)

            kelvin = clamp(kelvin, self._attr_min_color_temp_kelvin, self._attr_max_color_temp_kelvin)
            self._control_mode = ControlMode.TEMPERATURE
            self._temperature = int(kelvin)
            self._dirty_color = True
            red, green, blue = kelvin_to_rgb(kelvin)
            self._temp_rgb_color = [red, green, blue]
            self.set_state_attr("dirty_rgb_color", self._dirty_color)

        await self._cancel_packets_thread()
        self._keep_alive_task = self._hass.async_create_task(self._send_packets_thread())

        

    async def async_turn_off(self, **kwargs) -> None:

        self._temp_state = False
        self._dirty_state = True
        self.set_state_attr("dirty_state", self._dirty_state)

        await self._cancel_packets_thread()
        self._keep_alive_task = self._hass.async_create_task(self._send_packets_thread())


    async def _send_power(self, power):
        """Send the power state to the device."""
        self.set_state_attr("power_data", 0x1 if power else 0x0)

        try:
            return await self._send_bluetooth_data(LedCommand.POWER, [0x1 if power else 0x0])
        
        except Exception as exception:
            _LOGGER.error("Error sending power to %s: %s", self.name, exception)

        return False


    async def _send_brightness(self, brightness):
        """Send the brightness to the device."""
        brightness_max = ModelInfo.get(self.model, "brightness_max") #ModelInfo.get_brightness_max(self.model)
        brightness = math.floor(brightness / 255 * brightness_max)
        self.set_state_attr("brightness_data", brightness)

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


        model_min_kelvin = ModelInfo.get(self.model, "min_kelvin")
        model_max_kelvin = ModelInfo.get(self.model, "max_kelvin")

        if self._control_mode == ControlMode.TEMPERATURE and is_in_range(self._temperature, model_min_kelvin, model_max_kelvin):
            brightness = self._brightness / 255
            _R = _G = _B = 0xFF;
            _TK = int(self._temperature);
            # self._temp_rgb_color = [_R,_G,_B]
            pass
        self.set_state_attr("rgb_color_data", [_R,_G,_B])

        self.set_state_attr("control_mode", self._control_mode)
        self.set_state_attr("temperature", self._temperature)
        
        if not isinstance(_R, int) or _R < 0 or _R > 255:
            return ValueError("Invalid r");
        if not isinstance(_G, int) or _G < 0 or _G > 255:
            return ValueError("Invalid g");
        if not isinstance(_B, int) or _B < 0 or _B > 255:
            return ValueError("Invalid b");
        

        try:
            led_mode = ModelInfo.get(self.model, "led_mode")
            _payload = [led_mode]

            if led_mode == LedMode.MODE_1501:
                    
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
                        (-1 >> 8) & 0xFF,
                        -1 & 0xFF,
                        0,
                    ])
                    """
                    _payload.extend([
                        0x01,
                        _R,
                        _G,
                        _B,
                        0x00,
                        0x00,
                        0x00,
                        0x00,
                        0x00,
                        0xFF,
                        0x74,
                    ])"""
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

    def _add_to_queue(self):
        """Add the device to the queue."""
        if active_devices.get(self._ble_device):
            active_devices.pop(self._ble_device)
        if stale_devices.get(self._ble_device):
            stale_devices.pop(self._ble_device)
        if not queued_devices.get(self._ble_device):
            queued_devices[self._ble_device] = self
        self.set_state_attr("ble_status", "Queued")
    
    def _add_to_active(self):
        """Add the device to the active devices."""
        if queued_devices.get(self._ble_device):
            queued_devices.pop(self._ble_device)
        if stale_devices.get(self._ble_device):
            stale_devices.pop(self._ble_device)
        if not active_devices.get(self._ble_device):
            active_devices[self._ble_device] = self
        self.set_state_attr("ble_status", "Active")

    def _add_to_stale(self):
        """Add the device to the stale devices."""
        if queued_devices.get(self._ble_device):
            queued_devices.pop(self._ble_device)
        if active_devices.get(self._ble_device):
            active_devices.pop(self._ble_device)
        if not stale_devices.get(self._ble_device):
            stale_devices[self._ble_device] = self
        self.set_state_attr("ble_status", "Stale")

    def _remove_device_from_dicts(self):
        """Remove the device from the dictionaries."""
        if queued_devices.get(self._ble_device):
            queued_devices.pop(self._ble_device)
        if active_devices.get(self._ble_device):
            active_devices.pop(self._ble_device)
        if stale_devices.get(self._ble_device):
            stale_devices.pop(self._ble_device)
        self.set_state_attr("ble_status", "Removed")

    def _should_close_stale_connection(self):
        """Check if the stale connection should be closed."""
        if self._ping_roll > 0:
            return True
        
        if self._client is None or not self._client.is_connected:
            return True

        _total_devices = len(active_devices) + len(stale_devices)
        _LOGGER.debug("Total devices: %s", _total_devices)
        if _total_devices < MAX_ACTIVE_DEVICES:
            return False
        else:
            return True

    async def _cancel_packets_thread(self):
        """Cancel the packets thread."""
        try:
            _max_timeout = 10 
            _max_attempts = 50
            _attempts = 0
            _start_time = time.time()
            while self._keep_alive_task is not None and not self._keep_alive_task.done():
                if time.time() >= _start_time + _max_timeout or _attempts >= _max_attempts:
                    _LOGGER.error("Failed to cancel keep alive task for %s: %s attempts", self.name, _attempts)
                    return False
                _attempts += 1
                self._keep_alive_task.cancel()
                _LOGGER.debug("Cancelling keep alive task for %s", self.name)
                await asyncio.sleep(0.1 + (0.1 * _attempts))
            _LOGGER.debug("Cancelled keep alive task for %s", self.name)

            self._reconnect = 0
            self._keep_alive_task = None

            return True
        except Exception as exception:
            _LOGGER.error("Error cancelling keep alive task for %s: %s", self.name, exception)
            return False

    async def _send_packets_thread(self):
        """Send the packets to the device."""
        task_running = True
        self._reconnect = 0

        while task_running:
            """Connect to the device and send the packets."""
            try:

                # if client was previously not connected, this is their first time. set to queue. otherwise it's an active/stale device that's still running
                if not active_devices.get(self._ble_device) and not stale_devices.get(self._ble_device):
                    self._add_to_queue()
                    


                """Connect to the device."""
                if self.is_dirty() and not await self._connect():
                    self._reconnect += 1

                    if self._reconnect > self.MAX_RECONNECT_ATTEMPTS:
                        _LOGGER.error("Failed to connect to %s after %s attempts", self.name, self.MAX_RECONNECT_ATTEMPTS)
                        task_running = False
                        await self._handle_disconnect()
                        await asyncio.sleep(1)
                        continue

                    self._add_to_queue()
                    jitter = self._reconnect * random.uniform(0.1, 0.3)
                    await asyncio.sleep(0.9 + jitter)
                    continue

                _changed = True # send mqtt packet once mqtt is implemented

                if self.is_dirty():
                    self._add_to_active()
            
                """Send the packets."""
                if self._dirty_state:
                    self._state = self._temp_state
                    if not await self._send_power(self._temp_state):
                        await asyncio.sleep(0.9);
                        continue

                    self._dirty_state = False
                    self.set_state_attr("dirty_state", self._dirty_state)
                elif self._dirty_brightness:
                    self._brightness = self._temp_brightness
                    if not await self._send_brightness(self._temp_brightness):
                        await asyncio.sleep(0.9);
                        continue

                    self._dirty_brightness = False
                    self.set_state_attr("dirty_brightness", self._dirty_brightness)
                elif self._dirty_color:
                    self._rgb_color = self._temp_rgb_color
                    if not await self._send_rgb_color():
                        await asyncio.sleep(0.9);
                        continue

                    self._dirty_color = False
                    self.set_state_attr("dirty_rgb_color", self._dirty_color)
                else:
                    """Keep alive, send a packet every 1 second."""
                    _changed = False # no mqtt packet if no change

                    # if connection failed or other device needs to connect, disconnect
                    if self._should_close_stale_connection():
                        _LOGGER.debug("Closing stale connection for %s", self.name)
                        task_running = False

                        self._ping_roll = 0
                        self.set_state_attr("ping_roll", self._ping_roll)
                        await self._handle_disconnect()
                    elif self._ping_roll % 30 == 0:
                        self.updater.request_update()

                    if self._state:
                        self._add_to_stale()
                    

                    if (time.time() - self._last_device_update) >= 1:
                        _async_res = False
                        self._ping_roll += 1

                        _ping_interval = self.DEVICE_PING_INTERVAL * 3

                        if self._ping_roll % _ping_interval == 0 or self._state == 0:
                            _async_res = await self._send_power(self._state);
                        elif self._ping_roll % _ping_interval == 1:
                            _async_res = await self._send_brightness(self._brightness);
                        elif self._ping_roll % _ping_interval == 2:
                            _async_res = await self._send_rgb_color();
                        
                        self.set_state_attr("ping_roll", self._ping_roll)
                        

                    await asyncio.sleep(0.1)
                    continue
                
                if _changed:
                    # send mqtt packet
                    pass
                
                await asyncio.sleep(0.01)


            except Exception as exception:
                _LOGGER.error("Error sending packets to %s: %s", self.name, exception)
                task_running = False
                await self._handle_disconnect()
                await asyncio.sleep(1)





    async def _connect(self):

        if self._client != None and self._client.is_connected:
            return self._client
        
        if self._client != None:
            await self._handle_disconnect()
        
        if self._client != None:
            _LOGGER.error("Aborted connect: Failed to disconnect from inactive %s", self.name)
            return None

        def disconnected_callback(client):
            _LOGGER.debug("Disconnected from %s", self.name)
            self._remove_device_from_dicts()
            self._client = None
            self.set_state_attr("connection_status", "Disconnected")
            self._reconnect = 0
            self._ping_roll = 0
            self.set_state_attr("ping_roll", self._ping_roll)
            self._last_device_update = time.time()
            self.updater.request_update()

        try:
            '''
                https://developers.home-assistant.io/docs/bluetooth/
                Use a connection timeout of at least ten (10) seconds as BlueZ must resolve services when connecting to a new or updated device for the first time.
            '''

            self.set_state_attr("connection_status", "Establishing")

            client = await bleak_retry_connector.establish_connection(
                BleakClient,
                self._ble_device, 
                self.unique_id,
                disconnected_callback=disconnected_callback,
                # timeout=10.0  # Adjust the timeout as needed
            )
            self._client = client
            self.set_state_attr("connection_status", "Connected")

            return self._client.is_connected
        except Exception as exception:
            self.set_state_attr("connection_status", "Failed to connect")
            self._remove_device_from_dicts()
            _LOGGER.error("Failed to connect to %s: %s", self.name, exception)


        return None
    
    async def _handle_disconnect(self):
        """Handle the device's disconnection."""
        try:
            if self._client is not None and not self._client.is_connected:
                self._remove_device_from_dicts()
                _LOGGER.debug("Disconnecting from %s", self.name)
                self.set_state_attr("connection_status", "Disconnecting")
                await self._client.disconnect()
                self._client = None
                _LOGGER.debug("Disconnected from %s", self.name)
                self.set_state_attr("connection_status", "Disconnected")
                self.updater.request_update()
        except Exception as exception:
            self.set_state_attr("connection_status", "Failed to disconnect")
            _LOGGER.error("Error disconnecting from %s: %s", self.name, exception)
        
    

    async def _send_bluetooth_data(self, cmd, payload):
        if not isinstance(cmd, int):
            raise ValueError('Invalid command')
        if not isinstance(payload, bytes) and not (isinstance(payload, list) and all(isinstance(x, int) for x in payload)):
            raise ValueError('Invalid payload')
        if len(payload) > 17:
            raise ValueError('Payload too long')

        _LOGGER.debug("Sending command %s with payload %s to %s", cmd, payload, self.name)

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
            self._last_device_update = time.time()
            self.set_state_attr("command_sent", cmd)
            _LOGGER.debug("Sent data to %s: %s", self.name, frame)
            self.updater.request_update()
            return True
        except Exception as exception:
            _LOGGER.error("Error sending data to %s: %s", self.name, exception)
            await self._handle_disconnect()


        return False