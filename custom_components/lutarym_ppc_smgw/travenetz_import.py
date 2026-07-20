# Integrationsversion: 1.13.0
"""1:1-Import einer TraveNetz/iMSys-CSV-Exportdatei (stündliche

"Energie bezogen"-Werte) in die Langzeit-Statistik dieser Integration.

Anders als history_import.py (das eine ungenaue Quell-Entity über zwei
Ankerpunkte skaliert) übernimmt dieses Modul echte, vom Netzbetreiber
gelieferte Messwerte UNVERÄNDERT - keine Interpolation, keine Skalierung,
keine andere Entity nötig. Einzige Ausnahme: einzelne, vereinzelt
fehlende Stunden INNERHALB des Datenbereichs (Status "F"/"-" in der
Exportdatei) werden linear zwischen den beiden benachbarten echten Werten
interpoliert, damit die Reihe lückenlos bleibt - das ist keine Schätzung
der GRÖSSENORDNUNG, nur ein Lückenschluss zwischen zwei bekannten Punkten.

Erwartetes CSV-Format (TraveNetz-Kundenportal-Export):
    ;;"<Zählernummer> / Energie bezogen (stündlich)";"";"";
    "Uhrzeit von - (in Lokalzeit)";"Uhrzeit - bis (in Lokalzeit)";"Wert";"Einheit";"Status";
    "27.11.2025 - 00:00:00";"27.11.2025 - 01:00:00";"0,489460";"kW";"W";
    ...
Werte sind trotz Einheit "kW" tatsächlich kWh für die jeweilige volle
Stunde (Momentanleistungs-Mittelwert × 1h = Energie dieser Stunde).
Zeitstempel sind deutsche Lokalzeit (Europe/Berlin, inkl. Zeitumstellung)
und werden hier korrekt nach UTC konvertiert.
"""

from __future__ import annotations

import csv
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.core import HomeAssistant

from .history_import import HistoryImportError

_LOGGER = logging.getLogger(__name__)

_BERLIN = ZoneInfo("Europe/Berlin")
_UTC = ZoneInfo("UTC")


def _parse_value(raw: str) -> float | None:
    raw = raw.strip()
    if raw in ("-", ""):
        return None
    return float(raw.replace(",", "."))


def _parse_travenetz_csv_sync(path: str) -> list[tuple[datetime, float]]:
    """Blockierendes Datei-Parsing - MUSS im Executor laufen (siehe

    import_csv_history), nicht direkt im Event-Loop.
    """
    rows: list[tuple[datetime, float]] = []
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f, delimiter=";")
            for i, r in enumerate(reader):
                if i < 2 or len(r) < 5:
                    continue  # Kopfzeilen / leere/kurze Zeilen überspringen
                val = _parse_value(r[2])
                if val is None:
                    continue  # "-" (Status F) - Lücke, wird unten interpoliert
                try:
                    start_local_naive = datetime.strptime(
                        r[0].strip(), "%d.%m.%Y - %H:%M:%S"
                    )
                except ValueError:
                    continue
                start_local = start_local_naive.replace(tzinfo=_BERLIN)
                rows.append((start_local.astimezone(_UTC), val))
    except OSError as err:
        raise HistoryImportError(
            f"CSV-Datei '{path}' konnte nicht gelesen werden: {err}"
        ) from err

    if not rows:
        raise HistoryImportError(
            f"CSV-Datei '{path}' enthält keine verwertbaren Datenzeilen "
            "(erwartetes Format: TraveNetz-Kundenportal-Export, siehe Moduldocstring)."
        )
    rows.sort(key=lambda item: item[0])
    return rows


def _fill_internal_gaps(rows: list[tuple[datetime, float]]) -> dict[datetime, float]:
    """Baut ein lückenloses Stundenraster zwischen dem ersten und letzten

    echten Datenpunkt - einzelne fehlende Stunden DAZWISCHEN (Status "F")
    werden linear zwischen den beiden umgebenden echten Werten
    interpoliert. Lücken vor dem ersten bzw. nach dem letzten echten Punkt
    werden NICHT erzeugt (das wäre Extrapolation, nicht Lückenschluss).
    """
    by_ts = dict(rows)
    first_ts, last_ts = rows[0][0], rows[-1][0]

    filled: dict[datetime, float] = {}
    cur = first_ts
    pending_gap_start: datetime | None = None
    while cur <= last_ts:
        if cur in by_ts:
            if pending_gap_start is not None:
                # Lücke schliessen: linear zwischen dem Wert VOR der Lücke
                # und dem jetzt gefundenen Wert NACH der Lücke interpolieren.
                gap_hours = [
                    t for t in filled_pending_range(pending_gap_start, cur)
                ]
                before_val = by_ts[gap_hours[0] - timedelta(hours=1)] if gap_hours else 0.0
                after_val = by_ts[cur]
                n = len(gap_hours) + 1
                for i, gts in enumerate(gap_hours, start=1):
                    filled[gts] = before_val + (after_val - before_val) * (i / n)
                pending_gap_start = None
            filled[cur] = by_ts[cur]
        else:
            if pending_gap_start is None:
                pending_gap_start = cur
        cur += timedelta(hours=1)

    return filled


def filled_pending_range(start: datetime, stop_exclusive: datetime) -> list[datetime]:
    out = []
    cur = start
    while cur < stop_exclusive:
        out.append(cur)
        cur += timedelta(hours=1)
    return out


async def import_csv_history(
    hass: HomeAssistant,
    *,
    target_statistic_id: str,
    target_name: str,
    csv_path: str,
    start_value_kwh: float = 0.0,
    dry_run: bool = False,
) -> dict:
    """Liest eine TraveNetz-CSV-Exportdatei und schreibt die Werte 1:1

    (kumuliert ab start_value_kwh) in die Langzeit-Statistik der
    Ziel-Entity. Kein Skalieren, keine andere Entity - reine Übernahme
    echter Messwerte.
    """
    rows = await hass.async_add_executor_job(_parse_travenetz_csv_sync, csv_path)
    filled = _fill_internal_gaps(rows)
    timestamps = sorted(filled)

    cumulative = start_value_kwh
    stats: list[StatisticData] = []
    month_summary: dict[str, float] = {}
    for ts in timestamps:
        delta = filled[ts]
        cumulative += delta
        stats.append(
            StatisticData(start=ts, state=round(cumulative, 4), sum=round(cumulative, 4))
        )
        key = f"{ts.year:04d}-{ts.month:02d}"
        month_summary[key] = month_summary.get(key, 0.0) + delta

    summary = {
        "hourly_points": len(stats),
        "csv_path": csv_path,
        "first_timestamp": timestamps[0].isoformat(),
        "last_timestamp": timestamps[-1].isoformat(),
        "start_value_kwh": start_value_kwh,
        "final_computed_kwh": round(cumulative, 4),
        "monthly_breakdown_kwh": {k: round(v, 2) for k, v in sorted(month_summary.items())},
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
    )
    await async_import_statistics(hass, metadata, stats)
    return summary
