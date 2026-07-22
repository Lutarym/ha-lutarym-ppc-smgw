# Integrationsversion: 1.15.0
"""DataUpdateCoordinator für das PPC Smart Meter Gateway.

Ein Update-Zyklus (_async_update_data) entspricht genau einem
vollständigen Login → Abfrage(n) → Logout-Durchlauf gegen das Gateway.
Das Ergebnis ist ein flaches dict, dessen Schlüssel entweder
"<Zähler-Label>::<OBIS-Code>" (Zähler-Messwerte, siehe
METER_OBIS_SEPARATOR) oder "tarif:<Profil-Label>" (Auswertungsprofile,
siehe TARIFF_KEY_PREFIX) sind. sensor.py baut daraus die eigentlichen
Entitäten.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import PPCSmgwAuthError, PPCSmgwClient, PPCSmgwConnectionError, PPCSmgwParsingError
from .const import DOMAIN, MANUFACTURER, MODEL, VERSION

_LOGGER = logging.getLogger(__name__)

# Anzahl aufeinanderfolgender fehlgeschlagener Update-Zyklen, die toleriert
# werden, BEVOR die Entities tatsächlich auf "nicht verfügbar" gesetzt
# werden (siehe _async_update_data). Wichtig für total_increasing-Sensoren
# wie 1-0:1.8.0: wird eine Entity kurzzeitig "nicht verfügbar", scheint
# Home Assistants eigene Langzeit-Statistik-Kompilierung das als möglichen
# Zähler-Reset zu werten und fängt die 'sum'-Berechnung neu bei 0 an,
# sobald die Entity wiederkehrt - beobachtet nach einem einzelnen
# kurzzeitigen "Server disconnected"-Verbindungsfehler. Mit dieser
# Toleranz bleibt die Entity bei EINZELNEN Aussetzern auf ihrem letzten
# bekannten Wert verfügbar, statt die Statistik-Reihe zu gefährden - nur
# bei WIEDERHOLTEN, aufeinanderfolgenden Fehlern (echter, anhaltender
# Ausfall) wird sie wie bisher als nicht verfügbar markiert.
MAX_CONSECUTIVE_FAILURES_BEFORE_UNAVAILABLE = 3

# Präfix für Auswertungsprofil-Sensoren im data-dict, damit sie nicht mit
# Zähler-Messwert-Schlüsseln kollidieren können.
TARIFF_KEY_PREFIX = "tarif:"

# Trennzeichen zwischen Zähler-Label und OBIS-Code im data-dict-Schlüssel,
# z.B. "01005e318002.1lgz0081554715.sm::1-0:2.8.0".
METER_OBIS_SEPARATOR = "::"


def _is_more_current(new: dict, old: dict) -> bool:
    """Entscheidet bei zwei Auswertungsprofilen MIT DEMSELBEN LABEL, welches

    der beiden Registrierungen als "aktueller" gelten soll. Das kommt z.B.
    bei einem Lieferantenwechsel vor: Das SMGW behält ein historisches,
    bereits abgelaufenes Profil unter demselben Anzeigenamen wie das neue,
    aktive Profil (z.B. beide heißen "Bezug 15-Minuten"). Da das Gateway
    KEINE zuverlässige Reihenfolge (aktuell zuerst) liefert, müssen wir
    aktiv entscheiden:

    1. Ein nicht-abgelaufenes Profil gewinnt IMMER gegen ein abgelaufenes.
    2. Bei Gleichstand (beide abgelaufen, beide aktiv, oder Status
       unbekannt) gewinnt das Profil mit dem späteren Beginn der
       Validierungsperiode.
    """
    new_expired = new.get("abgelaufen")
    old_expired = old.get("abgelaufen")
    if new_expired is False and old_expired is not False:
        return True
    if old_expired is False and new_expired is not False:
        return False
    return (new.get("beginn_validierungsperiode") or "") > (
        old.get("beginn_validierungsperiode") or ""
    )


class PPCSmgwCoordinator(DataUpdateCoordinator[dict[str, dict]]):
    """Holt periodisch alle konfigurierten Zählerwerte + Auswertungsprofile.

    WICHTIG:
    - Sowohl die "mid" eines Zählers als auch die "tid" eines
      Auswertungsprofils sind NICHT stabil - sie rotieren bei jedem Login
      neu. Stabil sind nur die sichtbaren Namen ("label"). Deshalb wird
      intern über Labels identifiziert, aber bei JEDEM Update-Zyklus frisch
      die aktuell gültige mid/tid nachgeschlagen.
    - Ein einzelner Zähler kann MEHRERE Messwert-Zeilen gleichzeitig liefern
      (z.B. Bezug/1.8.0 UND Einspeisung/2.8.0) - pro gefundenem OBIS-Code
      wird ein eigener Eintrag (und damit eine eigene Sensor-Entität)
      angelegt, Schlüssel-Format: "<Zähler-Label>::<OBIS-Code>".
    - Das SMGW erlaubt nur eine aktive Session gleichzeitig und synchronisiert
      seine Register offenbar nicht zuverlässig, solange eine alte Session
      nicht sauber per Logout beendet wurde. JEDER Update-Zyklus MUSS daher
      mit einem Logout enden, auch im Fehlerfall (try/finally).
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client: PPCSmgwClient,
        meter_labels: list[str] | None,
        tariff_labels: list[str] | None,
        update_interval: timedelta,
    ) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=update_interval)
        self.client = client
        self.meter_labels = meter_labels  # None = alle am Gateway gefundenen Zähler
        self.tariff_labels = tariff_labels  # None = alle gefundenen Auswertungsprofile
        self.available_meters: list[dict[str, str]] = []
        self.available_tariff_profiles: list[dict[str, str]] = []
        # Siehe MAX_CONSECUTIVE_FAILURES_BEFORE_UNAVAILABLE weiter oben.
        self._consecutive_failures = 0

    async def _async_update_data(self) -> dict[str, dict]:
        token: str | None = None
        try:
            # Ein Login pro Zyklus - der zurückgegebene token bleibt für
            # den kompletten restlichen Zyklus gültig (siehe api.py:login).
            token = await self.client.login()
            self.available_meters = await self.client.list_meters(token)

            # Falls der Nutzer keine explizite Auswahl getroffen hat
            # (meter_labels ist None), werden ALLE aktuell am Gateway
            # gefundenen Zähler abgefragt.
            labels_to_fetch = self.meter_labels or [
                m["label"] for m in self.available_meters
            ]

            data: dict[str, dict] = {}
            for label in labels_to_fetch:
                # mid ist nur für DIESEN Login-Zyklus gültig - deshalb hier
                # jedes Mal frisch aus der gerade abgerufenen Zählerliste
                # nachschlagen, nicht aus einem früheren Zyklus wiederverwenden.
                match = next(
                    (m for m in self.available_meters if m["label"] == label), None
                )
                if match is None:
                    _LOGGER.warning(
                        "SMGW: Zähler '%s' nicht in der aktuellen Zählerliste des "
                        "Gateways gefunden - wird in diesem Zyklus übersprungen.",
                        label,
                    )
                    continue
                readings = await self.client.get_meter_readings(token, match["mid"])
                # Ein Zähler kann mehrere OBIS-Zeilen liefern (1.8.0 UND
                # 2.8.0) - jede wird zu einem eigenen data-Eintrag/Sensor.
                for reading in readings:
                    data[f"{label}{METER_OBIS_SEPARATOR}{reading['obis']}"] = reading

            if not data and labels_to_fetch:
                # Alle konfigurierten Zähler wurden nicht gefunden - meist,
                # weil die Integration nach einem Update mit geänderter
                # Zähler-Identifikation nicht neu eingerichtet wurde. Klare
                # Fehlermeldung statt stillschweigend 0 Entitäten anzuzeigen.
                available_labels = ", ".join(m["label"] for m in self.available_meters) or "(keine)"
                raise UpdateFailed(
                    "Keiner der konfigurierten Zähler "
                    f"({', '.join(labels_to_fetch)}) wurde in der aktuellen "
                    f"Gateway-Zählerliste gefunden (verfügbar: {available_labels}). "
                    "Integration entfernen und neu einrichten."
                )

            # Zusätzlich konfigurierte Auswertungsprofile abrufen (z.B.
            # "Bezug 15-Minuten", "Bezug Monat") und mit eigenem Präfix in
            # dieselbe data-Struktur aufnehmen, damit sensor.py daraus
            # eigene Entitäten anlegen kann. Leere Liste (explizit nichts
            # ausgewählt) bedeutet: keine Auswertungsprofile abrufen.
            self.available_tariff_profiles = await self.client.list_tariff_profiles(token)
            tariff_labels_to_fetch = (
                self.available_tariff_profiles
                if self.tariff_labels is None
                else [
                    p for p in self.available_tariff_profiles
                    if p["label"] in self.tariff_labels
                ]
            )
            for profile in tariff_labels_to_fetch:
                try:
                    value = await self.client.get_tariff_profile_value(
                        token, profile["tid"]
                    )
                except PPCSmgwConnectionError as err:
                    _LOGGER.warning(
                        "SMGW: Auswertungsprofil '%s' konnte nicht abgerufen werden: %s",
                        profile["label"],
                        err,
                    )
                    continue

                key = f"{TARIFF_KEY_PREFIX}{profile['label']}"
                existing = data.get(key)
                if existing is not None:
                    # Zwei (oder mehr) Profile mit demselben Anzeigenamen -
                    # typischerweise ein abgelaufenes historisches Profil
                    # (z.B. vor einem Lieferantenwechsel) neben dem aktuell
                    # aktiven. Siehe _is_more_current(): wir behalten gezielt
                    # das aktuellere statt beliebig das zuletzt abgerufene.
                    if not _is_more_current(value, existing):
                        _LOGGER.debug(
                            "SMGW: Doppeltes Auswertungsprofil-Label '%s' - "
                            "behalte bereits vorhandenes (aktuelleres) Profil, "
                            "verwerfe dieses Duplikat.",
                            profile["label"],
                        )
                        continue
                    _LOGGER.debug(
                        "SMGW: Doppeltes Auswertungsprofil-Label '%s' - ersetze "
                        "vorhandenes Duplikat durch aktuelleres Profil.",
                        profile["label"],
                    )
                data[key] = value

            self._consecutive_failures = 0
            return data
        except PPCSmgwAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (PPCSmgwParsingError, PPCSmgwConnectionError) as err:
            self._consecutive_failures += 1
            if (
                self._consecutive_failures < MAX_CONSECUTIVE_FAILURES_BEFORE_UNAVAILABLE
                and self.data
            ):
                # Toleranz für kurze Aussetzer (siehe Konstante oben): noch
                # nicht als "nicht verfügbar" markieren, sondern die letzten
                # bekannten Werte beibehalten - schützt insbesondere die
                # Langzeit-Statistik von 1-0:1.8.0 vor einem Reset auf 0
                # durch einen einzelnen kurzzeitigen Verbindungsfehler.
                _LOGGER.warning(
                    "SMGW: Update-Zyklus fehlgeschlagen (%d/%d, wird toleriert - "
                    "letzte bekannte Werte bleiben aktiv): %s",
                    self._consecutive_failures,
                    MAX_CONSECUTIVE_FAILURES_BEFORE_UNAVAILABLE,
                    err,
                )
                return self.data
            raise UpdateFailed(str(err)) from err
        finally:
            # IMMER ausloggen, auch bei Fehlern - siehe Klassen-Docstring.
            if token:
                await self.client.logout(token)


def build_device_info(coordinator: PPCSmgwCoordinator, entry: ConfigEntry) -> DeviceInfo:
    """Baut die Geräte-Info-Karte.

    Zentral hier definiert (statt in sensor.py/button.py dupliziert), damit
    alle Entitäten desselben Geräts konsistent dieselbe Geräte-Info
    liefern. Home Assistant erlaubt in der Übersichtskarte nur die drei
    fest beschrifteten Felder "Firmware" (sw_version), "Hardware"
    (hw_version) und "Seriennummer" (serial_number) - "Hardware" passt
    inhaltlich nicht für eine Integrationsversion. Stattdessen wird dafür
    `model_id` genutzt (frei nutzbare Zusatzzeile ohne feste
    "falsche" Beschriftung) - im Unterschied zu `name` wirkt sich das NICHT
    auf die Namen der einzelnen Entitäten aus (die würden sonst alle den
    kompletten Gerätenamen inkl. Versionsangabe als Präfix bekommen).
    Zusätzlich gibt es weiter unten in der Entitätenliste (Diagnose) eine
    eigene "Integrations-Version"-Entität (siehe sensor.py).
    """
    gateway_fw = coordinator.client.firmware_version

    zaehleradresse: str | None = None
    for key, reading in coordinator.data.items():
        if METER_OBIS_SEPARATOR in key and reading.get("zaehleradresse"):
            zaehleradresse = reading["zaehleradresse"]
            break

    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        manufacturer=MANUFACTURER,
        model=MODEL,
        model_id=f"Version: {VERSION}",
        name=f"PPC SMGW ({entry.data[CONF_HOST]})",
        configuration_url=f"https://{entry.data[CONF_HOST]}/",
        sw_version=(f"PPC-Firmware {gateway_fw}" if gateway_fw else "PPC-Firmware unbekannt").upper(),
        serial_number=(zaehleradresse or "unbekannt").upper(),
    )


def gateway_obis_status(coordinator: PPCSmgwCoordinator) -> dict[str, bool]:
    """Liefert für jeden bekannten Zähler-OBIS-Code, ob er im aktuellen

    Datensatz vorhanden ("aktiv") ist. Von den Gateway-Info-Diagnose-
    Sensoren genutzt (siehe sensor.py).
    """
    obis_present: set[str] = set()
    for key, reading in coordinator.data.items():
        if METER_OBIS_SEPARATOR not in key:
            continue
        obis = reading.get("obis")
        if obis:
            obis_present.add(obis)
    return {
        "1-0:1.8.0": "1-0:1.8.0" in obis_present,
        "1-0:2.8.0": "1-0:2.8.0" in obis_present,
    }


def gateway_gueltig_ab(coordinator: PPCSmgwCoordinator) -> str | None:
    """Liefert "Beginn Validierungsperiode" des ersten gefundenen

    Auswertungsprofils (repräsentativ für den Beginn des aktuellen
    HAN-Zugangs). Von den Gateway-Info-Diagnose-Sensoren genutzt.
    """
    for key, profile in coordinator.data.items():
        if key.startswith(TARIFF_KEY_PREFIX) and profile.get("beginn_validierungsperiode"):
            return profile["beginn_validierungsperiode"]
    return None
