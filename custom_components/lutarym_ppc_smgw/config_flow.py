# Integrationsversion: 1.12.0
"""Config Flow für die PPC Smart Meter Gateway (iMSys) Integration."""

from __future__ import annotations

import html
import logging
from datetime import date
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, OptionsFlow, ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import selector
import httpx

from .api import (
    PPCSmgwAuthError,
    PPCSmgwClient,
    PPCSmgwConnectionError,
    PPCSmgwParsingError,
    async_check_host_reachable,
)
from .const import (
    ATTR_CSV_PATH,
    ATTR_HISTORY_IMPORT,
    ATTR_SOURCE_ENTITY,
    ATTR_START_DATE,
    ATTR_START_VALUE,
    CONF_METER_IDS,
    CONF_TARIFF_IDS,
    DOMAIN,
    TARGET_OBIS,
    VERSION,
)

_LOGGER = logging.getLogger(__name__)


class PPCSmgwConfigFlow(ConfigFlow, domain=DOMAIN):
    """Führt den Benutzer durch die Einrichtung:

    1. Host eingeben -> Erreichbarkeit auf Port 443 prüfen
    2. Zugangsdaten eingeben -> NUR Login testen (Digest-Auth)
    3. Zähler abrufen und auswählen -> eigener Schritt mit eigener
       Fehleranzeige, damit ein Zähler-Problem nicht fälschlich als
       "Zugangsdaten falsch" im Login-Formular erscheint.
    4. Auswertungsprofile abrufen und auswählen -> zeigt ALLE über diese
       HAN-Zugangsdaten sichtbaren Profile (das können je nach
       Messstellenbetreiber auch abgelaufene/historische Profile früherer
       Lieferantenwechsel sein) und lässt den Nutzer wählen, welche als
       Sensoren angelegt werden sollen.
    """

    VERSION = 1

    def __init__(self) -> None:
        self._host: str | None = None
        self._username: str | None = None
        self._password: str | None = None
        self._token: str | None = None
        self._meters: list[dict[str, str]] = []
        self._tariff_profiles: list[dict[str, str]] = []
        self._selected_meter_ids: list[str] = []
        self._selected_tariff_ids: list[str] = []
        # Aktueller Live-Wert von OBIS 1-0:1.8.0 ("Bezug") - wird beim
        # Betreten von async_step_history einmalig ermittelt (siehe dort)
        # und als automatischer Endanker für den optionalen Historien-
        # Import genutzt (siehe const.TARGET_OBIS/history_import.py).
        self._current_1_8_0_value: float | None = None
        self._current_1_8_0_unit: str | None = None
        # WICHTIG: Dieselbe httpx-Client-/PPCSmgwClient-Instanz wird über
        # ALLE Einrichtungsschritte hinweg wiederverwendet (nicht pro
        # Schritt neu erzeugt!). Anders als beim früheren aiohttp-Ansatz
        # (wo Cookie+Token als einfache Strings zwischen unabhängigen
        # Kurz-Sessions weitergereicht werden konnten) steckt die Session
        # bei httpx (Cookie-Jar + Digest-Auth-Zustand) im Client-Objekt
        # selbst - ein neuer Client pro Schritt hätte eine leere Session
        # und würde "keine Zähler gefunden" liefern, obwohl der Login davor
        # erfolgreich war.
        self._httpx_client: httpx.AsyncClient | None = None
        self._client: PPCSmgwClient | None = None

    async def _async_close_client(self) -> None:
        """Schließt den aktuell offenen httpx-Client (falls vorhanden) und

        setzt die Referenzen zurück. Wird sowohl bei Fehlern (damit kein
        Client offen hängen bleibt) als auch am erfolgreichen Ende des
        Flows (nach dem letzten Request) aufgerufen.
        """
        if self._httpx_client is not None:
            await self._httpx_client.aclose()
            self._httpx_client = None
            self._client = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> "ConfigFlowResult":
        """Schritt 1: Nur die Host-Adresse abfragen und Erreichbarkeit prüfen."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]

            if await async_check_host_reachable(host, port=443):
                self._host = host
                return await self.async_step_credentials()

            errors["base"] = "cannot_connect"
            self._host = host  # Eingabe im Formular erhalten

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=self._host or "192.168.1.200"): str,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders={"version": VERSION},
        )

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> "ConfigFlowResult":
        """Schritt 2: Benutzername/Passwort abfragen und NUR den Login testen.

        Hier wird bewusst NICHT die Zählerliste abgerufen - ein Fehler beim
        Auslesen der Zähler ist kein Zugangsdaten-Problem und soll den
        Nutzer hier nicht fälschlich glauben lassen, sein Passwort sei
        falsch.
        """
        errors: dict[str, str] = {}
        debug_info = ""

        if user_input is not None:
            self._username = user_input[CONF_USERNAME]
            self._password = user_input[CONF_PASSWORD]

            await self._async_close_client()  # falls ein vorheriger Versuch noch offen war
            self._httpx_client = httpx.AsyncClient(verify=False)
            self._client = PPCSmgwClient(
                self._httpx_client, self._host, self._username, self._password
            )

            try:
                self._token = await self._client.login()
            except PPCSmgwAuthError as err:
                # Echte Ablehnung durch das Gateway (HTTP 401) -> tatsächlich
                # (meist) falsche Zugangsdaten.
                errors["base"] = "invalid_auth"
                debug_info = html.escape(err.details)
                await self._async_close_client()
            except PPCSmgwParsingError as err:
                # Login war erfolgreich (HTTP 200) - die Zugangsdaten waren
                # also richtig! Es gab nur ein Problem beim Auslesen der
                # Antwortseite. Eigene, korrekte Fehlermeldung dafür.
                errors["base"] = "parsing_error"
                debug_info = html.escape(err.details)
                await self._async_close_client()
            except PPCSmgwConnectionError as err:
                errors["base"] = "cannot_connect"
                debug_info = html.escape(err.details)
                await self._async_close_client()
            else:
                await self.async_set_unique_id(self._host)
                self._abort_if_unique_id_configured()
                return await self.async_step_meters()

        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME, default=self._username or ""): str,
                vol.Required(CONF_PASSWORD, default=self._password or ""): str,
            }
        )
        return self.async_show_form(
            step_id="credentials",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "host": self._host or "",
                "debug_info": debug_info,
                "version": VERSION,
            },
        )

    async def async_step_meters(
        self, user_input: dict[str, Any] | None = None
    ) -> "ConfigFlowResult":
        """Schritt 3: Zählerliste abrufen (eigener Schritt, eigene Fehleranzeige)

        und die gewünschten Zähler auswählen lassen.
        """
        errors: dict[str, str] = {}
        debug_info = ""

        if not self._meters:
            try:
                self._meters = await self._client.list_meters(self._token)
            except (PPCSmgwConnectionError, PPCSmgwAuthError) as err:
                # Kann bei PPCSmgwAuthError passieren, wenn die Session
                # zwischen Login-Schritt und diesem Schritt abgelaufen ist.
                errors["base"] = "cannot_connect"
                debug_info = html.escape(err.details)

        if user_input is not None and self._meters and not errors:
            self._selected_meter_ids = user_input[CONF_METER_IDS]
            return await self.async_step_tariffs()

        if not self._meters:
            if not errors:
                errors["base"] = "no_meters_found"
            return self.async_show_form(
                step_id="meters",
                data_schema=vol.Schema({}),
                errors=errors,
                description_placeholders={"debug_info": debug_info, "version": VERSION},
            )

        options_list = [
            selector.SelectOptionDict(value=m["label"], label=m["label"])
            for m in self._meters
        ]
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_METER_IDS, default=[m["label"] for m in self._meters]
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=options_list, multiple=True)
                )
            }
        )
        return self.async_show_form(
            step_id="meters",
            data_schema=schema,
            description_placeholders={"debug_info": "", "version": VERSION},
        )

    async def async_step_tariffs(
        self, user_input: dict[str, Any] | None = None
    ) -> "ConfigFlowResult":
        """Schritt 4: Auswertungsprofile abrufen und auswählen.

        Zeigt ALLE über diese HAN-Zugangsdaten sichtbaren Auswertungsprofile
        an (inkl. Profil-ID/Gültigkeitszeitraum in der Anzeige, damit man
        abgelaufene/historische Profile früherer Lieferantenwechsel erkennt
        und bei Bedarf abwählen kann). Falls keine Profile gefunden werden,
        wird der Schritt übersprungen (nicht jeder Zähler hat welche).
        """
        errors: dict[str, str] = {}
        debug_info = ""

        if not self._tariff_profiles:
            try:
                self._tariff_profiles = await self._client.list_tariff_profiles(self._token)
            except (PPCSmgwConnectionError, PPCSmgwAuthError) as err:
                errors["base"] = "cannot_connect"
                debug_info = html.escape(err.details)

        if user_input is not None and not errors:
            self._selected_tariff_ids = user_input.get(CONF_TARIFF_IDS, [])
            return await self.async_step_history()

        if not self._tariff_profiles:
            # Kein Fehlerfall - manche Zähler haben schlicht keine
            # Auswertungsprofile über HAN sichtbar. Direkt weiter zum
            # optionalen Historien-Schritt.
            if not errors:
                self._selected_tariff_ids = []
                return await self.async_step_history()
            return self.async_show_form(
                step_id="tariffs",
                data_schema=vol.Schema({}),
                errors=errors,
                description_placeholders={"debug_info": debug_info, "version": VERSION},
            )

        options_list = [
            selector.SelectOptionDict(value=p["label"], label=p["label"])
            for p in self._tariff_profiles
        ]
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_TARIFF_IDS, default=[p["label"] for p in self._tariff_profiles]
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=options_list, multiple=True)
                )
            }
        )
        return self.async_show_form(
            step_id="tariffs",
            data_schema=schema,
            description_placeholders={"debug_info": "", "version": VERSION},
        )

    async def async_step_history(
        self, user_input: dict[str, Any] | None = None
    ) -> "ConfigFlowResult":
        """Schritt 5 (optional): historische Korrektur für OBIS 1-0:1.8.0.

        Zwei Modi, je nachdem was ausgefüllt wird:
          - CSV-Pfad ausgefüllt -> 1:1-Import einer TraveNetz-Exportdatei
            (siehe travenetz_import.py) - echte Messwerte, keine Schätzung.
            Hat Vorrang vor Modus 2, falls beide ausgefüllt wären.
          - Startdatum + Quell-Entity ausgefüllt (kein CSV-Pfad) -> die
            ursprüngliche Variante: eine ungenaue Quell-Entity wird über
            zwei Ankerpunkte skaliert (siehe history_import.py).

        Ermittelt ausserdem den AKTUELLEN Live-Wert von 1-0:1.8.0 direkt
        vom Gateway zur Anzeige (für Modus 2 als automatischer Endanker
        genutzt; für Modus 1 nicht benötigt, da die CSV ihren Endpunkt
        selbst mitbringt).

        ALLE Felder sind optional - bleibt sowohl der CSV-Pfad als auch
        Startdatum/Quell-Entity leer, wird kein Import durchgeführt.
        """
        if self._current_1_8_0_value is None and self._token is not None:
            for meter in self._meters:
                if meter["label"] not in self._selected_meter_ids:
                    continue
                try:
                    readings = await self._client.get_meter_readings(self._token, meter["mid"])
                except (PPCSmgwConnectionError, PPCSmgwAuthError):
                    continue
                match = next((r for r in readings if r.get("obis") == TARGET_OBIS), None)
                if match and match.get("value"):
                    try:
                        self._current_1_8_0_value = float(str(match["value"]).replace(",", "."))
                        self._current_1_8_0_unit = match.get("unit")
                    except ValueError:
                        pass
                    break

        if user_input is not None:
            await self._async_close_client()

            history_payload: dict[str, Any] | None = None
            csv_path = (user_input.get(ATTR_CSV_PATH) or "").strip()
            start_value = user_input.get(ATTR_START_VALUE)

            if csv_path:
                # Modus 1: 1:1-CSV-Import - Vorrang vor Modus 2.
                history_payload = {
                    "mode": "csv",
                    "csv_path": csv_path,
                    "start_value": start_value if start_value is not None else 0.0,
                }
            else:
                raw_start_date = user_input.get(ATTR_START_DATE)
                # WICHTIG: selector.DateSelector() liefert einen ISO-String
                # ("2026-01-01"), KEIN datetime.date-Objekt - muss hier
                # geparst werden, sonst AttributeError auf .year weiter unten.
                start_date = date.fromisoformat(raw_start_date) if raw_start_date else None
                source_entity = user_input.get(ATTR_SOURCE_ENTITY)
                if (
                    start_date
                    and start_value is not None
                    and source_entity
                    and self._current_1_8_0_value is not None
                ):
                    monthly_kwh = {
                        f"{start_date.year:04d}-{m:02d}": user_input[f"month_{m:02d}"]
                        for m in range(1, 13)
                        if user_input.get(f"month_{m:02d}") is not None
                    }
                    history_payload = {
                        "mode": "scaled",
                        "start_date": start_date.isoformat(),
                        "start_value": start_value,
                        "source_entity": source_entity,
                        "monthly_kwh": monthly_kwh,
                        "end_value": self._current_1_8_0_value,
                        "end_unit": self._current_1_8_0_unit,
                    }

            entry_data: dict[str, Any] = {
                CONF_HOST: self._host,
                CONF_USERNAME: self._username,
                CONF_PASSWORD: self._password,
            }
            # Wird von __init__.py:_async_process_pending_history_import
            # EINMALIG verarbeitet und danach wieder aus entry.data
            # entfernt - daher hier als reiner "Auftrag", nicht als
            # Dauerkonfiguration.
            if history_payload:
                entry_data[ATTR_HISTORY_IMPORT] = history_payload

            return self.async_create_entry(
                title=f"PPC SMGW ({self._host})",
                data=entry_data,
                options={
                    CONF_METER_IDS: self._selected_meter_ids,
                    CONF_TARIFF_IDS: self._selected_tariff_ids,
                },
            )

        current_value_text = (
            f"{self._current_1_8_0_value} {self._current_1_8_0_unit or ''}".strip()
            if self._current_1_8_0_value is not None
            else "konnte nicht ermittelt werden"
        )

        kwh_selector = selector.NumberSelector(
            selector.NumberSelectorConfig(
                mode=selector.NumberSelectorMode.BOX, unit_of_measurement="kWh"
            )
        )
        month_fields = {
            vol.Optional(f"month_{m:02d}"): kwh_selector for m in range(1, 13)
        }
        schema = vol.Schema(
            {
                vol.Optional(ATTR_CSV_PATH): str,
                vol.Optional(ATTR_START_VALUE): kwh_selector,
                vol.Optional(ATTR_START_DATE): selector.DateSelector(),
                vol.Optional(ATTR_SOURCE_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                **month_fields,
            }
        )
        return self.async_show_form(
            step_id="history",
            data_schema=schema,
            description_placeholders={
                "version": VERSION,
                "current_value": current_value_text,
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "PPCSmgwOptionsFlow":
        """Verknüpft den "Konfigurieren"-Dialog einer bestehenden Integration

        mit PPCSmgwOptionsFlow (Zähler-/Auswertungsprofil-Auswahl ändern,
        ohne die Integration neu einrichten zu müssen).
        """
        return PPCSmgwOptionsFlow(config_entry)


class PPCSmgwOptionsFlow(OptionsFlow):
    """Erlaubt es, die ausgewählten Zähler und Auswertungsprofile

    nachträglich zu ändern.
    """

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> "ConfigFlowResult":
        """Zeigt/verarbeitet das Formular zur nachträglichen Zähler-/

        Auswertungsprofil-Auswahl. Ruft dafür einmalig Zählerliste UND
        Auswertungsprofile frisch vom Gateway ab (ein Login, ein Client,
        siehe PPCSmgwClient-Klassendocstring in api.py). Schlägt der Abruf
        fehl (z.B. Gateway kurzzeitig nicht erreichbar), wird auf die
        BISHER gespeicherte Auswahl zurückgefallen, damit der Dialog trotz
        Verbindungsproblem nutzbar bleibt (nur mit Fehlermeldung).
        """
        errors: dict[str, str] = {}
        httpx_client = httpx.AsyncClient(verify=False)
        client = PPCSmgwClient(
            httpx_client,
            self._config_entry.data[CONF_HOST],
            self._config_entry.data[CONF_USERNAME],
            self._config_entry.data[CONF_PASSWORD],
        )

        try:
            token = await client.login()
            meters = await client.list_meters(token)
            tariffs = await client.list_tariff_profiles(token)
        except (PPCSmgwAuthError, PPCSmgwParsingError, PPCSmgwConnectionError):
            errors["base"] = "cannot_connect"
            # Fallback: aus der bisher gespeicherten Auswahl "Fake"-Einträge
            # bauen (nur mit "label", ohne mid/tid), damit die
            # Auswahl-Checkboxen im Formular trotzdem etwas anzuzeigen haben.
            meters = [
                {"label": label}
                for label in self._config_entry.options.get(CONF_METER_IDS, [])
            ]
            tariffs = [
                {"label": label}
                for label in self._config_entry.options.get(CONF_TARIFF_IDS, [])
            ]
        finally:
            await httpx_client.aclose()

        if user_input is not None and not errors:
            return self.async_create_entry(
                data={
                    CONF_METER_IDS: user_input[CONF_METER_IDS],
                    CONF_TARIFF_IDS: user_input.get(CONF_TARIFF_IDS, []),
                }
            )

        current_meters = self._config_entry.options.get(
            CONF_METER_IDS, [m["label"] for m in meters]
        )
        current_tariffs = self._config_entry.options.get(
            CONF_TARIFF_IDS, [p["label"] for p in tariffs]
        )
        meter_options = [
            selector.SelectOptionDict(value=m["label"], label=m["label"]) for m in meters
        ]
        tariff_options = [
            selector.SelectOptionDict(value=p["label"], label=p["label"]) for p in tariffs
        ]
        schema = vol.Schema(
            {
                vol.Required(CONF_METER_IDS, default=current_meters): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=meter_options, multiple=True)
                ),
                vol.Optional(CONF_TARIFF_IDS, default=current_tariffs): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=tariff_options, multiple=True)
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
