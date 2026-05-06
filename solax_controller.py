#!/usr/bin/env python3
"""
solax_controller.py — Intelligent Solax Solar System Controller

Runs as a daemon and evaluates the optimal inverter operation mode every
LOOP_INTERVAL_MINUTES (default: 5).  In TEST mode it only prints what it
*would* do — no commands are sent to the inverter.

Decision inputs
  - Czech OTE spot electricity prices    (spotovaelektrina.cz API)
  - Current battery SOC + inverter state (Solax local HTTP API)
  - Historical consumption & PV patterns (MySQL PV_run table)

Usage
  # Normal (control) mode:
  python3 solax_controller.py

  # Test / dry-run mode — prints decisions, touches nothing:
  python3 solax_controller.py --test
  # or: set TEST_MODE=1 in config.env

Configuration
  Copy config.env.example → config.env and fill in your values.

PV_run columns used
  pdtime  — datetime
  ppv     — PV generation (W)
  ploadT  — house load (W)
  pgridT  — grid power (W, + export / – import)
  pbatts  — battery SOC (%)
  pbattw  — battery power (W, + charging / – discharging)
  psource — data source identifier (filter via PV_SOURCE env var)
"""

import asyncio
import json
import logging
import logging.handlers
import math
import os
import sys
import time
import urllib.request
from datetime import datetime, date, timedelta
from typing import Optional

import mysql.connector
import solax


# ── Bootstrap ─────────────────────────────────────────────────────────────────

def _load_env_file(path: str) -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if value and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            elif " #" in value:
                value = value.split(" #", 1)[0].rstrip()
            os.environ.setdefault(key, value)


_cfg_path = os.path.join(os.path.dirname(__file__), "config.env")
_load_env_file(_cfg_path)

TEST_MODE = ("--test" in sys.argv) or (os.getenv("TEST_MODE", "0") == "1")
OperationMode = str

_log_dir  = os.path.dirname(os.path.abspath(__file__))
_log_file = os.path.join(_log_dir, "solax.log")

_fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

_file_handler = logging.handlers.RotatingFileHandler(
    _log_file, maxBytes=5 * 1024 * 1024, backupCount=7, encoding="utf-8"
)
_file_handler.setFormatter(_fmt)

_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(_fmt)

logging.basicConfig(level=logging.INFO, handlers=[_file_handler, _console_handler])
log = logging.getLogger("solax")


def _req(key: str) -> str:
    v = os.environ.get(key)
    if not v:
        sys.exit(f"ERROR: required env var '{key}' is not set in config.env")
    return v


# ── Configuration ─────────────────────────────────────────────────────────────

INVERTER_IP        = os.getenv("INVERTER_IP", "")
INVERTER_PORT      = int(os.getenv("INVERTER_PORT", "80"))
INVERTER_PASSWORD  = os.getenv("INVERTER_PASSWORD", "")
MYSQL_HOST         = _req("MYSQL_HOST")
MYSQL_DB           = _req("MYSQL_DB")
MYSQL_USER         = _req("MYSQL_USER")
MYSQL_PASSWORD     = _req("MYSQL_PASSWORD")

PV_SOURCE          = os.getenv("PV_SOURCE", "SOLAX")
BATTERY_CAPACITY   = int(os.getenv("BATTERY_CAPACITY_WH", "7100"))   # Wh usable battery capacity
CHARGE_RATE_W      = int(os.getenv("CHARGE_RATE_W", "2750"))          # W   (20→98% in ~2h → (0.78×7100)/2)
# Dynamic thresholds: CHEAP_PERCENTILE lowest hours = cheap, top = expensive
# Fixed fallbacks used only when fewer than 24 h of price data are available
CHEAP_PERCENTILE   = int(os.getenv("CHEAP_PERCENTILE", "25"))         # bottom N% of next 24h = cheap
EXPENSIVE_PERCENTILE = int(os.getenv("EXPENSIVE_PERCENTILE", "75"))   # top N% of next 24h = expensive
PRICE_CHEAP        = float(os.getenv("PRICE_CHEAP", "1500"))          # CZK/MWh fallback
PRICE_EXPENSIVE    = float(os.getenv("PRICE_EXPENSIVE", "3000"))      # CZK/MWh fallback
# Threshold window: tomorrow hours beyond this cutoff are excluded from percentile
# calculation.  Overnight decisions (e.g. h22→h05) should not be distorted by
# tomorrow's afternoon/evening prices — only overnight + morning peak matter.
RECHARGE_HORIZON_H = int(os.getenv("RECHARGE_HORIZON_H", "13"))       # last tomorrow hour included in threshold window
PRICE_NO_EXPORT    = float(os.getenv("PRICE_NO_EXPORT", "200"))       # CZK/MWh — absorb surplus PV into battery rather than exporting below this price
EOD_HOUR           = int(os.getenv("EOD_HOUR", "14"))                  # hour from which end-of-day overnight strategy applies
MIN_SOC            = int(os.getenv("MIN_SOC", "20"))                  # %
NIGHT_MIN_SOC      = int(os.getenv("NIGHT_MIN_SOC", "20"))            # % (same as MIN_SOC — keeps reserve for UPS/backup server)
MIN_SOC_FLOOR      = int(os.getenv("MIN_SOC_FLOOR", "20"))            # % absolute minimum ever written
MIN_SOC_SAVE       = int(os.getenv("MIN_SOC_SAVE", "50"))             # % raised for GENERAL mode when expensive hours imminent
CHARGE_SOC_LIMIT   = int(os.getenv("CHARGE_SOC_LIMIT", "98"))         # % charge target — logic considers battery full at this level
ECO_MIN_SOC        = int(os.getenv("ECO_MIN_SOC", "95"))              # % written to battery_discharge_depth during ECO_CHARGE (PV buffer floor)
OUTAGE_PRE_CHARGE_SOC = int(os.getenv("OUTAGE_PRE_CHARGE_SOC", "98"))  # % target before planned outage
OUTAGE_LEAD_HOURS  = float(os.getenv("OUTAGE_LEAD_HOURS", "4"))        # h maximum advance pre-charging window
PREP_CHARGE_RATE_KW = float(os.getenv("PREP_CHARGE_RATE_KW", "3.0"))   # kW assumed grid charge rate for prep-charge timing
HISTORY_MONTHS     = int(os.getenv("HISTORY_MONTHS", "24"))
LOOP_INTERVAL_MIN  = int(os.getenv("LOOP_INTERVAL_MINUTES", "5"))
# When True: discharge_start is ranked by effective_price × load (not just price),
# filtered by a round-trip break-even vs the cheapest upcoming recharge; and the
# Rule-6 overnight hold floor is lowered from CHARGE_SOC_LIMIT to the minimum SOC
# needed at drain_start, allowing profitable pre-peak discharge during the hold window.
# Set SMART_DISCHARGE=0 in config.env to revert to the original conservative behaviour.
SMART_DISCHARGE    = os.getenv("SMART_DISCHARGE", "1") == "1"

# ── Distribution tariff (regulated, fixed annually by ERU) ───────────────────
# Grid charging cost = spot price + distribution fee.  During peak distribution
# hours the fee is higher, so the effective cost is adjusted upward before the
# cheap-price check (Rule 2).  Only the delta vs normal fee matters here.
# Default values: ČEZ Distribuce 2025 — normal=140.97, peak=913.27 CZK/MWh
DIST_FEE_NORMAL  = float(os.getenv("DIST_FEE_NORMAL",  "140.97"))  # CZK/MWh
DIST_FEE_PEAK    = float(os.getenv("DIST_FEE_PEAK",    "913.27"))  # CZK/MWh
# Comma-separated hours (0-23) when the peak distribution fee applies
_dist_peak_raw   = os.getenv("DIST_PEAK_HOURS", "8,12,15,19")
DIST_PEAK_HOURS  = {int(h.strip()) for h in _dist_peak_raw.split(",") if h.strip()}

# ── Arbitrage profitability ───────────────────────────────────────────────────
# Grid charging is only worth doing if the saving at discharge time exceeds
# the round-trip cost:
#   min_sell_price = (buy_price + DEVIATION_FEE) / BATTERY_EFFICIENCY
# DEVIATION_FEE: distributor charge for each kWh bought outside normal profile
# BATTERY_EFFICIENCY: round-trip efficiency (charge + discharge losses + standby)
DEVIATION_FEE      = float(os.getenv("DEVIATION_FEE",      "390"))   # CZK/MWh per direction
BATTERY_EFFICIENCY = float(os.getenv("BATTERY_EFFICIENCY", "0.92"))  # 0–1 (8% loss)

# ── Scheduled fixed loads ────────────────────────────────────────────────────
# Known daily loads that must be guaranteed in load_remaining_wh even when
# MySQL historical averages underestimate them.  Set DHW_WH=0 to disable.
DHW_START_HOUR = int(os.getenv("DHW_START_HOUR", "14"))   # inclusive
DHW_END_HOUR   = int(os.getenv("DHW_END_HOUR",   "15"))   # exclusive
DHW_WH         = int(os.getenv("DHW_WH",          "0"))   # Wh (e.g. 2550)

# ── Export blocking ──────────────────────────────────────────────────────────
# When prices are deeply negative the grid pays YOU to take electricity.
# Blocking export forces the inverter to curtail excess PV rather than
# push it to the grid at a loss.  Set EXPORT_BLOCK_ENABLED=1 to activate.
EXPORT_BLOCK_ENABLED   = os.getenv("EXPORT_BLOCK_ENABLED", "0") == "1"
EXPORT_BLOCK_THRESHOLD = float(os.getenv("EXPORT_BLOCK_THRESHOLD", "-500"))  # CZK/MWh

# ── PV Forecast (forecast.solar, free public API) ─────────────────────────────
# Set PV_LAT / PV_LON / PV_KWP to enable weather-based PV forecast.
# Falls back to historical PV_run patterns when blank or on API failure.
PV_LAT = float(os.getenv("PV_LAT", "0"))     # decimal latitude  (e.g. 49.8239)
PV_LON = float(os.getenv("PV_LON", "0"))     # decimal longitude (e.g. 18.1256)
PV_DEC = int(os.getenv("PV_DEC", "30"))      # panel tilt °  0=horizontal, 90=vertical
PV_AZ  = int(os.getenv("PV_AZ",  "0"))       # panel azimuth °  -90=E, 0=S, 90=W
PV_KWP = float(os.getenv("PV_KWP", "0"))     # installed peak power in kWp

# ── Single-node operation ───────────────────────────────────────────────────
NODE_NAME    = os.getenv("NODE_NAME", os.uname().nodename)
IS_LEADER    = True


# ── Electricity Prices (EL_dailypr table) ─────────────────────────────────────

_price_cache: dict[str, Optional[dict[int, float]]] = {}
_last_min_soc: Optional[int] = None        # fallback when read_setting is unsupported
_export_blocked: Optional[bool] = None     # None = first cycle (state unknown)


def fetch_prices(tomorrow: bool = False) -> Optional[dict[int, float]]:
    """
    Return {hour(0-23): price_eur_mwh} from the local EL_dailypr MySQL table.
    Cached in memory for 1 hour — prices don't change within an hour.
    """
    cache_key = "tomorrow" if tomorrow else "today"
    cache_tag = f"{cache_key}_{datetime.now().date()}_{datetime.now().hour}"
    if _price_cache.get("_tag_" + cache_key) == cache_tag and cache_key in _price_cache:
        return _price_cache[cache_key]

    sql = """
        SELECT etime, eprczk
        FROM EL_dailypr
        WHERE edate = DATE_ADD(CURDATE(), INTERVAL %s DAY)
        ORDER BY etime
    """
    try:
        conn = db_connect()
        cur  = conn.cursor()
        cur.execute(sql, (1 if tomorrow else 0,))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        if not rows:
            # Tomorrow's OTE prices are published at 13:33 — don't warn before then
            if tomorrow and datetime.now().hour < 14:
                log.debug("EL_dailypr: tomorrow prices not yet published (available after 13:33)")
            else:
                log.warning(f"EL_dailypr: no prices for {'tomorrow' if tomorrow else 'today'}")
            return None
        prices = {int(h): float(p) for h, p in rows}
        _price_cache[cache_key] = prices
        _price_cache["_tag_" + cache_key] = cache_tag
        return prices
    except Exception as exc:
        log.warning(f"Price fetch ({'tomorrow' if tomorrow else 'today'}) failed: {exc}")
        return None


# ── Planned grid outages ───────────────────────────────────────────────────────

import json
from datetime import timedelta

_OUTAGES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "planned_outages.json")


def load_planned_outages() -> list[dict]:
    """
    Load planned outages from planned_outages.json next to this script.
    Format: [{"start": "YYYY-MM-DD HH:MM", "end": "YYYY-MM-DD HH:MM", "note": "..."}]
    Returns empty list if file missing or malformed.
    """
    if not os.path.exists(_OUTAGES_FILE):
        return []
    try:
        with open(_OUTAGES_FILE) as f:
            entries = json.load(f)
        result = []
        for e in entries:
            result.append({
                "start": datetime.strptime(e["start"], "%Y-%m-%d %H:%M"),
                "end":   datetime.strptime(e["end"],   "%Y-%m-%d %H:%M"),
                "note":  e.get("note", ""),
            })
        return result
    except Exception as exc:
        log.warning(f"planned_outages.json parse error: {exc}")
        return []


def check_planned_outage(now: datetime, soc: float) -> tuple[bool, bool, str]:
    """
    Returns (pre_charge, in_outage, reason).
      pre_charge — outage starts within the time needed to charge from current SOC
      in_outage  — we are inside a planned outage window

    Lead time is calculated dynamically: how long at CHARGE_RATE_W to reach
    OUTAGE_PRE_CHARGE_SOC from current soc, capped at OUTAGE_LEAD_HOURS.
    """
    outages = load_planned_outages()
    charge_needed_wh = max(0.0, (OUTAGE_PRE_CHARGE_SOC - soc) / 100.0 * BATTERY_CAPACITY)
    dynamic_lead_h   = min(OUTAGE_LEAD_HOURS, charge_needed_wh / CHARGE_RATE_W)
    lead = timedelta(hours=max(dynamic_lead_h, 10 / 60))   # minimum 10-min lookahead
    for o in outages:
        note = f" ({o['note']})" if o["note"] else ""
        if o["start"] <= now <= o["end"]:
            return False, True, f"Inside planned outage{note} until {o['end'].strftime('%H:%M')}"
        if now < o["start"] <= now + lead:
            mins_away = int((o["start"] - now).total_seconds() / 60)
            return True, False, (
                f"Planned outage{note} in {mins_away} min "
                f"({o['start'].strftime('%H:%M')}–{o['end'].strftime('%H:%M')}) "
                f"— pre-charging to {OUTAGE_PRE_CHARGE_SOC}% (need {dynamic_lead_h*60:.0f} min)"
            )
    return False, False, ""


def compute_thresholds(today: dict[int, float], tomorrow: Optional[dict[int, float]],
                       hour: Optional[int] = None) -> tuple[float, float]:
    """
    Derive cheap/expensive price thresholds from the decision-relevant price window.

    Window = remaining hours today  +  tomorrow h00 … RECHARGE_HORIZON_H.
    Capping tomorrow at RECHARGE_HORIZON_H (default h13) prevents afternoon/evening
    prices from distorting overnight decisions.  Example: at h22, tomorrow's 7 000
    CZK afternoon peak would inflate cheap_thr so much that h22 (2 638 CZK) looks
    "neutral" — but compared to the actual overnight window it IS the cheapest hour.

    cheap_thr uses the maximum of today-only and combined windows.  This prevents
    tomorrow's very cheap or negative prices (e.g. h10–h13 = −1933 CZK on solar-surplus
    days) from pulling the combined 25th-percentile below today's cheapest hour and
    incorrectly excluding it from the cheap window.

    Returns (cheap_threshold, expensive_threshold) in CZK/MWh.
    Falls back to the configured fixed values when data is scarce.
    """
    now_h = hour if hour is not None else datetime.now().hour
    # Build an ordered list: rest of today + tomorrow up to recharge horizon
    upcoming: list[float] = []
    today_upcoming: list[float] = []
    for h in range(now_h, 24):
        if h in today:
            upcoming.append(today[h])
            today_upcoming.append(today[h])
    if tomorrow:
        for h in range(0, RECHARGE_HORIZON_H + 1):
            if h in tomorrow:
                upcoming.append(tomorrow[h])

    if len(upcoming) < 6:
        # Not enough data — use configured fallbacks
        return PRICE_CHEAP, PRICE_EXPENSIVE

    s = sorted(upcoming)
    n = len(s)

    def percentile(p: int) -> float:
        # Linear interpolation
        idx = (p / 100) * (n - 1)
        lo, hi = int(idx), min(int(idx) + 1, n - 1)
        return s[lo] + (idx - lo) * (s[hi] - s[lo])

    cheap_thr_combined = percentile(CHEAP_PERCENTILE)
    expensive_thr      = percentile(EXPENSIVE_PERCENTILE)

    # Also compute cheap_thr from today's remaining hours only.  Take the max of the
    # two so that tomorrow's very cheap/negative prices cannot drag the combined
    # threshold below today's cheapest buying opportunity.
    cheap_thr = cheap_thr_combined
    if len(today_upcoming) >= 4:
        st = sorted(today_upcoming)
        nt = len(st)
        idx_t = (CHEAP_PERCENTILE / 100) * (nt - 1)
        lo_t, hi_t = int(idx_t), min(int(idx_t) + 1, nt - 1)
        cheap_thr_today = st[lo_t] + (idx_t - lo_t) * (st[hi_t] - st[lo_t])
        cheap_thr = max(cheap_thr_combined, cheap_thr_today)

    return round(cheap_thr, 1), round(expensive_thr, 1)


# ── PV Generation Forecast (forecast.solar) ───────────────────────────────────

_pv_forecast_cache: dict = {}   # key: (date_str, hour) → (today_wh, tomorrow_wh)


def fetch_pv_forecast() -> tuple[dict[int, float], dict[int, float]]:
    """
    Fetch hourly PV generation forecast from forecast.solar (free, no API key).
    Returns (today_wh_per_hour, tomorrow_wh_per_hour) — Wh produced each clock hour.
    Cached per (date, hour) so we call the API at most once per hour.
    Falls back to ({}, {}) on config missing or network error.
    """
    if not PV_KWP or not PV_LAT or not PV_LON:
        return {}, {}   # not configured

    today_str    = date.today().isoformat()
    tomorrow_str = (date.today() + timedelta(days=1)).isoformat()
    cache_key    = (today_str, datetime.now().hour)

    if cache_key in _pv_forecast_cache:
        return _pv_forecast_cache[cache_key]

    url = (
        f"https://api.forecast.solar/estimate/watthours/period/"
        f"{PV_LAT}/{PV_LON}/{PV_DEC}/{PV_AZ}/{PV_KWP}"
    )
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())

        today_wh: dict[int, float]    = {}
        tomorrow_wh: dict[int, float] = {}

        for ts_str, wh in data["result"].items():
            # ts_str: "YYYY-MM-DD HH:MM:SS"  — Wh for the period ending at this timestamp
            ts_date = ts_str[:10]
            ts_hour = int(ts_str[11:13])
            if ts_date == today_str:
                today_wh[ts_hour]    = today_wh.get(ts_hour, 0.0) + wh
            elif ts_date == tomorrow_str:
                tomorrow_wh[ts_hour] = tomorrow_wh.get(ts_hour, 0.0) + wh

        _pv_forecast_cache.clear()
        _pv_forecast_cache[cache_key] = (today_wh, tomorrow_wh)
        log.info(
            f"  PV forecast (forecast.solar): today={sum(today_wh.values()):.0f} Wh  "
            f"tomorrow={sum(tomorrow_wh.values()):.0f} Wh"
        )
        return today_wh, tomorrow_wh

    except Exception as exc:
        log.warning(f"  forecast.solar unavailable ({exc}) — will use historical patterns")
        return {}, {}


def pv_forecast_remaining(forecast_wh: dict[int, float], from_hour: int) -> float:
    """Sum forecast Wh for all hours >= from_hour (remaining generation today)."""
    return sum(wh for h, wh in forecast_wh.items() if h >= from_hour)


def price_summary(prices: dict[int, float], cheap_thr: float, expensive_thr: float,
                  from_h: int = 0, to_h: int = 24) -> str:
    subset = {h: p for h, p in prices.items() if from_h <= h < to_h}
    if not subset:
        return "n/a"
    vals  = list(subset.values())
    cheap = [h for h, p in subset.items() if p <= cheap_thr]
    exp   = [h for h, p in subset.items() if p >= expensive_thr]
    return (
        f"min={min(vals):.0f} max={max(vals):.0f} avg={sum(vals)/len(vals):.0f} CZK/MWh  "
        f"thresholds=({cheap_thr:.0f}/{expensive_thr:.0f})  "
        f"cheap_hrs={cheap}  expensive_hrs={exp}"
    )


# ── MySQL Historical Patterns ─────────────────────────────────────────────────

def db_connect():
    return mysql.connector.connect(
        host=MYSQL_HOST, database=MYSQL_DB,
        user=MYSQL_USER, password=MYSQL_PASSWORD,
        connection_timeout=10,
    )


_DECISIONS_DDL = """
CREATE TABLE IF NOT EXISTS solax_decisions (
    id           BIGINT       NOT NULL AUTO_INCREMENT,
    logged_at    DATETIME     NOT NULL,
    node_name    VARCHAR(100) NOT NULL,
    is_leader    TINYINT(1)   NOT NULL,
    test_mode    TINYINT(1)   NOT NULL,
    soc_pct      DECIMAL(5,1),
    pv_w         INT,
    grid_w       INT,
    load_w       INT,
    battery_w    INT,
    price_now    DECIMAL(8,2),
    current_mode VARCHAR(30),
    target_mode  VARCHAR(30),
    target_min_soc INT,
    reason       TEXT,
    PRIMARY KEY (id),
    INDEX idx_logged_at (logged_at)
) ENGINE=InnoDB COMMENT='Solax controller decision log';
"""


def ensure_decisions_table() -> None:
    try:
        conn = db_connect()
        cur  = conn.cursor()
        cur.execute(_DECISIONS_DDL)
        conn.commit()
        cur.close()
        conn.close()
    except Exception as exc:
        log.warning(f"Could not create solax_decisions table: {exc}")


# ── Manual override table ─────────────────────────────────────────────────────

_CONTROL_DDL = """
CREATE TABLE IF NOT EXISTS solar_control (
    id           INT          NOT NULL DEFAULT 1,
    manual_mode  TINYINT(1)   NOT NULL DEFAULT 0  COMMENT '1 = manual, 0 = auto',
    forced_mode  VARCHAR(20)      NULL             COMMENT 'ECO_CHARGE / BACKUP / GENERAL',
    forced_min_soc INT            NULL             COMMENT '% min SOC when manual',
    updated_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    updated_by   VARCHAR(64)      NULL             COMMENT 'who set the override (web/node/user)',
    PRIMARY KEY (id)
) ENGINE=InnoDB COMMENT='Solax controller manual override state (single-row)';
"""


def ensure_control_table() -> None:
    try:
        conn = db_connect()
        cur  = conn.cursor()
        cur.execute(_CONTROL_DDL)
        # Ensure the single control row exists
        cur.execute(
            "INSERT IGNORE INTO solar_control (id, manual_mode) VALUES (1, 0)"
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as exc:
        log.warning(f"Could not create solar_control table: {exc}")


def read_manual_override() -> tuple[bool, Optional[str], Optional[int]]:
    """
    Returns (manual_mode, forced_mode, forced_min_soc).
    manual_mode=True means: run decide() for logging but skip inverter writes.
    """
    try:
        conn = db_connect()
        cur  = conn.cursor()
        cur.execute("SELECT manual_mode, forced_mode, forced_min_soc FROM solar_control WHERE id=1")
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row:
            return bool(row[0]), row[1], row[2]
    except Exception as exc:
        log.warning(f"Could not read solar_control: {exc}")
    return False, None, None


def log_decision(
    now: datetime,
    is_leader: bool,
    state: dict,
    price_now: Optional[float],
    target_mode: OperationMode,
    target_min_soc: int,
    reason: str,
) -> None:
    """Insert one row into solax_decisions for every controller cycle."""
    sql = """
        INSERT INTO solax_decisions
            (logged_at, node_name, is_leader, test_mode,
             soc_pct, pv_w, grid_w, load_w, battery_w,
             price_now, current_mode, target_mode, target_min_soc, reason)
        VALUES (%s,%s,%s,%s, %s,%s,%s,%s,%s, %s,%s,%s,%s,%s)
    """
    try:
        conn = db_connect()
        cur  = conn.cursor()
        cur.execute(sql, (
            now, NODE_NAME, int(is_leader), int(TEST_MODE),
            round(state["soc"], 1),
            int(state["pv_w"]), int(state["grid_w"]),
            int(state["load_w"]), int(state["battery_w"]),
            round(price_now, 2) if price_now is not None else None,
            state["mode"], target_mode, target_min_soc,
            reason,
        ))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as exc:
        log.warning(f"Could not log decision to MySQL: {exc}")


def query_hourly_patterns(month: int) -> dict[int, dict]:
    """
    Historical average per hour-of-day for a given calendar month.
    Uses the last HISTORY_MONTHS months of data for seasonal accuracy.
    Returns {hour: {avg_pv_w, avg_load_w, avg_soc_pct, avg_grid_w, n}}.
    """
    sql = """
        SELECT
            HOUR(pdtime)     AS h,
            AVG(ppv)         AS avg_pv_w,
            AVG(ploadT)      AS avg_load_w,
            AVG(pbatts)      AS avg_soc_pct,
            AVG(pgridT)      AS avg_grid_w,
            COUNT(*)         AS n
        FROM PV_run
        WHERE psource  = %s
          AND MONTH(pdtime) = %s
          AND pdtime   >= DATE_SUB(CURDATE(), INTERVAL %s MONTH)
        GROUP BY HOUR(pdtime)
        ORDER BY h
    """
    result: dict[int, dict] = {}
    try:
        conn = db_connect()
        cur  = conn.cursor(dictionary=True)
        cur.execute(sql, (PV_SOURCE, month, HISTORY_MONTHS))
        for row in cur.fetchall():
            result[int(row["h"])] = {
                "avg_pv_w":    float(row["avg_pv_w"]    or 0),
                "avg_load_w":  float(row["avg_load_w"]  or 0),
                "avg_soc_pct": float(row["avg_soc_pct"] or 0),
                "avg_grid_w":  float(row["avg_grid_w"]  or 0),
                "n":           int(row["n"]),
            }
        cur.close()
        conn.close()
    except Exception as exc:
        log.warning(f"MySQL query failed: {exc}")
    return result


def expected_pv_wh(patterns: dict[int, dict], from_hour: int) -> float:
    """Estimate remaining PV generation today in Wh (avg W × 1 h per slot)."""
    return sum(
        patterns[h]["avg_pv_w"]
        for h in range(from_hour, 24)
        if h in patterns and patterns[h]["avg_pv_w"] > 50   # ignore noise
    )


def expected_load_wh(patterns: dict[int, dict], from_hour: int, to_hour: int = 24) -> float:
    """Estimate house consumption in Wh between from_hour and to_hour.
    For hours within the DHW heating window, ensures at least DHW_WH is counted
    even when historical patterns underestimate it (avoids false pv_fills_battery).
    """
    total = 0.0
    for h in range(from_hour, to_hour):
        hist_w = patterns[h]["avg_load_w"] if h in patterns else 0.0
        if DHW_WH > 0 and DHW_START_HOUR <= h < DHW_END_HOUR:
            total += max(hist_w, float(DHW_WH))
        else:
            total += hist_w
    return total



def expected_night_load_wh(patterns: dict[int, dict]) -> float:
    """Estimated overnight house consumption (21:00–06:00) in Wh."""
    hours = list(range(21, 24)) + list(range(0, 6))
    return sum(patterns[h]["avg_load_w"] for h in hours if h in patterns)


# ── Inverter I/O ──────────────────────────────────────────────────────────────

# Solax inverter control (mode changes, min-SOC writes, export limiting) requires
# direct HTTP POST calls to model-specific local API endpoints such as
# /api/realTimeData.htm or related URLs. Keep these as stubs until the exact
# inverter model and writable endpoints are confirmed.
_WRITE_STUB_NOTE = (
    "Solax write not yet implemented; control requires model-specific local HTTP "
    "POST endpoints once the inverter model is confirmed."
)


async def read_inverter() -> dict:
    """Read current state from Solax inverter via HTTP local API."""
    inverter = await solax.discover(INVERTER_IP, INVERTER_PORT, INVERTER_PASSWORD)
    response = await inverter.get_data()
    data = response.data
    return {
        "soc": data.get("battery_percent", 0),
        "ppv": data.get("power_dc1", 0) + data.get("power_dc2", 0),
        "pgrid": data.get("feedin_power", 0),
        "pload": data.get("load_power", 0),
        "pbatt": data.get("battery_power", 0),
    }


async def apply_mode(target: OperationMode, current: OperationMode) -> None:
    """Stub for Solax mode writes until model-specific local API control is implemented."""
    if target == current:
        log.info(f"  → Mode already {current}, no change needed")
        return
    log.warning(f"  Solax write not yet implemented: mode {current} → {target}. {_WRITE_STUB_NOTE}")


async def apply_min_soc(target_soc: int) -> None:
    """Stub for Solax minimum-SOC writes until model-specific local API control is implemented."""
    target_soc = max(MIN_SOC_FLOOR, min(target_soc, CHARGE_SOC_LIMIT))
    log.warning(f"  Solax write not yet implemented: min SOC -> {target_soc}%. {_WRITE_STUB_NOTE}")


async def apply_export_block(should_block: bool) -> None:
    """Stub for Solax export blocking until model-specific local API control is implemented."""
    state = "ON" if should_block else "OFF"
    log.warning(f"  Solax write not yet implemented: export block {state}. {_WRITE_STUB_NOTE}")


# ── Decision Logic ────────────────────────────────────────────────────────────

def decide(
    now: datetime,
    soc: float,
    pv_w: float,
    grid_w: float,
    prices: dict[int, float],
    tomorrow_prices: Optional[dict[int, float]],
    pv_remaining_wh: float,
    load_remaining_wh: float,
    cheap_thr: float,
    expensive_thr: float,
    eod_hour: int,
    patterns: dict,
) -> tuple[str, int, str]:
    """
    Returns (mode_label, target_min_soc_pct, reason_string).

    Thresholds are computed dynamically from the next ~24h price window
    (passed in as cheap_thr / expensive_thr) so the controller adapts to
    seasonal and day-to-day price variation automatically.
    pv_remaining_wh:   estimated remaining PV generation today (Wh) — from
                       forecast.solar when available, historical patterns otherwise.
    load_remaining_wh: estimated remaining house consumption today until hour 16 (Wh)
                       from historical PV_run patterns.  Used to calculate net PV
                       available for battery charging (avoids false ECO_CHARGE skip).
    grid_w:            current grid power (W); negative = exporting to grid.
    tomorrow_prices:   OTE prices for tomorrow (available after ~13:33); None if not yet published.
    """
    hour     = now.hour
    is_night = hour >= 21 or hour < 6

    current_price = prices.get(hour)
    if current_price is None:
        target_min = NIGHT_MIN_SOC if is_night else MIN_SOC
        return "GENERAL", target_min, "No price data available — auto mode"

    # ── Energy budget ──────────────────────────────────────────────────────────
    soc_wh           = soc / 100.0 * BATTERY_CAPACITY
    headroom_wh      = max(0.0, CHARGE_SOC_LIMIT / 100.0 * BATTERY_CAPACITY - soc_wh)
    emergency_wh     = MIN_SOC_FLOOR / 100.0 * BATTERY_CAPACITY
    pv_remaining     = pv_remaining_wh
    # Net PV available for battery = forecast minus expected house consumption until
    # end of typical production (hour 16).  This prevents skipping ECO_CHARGE on
    # high-load days when raw forecast looks sufficient but house eats most of the PV.
    pv_net_wh        = max(0.0, pv_remaining - load_remaining_wh)
    pv_fills_battery = pv_net_wh >= headroom_wh and not is_night and hour < eod_hour

    # ── Price windows ──────────────────────────────────────────────────────────
    def _eff(h: int) -> float:
        """Spot price + distribution surcharge for hour h."""
        delta = (DIST_FEE_PEAK - DIST_FEE_NORMAL) if h in DIST_PEAK_HOURS else 0.0
        return prices.get(h, 9999.0) + delta

    upcoming_cheap     = [h for h in range(hour + 1, 24) if _eff(h) <= cheap_thr]
    # today_expensive_thr: 75th-percentile of today's prices, used alongside the
    # combined threshold so that today's expensive hours are not masked when
    # tomorrow's high prices inflate the combined threshold.
    today_all_prices = sorted(prices.values())
    today_expensive_thr = (
        today_all_prices[int(0.75 * len(today_all_prices))]
        if len(today_all_prices) >= 8 else expensive_thr
    )
    upcoming_expensive = [h for h in range(hour + 1, 24)
                          if prices.get(h, 0) >= expensive_thr
                          or prices.get(h, 0) >= today_expensive_thr]

    # Effective price for grid-charge decision: add distribution peak surcharge
    # during hours when the regulated distribution fee is higher (e.g. 12, 15).
    dist_delta    = (DIST_FEE_PEAK - DIST_FEE_NORMAL) if hour in DIST_PEAK_HOURS else 0.0
    effective_price = current_price + dist_delta

    log.info(
        f"  Budget: soc={soc:.0f}%  soc_wh={soc_wh:.0f}  headroom={headroom_wh:.0f} Wh  "
        f"pv_remaining={pv_remaining:.0f} Wh  load_remaining={load_remaining_wh:.0f} Wh  "
        f"pv_net={pv_net_wh:.0f} Wh  pv_fills_battery={pv_fills_battery}"
    )
    dist_note = f"  +dist_peak={dist_delta:.0f}" if dist_delta else ""
    log.info(
        f"  Price:  spot={current_price:.1f}{dist_note}  effective={effective_price:.1f} CZK/MWh  "
        f"thresholds=({cheap_thr:.0f}↓ / {expensive_thr:.0f}↑)  "
        f"cheap_later={upcoming_cheap}  expensive_later={upcoming_expensive}"
    )

    # ── Rule 0: Planned grid outage — override everything ─────────────────────
    pre_charge, in_outage, outage_reason = check_planned_outage(now, soc)
    if in_outage:
        # Grid is gone — GENERAL mode, keep battery as full as possible
        return (
            "GENERAL",
            OUTAGE_PRE_CHARGE_SOC,
            f"PLANNED OUTAGE ACTIVE — {outage_reason}",
        )
    if pre_charge and soc < OUTAGE_PRE_CHARGE_SOC:
        return (
            "ECO_CHARGE",
            OUTAGE_PRE_CHARGE_SOC,
            f"PRE-OUTAGE CHARGE — {outage_reason}",
        )

    # ── Rule 1: Emergency SOC — hold at floor, wait for cheap hours ───────────
    # Use GENERAL (not BACKUP): grid covers load but no active grid-charge.
    # Rule 2 will switch to ECO when cheap hours arrive.
    # Exception: during overnight prep-charge scenario (is_night, no cheap hours
    # before morning peak), fall through to Rule 2.5 so the deferral logic can
    # fire ECO_CHARGE at the cheapest overnight hour — even at emergency floor.
    if soc_wh <= emergency_wh:
        # During daytime always block
        if not is_night:
            return (
                "GENERAL",
                MIN_SOC_FLOOR,
                f"SOC {soc:.0f}% at emergency floor ({MIN_SOC_FLOOR}%) — holding, grid covers load until cheap hours",
            )
        # At night: check if cheap hours exist BEFORE the next morning peak.
        # After midnight (h00-h05), the peak is in today's prices; otherwise tomorrow's.
        # If yes, block (Rule 2 will charge at those hours).
        # If no, fall through to Rule 2.5 (overnight prep-charge).
        next_morning_peak = None
        if hour < 6:
            today_morning_exp = sorted(
                h for h in range(hour + 1, RECHARGE_HORIZON_H + 1)
                if prices.get(h, 0.0) >= expensive_thr
            )
            if today_morning_exp:
                next_morning_peak = today_morning_exp[0]
        if next_morning_peak is None and tomorrow_prices:
            tmrw_expensive_early = sorted(
                h for h in range(0, RECHARGE_HORIZON_H + 1)
                if tomorrow_prices.get(h, 0.0) >= expensive_thr
            )
            if tmrw_expensive_early:
                next_morning_peak = tmrw_expensive_early[0]
        if next_morning_peak is not None:
            cheap_before_peak = [h for h in upcoming_cheap if h < next_morning_peak]
            if cheap_before_peak:
                return (
                    "GENERAL",
                    MIN_SOC_FLOOR,
                    f"SOC {soc:.0f}% at emergency floor ({MIN_SOC_FLOOR}%) — holding, grid covers load until cheap hours",
                )
        # No cheap hours before morning peak → fall through to Rule 2.5

    # ── Rule 1.5: Very low / negative price — absorb surplus PV, avoid exporting ──
    # When the spot price is near zero or negative we are paid nothing (or penalised)
    # for grid export.  If cheaper sub-threshold hours are coming soon AND we still
    # have enough time to fully charge before the evening peak, defer to the cheaper
    # window (same can_wait logic as Rule 2).  Otherwise charge now.
    if effective_price <= PRICE_NO_EXPORT and soc < CHARGE_SOC_LIMIT:
        upcoming_no_export = [h for h in range(hour + 1, 24)
                              if _eff(h) < effective_price and _eff(h) <= PRICE_NO_EXPORT]
        if upcoming_no_export:
            # Only count hours that are meaningfully expensive (not just marginally above
            # the percentile threshold) as truly urgent — otherwise h03 at 245 CZK would
            # prevent deferral to h05-h06 at 87-97 CZK when the real peak is h19+ at 2500 CZK.
            meaningful_expensive_15 = [h for h in upcoming_expensive
                                       if prices.get(h, 0) >= PRICE_NO_EXPORT * 3]
            first_expensive_15 = meaningful_expensive_15[0] if meaningful_expensive_15 else 24
            hours_to_full_15   = math.ceil(headroom_wh / CHARGE_RATE_W) if CHARGE_RATE_W > 0 else 0
            last_safe_15       = first_expensive_15 - hours_to_full_15
            total_no_exp_hrs   = 1 + len(upcoming_no_export)
            cheapest_no_exp    = min(_eff(h) for h in upcoming_no_export)
            can_wait_15 = (total_no_exp_hrs > hours_to_full_15
                           and any(h <= last_safe_15 for h in upcoming_no_export)
                           and effective_price > cheapest_no_exp)
            if can_wait_15:
                hold_soc = max(MIN_SOC_SAVE, soc)
                return (
                    "GENERAL",
                    hold_soc,
                    f"Low price {effective_price:.1f} CZK/MWh (≤ {PRICE_NO_EXPORT:.0f}) but cheaper window ahead "
                    f"— next low-price hrs {upcoming_no_export[:3]} (cheapest {cheapest_no_exp:.1f} CZK/MWh), "
                    f"must start charging by h{last_safe_15} — holding SOC≥{hold_soc}%",
                )
        return (
            "ECO_CHARGE",
            CHARGE_SOC_LIMIT,
            f"Very low/negative price {effective_price:.1f} CZK/MWh (≤ {PRICE_NO_EXPORT:.0f}) "
            f"— absorbing surplus PV into battery instead of exporting to grid",
        )

    # ── Rule 2: Cheap electricity — charge from grid, save battery for later ──
    # Uses effective_price (spot + distribution peak surcharge) so that high-
    # distribution hours (e.g. 8, 12, 15, 19) are only charged from grid if the spot
    # price is low enough to compensate for the extra distribution cost.
    # Defers grid charging when cheaper hours are still coming and there is
    # enough time left to charge before the expensive window starts — this
    # lets PV charge the battery for free in the meantime.
    # Arbitrage profitability check: only grid-charge if the best upcoming
    # expensive price justifies the round-trip cost (deviation fee + battery loss).
    best_upcoming_price = max((prices.get(h, 0) for h in upcoming_expensive), default=0.0)
    min_sell_price      = (effective_price + DEVIATION_FEE) / BATTERY_EFFICIENCY
    arbitrage_profitable = best_upcoming_price >= min_sell_price
    if effective_price <= cheap_thr and soc < CHARGE_SOC_LIMIT and not pv_fills_battery and headroom_wh >= 50 and arbitrage_profitable:
        first_expensive   = upcoming_expensive[0] if upcoming_expensive else 24
        hours_to_full     = math.ceil(headroom_wh / CHARGE_RATE_W) if CHARGE_RATE_W > 0 else 0
        last_safe_start   = first_expensive - hours_to_full
        # Count all cheap hours from now onward (including current hour).
        # If remaining cheap hours ≤ hours needed, we cannot afford to wait.
        total_cheap_hours = 1 + len(upcoming_cheap)   # current hour + future cheap hours
        # Also don't wait if current price is already at or below the cheapest
        # upcoming cheap hour — there is nothing better coming.
        cheapest_upcoming = min((_eff(h) for h in upcoming_cheap), default=float('inf'))
        can_wait          = (total_cheap_hours > hours_to_full
                             and any(h <= last_safe_start for h in upcoming_cheap)
                             and effective_price > cheapest_upcoming)
        dist_str = f" (+{dist_delta:.0f} dist)" if dist_delta else ""
        if can_wait:
            # Hold current SOC while waiting — returning MIN_SOC_SAVE would let the
            # battery discharge back down, causing charge/discharge oscillation every cycle.
            hold_soc = max(MIN_SOC_SAVE, soc)
            return (
                "GENERAL",
                hold_soc,
                f"Cheap price {effective_price:.1f} CZK/MWh but cheaper/PV window available "
                f"— next cheap hrs {upcoming_cheap[:3]}, must start by h{last_safe_start} "
                f"(exp. h{first_expensive}, ~{hours_to_full}h to full) — holding SOC≥{hold_soc}% until then",
            )
        return (
            "ECO_CHARGE",
            CHARGE_SOC_LIMIT,
            f"Cheap effective price {effective_price:.1f} CZK/MWh "
            f"(spot={current_price:.1f}{dist_str} ≤ {cheap_thr:.0f}) "
            f"last cheap window before h{first_expensive} — grid-charge to {CHARGE_SOC_LIMIT}% "
            f"(arbitrage: buy@{effective_price:.0f} → sell@{best_upcoming_price:.0f} > break-even {min_sell_price:.0f})",
        )

    # Pre-compute the full SMART_DISCHARGE block so Rule 2.5 can detect when
    # charging now would immediately be undone by Rule 3 (oscillation guard).
    # Must come before Rule 2.5 because the oscillation guard needs discharge_start.
    break_even_sell: float | None = None
    cheapest_recharge_eff: float | None = None
    if SMART_DISCHARGE and upcoming_cheap:
        cheapest_recharge_eff = min(_eff(h) for h in upcoming_cheap)
        break_even_sell = (cheapest_recharge_eff + DEVIATION_FEE) / BATTERY_EFFICIENCY

    # Compute discharge_start: earliest hour among the most valuable remaining
    # hours the battery can cover, then let it run down.
    #
    # SMART_DISCHARGE mode (default on):
    #   - Computes a round-trip break-even: cheapest upcoming recharge price
    #     (rounded through battery efficiency + deviation fee).  Only hours
    #     where discharging actually saves money vs recharging are included.
    #   - Ranks qualifying hours by effective_price × expected_load (value
    #     extracted), so high-price AND high-load hours are prioritised.
    #   - Falls back to price-only sort if no qualifying hours found.
    # Legacy mode (SMART_DISCHARGE=0):
    #   - Sorts all remaining hours by raw price descending.
    if SMART_DISCHARGE and upcoming_cheap:
        qualifying = [
            (h, _eff(h))
            for h in range(hour, 24)
            if h in prices and _eff(h) > break_even_sell
        ]
        if qualifying:
            ranked_hours = sorted(
                qualifying,
                key=lambda x: -x[1] * max(expected_load_wh(patterns, x[0], x[0] + 1), 100.0),
            )
        else:
            # All remaining hours below break-even: fall back to price-only sort
            ranked_hours = sorted(
                [(h, prices[h]) for h in range(hour, 24) if h in prices],
                key=lambda x: -x[1],
            )
    else:
        ranked_hours = sorted(
            [(h, prices[h]) for h in range(hour, 24) if h in prices],
            key=lambda x: -x[1],
        )
    if ranked_hours:
        usable_wh = max(0.0, (soc - NIGHT_MIN_SOC) / 100.0 * BATTERY_CAPACITY)
        covered: list[int] = []
        cumulative = 0.0
        for h, _ in ranked_hours:
            hour_load = max(expected_load_wh(patterns, h, h + 1), 100.0)
            cumulative += hour_load
            if cumulative <= usable_wh:
                covered.append(h)
            else:
                if not covered:   # battery < 1 h load — commit to this hour anyway
                    covered.append(h)
                break
        discharge_start = min(covered)
    else:
        discharge_start = 24

    top_hours = [h for h, _ in ranked_hours[:3]]

    # Is the current hour itself profitable enough to discharge, even if not
    # crossing the "expensive" threshold?  Used in Rule 3 below.
    sd_hour_profitable = (
        break_even_sell is not None and _eff(hour) > break_even_sell
    )

    # ── Rule 2.5: Overnight prep-charge — no cheap window before morning peak ───
    # When no cheap hours exist tonight before the morning expensive window AND the
    # battery cannot cover all load until cheap hours restart, charge now at the
    # current neutral overnight price — better than buying at expensive price
    # during the morning peak.
    # Skipped if battery is already sufficient for the full load (summer: high SOC,
    # Hysteresis: only fire if SOC < CHARGE_SOC_LIMIT - 2 to avoid re-triggering
    # from 1% measurement noise after reaching the target.
    PREP_CHARGE_SOC_LIMIT = CHARGE_SOC_LIMIT - 2
    # low overnight load → no need to pull from grid).
    # Also skip if the current hour is already profitable to discharge AND Rule 3
    # would actually fire (hour >= discharge_start) — charging now at the same
    # price only to discharge immediately causes a buy/sell churn with ~8-10%
    # round-trip loss.  Rule 3 fires instead; cheap-window recharge (Rule 2)
    # refills the battery later at a much lower price.
    # The `hour >= discharge_start` guard is essential: without it, an extreme
    # negative future price (e.g. h13=−1250 CZK) can make break_even negative,
    # causing all overnight hours to look "profitable" even though Rule 3 would
    # NOT fire yet (discharge_start is still h20).  Pre-charging at h2 for the
    # h6-h7 morning peak is then incorrectly suppressed.
    _r25_discharge_profitable = (
        break_even_sell is not None
        and _eff(hour) > break_even_sell
        and hour >= discharge_start
    )
    if _r25_discharge_profitable:
        log.info(
            f"  Rule 2.5 skipped: current price {current_price:.0f} CZK/MWh is profitable "
            f"to discharge (break-even {break_even_sell:.0f}, discharge_start=h{discharge_start}) "
            f"— Rule 3 fires, battery recharges cheaply later"
        )
    if is_night and soc < PREP_CHARGE_SOC_LIMIT and not _r25_discharge_profitable:
        # Find next morning cheap window (today remaining OR tomorrow)
        tomorrow_cheap_hrs = sorted(
            h for h in range(0, 24)
            if tomorrow_prices and tomorrow_prices.get(h, 9999.0) <= cheap_thr
        )
        # Next cheap hour: today if available, else first tomorrow cheap hour
        next_cheap_today = min(upcoming_cheap) if upcoming_cheap else None
        next_cheap_tmrw  = tomorrow_cheap_hrs[0] if tomorrow_cheap_hrs else None
        next_cheap_any   = next_cheap_today  # prefer today

        # Find next morning peak:
        # After midnight (h00-h05) the peak is in today's prices — check those first.
        # In the evening (h21-h23) the peak is always tomorrow's.
        peak_is_today = False
        next_expensive_h = None
        if hour < 6:
            today_morning_exp = sorted(
                h for h in range(hour + 1, RECHARGE_HORIZON_H + 1)
                if prices.get(h, 0.0) >= expensive_thr
            )
            if today_morning_exp:
                next_expensive_h = today_morning_exp[0]
                peak_is_today = True
        if not peak_is_today:
            # Rule 2.5 targets tomorrow's early morning peak only.
            # Tonight's remaining expensive hours are handled by discharge rules —
            # using them here as the target causes the safety override to trigger
            # immediately (peak only 1-2h away, no time to charge sensibly).
            if tomorrow_prices:
                tmrw_expensive_early = sorted(
                    h for h in range(0, RECHARGE_HORIZON_H + 1)
                    if tomorrow_prices.get(h, 0.0) >= expensive_thr
                )
                next_expensive_h = tmrw_expensive_early[0] if tmrw_expensive_early else None
            else:
                next_expensive_h = None
        cheap_before_exp = [h for h in upcoming_cheap if next_expensive_h and h < next_expensive_h]

        # Economic gate: if a cheap recharge window exists AFTER the morning peak,
        # pre-charging now is only worthwhile if the effective charge cost (buy + fee +
        # efficiency loss) is cheaper than the cheapest direct grid import during the
        # bridge window (peak hours + deviation fee).  If it's cheaper to just import
        # directly at morning prices, skip Rule 2.5 — the battery will refill for free
        # at the post-peak cheap window anyway.
        if peak_is_today:
            cheap_after_peak = [h for h in upcoming_cheap if next_expensive_h and h > next_expensive_h]
        else:
            cheap_after_peak = [h for h in tomorrow_cheap_hrs if next_expensive_h and h > next_expensive_h]
        if cheap_after_peak:
            effective_now     = (current_price + DEVIATION_FEE) / BATTERY_EFFICIENCY
            if peak_is_today:
                bridge_prices = [prices[h] for h in range(next_expensive_h, cheap_after_peak[0])
                                 if prices.get(h) is not None]
            else:
                bridge_prices = [tomorrow_prices[h] for h in range(next_expensive_h, cheap_after_peak[0])
                                 if tomorrow_prices and tomorrow_prices.get(h) is not None]
            # Only use the genuinely expensive bridge hours (≥ expensive_thr, the
            # dynamically computed 75th-percentile threshold) in the min calculation.
            # Transitional cheap-ish hours between the peak and the cheap window (e.g.
            # h09=2220 or h10=1190 following an h06–h08 peak) must not be included:
            # they come AFTER the expensive period and cannot retroactively serve
            # demand that already occurred at h06–h07.  Including them drags
            # min_bridge_cost far below the actual peak price, incorrectly blocking
            # pre-charges that are genuinely profitable vs the true peak hours.
            expensive_bridge = [p for p in bridge_prices if p >= expensive_thr]
            if expensive_bridge:
                min_bridge_cost = min(expensive_bridge) + DEVIATION_FEE
            elif bridge_prices:
                min_bridge_cost = min(bridge_prices) + DEVIATION_FEE
            else:
                min_bridge_cost = 9999.0
            # When the cheapest upcoming recharge is negative-priced, pre-charging
            # has an opportunity cost: each kWh stored now is one less kWh the
            # battery can absorb at the negative-price window (which earns income).
            # Add that opportunity cost to effective_now before comparing to the
            # peak import price so that pre-charging is only justified when it
            # beats BOTH the direct import AND the foregone negative-price income.
            neg_price_opp_cost = (max(0.0, -cheapest_recharge_eff)
                                  if cheapest_recharge_eff is not None else 0.0)
            precharge_justified = (effective_now + neg_price_opp_cost) < min_bridge_cost
            if not precharge_justified:
                opp_note = (f", neg-price opp cost={neg_price_opp_cost:.0f} CZK/MWh"
                            f" (cheapest recharge={cheapest_recharge_eff:.0f})"
                            if neg_price_opp_cost > 0 else "")
                log.info(
                    f"  Rule 2.5 skipped: post-peak cheap at h{cheap_after_peak[0]}, "
                    f"effective charge now={effective_now:.0f}{opp_note} ≥ "
                    f"cheapest peak-bridge import={min_bridge_cost:.0f} CZK/MWh — "
                    f"direct import cheaper (or negative prices ahead), "
                    f"battery recharges at h{cheap_after_peak[0]}"
                )
        else:
            precharge_justified = True  # No post-peak cheap window — proceed as normal

        if not cheap_before_exp and next_expensive_h and precharge_justified and (next_cheap_any is not None or next_cheap_tmrw is not None):
            # Estimate load from now until next cheap window
            # Handle midnight crossing: load tonight + load tomorrow morning
            if next_cheap_any is not None:
                load_to_cheap = expected_load_wh(patterns, hour + 1, to_hour=next_cheap_any)
            else:
                # next cheap is tomorrow — bridge midnight
                load_to_cheap = (
                    expected_load_wh(patterns, hour + 1, to_hour=24) +
                    expected_load_wh(patterns, 0, to_hour=next_cheap_tmrw)
                )
            # Target SOC: enough to cover load + keep NIGHT_MIN_SOC floor
            needed_wh   = load_to_cheap + NIGHT_MIN_SOC / 100.0 * BATTERY_CAPACITY
            target_soc  = min(CHARGE_SOC_LIMIT, max(soc, round(needed_wh * 100.0 / BATTERY_CAPACITY)))
            if soc_wh < needed_wh:
                # ── Defer to cheapest overnight hour ──────────────────────────
                # Charge only when current hour is among the cheapest available
                # before the morning peak — avoid paying h22 prices when h02 is
                # significantly cheaper and there is still plenty of time.
                # Use actual battery headroom (target→current), not load deficit
                # (load until cheap window can exceed battery capacity entirely)
                wh_to_charge    = max(0.0, (target_soc - soc) / 100.0 * BATTERY_CAPACITY)
                hours_needed    = max(1, math.ceil(wh_to_charge / (PREP_CHARGE_RATE_KW * 1000)))
                # hours until morning peak (handles midnight wrap)
                if next_expensive_h < hour:        # peak is tomorrow (e.g. h06 < h22)
                    hours_until_peak = (24 - hour) + next_expensive_h
                else:                              # peak still ahead today (or early tomorrow after midnight)
                    hours_until_peak = next_expensive_h - hour
                # Collect prices for every remaining overnight hour before peak
                overnight_prices: list[float] = []
                if peak_is_today:
                    # Peak is today (early morning): only today's hours between now and peak
                    for h in range(hour + 1, next_expensive_h):
                        p = prices.get(h)
                        if p is not None:
                            overnight_prices.append(p)
                else:
                    # Peak is tomorrow: rest of today + tomorrow's hours before peak
                    for h in range(hour + 1, 24):
                        p = prices.get(h)
                        if p is not None:
                            overnight_prices.append(p)
                    if tomorrow_prices:
                        for h in range(0, next_expensive_h):
                            p = tomorrow_prices.get(h)
                            if p is not None:
                                overnight_prices.append(p)
                # Defer if we have time to spare and a cheaper hour is still coming
                if hours_until_peak > hours_needed and overnight_prices:
                    price_cutoff = sorted(overnight_prices)[min(hours_needed - 1, len(overnight_prices) - 1)]
                    if current_price > price_cutoff:
                        return (
                            "GENERAL",
                            NIGHT_MIN_SOC,
                            f"Prep-charge deferred: cheaper hour ahead "
                            f"(best={min(overnight_prices):.0f} CZK/MWh vs now={current_price:.0f}), "
                            f"need {hours_needed}h charge, {hours_until_peak}h until peak h{next_expensive_h}",
                        )
                # ── Fire prep-charge ──────────────────────────────────────────
                cheap_ref = f"today h{next_cheap_any}" if next_cheap_any else f"tomorrow h{next_cheap_tmrw}"
                return (
                    "ECO_CHARGE",
                    target_soc,
                    f"Overnight prep-charge: no cheap hours before h{next_expensive_h}, "
                    f"battery ({soc_wh:.0f} Wh) covers only {soc_wh - NIGHT_MIN_SOC/100*BATTERY_CAPACITY:.0f} Wh "
                    f"but load until {cheap_ref} = {load_to_cheap:.0f} Wh "
                    f"— charging to {target_soc}% at neutral price {current_price:.1f} CZK/MWh",
                )



    # Uses both the combined threshold AND a today-only threshold (computed above
    # in the price windows section) so today's expensive hours are not masked when
    # tomorrow's high prices inflate the combined threshold.
    is_expensive_now = current_price >= expensive_thr or current_price >= today_expensive_thr

    # ── Morning peak SOC preservation ────────────────────────────────────────────
    # During overnight hours, raise the discharge floor to preserve enough SOC to
    # cover house load during expensive morning peak hours (≥ 600 CZK) that occur
    # BEFORE the next cheap recharge window.  Without this, the battery drains to
    # 20% overnight and arrives empty at the morning peak.
    #
    # Case 1 (pre-midnight, h21-h23): tonight has no cheap hours left; look at
    #   tomorrow's morning peak hours. Fires even while actively discharging.
    # Case 2 (post-midnight, h00-h05): today's cheap window is later (h11+), but
    #   expensive peak hours lie between now and that window.  Only fires while
    #   hour < discharge_start to avoid blocking discharge once the peak begins.
    #
    # Capped at 85% so we never over-preserve on a well-charged battery.
    preserve_min_soc = MIN_SOC_FLOOR
    _preserve_peak_price_max = 0.0   # max effective price of the morning peak being preserved for
    if is_night:
        # Case 1: no cheap hours remaining tonight → look at tomorrow's morning peak
        if tomorrow_prices and not upcoming_cheap:
            def _eff_tmrw(h: int) -> float:
                delta = (DIST_FEE_PEAK - DIST_FEE_NORMAL) if h in DIST_PEAK_HOURS else 0.0
                return tomorrow_prices.get(h, 9999.0) + delta

            tmrw_cheap = sorted(
                h for h in tomorrow_prices
                if _eff_tmrw(h) <= cheap_thr or _eff_tmrw(h) <= PRICE_NO_EXPORT
            )
            if tmrw_cheap:
                tmrw_cheap_start = tmrw_cheap[0]
                tmrw_peak_before_cheap = [
                    h for h in range(0, tmrw_cheap_start)
                    if tomorrow_prices.get(h, 0) >= PRICE_NO_EXPORT * 3  # ≥ 600 CZK
                ]
                if tmrw_peak_before_cheap:
                    peak_load_wh = expected_load_wh(patterns, tmrw_peak_before_cheap[0],
                                                    tmrw_peak_before_cheap[-1] + 1)
                    needed_soc = MIN_SOC_FLOOR + peak_load_wh / BATTERY_CAPACITY * 100
                    preserve_min_soc = min(int(needed_soc), 85)
                    _preserve_peak_price_max = max(_eff_tmrw(h) for h in tmrw_peak_before_cheap)
                    log.info(
                        f"  Tomorrow peak h{tmrw_peak_before_cheap[0]}–h{tmrw_peak_before_cheap[-1]} "
                        f"({_preserve_peak_price_max:.0f} CZK/MWh eff) "
                        f"before cheap h{tmrw_cheap_start}: preserving SOC≥{preserve_min_soc}% "
                        f"(peak load={peak_load_wh:.0f} Wh)"
                    )
        # Case 2: post-midnight — cheap window is later today, but expensive peak
        # hours lie between now and that window. Gate on hour < discharge_start so
        # we don't block the battery from discharging once the peak actually begins.
        elif upcoming_cheap and hour < discharge_start:
            cheap_start_today = upcoming_cheap[0]
            today_peak_before_cheap = [
                h for h in range(hour + 1, cheap_start_today)
                if prices.get(h, 0) >= PRICE_NO_EXPORT * 3  # ≥ 600 CZK
            ]
            if today_peak_before_cheap:
                peak_load_wh = expected_load_wh(patterns, today_peak_before_cheap[0],
                                                today_peak_before_cheap[-1] + 1)
                needed_soc = MIN_SOC_FLOOR + peak_load_wh / BATTERY_CAPACITY * 100
                preserve_min_soc = min(int(needed_soc), 85)
                _preserve_peak_price_max = max(_eff(h) for h in today_peak_before_cheap)
                log.info(
                    f"  Today peak h{today_peak_before_cheap[0]}–h{today_peak_before_cheap[-1]} "
                    f"({_preserve_peak_price_max:.0f} CZK/MWh eff) "
                    f"before cheap h{cheap_start_today}: preserving SOC≥{preserve_min_soc}% "
                    f"(peak load={peak_load_wh:.0f} Wh, discharge_start=h{discharge_start})"
                )

    # ── Pre-charge opportunity check ─────────────────────────────────────────
    # Compute once here so both Rule 3 and DEFAULT can use it without
    # duplicating the logic.  True when there is a cheaper overnight hour
    # still ahead whose arbitrage spread covers round-trip losses + deviation
    # fee, making it economically better to discharge freely now and let
    # Rule 2.5 recharge at that cheaper hour.
    _precharge_possible = False
    if is_night and preserve_min_soc > NIGHT_MIN_SOC:
        if hour >= 21 and tomorrow_prices:
            _next_peak = next(
                (h for h in range(0, RECHARGE_HORIZON_H + 1)
                 if tomorrow_prices.get(h, 0.0) >= expensive_thr), None
            )
            if _next_peak is not None:
                _ov = ([prices[h] for h in range(hour + 1, 24) if prices.get(h) is not None]
                       + [tomorrow_prices[h] for h in range(0, _next_peak)
                          if tomorrow_prices.get(h) is not None])
                _pk = [tomorrow_prices[h] for h in range(_next_peak, RECHARGE_HORIZON_H + 1)
                       if tomorrow_prices.get(h, 0.0) >= expensive_thr]
                if _ov and _pk:
                    _precharge_possible = (
                        (min(_ov) + DEVIATION_FEE) / BATTERY_EFFICIENCY < min(_pk) + DEVIATION_FEE
                    )
        elif hour < 6:
            _next_peak = next(
                (h for h in range(hour + 1, RECHARGE_HORIZON_H + 1)
                 if prices.get(h, 0.0) >= expensive_thr), None
            )
            if _next_peak is not None:
                _ov = [prices[h] for h in range(hour + 1, _next_peak)
                       if prices.get(h) is not None]
                _pk = [prices[h] for h in range(_next_peak, RECHARGE_HORIZON_H + 1)
                       if prices.get(h, 0.0) >= expensive_thr]
                if _ov and _pk:
                    _precharge_possible = (
                        (min(_ov) + DEVIATION_FEE) / BATTERY_EFFICIENCY < min(_pk) + DEVIATION_FEE
                    )

    if (is_expensive_now or sd_hour_profitable) and hour >= discharge_start:
        # GENERAL mode cannot raise SOC (no grid charging), so if the battery is
        # already below preserve_min_soc the preservation goal is already lost —
        # setting a high floor just locks the battery idle while the expensive-hour
        # discharge opportunity is wasted.  Discharge freely whenever soc is below
        # the target (morning load will come from grid regardless).
        # Also discharge freely when a profitable overnight pre-charge is coming,
        # or when tonight's discharge price exceeds the morning peak we're preserving
        # for (it's more profitable to discharge now and grid-import tomorrow morning).
        already_below_preserve = soc < preserve_min_soc - 2
        current_beats_peak = (
            _preserve_peak_price_max > 0
            and preserve_min_soc > MIN_SOC_FLOOR
            and effective_price > _preserve_peak_price_max
        )
        r3_floor = (MIN_SOC_FLOOR
                    if already_below_preserve or _precharge_possible or current_beats_peak
                    else preserve_min_soc)
        if r3_floor == MIN_SOC_FLOOR:
            if current_beats_peak:
                r3_suffix = (
                    f" (current {effective_price:.0f} > morning peak {_preserve_peak_price_max:.0f}"
                    f" CZK/MWh — free discharge)"
                )
            elif already_below_preserve and not _precharge_possible:
                r3_suffix = " (already below preserve target — free discharge)"
            else:
                r3_suffix = " (pre-charge coming — free discharge)"
        else:
            r3_suffix = ""
        if is_expensive_now:
            price_label = (
                f"Expensive price {current_price:.1f} CZK/MWh "
                f"(≥ combined {expensive_thr:.0f} or today-75th {today_expensive_thr:.0f})"
            )
        else:
            price_label = (
                f"Profitable price {current_price:.1f} CZK/MWh "
                f"(> break-even {break_even_sell:.0f})"
            )
        return (
            "GENERAL",
            r3_floor,
            f"{price_label} discharge_start=h{discharge_start} — min SOC lowered to {r3_floor}%, battery covers house load"
            + r3_suffix,
        )
    if is_expensive_now and hour < discharge_start:
        # If SMART_DISCHARGE has picked a discharge_start that is actually CHEAPER
        # than the current expensive hour, there is no economic reason to hold —
        # discharge now rather than preserving battery for a lower-price target.
        discharge_start_price = prices.get(discharge_start, 9999.0)
        if discharge_start_price < current_price:
            already_below_preserve = soc < preserve_min_soc - 2
            r3_floor = (MIN_SOC_FLOOR
                        if already_below_preserve or _precharge_possible
                        else preserve_min_soc)
            return (
                "GENERAL",
                r3_floor,
                f"Expensive price {current_price:.1f} CZK/MWh — discharging now: "
                f"target h{discharge_start} ({discharge_start_price:.0f}) is cheaper, "
                f"no reason to hold (top: h{top_hours})",
            )
        # Discharge now if the battery can be recharged (cheap grid or PV) before
        # discharge_start — it's worth using the battery in both windows.
        cheap_before_discharge = [h for h in upcoming_cheap if h < discharge_start]
        if cheap_before_discharge:
            return (
                "GENERAL",
                preserve_min_soc,
                f"Expensive price {current_price:.1f} CZK/MWh — discharging now; "
                f"battery will recharge at h{cheap_before_discharge} before evening "
                f"discharge window h{discharge_start} (top: h{top_hours})",
            )
        # Also discharge now if there is no cheap/neutral gap between now and
        # discharge_start — hours in between are also expensive (same continuous
        # block), so holding makes no difference and we should just run the battery
        # through the whole expensive period.
        hours_between = range(hour + 1, discharge_start)
        neutral_or_cheap_between = [
            h for h in hours_between
            if h in prices and prices[h] < expensive_thr and prices[h] < today_expensive_thr
        ]
        if not neutral_or_cheap_between:
            return (
                "GENERAL",
                preserve_min_soc,
                f"Expensive price {current_price:.1f} CZK/MWh — continuous expensive block "
                f"to h{discharge_start}, no cheap/neutral gap — discharging through full block "
                f"(top: h{top_hours})",
            )
        return (
            "GENERAL",
            ECO_MIN_SOC,
            f"Expensive price {current_price:.1f} CZK/MWh but holding battery for "
            f"more-expensive hours (discharge_start=h{discharge_start}, top: h{top_hours}) "
            f"— preserving at {ECO_MIN_SOC}%",
        )

    # ── Rule 4: Expensive hours imminent + PV running — save battery first ────
    # Skip if there is a cheap recharge window before the first expensive hour —
    # the battery will be topped up cheaply anyway, so holding it now adds no value.
    if upcoming_expensive and soc < 70 and pv_w > 500 and not is_night:
        first_expensive_tonight = upcoming_expensive[0]
        cheap_before_expensive  = [h for h in upcoming_cheap if h < first_expensive_tonight]
        if not cheap_before_expensive:
            return (
                "GENERAL",
                MIN_SOC_SAVE,
                f"Expensive hours coming {upcoming_expensive}, PV={pv_w:.0f} W "
                f"— min SOC raised to {MIN_SOC_SAVE}% to save battery",
            )

    # ── Rule 5: End-of-day strategy ───────────────────────────────────────────
    # Active only in the evening transition window (eod_hour..21:00), not during
    # the night itself — overnight cheap charging (Rule 2) handles the rest.
    # Uses the dynamically computed eod_hour (last PV hour + 1), NOT the config
    # constant EOD_HOUR (which is only the fallback when no forecast is available).
    # Discharge_start (from all-hours sort above) determines the split:
    # • Before discharge_start → hold battery at current SOC (≥ ECO_MIN_SOC) for
    #   more-expensive hours — prevents Rule 2 oscillation (charge→hold→drain→repeat)
    # • At/after discharge_start → let battery run down; floor = overnight strategy:
    #     - Tomorrow morning cheap  → coast to NIGHT_MIN_SOC (recharge cheaply later)
    #     - Tomorrow morning expensive → hold at MIN_SOC_SAVE for tomorrow's costs
    # • Tomorrow prices not yet published (before ~13:33) → fall through.
    if hour >= eod_hour and not is_night and tomorrow_prices:
        if hour < discharge_start:
            # Don't drain from current SOC during the hold window — use current level
            # as the floor (capped at CHARGE_SOC_LIMIT) so Rule 2 doesn't keep topping
            # up 1–2% then seeing Rule 5 immediately discharge back to ECO_MIN_SOC.
            hold_floor = min(CHARGE_SOC_LIMIT, max(ECO_MIN_SOC, int(soc)))
            return (
                "GENERAL",
                hold_floor,
                f"End-of-day: holding battery for h{discharge_start}+ "
                f"(top remaining hours: h{top_hours}) — preserving at {hold_floor}%",
            )
        morning_hours = [h for h in range(6, 11)]   # 06:00–10:00
        morning_prices = [tomorrow_prices[h] for h in morning_hours if h in tomorrow_prices]
        if morning_prices:
            morning_cheap = [p for p in morning_prices if p <= cheap_thr]
            if morning_cheap:
                return (
                    "GENERAL",
                    NIGHT_MIN_SOC,
                    f"End-of-day h{discharge_start}+ discharge: tomorrow morning cheap "
                    f"(min={min(morning_cheap):.0f} CZK/MWh) — coast to {NIGHT_MIN_SOC}%",
                )
            else:
                return (
                    "GENERAL",
                    MIN_SOC_SAVE,
                    f"End-of-day h{discharge_start}+ discharge: tomorrow morning expensive "
                    f"(min={min(morning_prices):.0f} CZK/MWh) — floor at {MIN_SOC_SAVE}%",
                )

    # ── Rule 6: Overnight hold/drain — preserve battery for morning peak ──────
    # When expensive hours arrive before the next cheap window (e.g. h06–h08
    # peak, then cheap h10+), compute a drain_start hour: hold battery until
    # that hour (grid covers load), then let battery drain to NIGHT_MIN_SOC to
    # cover all remaining load until cheap charging restarts.
    # drain_start = latest hour h where expected_load(h→next_cheap) fits within
    # the (CHARGE_SOC_LIMIT → NIGHT_MIN_SOC) battery band.
    #
    # SMART_DISCHARGE: instead of locking the hold floor at CHARGE_SOC_LIMIT,
    # compute the minimum SOC needed at the start of discharge_start (or
    # drain_start, whichever comes first) so the battery still covers the whole
    # morning load window.  This allows profitable pre-peak discharge during the
    # hold hours (e.g. h04/h05 at 2800 CZK rather than keeping 98% idle).
    if is_night and upcoming_expensive:
        next_expensive = min(upcoming_expensive)
        if upcoming_cheap and next_expensive < min(upcoming_cheap):
            next_cheap = min(upcoming_cheap)
            usable_wh = (CHARGE_SOC_LIMIT - NIGHT_MIN_SOC) / 100 * BATTERY_CAPACITY
            drain_start = next_cheap  # fallback: can't cover any window
            for h in range(hour, next_cheap):
                if expected_load_wh(patterns, h, to_hour=next_cheap) <= usable_wh:
                    drain_start = h
                    break
            if SMART_DISCHARGE:
                # cover_from: earliest hour battery will discharge freely.
                # Must reserve for load from cover_from all the way to next_cheap
                # since Rule 3 (at discharge_start) and Rule 6 drain (at drain_start)
                # both reduce SOC before the cheap window.
                cover_from = min(discharge_start, drain_start)
                needed_soc_pct = (
                    NIGHT_MIN_SOC
                    + expected_load_wh(patterns, cover_from, to_hour=next_cheap) / BATTERY_CAPACITY * 100
                )
                # +3% safety buffer; never drop below NIGHT_MIN_SOC + 10
                hold_floor = int(min(
                    CHARGE_SOC_LIMIT,
                    max(needed_soc_pct + 3, NIGHT_MIN_SOC + 10),
                ))
                hold_note = (
                    f" (smart floor={hold_floor}%, covers h{cover_from}–h{next_cheap})"
                    if hold_floor < CHARGE_SOC_LIMIT
                    else ""
                )
                rule6_guard = hold_floor - 2
            else:
                hold_floor = CHARGE_SOC_LIMIT
                hold_note = ""
                rule6_guard = ECO_MIN_SOC
            if soc >= rule6_guard:
                if hour < drain_start:
                    return (
                        "GENERAL",
                        hold_floor,
                        f"Overnight hold until h{drain_start}: battery will cover load "
                        f"h{drain_start}–h{next_cheap} "
                        f"({expected_load_wh(patterns, drain_start, to_hour=next_cheap):.0f} Wh) "
                        f"— grid covers load until then{hold_note}",
                    )
                else:
                    return (
                        "GENERAL",
                        NIGHT_MIN_SOC,
                        f"Overnight drain h{drain_start}–h{next_cheap}: battery covers "
                        f"{expected_load_wh(patterns, drain_start, to_hour=next_cheap):.0f} Wh "
                        f"until cheap window (peak h{next_expensive} covered by battery)",
                    )

    # ── Default: normal reserve ────────────────────────────────────────────────
    # At night, apply preserve_min_soc to hold charge for the morning peak —
    # BUT only when no profitable pre-charge opportunity exists at a cheaper
    # upcoming overnight hour.  If a cheaper hour is still ahead and the spread
    # vs the morning peak covers the round-trip loss + deviation fee, it is
    # better to discharge freely now (covering moderate overnight load from free
    # battery energy) and let Rule 2.5 grid-charge at that cheaper hour.
    # When no profitable pre-charge is coming, preserve_min_soc ensures the
    # battery arrives at the morning peak with enough charge.
    if is_night:
        # _precharge_possible was already computed above (before Rule 3).
        if _precharge_possible and soc < PREP_CHARGE_SOC_LIMIT - 2:
            # Free discharge: cheaper pre-charge is coming and battery hasn't been
            # pre-charged yet — discharge freely now (covering overnight load from
            # free battery energy), Rule 2.5 will recharge at the cheaper hour.
            target_min = NIGHT_MIN_SOC
            log.info(
                f"  DEFAULT overnight: pre-charge profitable at cheaper upcoming hour "
                f"— allowing free discharge (floor={NIGHT_MIN_SOC}%), Rule 2.5 will recharge"
            )
        else:
            # Hold: either battery is already pre-charged (soc ≥ 94%), or no
            # profitable pre-charge hour remains.  In both cases preserve the
            # charge for the morning peak.
            target_min = max(NIGHT_MIN_SOC, preserve_min_soc)
            # If battery is well above preserve_min_soc (e.g. pre-charged to 95%+)
            # and the morning discharge window hasn't started yet, hold at the current
            # SOC — don't drain it to 85% at moderate h04–h05 prices when h06–h08
            # peak prices are significantly higher.
            if soc > preserve_min_soc + 2 and hour < discharge_start:
                target_min = max(target_min, int(soc))
    elif soc >= CHARGE_SOC_LIMIT:
        # Battery is fully charged — set a PV buffer floor during the day so
        # small PV fluctuations can absorb into the 95-98% band without buying
        # from grid.  At night there is no PV, so lock in at full (98%).
        target_min = ECO_MIN_SOC if (pv_w > 100 and not is_night) else CHARGE_SOC_LIMIT
    elif pv_fills_battery and soc >= ECO_MIN_SOC:
        # PV can cover the remaining headroom to CHARGE_SOC_LIMIT — battery is in
        # the 95-98% band being maintained by solar.  Hold floor at ECO_MIN_SOC so
        # the inverter doesn't buy from grid to chase the last few percent.
        target_min = ECO_MIN_SOC
    else:
        target_min = MIN_SOC
    return (
        "GENERAL",
        target_min,
        f"Normal conditions: price={current_price:.1f} CZK/MWh  SOC={soc:.0f}%  "
        f"PV={pv_w:.0f} W  min_soc={target_min}%"
        + (f"  (preserve floor={preserve_min_soc}% for morning peak)"
           if is_night and preserve_min_soc > NIGHT_MIN_SOC and not _precharge_possible else "")
        + (f"  (free discharge — pre-charge coming at cheaper overnight hour)"
           if is_night and _precharge_possible else ""),
    )


# ── One control cycle ─────────────────────────────────────────────────────────

async def run_cycle(patterns_cache: dict) -> None:
    now = datetime.now()

    is_leader = IS_LEADER
    log.info(f"{'[TEST] ' if TEST_MODE else ''}=== Cycle {now.strftime('%Y-%m-%d %H:%M')} ===")

    # ── OTE prices ─────────────────────────────────────────────────────────────
    prices          = fetch_prices(tomorrow=False)
    tomorrow_prices = fetch_prices(tomorrow=True)

    # ── Dynamic thresholds from next ~24h window ───────────────────────────────
    cheap_thr, expensive_thr = compute_thresholds(prices or {}, tomorrow_prices, hour=now.hour)

    if prices:
        log.info(f"  Today prices:    {price_summary(prices, cheap_thr, expensive_thr)}")
    else:
        log.warning("  No price data — will use GENERAL mode this cycle")
        prices = {}
    if tomorrow_prices:
        log.info(f"  Tomorrow prices: {price_summary(tomorrow_prices, cheap_thr, expensive_thr)}")

    # ── Historical patterns (refresh once per hour) ────────────────────────────
    cache_key = (now.month, now.hour)
    if cache_key not in patterns_cache:
        patterns = query_hourly_patterns(month=now.month)
        patterns_cache.clear()
        patterns_cache[cache_key] = patterns
        log.info(f"  Historical patterns loaded: {len(patterns)} hourly slots (month={now.month})")
    else:
        patterns = patterns_cache[cache_key]

    # ── PV generation forecast (forecast.solar, falls back to historical) ──────
    pv_today_fc, _pv_tomorrow_fc = fetch_pv_forecast()
    # Derive end-of-day hour dynamically: last hour with meaningful PV (>500 Wh).
    # Falls back to EOD_HOUR config value when no forecast is available.
    if pv_today_fc:
        pv_hours = [h for h, wh in pv_today_fc.items() if wh > 500]
        eod_hour = (max(pv_hours) + 1) if pv_hours else EOD_HOUR
        pv_remaining_wh = pv_forecast_remaining(pv_today_fc, now.hour + 1)
        pv_src = "forecast"
    else:
        pv_hist_hours = [h for h in range(24) if patterns.get(h, {}).get('avg_pv_w', 0) > 500]
        eod_hour = (max(pv_hist_hours) + 1) if pv_hist_hours else EOD_HOUR
        pv_remaining_wh = expected_pv_wh(patterns, now.hour + 1)
        pv_src = "historical"
    # Expected house consumption from now until end of PV production.
    # Used to compute net PV available for battery — avoids skipping ECO_CHARGE
    # on high-load days where raw PV forecast looks sufficient but house eats it.
    load_remaining_wh = expected_load_wh(patterns, now.hour + 1, to_hour=eod_hour)
    load_total_remaining_wh = expected_load_wh(patterns, now.hour + 1, to_hour=24)
    dhw_note = ""
    if DHW_WH > 0 and now.hour < DHW_END_HOUR:
        dhw_note = f"  (incl. DHW {DHW_WH} Wh @ {DHW_START_HOUR}:00–{DHW_END_HOUR}:00)"
    log.info(f"  PV remaining today: {pv_remaining_wh:.0f} Wh  (source={pv_src})  eod_hour={eod_hour}")
    log.info(f"  Load remaining today (until {eod_hour}:00): {load_remaining_wh:.0f} Wh{dhw_note}  |  total until midnight: {load_total_remaining_wh:.0f} Wh")

    # ── Read inverter (both leader and standby monitor state) ──────────────────
    log.info(f"  Connecting to inverter at {INVERTER_IP} …")
    state = None
    for attempt in range(1, 4):
        try:
            raw_state = await read_inverter()
            state = {
                "mode": "UNKNOWN",
                "soc": float(raw_state.get("soc", 0)),
                "pv_w": float(raw_state.get("ppv", 0)),
                "grid_w": float(raw_state.get("pgrid", 0)),
                "load_w": float(raw_state.get("pload", 0)),
                "battery_w": float(raw_state.get("pbatt", 0)),
            }
            break
        except Exception as exc:
            log.warning(f"  Inverter did not respond (attempt {attempt}/3): {exc}")
            if attempt < 3:
                await asyncio.sleep(10)
    if state is None:
        log.error("  Inverter unreachable after 3 attempts — skipping cycle")
        return

    log.info(
        f"  Inverter state: mode={state['mode']}  SOC={state['soc']:.0f}%  "
        f"PV={state['pv_w']:.0f} W  grid={state['grid_w']:.0f} W  "
        f"load={state['load_w']:.0f} W  battery={state['battery_w']:.0f} W"
    )

    # ── Decide ─────────────────────────────────────────────────────────────────
    target_mode, target_min_soc, reason = decide(
        now=now,
        soc=state["soc"],
        pv_w=state["pv_w"],
        grid_w=state["grid_w"],
        prices=prices,
        tomorrow_prices=tomorrow_prices,
        pv_remaining_wh=pv_remaining_wh,
        load_remaining_wh=load_remaining_wh,
        cheap_thr=cheap_thr,
        expensive_thr=expensive_thr,
        eod_hour=eod_hour,
        patterns=patterns,
    )
    log.info(f"  Decision → mode={target_mode}  min_soc={target_min_soc}%: {reason}")

    # ── Log decision to MySQL (always, even in TEST mode) ─────────────────────
    log_decision(
        now=now,
        is_leader=is_leader,
        state=state,
        price_now=prices.get(now.hour),
        target_mode=target_mode,
        target_min_soc=target_min_soc,
        reason=reason,
    )

    # ── Apply (skipped in TEST mode or manual override) ───────────────────────
    manual_mode, forced_mode, forced_min_soc = read_manual_override()
    if manual_mode:
        if forced_mode or forced_min_soc is not None:
            # User set explicit forced values — apply them to the inverter
            apply_target_mode = forced_mode if forced_mode else target_mode
            apply_target_soc  = forced_min_soc if forced_min_soc is not None else target_min_soc
            log.info(
                f"  [MANUAL] Applying forced values: mode={apply_target_mode}  min_soc={apply_target_soc}%"
                f"  (controller would: mode={target_mode}  min_soc={target_min_soc}%)"
            )
            await apply_mode(apply_target_mode, state["mode"])
            await apply_min_soc(apply_target_soc)
        else:
            log.info(
                f"  [MANUAL] Override active — inverter writes suppressed. "
                f"Controller would: mode={target_mode}  min_soc={target_min_soc}%"
            )
    else:
        await apply_mode(target_mode, state["mode"])
        await apply_min_soc(target_min_soc)

    # ── Export block (applied regardless of manual override) ──────────────────
    if EXPORT_BLOCK_ENABLED:
        price_now_val = prices.get(now.hour)
        should_block_export = price_now_val is not None and price_now_val <= EXPORT_BLOCK_THRESHOLD
        if should_block_export:
            log.info(
                f"  Export block: price {price_now_val:.1f} CZK/MWh ≤ {EXPORT_BLOCK_THRESHOLD:.0f} threshold"
                f" — blocking grid export"
            )
        await apply_export_block(should_block_export)


# ── Daemon loop ───────────────────────────────────────────────────────────────

async def main() -> None:
    log.info("━" * 70)
    log.info(f"Solax Controller starting  (TEST_MODE={'ON' if TEST_MODE else 'OFF'})")
    log.info(f"  Inverter     : {INVERTER_IP}")
    log.info(f"  Loop interval: {LOOP_INTERVAL_MIN} minutes")
    log.info(
        f"  Thresholds   : cheap < {PRICE_CHEAP} CZK/MWh  "
        f"expensive > {PRICE_EXPENSIVE} CZK/MWh"
    )
    log.info(
        f"  SOC limits   : min={MIN_SOC}%  night_min={NIGHT_MIN_SOC}%  floor={MIN_SOC_FLOOR}%  "
        f"eco_min={ECO_MIN_SOC}%  charge_limit={CHARGE_SOC_LIMIT}%"
    )
    log.info(f"  Battery      : {BATTERY_CAPACITY} Wh  charge_rate={CHARGE_RATE_W} W  history={HISTORY_MONTHS} months  smart_discharge={'ON' if SMART_DISCHARGE else 'OFF'}")
    log.info(f"  Outage file  : {_OUTAGES_FILE}  (max_lead={OUTAGE_LEAD_HOURS}h  target={OUTAGE_PRE_CHARGE_SOC}%)")
    log.info(f"  Controller   : single node  node={NODE_NAME}")
    ensure_decisions_table()
    ensure_control_table()
    log.info(f"  Decision log : {_log_file}  +  MySQL solax_decisions table")
    log.info("━" * 70)

    once = "--once" in sys.argv
    patterns_cache: dict = {}
    interval_s = LOOP_INTERVAL_MIN * 60

    while True:
        try:
            await run_cycle(patterns_cache)
        except Exception as exc:
            log.error(f"Unhandled error in cycle: {exc}", exc_info=True)

        if once:
            log.info("  --once flag set, exiting.")
            return

        log.info(f"  Sleeping {LOOP_INTERVAL_MIN} minutes …\n")
        await asyncio.sleep(interval_s)


if __name__ == "__main__":
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        sys.exit(0)
    asyncio.run(main())
