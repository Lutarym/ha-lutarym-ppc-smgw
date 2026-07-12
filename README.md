# PPC Smart Meter Gateway (iMSys) – Home Assistant Integration (by Lutarym)

**Aktuelle Version: 1.10.0** – siehe [CHANGELOG.md](CHANGELOG.md) für alle
Änderungen im Detail.

Custom Component für Home Assistant zum Auslesen eines **PPC LTE Smart
Meter Gateways** (SMGW) über dessen lokale **HAN-Schnittstelle**
(`/cgi-bin/hanservice.cgi`), wie es bei intelligenten Messsystemen (iMSys)
in Deutschland zum Einsatz kommt.

Die Integration meldet sich per HTTP Digest Auth am Gateway an, liest
Zählerstände ("Zählerprofil") sowie optional Auswertungsprofile aus und
legt dafür ein eigenes **Gerät** in der Home-Assistant-Geräteverwaltung
an – mit je einer Entität pro Messwert/Feld.

---

## Inhalt

- [Features](#features)
- [Voraussetzungen](#voraussetzungen)
- [Installation](#installation)
- [Einrichtung](#einrichtung)
- [Angelegte Entitäten](#angelegte-entitäten)
- [Bekannte Einschränkungen](#bekannte-einschränkungen)
- [Fehlersuche / Troubleshooting](#fehlersuche--troubleshooting)
- [Architektur (für Mitwirkende)](#architektur-für-mitwirkende)
- [Ein Update veröffentlichen (für Repo-Betreuer)](#ein-update-veröffentlichen-für-repo-betreuer)
- [Lizenz](#lizenz)

---

## Features

- Config Flow über die HA-Oberfläche (kein YAML nötig)
- Erreichbarkeitsprüfung der Gateway-IP auf Port 443, **bevor** Zugangsdaten
  abgefragt werden
- Auswahl, welche am Gateway gefundenen Zähler als Sensoren angelegt werden
  sollen
- Optionaler Abruf von Auswertungsprofilen (z. B. "Bezug 15-Minuten",
  "Bezug Monat"), inkl. automatischer Erkennung/Bevorzugung des jeweils
  aktuellen (nicht abgelaufenen) Profils bei mehreren gleichnamigen
  Einträgen (z. B. nach einem Lieferantenwechsel)
- "Gateway neu starten"-Button (Selbsttest-Auslösung), falls die lokale
  HAN-Synchronisation des Gateways einmal hängen sollte
- Diagnose-Entitäten für Firmware, Integrationsversion, IP-Adresse,
  Benutzername, OBIS-Aktiv-Status (1.8.0/2.8.0) und Gültigkeitsbeginn des
  HAN-Zugangs
- Nachträgliche Änderung der Zähler-/Auswertungsprofil-Auswahl über den
  Options-Dialog der Integration
- Deutsche und englische Oberfläche

## Voraussetzungen

- Ein PPC LTE Smart Meter Gateway mit **freigeschalteter HAN-Schnittstelle**
  (viele Messstellenbetreiber schalten diese nicht automatisch frei -
  ggf. explizit beim Messstellenbetreiber (MSB) beantragen)
- **Aktuelle** HAN-Zugangsdaten (Benutzername + Passwort) von deinem
  Messstellenbetreiber - siehe unbedingt den Hinweis zu veralteten
  Zugangsdaten unter [Fehlersuche](#fehlersuche--troubleshooting)
- Dein Home-Assistant-Host muss das Gateway im Netzwerk erreichen können
  (das Gateway hat oft eine feste IP ohne DHCP im eigenen Subnetz - ggf.
  muss dein HA-Host eine zweite, statische IP in diesem Subnetz bekommen).
  Bei **TraveNetz AG** werden die iMSys-Gateways z. B. unter der festen
  IP `172.20.0.1` angesprochen (abweichend vom Gateway-Standardwert
  `192.168.1.200`) - bei anderen Messstellenbetreibern kann die Adresse
  abweichen.

## Installation

### Über HACS (empfohlen)

1. HACS → Menü (⋮) → **Benutzerdefinierte Repositories**
2. Dieses Repository (`https://github.com/Lutarym/ha-lutarym-ppc-smgw`)
   als **Integration** hinzufügen
3. "PPC Smart Meter Gateway (iMSys) by Lutarym" installieren
4. Home Assistant neu starten

### Manuell

1. Den Ordner `custom_components/lutarym_ppc_smgw/` in deinen
   `custom_components`-Ordner kopieren
2. Home Assistant neu starten

## Einrichtung

**Einstellungen → Geräte & Dienste → Integration hinzufügen** → nach
"PPC Smart Meter Gateway" oder "iMSys" suchen.

1. IP-Adresse/Hostname des Gateways eingeben (wird auf Port 443 geprüft,
   bevor es weitergeht)
2. HAN-Benutzername + Passwort eingeben (reiner Digest-Auth-Login-Test,
   noch kein Zähler-Abruf - ein Fehler hier bedeutet wirklich falsche
   Zugangsdaten oder ein nicht erreichbares Gateway)
3. Zähler auswählen, die als Sensoren angelegt werden sollen
4. Optional: Auswertungsprofile auswählen (falls am Gateway vorhanden)

Zähler-/Auswertungsprofil-Auswahl lässt sich später jederzeit über
**Einstellungen → Geräte & Dienste → PPC Smart Meter Gateway →
Konfigurieren** ändern (Benutzername/Passwort dort NICHT änderbar -
dafür Integration löschen und neu einrichten).

## Angelegte Entitäten

Pro ausgewähltem Zähler:

- **Werte-Sensor** je gefundenem OBIS-Code (z. B. `1.8.0`, `2.8.0`) mit
  dem eigentlichen Zählerstand
- Diagnose-Felder dazu: Zähler-ID, Name, Beschreibung, Kommunikationstyp,
  Protokoll-Typ/-Version, Ausleseintervall, Abfrageversuche,
  Zähleradresse, Medium, Zeitstempel, Ist valide, Signiert
  (eichrechtskonformer Ablesewert)

Pro ausgewähltem Auswertungsprofil:

- Platzhalter-Entität (zeigt "unbekannt" - Auswertungsprofile liefern
  keinen eigenen Messwert, nur Konfigurationsdaten)
- Diagnose-Felder: Profil-ID, TAF-Typ, OBIS, Messgröße, Register-/
  Abrechnungsperiode, Vorhaltezeit, Beginn/Ende Gültigkeit, Abgelaufen,
  Alias, Zählpunkt, Tarifstufen, Tag Beginn

Einmal pro Gerät:

- **Gateway neu starten** (Button)
- Firmware, Integrations-Version, IP-Adresse, Benutzername,
  1.8.0 Aktiv, 2.8.0 Aktiv, Zugang gültig ab (Diagnose-Sensoren)

## Bekannte Einschränkungen

- Das SMGW liefert i. d. R. nur alle 15 Minuten einen neuen Messwert –
  kein Echtzeitwert. Das Standard-Poll-Intervall der Integration beträgt
  daher ebenfalls 15 Minuten (900 Sekunden).
- Das Gateway erlaubt laut Beobachtung **nur eine aktive Session
  gleichzeitig**. Läuft parallel eine andere Integration/App mit
  denselben Zugangsdaten gegen dasselbe Gateway, kann es zu vereinzelten
  Verbindungsfehlern kommen.
- Getestet gegen die per HAN-Schnittstelle beobachtete Formular-Logik
  dieses PPC-Gateway-Modells. Bei abweichender Firmware kann sich das
  HTML-Layout unterscheiden – bitte in diesem Fall ein Issue mit der
  Fehlermeldung öffnen.
- Nicht jeder Zähler/Zählpunkt liefert über HAN beide Register (1.8.0
  Bezug UND 2.8.0 Einspeisung) - manche Messstellenbetreiber schalten
  standardmäßig nur eines davon frei. Fehlt eine Entität, beim MSB
  nachfragen, ob das jeweils andere Register im HAN-Ausgabeprofil
  aktiviert werden kann.

## Fehlersuche / Troubleshooting

### Zählerstand wirkt "eingefroren" (ändert sich nie)

Die häufigste tatsächliche Ursache dafür ist überraschend: **veraltete
HAN-Zugangsdaten.** Nach einem Lieferanten- oder
Messstellenbetreiberwechsel vergibt der MSB oft neue HAN-Zugangsdaten -
der Login mit den **alten** Zugangsdaten funktioniert dabei oft weiterhin
fehlerfrei (kein 401, keine Fehlermeldung!), liefert aber dauerhaft nur
den letzten Stand zum Zeitpunkt des Wechsels, nicht mehr aktuelle Werte.

Prüfen:
1. Im Kundenportal deines Messstellenbetreibers nachsehen, welcher
   HAN-Benutzername dort **aktuell** als gültig hinterlegt ist.
2. Mit dem in der Integration hinterlegten Benutzernamen vergleichen
   (siehe Diagnose-Entität "Benutzername" am Gerät).
3. Bei Abweichung: Integration löschen und mit den aktuellen
   Zugangsdaten neu einrichten.

### "Gateway neu starten"-Button

Falls "Zugang gültig ab" / Zeitstempel sich über einen längeren Zeitraum
gar nicht mehr bewegen, obwohl die Zugangsdaten nachweislich aktuell
sind: Den "Gateway neu starten"-Button drücken. Das löst einen
Selbsttest/Neustart der lokalen HAN-Schnittstelle des Gateways aus (nicht
des Zählers oder der Backend-Anbindung beim Netzbetreiber) und kann eine
hängende Synchronisation wieder in Gang bringen.

### Login schlägt fehl ("Anmeldung fehlgeschlagen" trotz HTTP 200)

- Zugangsdaten nochmal exakt per Copy-Paste (nicht abtippen) aus dem
  Passwort-Manager/Kundenportal übernehmen - Tippfehler bei diesen langen
  Benutzernamen/Passwörtern sind die häufigste Ursache.
- Prüfen, ob parallel eine andere Integration/App mit denselben
  Zugangsdaten aktiv gegen dasselbe Gateway pollt (nur eine Session
  gleichzeitig erlaubt) - testweise deaktivieren und 1-2 Minuten warten.

### 2.8.0 (Einspeisung) fehlt, obwohl PV/Batterie vorhanden

Siehe [Bekannte Einschränkungen](#bekannte-einschränkungen) - das ist in
der Regel eine Freischaltungsfrage beim Messstellenbetreiber, keine
Fehlkonfiguration der Integration.

## Architektur (für Mitwirkende)

- HTTP-Client: `httpx.AsyncClient` (über Home Assistants
  `create_async_httpx_client`-Hilfsfunktion), NICHT `aiohttp`. Wichtig:
  Digest-Auth (`httpx.DigestAuth`) wird bei **jedem einzelnen Request**
  neu durchlaufen, nicht nur beim initialen Login - siehe Docstring in
  `api.py` für den Hintergrund dieser Entscheidung.
- HTML-Parsing: reine Regex-Extraktion (kein `BeautifulSoup`/zusätzliche
  Abhängigkeit), da das Gateway teils fehlerhaftes/nicht-striktes HTML
  liefert.
- `coordinator.py`: `DataUpdateCoordinator`, ein Update-Zyklus = ein
  vollständiger Login → Abfrage(n) → Logout-Durchlauf.
- `build_device_info()` zentral in `coordinator.py`, um Geräte-Info
  konsistent über `sensor.py` und `button.py` hinweg bereitzustellen.

- `brand/icon.png` (256×256) und `brand/icon@2x.png` (512×512) - eigenes
  Integrations-Icon, seit Home Assistant 2026.3 direkt im Integrations-
  ordner unterstützt (kein Pull Request an ein externes Repository mehr
  nötig). Bei älteren HA-Versionen wird einfach kein Icon angezeigt.

## Ein Update veröffentlichen (für Repo-Betreuer)

Damit HACS ein neues Update erkennt, reicht das Hochladen der Dateien auf
GitHub allein **nicht** aus - HACS orientiert sich an **GitHub Releases**,
nicht am neuesten Commit. Bei jeder neuen Version:

1. Geänderte Dateien auf GitHub aktualisieren (z. B. per GitHub Desktop:
   committen & pushen).
2. **Wichtig:** `hacs.json` im Hauptverzeichnis hat ein **eigenes**
   `"name"`-Feld, unabhängig von `custom_components/lutarym_ppc_smgw/
   manifest.json` - bei Namensänderungen beide Dateien aktualisieren.
3. Auf GitHub einen neuen **Release** anlegen (Releases → Create a new
   release), Tag passend zur **aktuellen** Version in `manifest.json`
   (z. B. bei `"version": "1.2.3"` den Tag `v1.2.3`).
4. In HACS bei der Integration: ⋮ → **Redownload**, danach Home Assistant
   neu starten.

## Lizenz

Kein Open-Source-Lizenz im klassischen Sinn - Nutzung/Installation über
HACS oder manuell ist ausdrücklich erlaubt, Kopieren, Verändern oder
Weiterverbreiten des Quellcodes NICHT. Siehe [LICENSE](LICENSE) für den
vollständigen Text.
