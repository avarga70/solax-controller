#!/usr/bin/env python3
"""
solax_poller.py — polls Solax inverter every N minutes, stores to MySQL PV_run table.
This replaces the Grafana DB data source used in the GoodWe installation.
"""

import asyncio
import logging
import os
import time
from datetime import datetime

import aiohttp
import mysql.connector
import solax


def load_env_file(path: str) -> None:
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


load_env_file("/etc/solax/config.env")
load_env_file(os.path.join(os.path.dirname(__file__), "config.env"))

INVERTER_IP = os.getenv("INVERTER_IP", "")
INVERTER_PORT = int(os.getenv("INVERTER_PORT", "80"))
INVERTER_PASSWORD = os.getenv("INVERTER_PASSWORD", "")
MYSQL_HOST = os.getenv("MYSQL_HOST", "localhost")
MYSQL_DB = os.getenv("MYSQL_DB", "solax")
MYSQL_USER = os.getenv("MYSQL_USER", "solax")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", "300"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("/var/log/solax_poller.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `PV_run` (
  `psource`  varchar(15)    NOT NULL DEFAULT 'SOLAX',
  `pdtime`   datetime       NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `ppv`      int            NOT NULL DEFAULT 0,
  `pgridT`   int            NOT NULL DEFAULT 0,
  `pbattw`   int            NOT NULL DEFAULT 0,
  `pbatts`   decimal(6,2)   NOT NULL DEFAULT 0,
  `ploadT`   int            NOT NULL DEFAULT 0,
  `ptemp1`   decimal(6,2)   NOT NULL DEFAULT 0,
  PRIMARY KEY (`psource`, `pdtime`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Solax inverter runtime readings';
"""


async def read_inverter() -> dict:
    async with aiohttp.ClientSession() as session:
        inverter = await solax.discover(INVERTER_IP, INVERTER_PORT, INVERTER_PASSWORD, session)
        response = await inverter.get_data()
        d = response.data
        return {
            "ppv": int(d.get("power_dc1", 0) + d.get("power_dc2", 0)),
            "pgrid": int(d.get("feedin_power", 0)),
            "pbatt": int(d.get("battery_power", 0)),
            "soc": float(d.get("battery_percent", 0)),
            "pload": int(d.get("load_power", 0)),
            "temp": float(d.get("inverter_temperature", 0)),
        }


def store_reading(db, reading: dict) -> None:
    cur = db.cursor()
    try:
        cur.execute(
            """
            INSERT INTO PV_run (psource, pdtime, ppv, pgridT, pbattw, pbatts, ploadT, ptemp1)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                ppv=%s, pgridT=%s, pbattw=%s, pbatts=%s, ploadT=%s, ptemp1=%s
            """,
            (
                "SOLAX",
                datetime.now(),
                reading["ppv"],
                reading["pgrid"],
                reading["pbatt"],
                reading["soc"],
                reading["pload"],
                reading["temp"],
                reading["ppv"],
                reading["pgrid"],
                reading["pbatt"],
                reading["soc"],
                reading["pload"],
                reading["temp"],
            ),
        )
        db.commit()
    finally:
        cur.close()


def connect_db():
    return mysql.connector.connect(
        host=MYSQL_HOST,
        database=MYSQL_DB,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        autocommit=False,
    )


def main() -> None:
    db = connect_db()
    cur = db.cursor()
    try:
        cur.execute(CREATE_TABLE_SQL)
        db.commit()
    finally:
        cur.close()
    log.info("solax_poller started, polling every %ds", POLL_INTERVAL_SEC)

    while True:
        try:
            reading = asyncio.run(read_inverter())
            store_reading(db, reading)
            log.info(
                "Stored: PV=%dW grid=%dW batt=%dW SOC=%.1f%% load=%dW",
                reading["ppv"],
                reading["pgrid"],
                reading["pbatt"],
                reading["soc"],
                reading["pload"],
            )
        except Exception as exc:
            log.error("Poll error: %s", exc)
            try:
                db.close()
            except Exception:
                pass
            try:
                db = connect_db()
            except Exception as dbe:
                log.error("DB reconnect failed: %s", dbe)
        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
