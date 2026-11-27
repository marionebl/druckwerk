"""Device registry lookups (externals.py stays free of Home Assistant imports: the hub tests load it)."""

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .externals import VENDOR_DOMAIN


def serial_from_device(hass: HomeAssistant, device_id: str) -> str | None:
    """Serial of a vendor printer device, None for any other id."""
    device = dr.async_get(hass).async_get(device_id)
    for domain, serial in device.identifiers if device else ():
        if domain == VENDOR_DOMAIN:
            return serial
    return None
