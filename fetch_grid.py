"""
fetch_grid.py
=============
Senior Data Engineer ETL script for the GB Grid Dashboard.

Ingests GB electricity telemetry from three public streams into a local
SQLite warehouse and exports compact static JSON files for a front-end
dashboard (e.g. a map + timeline visualisation).

Data sources
------------
1. NESO Carbon Intensity API (national)  - https://api.carbonintensity.org.uk
   - GET /generation/{from}/{to}  -> 30-min fuel mix percentages
   - GET /intensity/{from}/{to}   -> 30-min carbon intensity (gCO2/kWh)
2. Elexon Insights Solution API (BMRS)   - https://data.elexon.co.uk/bmrs/api/v1
   - GET /system/frequency                        -> system frequency (Hz)
   - GET /balancing/settlement/system-prices/{d}   -> net imbalance volume (MW)
   - GET /demand/actual/total                      -> actual total demand (MW)
3. NESO Regional Carbon Intensity API    - https://api.carbonintensity.org.uk/regional
   - GET /regional -> current half-hour snapshot for all GB DNO regions

Design notes
------------
This script is intended to run periodically (e.g. via cron every 30 minutes).
Each run fetches a small recent window of data (default: last 2 days) and
UPSERTs it into SQLite, which is idempotent thanks to PRIMARY KEY conflict
handling. An optional `--backfill-days` flag can be used on a first run to
pull a longer history (chunked to respect each API's max date-range limits).
After ingestion, the six export JSON files are regenerated from whatever
history currently exists in the database.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

import requests

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "grid_telemetry.db"
EXPORT_DIR = BASE_DIR / "exports"

CARBON_INTENSITY_BASE = "https://api.carbonintensity.org.uk"
ELEXON_BASE = "https://data.elexon.co.uk/bmrs/api/v1"

REQUEST_TIMEOUT = 10  # seconds - hard timeout per HTTP request
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2

# Per-API max date-range limits (days) to keep requests within documented caps.
CARBON_INTENSITY_CHUNK_DAYS = 14
DEMAND_CHUNK_DAYS = 7
FREQUENCY_CHUNK_DAYS = 1  # per-minute data - keep chunks small

# The 14 GB DNO regions have regionid 1-14 (15-17 are England/Scotland/Wales
# aggregates and are excluded from the regional_latest snapshot).
DNO_REGION_MIN_ID = 1
DNO_REGION_MAX_ID = 14

FUEL_COLUMNS = ("gas", "nuclear", "wind", "solar", "biomass", "hydro", "imports")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(BASE_DIR / "fetch_grid.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("fetch_grid")


# --------------------------------------------------------------------------
# HTTP helper
# --------------------------------------------------------------------------
def http_get(url: str, params: Optional[dict] = None) -> Any:
    """GET a URL with a hard 10s timeout and bounded retries. Returns parsed JSON."""
    last_exc: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(
                url,
                params=params,
                timeout=REQUEST_TIMEOUT,
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            logger.warning(
                "Request failed (attempt %d/%d) GET %s params=%s: %s",
                attempt, MAX_RETRIES, url, params, exc,
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    logger.error("Exhausted retries for GET %s params=%s", url, params)
    raise last_exc  # type: ignore[misc]


def iso(dt: datetime) -> str:
    """Format a UTC datetime in the ISO8601 form expected by the Carbon Intensity API."""
    return dt.strftime("%Y-%m-%dT%H:%MZ")


def chunk_date_range(start: datetime, end: datetime, chunk_days: int) -> Iterator[tuple[datetime, datetime]]:
    """Yield (chunk_start, chunk_end) windows covering [start, end], each <= chunk_days."""
    cursor = start
    step = timedelta(days=chunk_days)
    while cursor < end:
        chunk_end = min(cursor + step, end)
        yield cursor, chunk_end
        cursor = chunk_end


def daterange_days(start: datetime, end: datetime) -> Iterator[datetime]:
    """Yield each calendar day (UTC midnight) from start to end inclusive."""
    cursor = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    end_day = datetime(end.year, end.month, end.day, tzinfo=timezone.utc)
    while cursor <= end_day:
        yield cursor
        cursor += timedelta(days=1)


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS settlement_national (
    timestamp TEXT PRIMARY KEY,
    carbon_intensity REAL,
    gas_mw REAL,
    nuclear_mw REAL,
    wind_mw REAL,
    solar_mw REAL,
    biomass_mw REAL,
    hydro_mw REAL,
    imports_mw REAL,
    total_demand_mw REAL
);

CREATE TABLE IF NOT EXISTS system_balancing (
    timestamp TEXT PRIMARY KEY,
    system_frequency REAL,
    balancing_volume_mw REAL
);

CREATE TABLE IF NOT EXISTS regional_latest (
    region_id INTEGER PRIMARY KEY,
    dno_region TEXT,
    carbon_intensity REAL,
    index_level TEXT,
    top_fuel TEXT
);
"""


@contextmanager
def db_connection() -> Iterator[sqlite3.Connection]:
    """Yield a SQLite connection wrapped in an atomic transaction (commit/rollback)."""
    conn = sqlite3.connect(DB_PATH, timeout=REQUEST_TIMEOUT)
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with db_connection() as conn:
        conn.executescript(SCHEMA)
    logger.info("Database schema ensured at %s", DB_PATH)


def upsert_national(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    conn.executemany(
        """
        INSERT INTO settlement_national
            (timestamp, carbon_intensity, gas_mw, nuclear_mw, wind_mw, solar_mw,
             biomass_mw, hydro_mw, imports_mw, total_demand_mw)
        VALUES (:timestamp, :carbon_intensity, :gas, :nuclear, :wind, :solar,
                :biomass, :hydro, :imports, :total_demand_mw)
        ON CONFLICT(timestamp) DO UPDATE SET
            carbon_intensity = COALESCE(excluded.carbon_intensity, carbon_intensity),
            gas_mw = COALESCE(excluded.gas_mw, gas_mw),
            nuclear_mw = COALESCE(excluded.nuclear_mw, nuclear_mw),
            wind_mw = COALESCE(excluded.wind_mw, wind_mw),
            solar_mw = COALESCE(excluded.solar_mw, solar_mw),
            biomass_mw = COALESCE(excluded.biomass_mw, biomass_mw),
            hydro_mw = COALESCE(excluded.hydro_mw, hydro_mw),
            imports_mw = COALESCE(excluded.imports_mw, imports_mw),
            total_demand_mw = COALESCE(excluded.total_demand_mw, total_demand_mw)
        """,
        rows,
    )
    return len(rows)


def upsert_balancing(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    conn.executemany(
        """
        INSERT INTO system_balancing (timestamp, system_frequency, balancing_volume_mw)
        VALUES (:timestamp, :system_frequency, :balancing_volume_mw)
        ON CONFLICT(timestamp) DO UPDATE SET
            system_frequency = COALESCE(excluded.system_frequency, system_frequency),
            balancing_volume_mw = COALESCE(excluded.balancing_volume_mw, balancing_volume_mw)
        """,
        rows,
    )
    return len(rows)


def replace_regional(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    conn.execute("DELETE FROM regional_latest")
    conn.executemany(
        """
        INSERT INTO regional_latest (region_id, dno_region, carbon_intensity, index_level, top_fuel)
        VALUES (:region_id, :dno_region, :carbon_intensity, :index_level, :top_fuel)
        """,
        rows,
    )
    return len(rows)


# --------------------------------------------------------------------------
# Fetchers - National generation mix + carbon intensity
# --------------------------------------------------------------------------
def fetch_generation_mix(start: datetime, end: datetime) -> dict[str, dict]:
    """Fetch fuel-mix percentages keyed by settlement 'from' timestamp."""
    by_timestamp: dict[str, dict] = {}
    for chunk_start, chunk_end in chunk_date_range(start, end, CARBON_INTENSITY_CHUNK_DAYS):
        url = f"{CARBON_INTENSITY_BASE}/generation/{iso(chunk_start)}/{iso(chunk_end)}"
        try:
            payload = http_get(url)
        except requests.exceptions.RequestException:
            logger.error("Skipping generation-mix chunk %s -> %s due to request failure", chunk_start, chunk_end)
            continue
        for entry in payload.get("data", []):
            ts = entry.get("from")
            mix = {item["fuel"]: item["perc"] for item in entry.get("generationmix", [])}
            by_timestamp[ts] = mix
        logger.info("Fetched generation mix for %s -> %s (%d periods)", chunk_start, chunk_end, len(payload.get("data", [])))
    return by_timestamp


def fetch_carbon_intensity(start: datetime, end: datetime) -> dict[str, float]:
    """Fetch actual/forecast carbon intensity keyed by settlement 'from' timestamp."""
    by_timestamp: dict[str, float] = {}
    for chunk_start, chunk_end in chunk_date_range(start, end, CARBON_INTENSITY_CHUNK_DAYS):
        url = f"{CARBON_INTENSITY_BASE}/intensity/{iso(chunk_start)}/{iso(chunk_end)}"
        try:
            payload = http_get(url)
        except requests.exceptions.RequestException:
            logger.error("Skipping intensity chunk %s -> %s due to request failure", chunk_start, chunk_end)
            continue
        for entry in payload.get("data", []):
            ts = entry.get("from")
            intensity = entry.get("intensity", {})
            value = intensity.get("actual")
            if value is None:
                value = intensity.get("forecast")
            by_timestamp[ts] = value
        logger.info("Fetched carbon intensity for %s -> %s (%d periods)", chunk_start, chunk_end, len(payload.get("data", [])))
    return by_timestamp


def fetch_total_demand(start: datetime, end: datetime) -> dict[str, float]:
    """Fetch actual total demand (MW) from Elexon, keyed by half-hour timestamp."""
    by_timestamp: dict[str, float] = {}
    for chunk_start, chunk_end in chunk_date_range(start, end, DEMAND_CHUNK_DAYS):
        url = f"{ELEXON_BASE}/demand/actual/total"
        params = {"from": iso(chunk_start), "to": iso(chunk_end)}
        try:
            payload = http_get(url, params=params)
        except requests.exceptions.RequestException:
            logger.error("Skipping demand chunk %s -> %s due to request failure", chunk_start, chunk_end)
            continue
        records = payload.get("data", payload) if isinstance(payload, dict) else payload
        if not isinstance(records, list):
            records = []
        for record in records:
            start_time = record.get("startTime") or record.get("settlementDate")
            demand = record.get("demand")
            if start_time is not None and demand is not None:
                by_timestamp[normalise_timestamp(start_time)] = demand
        logger.info("Fetched total demand for %s -> %s (%d records)", chunk_start, chunk_end, len(records))
    return by_timestamp


def normalise_timestamp(value: str) -> str:
    """Normalise a datetime string to the 'YYYY-MM-DDThh:mmZ' format used as the DB key."""
    text = value.replace("+00:00", "Z")
    if text.endswith("Z") and "." in text:
        text = text.split(".")[0] + "Z"
    # Trim seconds if present, e.g. 2024-01-01T00:00:00Z -> 2024-01-01T00:00Z
    if len(text) > 17 and text[16] == ":":
        text = text[:16] + "Z"
    return text


def fetch_national_range(start: datetime, end: datetime) -> list[dict]:
    """Merge generation mix, carbon intensity, and total demand into national rows."""
    mix_by_ts = fetch_generation_mix(start, end)
    intensity_by_ts = fetch_carbon_intensity(start, end)
    demand_by_ts = fetch_total_demand(start, end)

    all_timestamps = set(mix_by_ts) | set(intensity_by_ts)
    rows = []
    for ts in all_timestamps:
        mix = mix_by_ts.get(ts, {})
        row = {"timestamp": ts, "carbon_intensity": intensity_by_ts.get(ts)}
        for fuel in FUEL_COLUMNS:
            row[fuel] = mix.get(fuel)
        row["total_demand_mw"] = demand_by_ts.get(ts)
        rows.append(row)
    return rows


# --------------------------------------------------------------------------
# Fetchers - System balancing (Elexon frequency + net imbalance volume)
# --------------------------------------------------------------------------
def fetch_system_frequency(start: datetime, end: datetime) -> dict[str, float]:
    by_timestamp: dict[str, float] = {}
    for chunk_start, chunk_end in chunk_date_range(start, end, FREQUENCY_CHUNK_DAYS):
        url = f"{ELEXON_BASE}/system/frequency"
        params = {"from": iso(chunk_start), "to": iso(chunk_end)}
        try:
            payload = http_get(url, params=params)
        except requests.exceptions.RequestException:
            logger.error("Skipping frequency chunk %s -> %s due to request failure", chunk_start, chunk_end)
            continue
        records = payload.get("data", payload) if isinstance(payload, dict) else payload
        if not isinstance(records, list):
            records = []
        for record in records:
            ts_raw = record.get("measurementTime") or record.get("startTime")
            freq = record.get("frequency")
            if ts_raw is not None and freq is not None:
                ts = align_to_half_hour(ts_raw)
                by_timestamp[ts] = freq  # latest reading in the half-hour wins
        logger.info("Fetched system frequency for %s -> %s (%d readings)", chunk_start, chunk_end, len(records))
    return by_timestamp


def fetch_balancing_volumes(start: datetime, end: datetime) -> dict[str, float]:
    """Fetch net imbalance volume (MW) per settlement period via the Elexon DISEBSP endpoint."""
    by_timestamp: dict[str, float] = {}
    for day in daterange_days(start, end):
        settlement_date = day.strftime("%Y-%m-%d")
        url = f"{ELEXON_BASE}/balancing/settlement/system-prices/{settlement_date}"
        try:
            payload = http_get(url)
        except requests.exceptions.RequestException:
            logger.error("Skipping balancing volumes for %s due to request failure", settlement_date)
            continue
        records = payload.get("data", payload) if isinstance(payload, dict) else payload
        if not isinstance(records, list):
            records = []
        for record in records:
            volume = record.get("netImbalanceVolume")
            period = record.get("settlementPeriod")
            start_time = record.get("startTime")
            if start_time is not None:
                ts = normalise_timestamp(start_time)
            elif period is not None:
                ts = iso(day + timedelta(minutes=30 * (int(period) - 1)))
            else:
                continue
            if volume is not None:
                by_timestamp[ts] = volume
        logger.info("Fetched balancing volumes for %s (%d periods)", settlement_date, len(records))
    return by_timestamp


def align_to_half_hour(ts_raw: str) -> str:
    """Floor a timestamp to its enclosing half-hour settlement period."""
    text = ts_raw.replace("Z", "").split(".")[0]
    dt = datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
    floored_minute = 0 if dt.minute < 30 else 30
    dt = dt.replace(minute=floored_minute, second=0, microsecond=0)
    return iso(dt)


def fetch_balancing_range(start: datetime, end: datetime) -> list[dict]:
    freq_by_ts = fetch_system_frequency(start, end)
    volume_by_ts = fetch_balancing_volumes(start, end)
    all_timestamps = set(freq_by_ts) | set(volume_by_ts)
    return [
        {
            "timestamp": ts,
            "system_frequency": freq_by_ts.get(ts),
            "balancing_volume_mw": volume_by_ts.get(ts),
        }
        for ts in all_timestamps
    ]


# --------------------------------------------------------------------------
# Fetchers - Regional carbon intensity snapshot
# --------------------------------------------------------------------------
def fetch_regional_latest() -> list[dict]:
    url = f"{CARBON_INTENSITY_BASE}/regional"
    payload = http_get(url)
    entries = payload.get("data", [])
    if not entries:
        return []
    regions = entries[0].get("regions", [])
    rows = []
    for region in regions:
        region_id = region.get("regionid")
        if region_id is None or not (DNO_REGION_MIN_ID <= region_id <= DNO_REGION_MAX_ID):
            continue  # skip England/Scotland/Wales aggregates (15-17)
        mix = region.get("generationmix", [])
        top_fuel = max(mix, key=lambda item: item.get("perc", 0))["fuel"] if mix else None
        rows.append(
            {
                "region_id": region_id,
                "dno_region": region.get("dnoregion"),
                "carbon_intensity": region.get("intensity", {}).get("forecast"),
                "index_level": region.get("intensity", {}).get("index"),
                "top_fuel": top_fuel,
            }
        )
    logger.info("Fetched regional snapshot for %d DNO regions", len(rows))
    return rows


# --------------------------------------------------------------------------
# Export - compact JSON timelines + regional snapshot
# --------------------------------------------------------------------------
TIMELINE_COLUMNS = [
    "timestamp", "carbon_intensity", "gas_mw", "nuclear_mw", "wind_mw", "solar_mw",
    "biomass_mw", "hydro_mw", "imports_mw", "total_demand_mw",
    "system_frequency", "balancing_volume_mw",
]

TIMELINE_QUERY = f"""
SELECT n.timestamp, n.carbon_intensity, n.gas_mw, n.nuclear_mw, n.wind_mw, n.solar_mw,
       n.biomass_mw, n.hydro_mw, n.imports_mw, n.total_demand_mw,
       b.system_frequency, b.balancing_volume_mw
FROM settlement_national n
LEFT JOIN system_balancing b ON b.timestamp = n.timestamp
WHERE n.timestamp >= ? AND n.timestamp < ?
ORDER BY n.timestamp ASC
"""


def export_timeline(conn: sqlite3.Connection, name: str, start: datetime, end: datetime) -> None:
    cursor = conn.execute(TIMELINE_QUERY, (iso(start), iso(end)))
    rows = [list(row) for row in cursor.fetchall()]
    payload = {"columns": TIMELINE_COLUMNS, "rows": rows}
    _write_json(name, payload)
    logger.info("Exported %s: %d rows (%s -> %s)", name, len(rows), start, end)


def export_regional_latest(conn: sqlite3.Connection) -> None:
    cursor = conn.execute(
        "SELECT region_id, dno_region, carbon_intensity, index_level, top_fuel "
        "FROM regional_latest ORDER BY region_id ASC"
    )
    rows = [list(row) for row in cursor.fetchall()]
    payload = {
        "columns": ["region_id", "dno_region", "carbon_intensity", "index_level", "top_fuel"],
        "rows": rows,
    }
    _write_json("regional_latest", payload)
    logger.info("Exported regional_latest: %d regions", len(rows))


def _write_json(name: str, payload: dict) -> None:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = EXPORT_DIR / f"{name}.json"
    out_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")


def export_all(now: datetime) -> None:
    with db_connection() as conn:
        export_timeline(conn, "day", now - timedelta(days=1), now)
        export_timeline(conn, "week", now - timedelta(weeks=1), now)
        export_timeline(conn, "month", now - timedelta(days=30), now)
        export_timeline(conn, "year", now - timedelta(days=365), now)
        export_timeline(conn, "previous_year", now - timedelta(days=730), now - timedelta(days=365))
        export_regional_latest(conn)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def ingest(start: datetime, end: datetime) -> None:
    logger.info("Ingesting national + balancing telemetry for %s -> %s", start, end)
    national_rows = fetch_national_range(start, end)
    balancing_rows = fetch_balancing_range(start, end)
    regional_rows = fetch_regional_latest()

    with db_connection() as conn:
        n = upsert_national(conn, national_rows)
        b = upsert_balancing(conn, balancing_rows)
        r = replace_regional(conn, regional_rows)
    logger.info("Ingested %d national rows, %d balancing rows, %d regional rows", n, b, r)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch GB grid telemetry and export dashboard JSON.")
    parser.add_argument(
        "--lookback-days", type=int, default=2,
        help="Days of recent data to (re)fetch and upsert on every run (default: 2).",
    )
    parser.add_argument(
        "--backfill-days", type=int, default=0,
        help="Additional historical days to backfill before the lookback window (default: 0).",
    )
    parser.add_argument(
        "--export-only", action="store_true",
        help="Skip fetching and only regenerate JSON exports from the existing database.",
    )
    args = parser.parse_args()

    init_db()
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

    if not args.export_only:
        if args.backfill_days > 0:
            backfill_start = now - timedelta(days=args.backfill_days + args.lookback_days)
            backfill_end = now - timedelta(days=args.lookback_days)
            ingest(backfill_start, backfill_end)
        ingest(now - timedelta(days=args.lookback_days), now)

    export_all(now)
    logger.info("Run complete.")


if __name__ == "__main__":
    main()
