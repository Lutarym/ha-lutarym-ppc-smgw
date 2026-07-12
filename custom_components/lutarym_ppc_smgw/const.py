# Integrationsversion: 1.10.0
"""Konstanten für die PPC Smart Meter Gateway (iMSys) Integration."""

# Interner, technischer Bezeichner der Integration. Wird u.a. für
# Entity-IDs (z.B. sensor.lutarym_ppc_smgw_...) und als Schlüssel in
# hass.data verwendet. NICHT ändern - eine Änderung würde alle
# bestehenden Entitäten/Config-Entries verwaisen lassen und eine
# komplette Neueinrichtung erzwingen. Der sichtbare Anzeigename der
# Integration ("PPC Smart Meter Gateway (iMSys) by Lutarym") ist davon
# unabhängig und steht in manifest.json/hacs.json.
DOMAIN = "lutarym_ppc_smgw"

# Schlüssel für die in entry.options gespeicherte Zähler-Auswahl (Liste
# von Zähler-"label"-Strings, siehe api.py:_extract_meter_options).
CONF_METER_IDS = "meter_ids"
# Schlüssel für die in entry.options gespeicherte Auswertungsprofil-
# Auswahl (Liste von Profil-"label"-Strings, siehe
# api.py:list_tariff_profiles). None (Schlüssel fehlt) bedeutet "alle
# gefundenen Profile abrufen", eine leere Liste bedeutet "explizit keine".
CONF_TARIFF_IDS = "tariff_ids"

DEFAULT_SCAN_INTERVAL_SECONDS = 900  # Entspricht dem Ausleseintervall des Gateways (15 Min).

MANUFACTURER = "Power Plus Communications AG"
MODEL = "LTE Smart Meter Gateway"

# Fester Pfad der HAN-CGI-Schnittstelle auf dem Gateway. Wird mit
# "https://" + Host zur vollen URL zusammengesetzt (siehe api.py).
HAN_PATH = "/cgi-bin/hanservice.cgi"

# WICHTIG: Muss manuell synchron zur "version" in manifest.json gehalten werden.
# Wird im Config-Flow-Dialog und als Geräte-Softwareversion angezeigt, damit
# man die installierte Version prüfen kann, auch bevor eine Verbindung
# erfolgreich zustande kommt.
VERSION = "1.10.0"
