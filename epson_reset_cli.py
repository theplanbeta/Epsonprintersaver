#!/usr/bin/env python3
"""
Epson EcoTank Waste Ink Pad Reset - Command Line Version
Usage: python epson_reset_cli.py --ip 192.168.2.200
"""
import argparse
import sys
import time
from epson_reset import (
    validate_ip, is_supported_model, SUPPORTED_MODELS,
    check_snmp_connectivity, get_printer_model,
    read_eeprom, write_eeprom,
    WASTE_INK_RESET, MAIN_WASTE_ADDRS, BORDERLESS_WASTE_ADDRS,
    MAIN_WASTE_DIVIDER, BORDERLESS_WASTE_DIVIDER, MAINTENANCE_THRESHOLD,
)


def read_counters(ip):
    main_bytes = []
    failed = False
    for a in MAIN_WASTE_ADDRS:
        v = read_eeprom(ip, a)
        if v is None:
            print(f"  WARNING: EEPROM[{a}] read failed")
            failed = True
            main_bytes.append(0)
        else:
            main_bytes.append(v)

    border_bytes = []
    for a in BORDERLESS_WASTE_ADDRS:
        v = read_eeprom(ip, a)
        if v is None:
            failed = True
            border_bytes.append(0)
        else:
            border_bytes.append(v)

    main_value = main_bytes[0] + (main_bytes[1] << 8) + (main_bytes[2] << 16)
    border_value = border_bytes[0] + (border_bytes[1] << 8) + (border_bytes[2] << 16)
    maint1 = read_eeprom(ip, 54)
    maint2 = read_eeprom(ip, 55)
    if maint1 is None or maint2 is None:
        failed = True
    return main_value, border_value, maint1, maint2, failed


def main():
    parser = argparse.ArgumentParser(description="Epson EcoTank Waste Ink Pad Reset")
    parser.add_argument("--ip", required=True, help="Printer IP address (e.g. 192.168.1.100)")
    parser.add_argument("--reset", action="store_true", help="Reset counters (default: read only)")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")
    parser.add_argument("--force", action="store_true", help="Skip model check (dangerous)")
    args = parser.parse_args()

    # Validate IP
    ip, err = validate_ip(args.ip)
    if err:
        print(f"ERROR: {err}")
        sys.exit(1)

    print(f"Connecting to {ip}...")

    if not check_snmp_connectivity(ip):
        print(f"ERROR: No response from {ip}.")
        print("Check: printer is on, connected to WiFi, UDP port 161 not blocked.")
        sys.exit(1)

    model = get_printer_model(ip)
    print(f"Printer: {model or 'Unknown'}")

    # Model check — block unknown AND unsupported models
    if not model:
        print("\nERROR: Could not identify printer model.")
        print("Cannot verify EEPROM compatibility. Reset is not allowed "
              "for unidentified printers.")
        if not args.force:
            print("Use --force to override (dangerous).")
            sys.exit(1)
    elif not is_supported_model(model):
        print(f"\nWARNING: {model} is not a tested model!")
        print(f"Supported: {', '.join(SUPPORTED_MODELS)}")
        print("EEPROM addresses may differ. Proceeding could damage your printer.")
        if not args.force:
            print("Use --force to override this check.")
            sys.exit(1)

    main_value, border_value, maint1, maint2, read_failed = read_counters(ip)
    main_pct = main_value / MAIN_WASTE_DIVIDER
    border_pct = border_value / BORDERLESS_WASTE_DIVIDER
    is_over = maint1 is not None and maint1 > MAINTENANCE_THRESHOLD

    print(f"\nWaste Ink Status:")
    print(f"  Main waste:       {main_value:5d} raw = {main_pct:.1f}%")
    print(f"  Borderless waste: {border_value:5d} raw = {border_pct:.1f}%")
    print(f"  Maintenance 1st:  {maint1} (threshold: {MAINTENANCE_THRESHOLD})")
    print(f"  Maintenance 2nd:  {maint2} (threshold: {MAINTENANCE_THRESHOLD})")
    print(f"  Status:           {'*** OVER LIMIT ***' if is_over else 'OK'}")

    if read_failed:
        print("\n  WARNING: Some reads failed. Values may be inaccurate.")

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
    for addr, label, value in WASTE_INK_RESET:
        result = write_eeprom(ip, addr, value)
        status = "OK" if result is True else "FAIL" if result is False else "NO RESP"
        print(f"  EEPROM[{addr:3d}] = {value:3d} ... {status}  ({label})")
        if result is not True:
            success = False
            print("\n  Write failed — aborting. Remaining addresses not modified.")
            print("  Try power cycling the printer and running again.")
            break
        time.sleep(0.2)

    print("\nVerifying...")
    time.sleep(1)
    main_value, border_value, maint1, maint2, _ = read_counters(ip)

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
