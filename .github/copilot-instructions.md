# Copilot Instructions

## Project Overview

Autonomous solar battery management daemon for a GoodWe 10K ET inverter. It optimizes battery charge/discharge decisions every 10 minutes based on Czech OTE spot electricity prices, current battery state of charge (SOC), PV generation forecasts, site-specific measured consumption forecasts (hourly averages from historical `PV_run` data), and planned grid outages. Targets a residential Czech solar installation (4.56 kWp, 7.1 kWh battery).

## Running the Daemon

```bash
# Production daemon (loops every LOOP_INTERVAL_MINUTES, default 10)
python3 solar_controller.py

# Dry-run mode — no inverter writes, safe to run anywhere
python3 solar_controller.py --test

# Single cycle then exit (best for debugging)
python3 solar_controller.py --test --once

# Discover inverter sensor IDs (pass inverter IP)
python3 check_inverter.py 192.168.1.xxx

# Download historical energy data from Czech grid operator
python3 download_pnd.py
```

There are no automated tests or linting configurations in this repository.

## Configuration

Copy `config.env.example` to `config.env`. Required (no defaults):

- `INVERTER_IP` — Local IP of the GoodWe inverter
- `MYSQL_HOST`, `MYSQL_DB`, `MYSQL_USER`, `MYSQL_PASSWORD`

Key optional settings:
- `TEST_MODE=1` — Dry-run, no inverter writes (also enabled by `--test` flag)
- `HA_ENABLED=1` — Two-node high-availability mode with MySQL leader election
- `PV_KWP=0` — Set to 0 to disable forecast.solar API and use historical patterns only

## Architecture

All logic lives in `solar_controller.py` (~1,400 lines). The cycle in `run_cycle()`:

1. **Read inputs**: spot prices from MySQL (`EL_dailypr`), inverter state via GoodWe UDP/8899, PV forecast from forecast.solar API (or historical fallback from `PV_run` table), `planned_outages.json`
2. **Call `decide()`**: applies a 6-rule priority hierarchy, returns `(OperationMode, target_min_soc_pct, reason_string)`
3. **Apply**: write mode + min-SOC to inverter (skipped in test mode or when not HA leader)
4. **Log**: append row to MySQL `solar_decisions` table + rotating file `solar.log`

### MySQL Tables

| Table | Purpose |
|---|---|
| `EL_dailypr` | Hourly spot prices (CZK/MWh); populated externally |
| `PV_run` | Historical inverter readings (PV, load, grid, SOC, battery power) |
| `solar_decisions` | Decision log written each cycle |
| `solar_control` | Manual override state (set by web UI) |
| `solar_leader_lease` | HA leader election lease (single row) |

`solar_decisions`, `solar_control`, and `solar_leader_lease` are auto-created by the daemon on first run.

### Web UI (`solar_web/`)

PHP dashboard (`index.php`) reads from MySQL and lets operators switch between AUTO and MANUAL mode. Requires `db.php` credentials at `/etc/solar/db.php` and HTTP Basic Auth via `/etc/solar/.htpasswd`. See `deploy.sh` for setup steps.

## Decision Logic (`decide()` — lines ~840–1220)

Rules are evaluated in strict priority order. The first matching rule wins:

| Rule | Trigger | Action |
|---|---|---|
| 0 | Planned outage imminent or active | ECO_CHARGE to `OUTAGE_PRE_CHARGE_SOC` (default 98%) |
| 1 | SOC below `MIN_SOC_FLOOR` | ECO_CHARGE emergency floor |
| 1.5 | Current price ≤ 0 (negative prices) | ECO_CHARGE to absorb free electricity |
| 2 | Current price ≤ cheap threshold AND arbitrage is profitable | ECO_CHARGE |
| 2.5 | Night prep-charge needed for morning peak | ECO_CHARGE, deferred to cheapest upcoming hour |
| 3–6 | Expensive hour / expensive upcoming hours / default | GENERAL with computed min-SOC |

## Key Conventions

**Dynamic price thresholds** — Not fixed values. Each cycle computes cheap/expensive thresholds as percentiles (default: 25th / 75th) of the upcoming ~24-hour price window. `RECHARGE_HORIZON_H` (default 13) caps how many of tomorrow's hours are included, preventing high afternoon peaks from masking today's expensive hours.

**Arbitrage profitability gate** — Grid-charging only proceeds if:
```
max_upcoming_sell_price >= (buy_price + DEVIATION_FEE) / BATTERY_EFFICIENCY
```
This prevents buying when battery round-trip losses + Czech deviation fees make it uneconomical.

**Distribution tariff surcharge** — Czech peak hours (`DIST_PEAK_HOURS`, default 12 and 15) carry a ~6× higher distribution fee. The daemon adds this delta to the effective spot price before evaluating the cheap-charge condition.

**Site-specific consumption forecasting** — Load forecasts are derived from *actual measured consumption* stored in `PV_run`, not generic statistics. `query_hourly_patterns()` computes average load per hour-of-day for the current calendar month, looking back `HISTORY_MONTHS` (default 24) months. This per-site, per-month, per-hour profile drives four decisions:
- **Overnight prep-charge (Rule 2.5)**: `expected_load_wh(patterns, now→next_cheap)` determines how much charge is needed to bridge to the next cheap window.
- **Net PV check**: subtracts `load_remaining_wh` from the PV forecast — raw PV surplus can vanish on high-consumption days.
- **Discharge-start algorithm**: uses per-hour load to rank which hours the battery can actually cover.
- **Drain-start (overnight hold)**: checks whether expected overnight load fits in usable battery capacity before allowing early discharge.

`expected_load_wh()` also applies a DHW floor (`DHW_WH`) during the hot-water heater window to prevent historical underestimates from masking a known fixed load.

**HA leader election** — Uses a single-row MySQL table with `UPDATE … WHERE node_name = THIS_NODE OR expires_at < NOW()`. Only the node whose UPDATE succeeds becomes leader for that cycle. Lease TTL = `LOOP_INTERVAL_MIN * 2 + 30s`.

**Manual override** — The `solar_control` table row is read every cycle. When `manual_mode=1` the daemon applies `forced_mode`/`forced_min_soc` instead of running `decide()`, without requiring a restart.

**Inverter I/O** — All reads and writes use the `goodwe` library over UDP/8899 and are `asyncio.run()` wrapped. Operation modes are `goodwe.OperationMode` enum values: `GENERAL`, `ECO_CHARGE`, `BACKUP`. Battery floor is written to the `battery_discharge_depth` register.

**`planned_outages.json`** format:
```json
[{"start": "2026-03-05 08:00", "end": "2026-03-05 14:00", "note": "maintenance"}]
```
An empty array `[]` disables outage handling. The file is re-read every cycle.
