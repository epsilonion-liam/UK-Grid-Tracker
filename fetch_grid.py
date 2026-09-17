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
   - GET /generation/{from}/{to}  -> 30-min fuel mix percentages (used only for
     solar, which Elexon FUELINST does not meter separately)
   - GET /intensity/{from}/{to}   -> 30-min carbon intensity (gCO2/kWh)
2. Elexon Insights Solution API (BMRS)   - https://data.elexon.co.uk/bmrs/api/v1
   - GET /system/frequency                        -> system frequency (Hz)
   - GET /balancing/settlement/system-prices/{d}   -> net imbalance volume (MW)
   - GET /demand/actual/total                      -> actual total demand (MW),
     used only to convert the solar percentage above into an estimated MW figure
   - GET /datasets/FUELINST -> exact 5-min MW by fuel type: gas (CCGT/OCGT), coal,
     nuclear, wind, hydro (NPSHYD), biomass, pumped storage, and every GB
     interconnector (France IFA/IFA2/ElecLink, Netherlands BritNed, Belgium Nemo,
     Norway North Sea Link, Denmark Viking Link, Ireland Moyle/EWIC/Greenlink)
   - GET /datasets/MID -> Market Index Data -> wholesale electricity price (£/MWh)
3. NESO Regional Carbon Intensity API    - https://api.carbonintensity.org.uk/regional
   - GET /regional -> current half-hour snapshot for all GB DNO regions

Derived aggregates (computed, not fetched)
-------------------------------------------
- total_generation_mw = sum of active generator fuel types (gas, coal, nuclear,
  wind, solar, biomass, hydro) - excludes storage discharge and interconnector
  imports.
- total_net_transfer_mw = net sum of all interconnector flows (positive = net
  import into GB, negative = net export).
- storage_charging_mw = MW drawn by pumped storage/battery while charging
  (i.e. the negative portion of their FUELINST readings).
- total_demand_mw = total_generation_mw + total_net_transfer_mw - storage_charging_mw.
- Per-fuel/interconnector percentages are computed at export time relative to
  total_demand_mw and are not persisted in SQLite (derivable on demand).

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
import bisect
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
EXPORT_DIR = BASE_DIR / "docs" / "data"

CARBON_INTENSITY_BASE = "https://api.carbonintensity.org.uk"
ELEXON_BASE = "https://data.elexon.co.uk/bmrs/api/v1"

REQUEST_TIMEOUT = 10  # seconds - hard timeout per HTTP request
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2

# Per-API max date-range limits (days) to keep requests within documented caps.
CARBON_INTENSITY_CHUNK_DAYS = 14
DEMAND_CHUNK_DAYS = 7
FREQUENCY_CHUNK_DAYS = 1  # per-minute data - keep chunks small
FUELINST_CHUNK_DAYS = 1   # 5-minute data across ~20 fuel types - keep chunks small
MID_CHUNK_DAYS = 7

# The 14 GB DNO regions have regionid 1-14 (15-17 are England/Scotland/Wales
# aggregates and are excluded from the regional_latest snapshot).
DNO_REGION_MIN_ID = 1
DNO_REGION_MAX_ID = 14

# Elexon FUELINST fuelType -> our DB column, split into three groups that feed
# the Total Generation / Net Transfer / Storage Charging calculations below.
GENERATOR_FUEL_MAP = {
    "CCGT": "gas_ccgt_mw",
    "OCGT": "gas_ocgt_mw",
    "COAL": "coal_mw",
    "NUCLEAR": "nuclear_mw",
    "WIND": "wind_mw",
    "NPSHYD": "hydro_mw",
    "BIOMASS": "biomass_mw",
}
STORAGE_FUEL_MAP = {
    "PS": "pumped_storage_mw",
    "BATTERY": "battery_storage_mw",  # not yet published by Elexon; future-proofed
}
INTERCONNECTOR_FUEL_MAP = {
    "INTFR": "intercon_ifa_mw",          # France - IFA
    "INTIFA2": "intercon_ifa2_mw",       # France - IFA2
    "INTELEC": "intercon_eleclink_mw",   # France - ElecLink
    "INTNED": "intercon_britned_mw",     # Netherlands - BritNed
    "INTNEM": "intercon_nemo_mw",        # Belgium - Nemo Link
    "INTNSL": "intercon_nsl_mw",         # Norway - North Sea Link
    "INTVKL": "intercon_viking_mw",      # Denmark - Viking Link
    "INTIRL": "intercon_moyle_mw",       # Ireland - Moyle
    "INTEW": "intercon_ewic_mw",         # Ireland - EWIC
    "INTGRNL": "intercon_greenlink_mw",  # Ireland - Greenlink
}
FUELINST_FUEL_MAP = {**GENERATOR_FUEL_MAP, **STORAGE_FUEL_MAP, **INTERCONNECTOR_FUEL_MAP}

# solar_mw is included in generation even though it is estimated from the NESO
# generation-mix percentage (see fetch_national_range) rather than metered via FUELINST.
GENERATOR_COLUMNS = tuple(GENERATOR_FUEL_MAP.values()) + ("solar_mw",)
STORAGE_COLUMNS = tuple(STORAGE_FUEL_MAP.values())
INTERCONNECTOR_COLUMNS = tuple(INTERCONNECTOR_FUEL_MAP.values())

# Full set of per-stream MW columns persisted in settlement_national, beyond
# the original (timestamp, carbon_intensity, ..., total_demand_mw) columns.
DETAIL_MW_COLUMNS = (
    "gas_ccgt_mw", "gas_ocgt_mw", "coal_mw",
    "pumped_storage_mw", "battery_storage_mw",
    "intercon_ifa_mw", "intercon_ifa2_mw", "intercon_eleclink_mw",
    "intercon_britned_mw", "intercon_nemo_mw", "intercon_nsl_mw",
    "intercon_viking_mw", "intercon_moyle_mw", "intercon_ewic_mw",
    "intercon_greenlink_mw",
    "total_generation_mw", "total_net_transfer_mw", "storage_charging_mw",
    "wholesale_price_gbp_mwh",
)

# Grid decarbonisation + frequency-stability metrics, computed in fetch_national_range.
DECARB_COLUMNS = (
    "zero_carbon_mw", "zero_carbon_share_pct", "net_interconnector_mw", "freq_delta",
)

# All updatable columns of settlement_national (excludes the timestamp PK).
NATIONAL_VALUE_COLUMNS = (
    "carbon_intensity", "gas_mw", "nuclear_mw", "wind_mw", "solar_mw",
    "biomass_mw", "hydro_mw", "imports_mw", "total_demand_mw",
) + DETAIL_MW_COLUMNS + DECARB_COLUMNS

# Columns that get a *_pct column at export time, relative to total_demand_mw.
PERCENTAGE_SOURCE_COLUMNS = (
    "wind_mw", "solar_mw", "gas_mw", "coal_mw", "nuclear_mw", "biomass_mw",
    "hydro_mw", "pumped_storage_mw", "battery_storage_mw", "total_net_transfer_mw",
)

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

CREATE TABLE IF NOT EXISTS carbon_forecast_48h (
    timestamp TEXT PRIMARY KEY,
    forecast_intensity INTEGER
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
        migrate_schema(conn)
    logger.info("Database schema ensured at %s", DB_PATH)


def migrate_schema(conn: sqlite3.Connection) -> None:
    """Add any newly-introduced settlement_national columns (idempotent, additive)."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(settlement_national)")}
    for column in DETAIL_MW_COLUMNS + DECARB_COLUMNS:
        if column not in existing:
            conn.execute(f"ALTER TABLE settlement_national ADD COLUMN {column} REAL")
            logger.info("Migrated settlement_national: added column %s", column)


def upsert_national(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    columns_sql = ", ".join(("timestamp",) + NATIONAL_VALUE_COLUMNS)
    placeholders_sql = ", ".join(f":{c}" for c in ("timestamp",) + NATIONAL_VALUE_COLUMNS)
    update_sql = ", ".join(f"{c} = COALESCE(excluded.{c}, {c})" for c in NATIONAL_VALUE_COLUMNS)
    conn.executemany(
        f"""
        INSERT INTO settlement_national ({columns_sql})
        VALUES ({placeholders_sql})
        ON CONFLICT(timestamp) DO UPDATE SET {update_sql}
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


def replace_carbon_forecast(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    conn.execute("DELETE FROM carbon_forecast_48h")
    conn.executemany(
        """
        INSERT INTO carbon_forecast_48h (timestamp, forecast_intensity)
        VALUES (:timestamp, :forecast_intensity)
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


def fetch_atl_demand(start: datetime, end: datetime) -> dict[str, float]:
    """Fetch actual total load (MW) from Elexon ATL. Used only to convert the
    NESO solar mix percentage into an estimated MW figure, since solar is not
    separately metered in FUELINST."""
    by_timestamp: dict[str, float] = {}
    for chunk_start, chunk_end in chunk_date_range(start, end, DEMAND_CHUNK_DAYS):
        url = f"{ELEXON_BASE}/demand/actual/total"
        params = {"from": iso(chunk_start), "to": iso(chunk_end)}
        try:
            payload = http_get(url, params=params)
        except requests.exceptions.RequestException:
            logger.error("Skipping ATL demand chunk %s -> %s due to request failure", chunk_start, chunk_end)
            continue
        records = payload.get("data", payload) if isinstance(payload, dict) else payload
        if not isinstance(records, list):
            records = []
        for record in records:
            start_time = record.get("startTime") or record.get("settlementDate")
            demand = record.get("quantity")
            if start_time is not None and demand is not None:
                by_timestamp[normalise_timestamp(start_time)] = demand
        logger.info("Fetched ATL demand for %s -> %s (%d records)", chunk_start, chunk_end, len(records))
    return by_timestamp


def fetch_fuel_inst(start: datetime, end: datetime) -> dict[str, dict]:
    """Fetch exact per-fuel-type MW from Elexon FUELINST (5-min data), floored
    to its enclosing half-hour settlement period (latest reading wins)."""
    by_timestamp: dict[str, dict] = {}
    for chunk_start, chunk_end in chunk_date_range(start, end, FUELINST_CHUNK_DAYS):
        url = f"{ELEXON_BASE}/datasets/FUELINST"
        params = {
            "publishDateTimeFrom": iso(chunk_start),
            "publishDateTimeTo": iso(chunk_end),
        }
        try:
            payload = http_get(url, params=params)
        except requests.exceptions.RequestException:
            logger.error("Skipping FUELINST chunk %s -> %s due to request failure", chunk_start, chunk_end)
            continue
        records = payload.get("data", payload) if isinstance(payload, dict) else payload
        if not isinstance(records, list):
            records = []
        for record in records:
            column = FUELINST_FUEL_MAP.get(record.get("fuelType"))
            start_time = record.get("startTime")
            generation = record.get("generation")
            if column is None or start_time is None or generation is None:
                continue  # unmapped fuel type (e.g. OIL, OTHER) - not required by the schema
            ts = align_to_half_hour(start_time)
            by_timestamp.setdefault(ts, {})[column] = generation
        logger.info("Fetched FUELINST for %s -> %s (%d readings)", chunk_start, chunk_end, len(records))
    return by_timestamp


def fetch_market_index(start: datetime, end: datetime) -> dict[str, float]:
    """Fetch the volume-weighted wholesale price (£/MWh) per settlement period
    from Elexon Market Index Data (MID), across all reporting providers."""
    weighted_sum: dict[str, float] = {}
    weight_total: dict[str, float] = {}
    simple_prices: dict[str, list[float]] = {}
    for chunk_start, chunk_end in chunk_date_range(start, end, MID_CHUNK_DAYS):
        url = f"{ELEXON_BASE}/datasets/MID"
        params = {"from": iso(chunk_start), "to": iso(chunk_end)}
        try:
            payload = http_get(url, params=params)
        except requests.exceptions.RequestException:
            logger.error("Skipping MID chunk %s -> %s due to request failure", chunk_start, chunk_end)
            continue
        records = payload.get("data", payload) if isinstance(payload, dict) else payload
        if not isinstance(records, list):
            records = []
        for record in records:
            start_time = record.get("startTime")
            price = record.get("price")
            volume = record.get("volume") or 0
            if start_time is None or price is None:
                continue
            ts = normalise_timestamp(start_time)
            simple_prices.setdefault(ts, []).append(price)
            if volume > 0:
                weighted_sum[ts] = weighted_sum.get(ts, 0.0) + price * volume
                weight_total[ts] = weight_total.get(ts, 0.0) + volume
        logger.info("Fetched Market Index Data for %s -> %s (%d records)", chunk_start, chunk_end, len(records))

    by_timestamp: dict[str, float] = {}
    for ts, prices in simple_prices.items():
        if ts in weight_total and weight_total[ts] > 0:
            by_timestamp[ts] = weighted_sum[ts] / weight_total[ts]
        else:
            by_timestamp[ts] = sum(prices) / len(prices)
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


def fetch_national_range(
    start: datetime, end: datetime, freq_delta_by_ts: dict[str, float]
) -> list[dict]:
    """Merge FUELINST, carbon intensity, Market Index price, solar MW estimate,
    and frequency delta into detailed national rows, then compute generation/
    demand/decarbonisation totals."""
    fuel_by_ts = fetch_fuel_inst(start, end)
    intensity_by_ts = fetch_carbon_intensity(start, end)
    price_by_ts = fetch_market_index(start, end)
    mix_by_ts = fetch_generation_mix(start, end)  # only .get("solar") is used
    atl_by_ts = fetch_atl_demand(start, end)

    all_timestamps = (
        set(fuel_by_ts) | set(intensity_by_ts) | set(price_by_ts)
        | set(mix_by_ts) | set(freq_delta_by_ts)
    )
    rows = []
    for ts in all_timestamps:
        fuels = dict(fuel_by_ts.get(ts, {}))  # copy: solar_mw is added below

        solar_pct = mix_by_ts.get(ts, {}).get("solar")
        atl_demand = atl_by_ts.get(ts)
        fuels["solar_mw"] = (
            (solar_pct / 100 * atl_demand) if (solar_pct is not None and atl_demand is not None) else None
        )

        generator_values = [fuels[c] for c in GENERATOR_COLUMNS if fuels.get(c) is not None]
        total_generation_mw = sum(generator_values) if generator_values else None

        interconnector_values = [fuels[c] for c in INTERCONNECTOR_COLUMNS if fuels.get(c) is not None]
        total_net_transfer_mw = sum(interconnector_values) if interconnector_values else None

        storage_present = [fuels[c] for c in STORAGE_COLUMNS if fuels.get(c) is not None]
        storage_charging_mw = sum(max(0.0, -v) for v in storage_present) if storage_present else None

        total_demand_mw = None
        if total_generation_mw is not None and total_net_transfer_mw is not None:
            total_demand_mw = total_generation_mw + total_net_transfer_mw - (storage_charging_mw or 0.0)

        gas_ccgt = fuels.get("gas_ccgt_mw")
        gas_ocgt = fuels.get("gas_ocgt_mw")
        gas_mw = (
            (gas_ccgt or 0.0) + (gas_ocgt or 0.0) if (gas_ccgt is not None or gas_ocgt is not None) else None
        )

        zero_carbon_parts = [
            fuels.get(c) for c in ("wind_mw", "solar_mw", "nuclear_mw", "hydro_mw")
        ]
        zero_carbon_present = [v for v in zero_carbon_parts if v is not None]
        zero_carbon_mw = sum(zero_carbon_present) if zero_carbon_present else None
        zero_carbon_share_pct = (
            zero_carbon_mw / total_generation_mw * 100
            if (zero_carbon_mw is not None and total_generation_mw)
            else None
        )

        row = {
            "timestamp": ts,
            "carbon_intensity": intensity_by_ts.get(ts),
            "wholesale_price_gbp_mwh": price_by_ts.get(ts),
            "gas_mw": gas_mw,
            "nuclear_mw": fuels.get("nuclear_mw"),
            "wind_mw": fuels.get("wind_mw"),
            "solar_mw": fuels.get("solar_mw"),
            "biomass_mw": fuels.get("biomass_mw"),
            "hydro_mw": fuels.get("hydro_mw"),
            "imports_mw": total_net_transfer_mw,
            "total_demand_mw": total_demand_mw,
            "total_generation_mw": total_generation_mw,
            "total_net_transfer_mw": total_net_transfer_mw,
            "storage_charging_mw": storage_charging_mw,
            "zero_carbon_mw": zero_carbon_mw,
            "zero_carbon_share_pct": zero_carbon_share_pct,
            "net_interconnector_mw": total_net_transfer_mw,
            "freq_delta": freq_delta_by_ts.get(ts),
        }
        for column in DETAIL_MW_COLUMNS:
            row.setdefault(column, fuels.get(column))
        rows.append(row)
    return rows


# --------------------------------------------------------------------------
# Fetchers - System balancing (Elexon frequency + net imbalance volume)
# --------------------------------------------------------------------------
def parse_iso_datetime(value: str) -> datetime:
    """Parse an Elexon/NESO ISO8601 timestamp (with optional seconds/offset) to UTC."""
    text = value.replace("Z", "").replace("+00:00", "").split(".")[0]
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


def fetch_frequency_data(start: datetime, end: datetime) -> tuple[dict[str, float], dict[str, float]]:
    """Fetch Elexon system frequency readings and derive two views from the
    same raw samples: a half-hour snapshot (latest reading per settlement
    period, used by system_balancing) and a 10-minute delta per period (used
    by settlement_national.freq_delta)."""
    samples: list[tuple[datetime, float]] = []
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
                samples.append((parse_iso_datetime(ts_raw), freq))
        logger.info("Fetched system frequency for %s -> %s (%d readings)", chunk_start, chunk_end, len(records))

    samples.sort(key=lambda s: s[0])
    freq_by_ts: dict[str, float] = {}
    latest_sample_time_by_bucket: dict[str, datetime] = {}
    for sample_time, freq in samples:
        bucket = align_to_half_hour(iso(sample_time))
        freq_by_ts[bucket] = freq  # latest reading in the half-hour wins
        latest_sample_time_by_bucket[bucket] = sample_time

    sample_times = [s[0] for s in samples]
    delta_by_ts: dict[str, float] = {}
    for bucket, current_freq in freq_by_ts.items():
        target = latest_sample_time_by_bucket[bucket] - timedelta(minutes=10)
        idx = bisect.bisect_right(sample_times, target) - 1
        if idx >= 0:
            delta_by_ts[bucket] = current_freq - samples[idx][1]
    return freq_by_ts, delta_by_ts


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


def fetch_balancing_range(start: datetime, end: datetime, freq_by_ts: dict[str, float]) -> list[dict]:
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


def fetch_carbon_forecast_48h() -> list[dict]:
    """Fetch the next 48-hour national carbon intensity forecast from NESO.

    Uses /intensity/{from}/fw48h rather than /intensity/date, since the latter
    only returns *today's* data and cannot provide a forward-looking forecast.
    """
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    url = f"{CARBON_INTENSITY_BASE}/intensity/{iso(now)}/fw48h"
    try:
        payload = http_get(url)
    except requests.exceptions.RequestException:
        logger.error("Failed to fetch 48h carbon intensity forecast")
        return []
    rows = []
    for entry in payload.get("data", []):
        ts = entry.get("from")
        forecast = entry.get("intensity", {}).get("forecast")
        if ts is not None and forecast is not None:
            rows.append({"timestamp": ts, "forecast_intensity": forecast})
    logger.info("Fetched 48h carbon intensity forecast (%d periods)", len(rows))
    return rows


# --------------------------------------------------------------------------
# Export - compact JSON timelines + regional snapshot
# --------------------------------------------------------------------------
PERCENTAGE_COLUMNS = [f"{col[:-3]}_pct" for col in PERCENTAGE_SOURCE_COLUMNS]
PERCENTAGE_COLUMNS[PERCENTAGE_SOURCE_COLUMNS.index("total_net_transfer_mw")] = "interconnector_pct"

TIMELINE_COLUMNS = (
    ["timestamp", "carbon_intensity", "wholesale_price_gbp_mwh"]
    + ["gas_mw"] + list(DETAIL_MW_COLUMNS[:3])  # gas_ccgt_mw, gas_ocgt_mw, coal_mw
    + ["nuclear_mw", "wind_mw", "solar_mw", "biomass_mw", "hydro_mw", "imports_mw"]
    + list(DETAIL_MW_COLUMNS[3:18])  # storage + interconnector columns
    + ["total_demand_mw", "zero_carbon_mw", "zero_carbon_share_pct", "net_interconnector_mw"]
    + ["system_frequency", "freq_delta", "balancing_volume_mw"]
    + PERCENTAGE_COLUMNS
)

TIMELINE_QUERY = f"""
SELECT n.timestamp, n.carbon_intensity, n.wholesale_price_gbp_mwh,
       n.gas_mw, n.gas_ccgt_mw, n.gas_ocgt_mw, n.coal_mw,
       n.nuclear_mw, n.wind_mw, n.solar_mw, n.biomass_mw, n.hydro_mw, n.imports_mw,
       n.pumped_storage_mw, n.battery_storage_mw,
       n.intercon_ifa_mw, n.intercon_ifa2_mw, n.intercon_eleclink_mw,
       n.intercon_britned_mw, n.intercon_nemo_mw, n.intercon_nsl_mw,
       n.intercon_viking_mw, n.intercon_moyle_mw, n.intercon_ewic_mw,
       n.intercon_greenlink_mw,
       n.total_generation_mw, n.total_net_transfer_mw, n.storage_charging_mw,
       n.total_demand_mw, n.zero_carbon_mw, n.zero_carbon_share_pct, n.net_interconnector_mw,
       b.system_frequency, n.freq_delta, b.balancing_volume_mw
FROM settlement_national n
LEFT JOIN system_balancing b ON b.timestamp = n.timestamp
WHERE n.timestamp >= ? AND n.timestamp < ?
ORDER BY n.timestamp ASC
"""

# Column index of each PERCENTAGE_SOURCE_COLUMNS entry within a raw SQL row
# (mirrors TIMELINE_QUERY's SELECT order, before the percentage columns are appended).
_RAW_COLUMN_ORDER = TIMELINE_COLUMNS[: len(TIMELINE_COLUMNS) - len(PERCENTAGE_COLUMNS)]


def _with_percentages(raw_row: tuple) -> list:
    """Append per-stream percentage-of-demand columns to a raw SQL row."""
    row = list(raw_row)
    values_by_column = dict(zip(_RAW_COLUMN_ORDER, row))
    total_demand = values_by_column.get("total_demand_mw")
    for source_col in PERCENTAGE_SOURCE_COLUMNS:
        value = values_by_column.get(source_col)
        if value is None or not total_demand:
            row.append(None)
        else:
            row.append(value / total_demand * 100)
    return row


def export_timeline(conn: sqlite3.Connection, name: str, start: datetime, end: datetime) -> None:
    cursor = conn.execute(TIMELINE_QUERY, (iso(start), iso(end)))
    rows = [_with_percentages(row) for row in cursor.fetchall()]
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


def export_carbon_forecast(conn: sqlite3.Connection) -> None:
    cursor = conn.execute(
        "SELECT timestamp, forecast_intensity FROM carbon_forecast_48h ORDER BY timestamp ASC"
    )
    rows = [list(row) for row in cursor.fetchall()]
    payload = {"columns": ["timestamp", "forecast_intensity"], "rows": rows}
    _write_json("carbon_forecast", payload)
    logger.info("Exported carbon_forecast: %d periods", len(rows))


def _write_json(name: str, payload: dict) -> None:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = EXPORT_DIR / f"{name}.json"
    out_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")


def export_all(now: datetime) -> None:
    with db_connection() as conn:
        export_timeline(conn, "day", now - timedelta(days=1), now)
        export_timeline(conn, "previous_day", now - timedelta(days=2), now - timedelta(days=1))
        export_timeline(conn, "3days", now - timedelta(days=3), now)
        export_timeline(conn, "week", now - timedelta(weeks=1), now)
        export_timeline(conn, "month", now - timedelta(days=30), now)
        export_timeline(conn, "year", now - timedelta(days=365), now)
        export_timeline(conn, "previous_year", now - timedelta(days=730), now - timedelta(days=365))
        export_regional_latest(conn)
        export_carbon_forecast(conn)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def ingest(start: datetime, end: datetime) -> None:
    logger.info("Ingesting national + balancing telemetry for %s -> %s", start, end)
    freq_by_ts, freq_delta_by_ts = fetch_frequency_data(start, end)
    national_rows = fetch_national_range(start, end, freq_delta_by_ts)
    balancing_rows = fetch_balancing_range(start, end, freq_by_ts)
    regional_rows = fetch_regional_latest()
    carbon_forecast_rows = fetch_carbon_forecast_48h()

    with db_connection() as conn:
        n = upsert_national(conn, national_rows)
        b = upsert_balancing(conn, balancing_rows)
        r = replace_regional(conn, regional_rows)
        f = replace_carbon_forecast(conn, carbon_forecast_rows)
    logger.info(
        "Ingested %d national rows, %d balancing rows, %d regional rows, %d forecast periods",
        n, b, r, f,
    )


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
