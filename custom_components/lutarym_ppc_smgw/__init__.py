# Integrationsversion: 1.10.0
"""PPC Smart Meter Gateway (iMSys) Integration für Home Assistant.

Einstiegspunkt der Integration (von Home Assistant automatisch anhand des
Domain-Namens gefunden). Verantwortlich für: Erzeugen des HTTP-Clients
und des API-Clients, Erzeugen und ersten Abruf des DataUpdateCoordinator,
Weiterreichen an die Plattformen (sensor.py, button.py), sowie sauberes
Auf-/Abräumen beim Hinzufügen/Entfernen der Integration.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.httpx_client import create_async_httpx_client

from .api import PPCSmgwClient
from .const import CONF_METER_IDS, CONF_TARIFF_IDS, DEFAULT_SCAN_INTERVAL_SECONDS, DOMAIN
from .coordinator import PPCSmgwCoordinator

# Von dieser Integration bereitgestellte Entitäts-Plattformen. Wird sowohl
# beim Einrichten (async_forward_entry_setups) als auch beim Entfernen
# (async_unload_platforms) verwendet.
PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BUTTON]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Richtet einen Config Entry (= ein Gateway/Gerät) ein.

    WICHTIG (seit Version 0.13.0): Nutzt `httpx` statt `aiohttp` als
    HTTP-Client, über Home Assistants `create_async_httpx_client`-Hilfe.
    Diese liefert eine DEDIZIERTE (nicht die geteilte) httpx-Instanz pro
    Aufruf - mit eigenem, automatischem Cookie-Jar, den der Client in
    api.py bewusst nutzt (siehe dortiger Moduldocstring für die Begründung
    des Wechsels von aiohttp zu httpx).
    """
    # verify_ssl=False: das Gateway nutzt ein selbstsigniertes Zertifikat,
    # eine echte CA-Prüfung würde hier immer fehlschlagen.
    httpx_client = create_async_httpx_client(hass, verify_ssl=False)

    client = PPCSmgwClient(
        httpx_client,
        entry.data[CONF_HOST],
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
    )

    # None = "noch nichts ausgewählt" bzw. "alle verfügbaren abrufen" -
    # siehe jeweilige Docstrings in coordinator.py, wie None von einer
    # leeren Liste (explizit nichts) unterschieden wird.
    meter_ids = entry.options.get(CONF_METER_IDS)
    tariff_ids = entry.options.get(CONF_TARIFF_IDS)
    coordinator = PPCSmgwCoordinator(
        hass,
        client,
        meter_ids,
        tariff_ids,
        timedelta(seconds=DEFAULT_SCAN_INTERVAL_SECONDS),
    )
    # Erster Abruf synchron beim Setup - schlägt er fehl, bricht das
    # Setup des Config Entry mit einer aussagekräftigen Fehlermeldung ab,
    # statt eine Integration mit leeren/fehlenden Entitäten anzulegen.
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Sorgt dafür, dass eine Änderung der Optionen (Zähler-/
    # Auswertungsprofil-Auswahl über den Options-Flow) die Integration
    # automatisch neu lädt, damit die neue Auswahl sofort greift.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Lädt die Integration neu, wenn sich die Optionen (Zählerauswahl) ändern."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Entfernt einen Config Entry wieder.

    Die httpx-Client-Instanz von create_async_httpx_client wird von Home
    Assistant selbst verwaltet/geschlossen - hier ist kein manuelles
    Schließen nötig (anders als bei der vorherigen aiohttp-Variante mit
    selbst erzeugter Session).
    """
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
