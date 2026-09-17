# GB Grid Telemetry Dashboard

A static, self-hosted GB (Great Britain) electricity grid dashboard. A Python
ETL script pulls live telemetry into a local SQLite warehouse and exports
compact JSON files; a single-file HTML/JS front-end (uPlot) renders them as
interactive timelines, a decarbonisation gauge, and a regional carbon intensity map. A GitOps pipeline (systemd timer + git push script) keeps a
deployed VM updating the JSON automatically.

## Contents

| File | Purpose |
|---|---|
| `fetch_grid.py` | Fetches telemetry from NESO + Elexon APIs, stores it in `grid_telemetry.db`, exports JSON to `docs/data/`. |
| `git_push.py` | Commits/pushes `docs/data/*.json` to `origin/main`, non-interactively. |
| `docs/index.html` | The dashboard itself. Open directly or serve statically — reads from `data/` (relative to itself). Also what GitHub Pages serves (see [DEPLOYMENT.md](DEPLOYMENT.md)). |
| `grid-tracker.service` / `grid-tracker.timer` | systemd units to run `fetch_grid.py` + `git_push.py` automatically on a Linux VM. |
| `setup_deploy_key.sh` | Reference commands for provisioning a GitHub deploy key on a headless VM. |
| `requirements.txt` | Python dependencies (`requests`). |
| `grid_telemetry.db` | SQLite warehouse (created automatically on first run). |
| `docs/data/` | Exported JSON consumed by `docs/index.html` (and committed to git; served by GitHub Pages). |
| `fetch_grid.log` / `update.log` | Logs from `fetch_grid.py` / `git_push.py`. |

## Setup

```powershell
pip install -r requirements.txt
```

## Running `fetch_grid.py`

```powershell
python fetch_grid.py [--lookback-days N] [--backfill-days N] [--export-only]
```

| Flag | Default | Description |
|---|---|---|
| `--lookback-days N` | `2` | Days of recent data to (re)fetch and upsert on every run. Safe to run repeatedly — upserts are idempotent (`INSERT ... ON CONFLICT`), so overlapping windows just refresh/correct existing rows rather than duplicating them. |
| `--backfill-days N` | `0` | Additional *historical* days to pull **before** the lookback window, on top of it. Use this once to seed longer history (e.g. so `week.json`/`month.json`/`year.json` aren't mostly empty). Chunked internally to respect each API's max date-range limits, so large values (e.g. 365+) will make many requests — expect it to take a while. |
| `--export-only` | off | Skip all network calls; just regenerate the JSON files in `docs/data/` from whatever is already in `grid_telemetry.db`. Useful for quickly re-exporting after a schema/format change to the export code, or for testing the front-end without hitting the APIs. |

Examples:
```powershell
# Normal scheduled run (what the systemd timer does)
python fetch_grid.py

# First-time setup: backfill a month of history, then top up the last 2 days
python fetch_grid.py --backfill-days 30

# Re-export JSON only, no API calls
python fetch_grid.py --export-only
```

On every run this creates/migrates `grid_telemetry.db` (additive schema
migrations only — safe to pull the latest `fetch_grid.py` and rerun against an
existing database), then (re)writes all of:
`docs/data/day.json`, `docs/data/previous_day.json`, `docs/data/3days.json`,
`docs/data/week.json`, `docs/data/month.json`, `docs/data/year.json`,
`docs/data/previous_year.json`, `docs/data/regional_latest.json`, `docs/data/carbon_forecast.json`.

**Data sources**: NESO Carbon Intensity API (national + regional), Elexon
Insights/BMRS (`FUELINST`, `MID`, `/system/frequency`,
`/balancing/settlement/system-prices`, `/demand/actual/total`). No API keys
required — all endpoints are public.

**Scheduling note**: `fetch_grid.py` does not poll on its own; it's a
single run-and-exit script. Automatic hourly updates come from
`grid-tracker.timer` (see below).

## Running `git_push.py`

```powershell
python git_push.py
```

No flags. It:
1. `git add docs/data/*.json`
2. Checks `git status --porcelain` — if there are no changes, exits cleanly (exit code 0) without committing.
3. Commits with message `chore(telemetry): automated update of national, balancing, and regional grid data`.
4. `git push origin main`.

Logs to `update.log` in the repo root. Any failure (git error, timeout, push
rejection) logs a `CRITICAL ALERT:` line and exits non-zero, so it can be
monitored (e.g. via `systemctl status`/journal on a deployed VM). Requires
`git` on PATH and a configured `origin` remote with push access (see deploy
key setup below for a headless VM).

## Viewing the dashboard

`index.html` fetches JSON via relative paths (`data/<tab>.json?v=...`), so it
must be served over HTTP, not opened as a `file://` URL (browsers block
`fetch()` against local files). Any static file server works, e.g.:

```powershell
cd docs
python -m http.server 8000
```

Then open `http://localhost:8000/index.html`.

In production, GitHub Pages serves this same `docs/` folder directly — see
[DEPLOYMENT.md](DEPLOYMENT.md) for how to enable it.

### Dashboard features
- **History nav bar**: Today / Previous Day / 3 Days / Week / Month / Year / Previous Year.
  Note: only `day`, `week`, `month`, `year`, `previous_year` are currently
  exported by `fetch_grid.py` — "Previous Day" and "3 Days" tabs will show
  "failed to load" until those exports are added.
- **Summary banner**: time, wholesale price, emissions, demand/generation/transfers (GW), frequency.
- **Upper chart**: fuel mix (wind/solar/gas/nuclear/biomass/imports, MW) + carbon intensity, synced cursor with the lower chart.
- **Lower chart**: balancing volume (MW) + system frequency (Hz).
- **Grid Decarbonisation card**: zero-carbon share gauge, low-carbon vs fossil MW, 48h carbon intensity forecast sparkline.
- **Grid Operations card**: frequency + 10-min delta arrow, net interconnector transfer badge (import/export, color-coded).
- **Fuel & Transfer Breakdown**: categorized cards (Fossil, Renewables, Other, Interconnectors, Storage) with GW + % of demand.
- **Regional map**: 14 GB DNO regions, carbon intensity + top fuel, color-coded by index level.
- The "Today" tab and regional/forecast data auto-refresh every 60 seconds while open.
- All chart/tooltip values are shown **unrounded**, exactly as stored in the source JSON.

## Automated deployment (Linux VM)

> For a full copy-paste walkthrough (SCP'ing files across, installing git,
> generating a deploy key, and connecting to GitHub step by step), see
> [DEPLOYMENT.md](DEPLOYMENT.md).

1. Copy `fetch_grid.py`, `git_push.py`, `requirements.txt`, and the repo (with
   its git remote configured) to the VM, e.g. `/opt/grid-dashboard`.
2. Set up a GitHub deploy key so the VM can push without a password prompt —
   follow the commands in `setup_deploy_key.sh` (generates an ed25519 key,
   registers it as a repo deploy key with write access, and points `origin`
   at an SSH host alias).
3. Install the systemd units:
   ```bash
   sudo cp fetch_grid.py git_push.py /opt/grid-dashboard/
   sudo cp grid-tracker.service grid-tracker.timer /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now grid-tracker.timer
   ```
4. Verify it's scheduled and check recent runs:
   ```bash
   systemctl list-timers grid-tracker.timer
   systemctl status grid-tracker.service
   journalctl -u grid-tracker.service -n 50
   ```

`grid-tracker.timer` fires `grid-tracker.service` **every hour at :05**
(`OnCalendar=*:05:00`, `Persistent=true` — a missed run due to downtime fires
as soon as the VM is back up). Each run executes `fetch_grid.py` (default
`--lookback-days 2`) then `git_push.py`, so the deployed `data/*.json` stays
current automatically without manual intervention.

## Notes / known limitations

- `battery_storage_mw` is always `--`/null — Elexon does not currently publish
  a separate `BATTERY` fuel type in `FUELINST`; the column is future-proofed
  for when/if it becomes available.
- `solar_mw` is an *estimate* (NESO's generation-mix percentage × Elexon ATL
  demand), since solar is not separately metered in `FUELINST`.
- `total_demand_mw` is computed as `total_generation_mw + total_net_transfer_mw
  - storage_charging_mw`, not fetched directly.
- Running `fetch_grid.py` repeatedly (e.g. testing locally) is safe — SQLite
  upserts prevent duplicate rows, and `git_push.py` no-ops if nothing changed.
