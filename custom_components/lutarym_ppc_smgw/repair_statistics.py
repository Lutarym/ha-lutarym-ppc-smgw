# Integrationsversion: 1.14.0
"""Repariert einen Statistik-Reset (sum auf 0 gefallen, state aber

korrekt weitergelaufen) OHNE zu raten oder zu interpolieren.

Anders als history_import.py/travenetz_import.py (die fehlende Daten
schätzen/skalieren) korrigiert dieses Modul bereits VORHANDENE, aber
falsch referenzierte Datenpunkte: wenn 'sum' zu einem bestimmten
Zeitpunkt fälschlich auf 0 zurückgesetzt wurde (z.B. durch einen HA-
internen Effekt beim Statistik-Tracking) und von dort an KORREKT relativ
weiterzählt, muss nur der EINE fehlende Offset (der letzte gültige Stand
vor dem Reset) auf jeden betroffenen Punkt addiert werden - exakt, ohne
Schätzung, da die relativen Zuwächse seit dem Reset bereits real und
korrekt aufgezeichnet sind.

WICHTIG (siehe Empfehlung an den Nutzer in der Service-Beschreibung):
Vor dem Ausführen sollte der Recorder pausiert werden
(Aktion "Recorder: Deaktivieren"), damit während der Korrektur kein
neuer, live geschriebener Punkt für dieselbe Stunde dazwischenfunkt -
danach wieder aktivieren.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.core import HomeAssistant

from .history_import import HistoryImportError, _fetch_hourly_sum

_LOGGER = logging.getLogger(__name__)


async def repair_statistics_reset(
    hass: HomeAssistant,
    *,
    target_statistic_id: str,
    target_name: str,
    since: datetime,
    dry_run: bool = False,
) -> dict:
    """Korrigiert alle Punkte AB `since` um den exakten Offset (letzter

    gültiger Punkt VOR `since`) - keine Schätzung, reine Verschiebung
    bereits vorhandener, aber falsch referenzierter Werte.
    """
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)

    lookback_start = since - timedelta(hours=48)
    now = datetime.now(tz=timezone.utc)
    points = await _fetch_hourly_sum(hass, target_statistic_id, lookback_start, now)

    before = [(t, v) for t, v in points if t < since]
    broken = [(t, v) for t, v in points if t >= since]

    if not before:
        raise HistoryImportError(
            f"Kein gültiger Statistik-Punkt vor {since.isoformat()} gefunden - "
            "Offset kann nicht ermittelt werden. Bitte 'since' prüfen oder einen "
            "früheren Zeitpunkt angeben."
        )
    if not broken:
        raise HistoryImportError(
            f"Keine Statistik-Punkte ab {since.isoformat()} gefunden - nichts zu "
            "reparieren. Bitte 'since' prüfen."
        )

    offset = before[-1][1]
    offset_timestamp = before[-1][0]

    stats: list[StatisticData] = []
    for ts, broken_value in broken:
        corrected = round(broken_value + offset, 4)
        stats.append(StatisticData(start=ts, state=corrected, sum=corrected))

    summary = {
        "hourly_points_corrected": len(stats),
        "offset_kwh": offset,
        "offset_reference_timestamp": offset_timestamp.isoformat(),
        "repaired_from": broken[0][0].isoformat(),
        "repaired_to": broken[-1][0].isoformat(),
        "value_before_repair": broken[-1][1],
        "value_after_repair": stats[-1]["sum"] if stats else None,
        "dry_run": dry_run,
    }

    if dry_run or not stats:
        return summary

    metadata = StatisticMetaData(
        has_mean=False,
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=target_name,
        source="recorder",
        statistic_id=target_statistic_id,
        unit_of_measurement="kWh",
        unit_class="energy",
    )
    # WICHTIG: async_import_statistics ist @callback (synchron), siehe
    # travenetz_import.py/history_import.py für Details - nicht awaiten.
    async_import_statistics(hass, metadata, stats)
    return summary
