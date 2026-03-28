#!/usr/bin/env python3
"""
Epson EcoTank Waste Ink Pad Reset - Command Line Version
Usage: python epson_reset_cli.py --ip 192.168.2.200
"""
import argparse
import sys
import time
from epson_reset import (
    check_snmp_connectivity, get_printer_model,
    read_eeprom, write_eeprom,
    WASTE_INK_ADDRS, MAIN_WASTE_ADDRS, BORDERLESS_WASTE_ADDRS,
    MAIN_WASTE_DIVIDER, BORDERLESS_WASTE_DIVIDER, MAINTENANCE_THRESHOLD,
)


def read_counters(ip):
    main_bytes = [read_eeprom(ip, a) or 0 for a in MAIN_WASTE_ADDRS]
    border_bytes = [read_eeprom(ip, a) or 0 for a in BORDERLESS_WASTE_ADDRS]
    main_value = main_bytes[0] + (main_bytes[1] << 8) + (main_bytes[2] << 16)
    border_value = border_bytes[0] + (border_bytes[1] << 8) + (border_bytes[2] << 16)
    maint1 = read_eeprom(ip, 54)
    maint2 = read_eeprom(ip, 55)
    return main_value, border_value, maint1, maint2


def main():
    parser = argparse.ArgumentParser(description="Epson EcoTank Waste Ink Pad Reset")
    parser.add_argument("--ip", required=True, help="Printer IP address")
    parser.add_argument("--reset", action="store_true", help="Reset counters (default: read only)")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args()

    ip = args.ip
    print(f"Connecting to {ip}...")

    if not check_snmp_connectivity(ip):
        print(f"ERROR: No response from {ip}. Check IP and network.")
        sys.exit(1)

    model = get_printer_model(ip)
    print(f"Printer: {model or 'Unknown'}")

    main_value, border_value, maint1, maint2 = read_counters(ip)
    main_pct = main_value / MAIN_WASTE_DIVIDER
    border_pct = border_value / BORDERLESS_WASTE_DIVIDER
    is_over = maint1 is not None and maint1 > MAINTENANCE_THRESHOLD

    print(f"\nWaste Ink Status:")
    print(f"  Main waste:       {main_value:5d} raw = {main_pct:.1f}%")
    print(f"  Borderless waste: {border_value:5d} raw = {border_pct:.1f}%")
    print(f"  Maintenance 1st:  {maint1} (threshold: {MAINTENANCE_THRESHOLD})")
    print(f"  Maintenance 2nd:  {maint2} (threshold: {MAINTENANCE_THRESHOLD})")
    print(f"  Status:           {'*** OVER LIMIT ***' if is_over else 'OK'}")

    if not args.reset:
        print("\nRun with --reset to reset the counters.")
        return

    if not args.yes:
        resp = input("\nReset waste ink counters? [y/N] ").strip().lower()
        if resp != 'y':
            print("Aborted.")
            return

    print("\nResetting...")
    success = True
    for addr, (label, value) in WASTE_INK_ADDRS.items():
        result = write_eeprom(ip, addr, value)
        status = "OK" if result is True else "FAIL" if result is False else "NO RESP"
        print(f"  EEPROM[{addr:3d}] = {value:3d} ... {status}  ({label})")
        if result is not True:
            success = False
        time.sleep(0.2)

    print("\nVerifying...")
    time.sleep(1)
    main_value, border_value, maint1, maint2 = read_counters(ip)

    if success and main_value == 0 and maint1 == 94:
        print("\nRESET SUCCESSFUL!")
        print("\nNext steps:")
        print("  1. Unplug the printer")
        print("  2. Wait 30 seconds")
        print("  3. Plug back in and turn on")
        print("  4. Print a test page")
    else:
        print(f"\nVerification: main={main_value}, maint={maint1}")
        print("Try power cycling and running again.")


if __name__ == '__main__':
    main()
