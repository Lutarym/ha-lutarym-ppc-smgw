# Integrationsversion: 1.11.0
"""PPC Smart Meter Gateway (iMSys) Integration für Home Assistant.

Einstiegspunkt der Integration (von Home Assistant automatisch anhand des
Domain-Namens gefunden). Verantwortlich für: Erzeugen des HTTP-Clients
und des API-Clients, Erzeugen und ersten Abruf des DataUpdateCoordinator,
Weiterreichen an die Plattformen (sensor.py, button.py), Registrieren des
Service `lutarym_ppc_smgw.import_history` (siehe history_import.py),
sowie sauberes Auf-/Abräumen beim Hinzufügen/Entfernen der Integration.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.httpx_client import create_async_httpx_client

from .api import PPCSmgwClient
from .const import (
    ATTR_DRY_RUN,
    ATTR_MONTHLY_KWH,
    ATTR_SOURCE_ENTITY,
    ATTR_START_DATE,
    ATTR_START_VALUE,
    ATTR_TARGET_ENTITY,
    CONF_METER_IDS,
    CONF_TARIFF_IDS,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DOMAIN,
    SERVICE_IMPORT_HISTORY,
    TARGET_OBIS,
)
from .coordinator import METER_OBIS_SEPARATOR, PPCSmgwCoordinator
from .history_import import HistoryImportError, import_history

_LOGGER = logging.getLogger(__name__)

# Von dieser Integration bereitgestellte Entitäts-Plattformen. Wird sowohl
# beim Einrichten (async_forward_entry_setups) als auch beim Entfernen
# (async_unload_platforms) verwendet.
PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BUTTON]

IMPORT_HISTORY_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_START_DATE): cv.date,
        vol.Required(ATTR_START_VALUE): vol.Coerce(float),
        vol.Required(ATTR_SOURCE_ENTITY): cv.entity_id,
        vol.Optional(ATTR_MONTHLY_KWH): {cv.string: vol.Coerce(float)},
        vol.Optional(ATTR_TARGET_ENTITY): cv.entity_id,
        vol.Optional(ATTR_DRY_RUN, default=False): cv.boolean,
    }
)


async def async_setup(hass: HomeAssistant, _config: dict) -> bool:
    """Läuft genau EINMAL beim Start, unabhängig davon wie viele Gateways

    (Config Entries) konfiguriert sind - der richtige Ort, um einen
    domain-weiten Service EINMAL zu registrieren (nicht pro Entry, das
    würde bei mehreren Gateways zu einem Registrierungs-Konflikt führen).
    """
    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_HISTORY,
        lambda call: _async_handle_import_history(hass, call),
        schema=IMPORT_HISTORY_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


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

    Der Service `import_history` bleibt bewusst registriert, solange
    MINDESTENS EIN Config Entry existiert - erst wenn das letzte Gateway
    entfernt wird, wird er auch wieder abgemeldet (siehe async_setup, wo
    er domain-weit einmalig registriert wird).
    """
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, SERVICE_IMPORT_HISTORY)
    return unload_ok


def _find_target_1_8_0_entity(hass: HomeAssistant, registry: er.EntityRegistry) -> str:
    """Sucht über ALLE geladenen Gateways hinweg die Entity für

    OBIS 1-0:1.8.0 ("Bezug") - siehe const.TARGET_OBIS. Wird nur genutzt,
    wenn der Service ohne explizites `target_entity` aufgerufen wird.
    """
    matches: list[str] = []
    for entry_id, coordinator in hass.data.get(DOMAIN, {}).items():
        for key, reading in coordinator.data.items():
            if METER_OBIS_SEPARATOR not in key:
                continue
            if reading.get("obis") != TARGET_OBIS:
                continue
            unique_id = f"{entry_id}_{key}"
            entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
            if entity_id:
                matches.append(entity_id)

    if not matches:
        raise HistoryImportError(
            f"Keine Entity mit OBIS {TARGET_OBIS} gefunden - bitte 'target_entity' "
            "im Service-Aufruf explizit angeben."
        )
    if len(matches) > 1:
        raise HistoryImportError(
            f"Mehrere Entities mit OBIS {TARGET_OBIS} gefunden ({', '.join(matches)}) "
            "- bitte 'target_entity' im Service-Aufruf explizit angeben."
        )
    return matches[0]


def _coordinator_for_entity(
    hass: HomeAssistant, registry: er.EntityRegistry, entity_id: str
) -> PPCSmgwCoordinator | None:
    """Findet den Coordinator, dem eine Entity gehört (für einen frischen

    Refresh vor dem Auslesen des Endwerts) - None, falls die Entity nicht
    zu einem geladenen Gateway dieser Integration gehört (z.B. wenn
    `target_entity` bewusst auf eine fremde Entity zeigt).
    """
    entity_entry = registry.async_get(entity_id)
    if entity_entry is None or entity_entry.config_entry_id is None:
        return None
    return hass.data.get(DOMAIN, {}).get(entity_entry.config_entry_id)


async def _async_handle_import_history(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Handler für den Service `lutarym_ppc_smgw.import_history`.

    Ermittelt die Ziel-Entity (explizit angegeben oder automatisch über
    OBIS 1-0:1.8.0 gefunden), aktualisiert deren Coordinator für einen
    möglichst frischen Endwert, und übergibt an history_import.py für die
    eigentliche Berechnung/den Schreibvorgang.
    """
    registry = er.async_get(hass)
    target_entity = call.data.get(ATTR_TARGET_ENTITY) or _find_target_1_8_0_entity(
        hass, registry
    )

    coordinator = _coordinator_for_entity(hass, registry, target_entity)
    if coordinator is not None:
        await coordinator.async_request_refresh()

    state = hass.states.get(target_entity)
    if state is None or state.state in (None, "unknown", "unavailable"):
        raise HistoryImportError(
            f"Ziel-Entity '{target_entity}' hat aktuell keinen gültigen Zustand - "
            "Import abgebrochen."
        )
    try:
        raw_value = float(str(state.state).replace(",", "."))
    except ValueError as err:
        raise HistoryImportError(
            f"Zustand von '{target_entity}' ('{state.state}') ist keine Zahl."
        ) from err

    unit = (state.attributes.get("unit_of_measurement") or "").strip().lower()
    if unit == "wh":
        end_value_kwh = raw_value / 1000
    elif unit in ("kwh", ""):
        end_value_kwh = raw_value
    else:
        _LOGGER.warning(
            "SMGW Historien-Import: unbekannte Einheit '%s' an '%s' - Wert wird "
            "unverändert als kWh interpretiert.",
            state.attributes.get("unit_of_measurement"),
            target_entity,
        )
        end_value_kwh = raw_value

    friendly_name = state.attributes.get("friendly_name", target_entity)

    summary = await import_history(
        hass,
        target_statistic_id=target_entity,
        target_name=friendly_name,
        start_date=call.data[ATTR_START_DATE],
        start_value_kwh=call.data[ATTR_START_VALUE],
        source_entity_id=call.data[ATTR_SOURCE_ENTITY],
        end_value_kwh=end_value_kwh,
        monthly_kwh=call.data.get(ATTR_MONTHLY_KWH),
        dry_run=call.data[ATTR_DRY_RUN],
    )
    summary["target_entity"] = target_entity
    return summary
