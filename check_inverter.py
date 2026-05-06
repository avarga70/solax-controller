#!/usr/bin/env python3
"""
check_inverter.py — GoodWe inverter diagnostic / key-discovery script.

Run once to verify connectivity and print all available sensor IDs
so you can confirm key names before running solar_controller.py.

Usage:
    python3 check_inverter.py <INVERTER_IP>
"""

import asyncio
import sys

import goodwe
from goodwe.et import OperationMode


async def main(ip: str) -> None:
    print(f"Connecting to inverter at {ip} …")
    try:
        inv = await goodwe.connect(ip)
    except Exception as exc:
        print(f"ERROR: could not connect: {exc}")
        sys.exit(1)

    print(f"\nInverter : {inv.__class__.__name__}  model={getattr(inv, 'model_name', '?')}")
    print(f"Serial   : {getattr(inv, 'serial_number', '?')}")
    print(f"Firmware : {getattr(inv, 'firmware_version', '?')}\n")

    # Current operation mode
    try:
        mode = await inv.get_operation_mode()
        print(f"Current operation mode: {mode.name} (value={mode.value})\n")
    except Exception as exc:
        print(f"get_operation_mode() failed: {exc}\n")

    # All available modes
    print("Available OperationMode values:")
    for m in OperationMode:
        print(f"  {m.value:2d}  {m.name}")

    # All runtime sensor data
    print("\n── Runtime data ──────────────────────────────────────────────────────────")
    try:
        data = await inv.read_runtime_data()
        for sensor in inv.sensors():
            if sensor.id_ in data:
                val = data[sensor.id_]
                print(f"  {sensor.id_:<30s} {sensor.name:<40s} = {val} {sensor.unit}")
    except Exception as exc:
        print(f"read_runtime_data() failed: {exc}")

    # Settings (read-only / non-destructive)
    print("\n── Settings ──────────────────────────────────────────────────────────────")
    try:
        settings = await inv.read_settings_data()
        for sensor in inv.settings():
            if sensor.id_ in settings:
                val = settings[sensor.id_]
                print(f"  {sensor.id_:<30s} {sensor.name:<40s} = {val} {sensor.unit}")
    except Exception as exc:
        print(f"read_settings_data() failed (may be unsupported): {exc}")


if __name__ == "__main__":
    ip = sys.argv[1] if len(sys.argv) > 1 else None
    if not ip:
        print("Usage: python3 check_inverter.py <INVERTER_IP>")
        sys.exit(1)
    asyncio.run(main(ip))
