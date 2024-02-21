from __future__ import annotations

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry, SOURCE_IMPORT
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.const import CONF_ADDRESS, CONF_MODEL, CONF_NAME
from homeassistant.helpers.device_registry import async_get_registry as async_get_device_registry

from .const import DOMAIN

from .govee_controller import GoveeBluetoothController
from .light import GoveeBleLight

PLATFORMS: list[str] = ["light"]

"""
class Hub:
    def __init__(self, hass: HomeAssistant, address: str, controller: GoveeBluetoothController) -> None:

        self.address = address
        self.controller = controller

        """

async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up Govee devices configured through configuration.yaml."""
    if DOMAIN not in config:
        return True

    configured_addresses = [entry.data['address'] for entry in hass.config_entries.async_entries(DOMAIN)]

    for device_config in config[DOMAIN].get('devices', []):
        address = device_config[CONF_ADDRESS]

        # Skip already configured devices
        if address in configured_addresses:
            continue

        # Create a new config entry. This doesn't set up the device yet; it schedules setup via async_setup_entry
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={'source': SOURCE_IMPORT},
                data=device_config
            )
        )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """
    Set up Govee BLE device from a config entry.

    Args:
        hass (HomeAssistant): The Home Assistant instance.
        entry (ConfigEntry): The config entry representing the Govee BLE device.

    Returns:
        bool: True if the setup was successful, False otherwise.
    """
    address = entry.unique_id
    assert address is not None

    # Initialize or retrieve the shared controller.
    if 'controller' not in hass.data.get(DOMAIN, {}):
        hass.data.setdefault(DOMAIN, {})['controller'] = GoveeBluetoothController(hass, address)

    controller = hass.data[DOMAIN]['controller']

    # controller = hass.data[DOMAIN]["controller"]

    # Use the Bluetooth API to get the BLE device object
    ble_device = bluetooth.async_ble_device_from_address(hass, address.upper(), True)
    if not ble_device:
        raise ConfigEntryNotReady(f"Could not find LED BLE device with address {address}")

    device_registry = await async_get_device_registry(hass)
    hub_device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "controller_" + address)},
        name="Govee Controller",
        manufacturer="AznDibs",
    )
    # Store BLE device and other relevant info in hass.data for use in the platform setup.
    hass.data[DOMAIN][entry.entry_id] = {
        "ble_device": ble_device,
        "address": address,
        "controller": controller,  # Store the controller for use in platform setup.
        "hub_device": hub_device,
    }


    await hass.config_entries.async_forward_entry_setup(entry, ['light'])

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok