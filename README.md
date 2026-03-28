# Epson EcoTank Ink Pad Reset Tool

Reset the waste ink pad counter on Epson ET-2720 / ET-2721 / ET-2700 series printers **over WiFi** — no USB cable, no special drivers, no paid software.

## The Problem

Epson EcoTank printers track how much ink flows into the waste ink pad. When the internal counter exceeds a threshold, the printer **refuses to print** and displays a "waste ink pad full" error — even if the pad isn't actually full.

Epson's official solution: pay for a service visit or buy a new printer.

This tool resets that counter to zero via your local network, for free.

## How It Works

The tool communicates with your printer over **SNMP (UDP port 161)** on your local WiFi network. It reads and writes specific EEPROM addresses that store the waste ink counters. No firmware modification, no USB connection needed.

The EEPROM write commands are sent as SNMP GET requests with the command encoded in the OID — a quirk of Epson's proprietary protocol discovered by the [epson_print_conf](https://github.com/Ircama/epson_print_conf) project.

## Supported Printers

| Model | Status |
|-------|--------|
| ET-2720 | Tested, working |
| ET-2721 | Should work (same internals) |
| ET-2700 | Should work (same config) |

Other EcoTank models may use different EEPROM addresses and keys. PRs welcome.

## Quick Start

### Option 1: Run from source (any OS)

Requires Python 3.8+. No pip dependencies — uses only the standard library.

```bash
git clone https://github.com/theplanbeta/Epsonprintersaver.git
cd Epsonprintersaver
python epson_reset.py
```

### Option 2: Download pre-built binary

Check the [Releases](https://github.com/theplanbeta/Epsonprintersaver/releases) page for Windows (.exe) and macOS (.app) downloads.

### Option 3: Command-line only (no GUI)

```bash
python epson_reset_cli.py --ip 192.168.2.200
```

## Usage

1. Make sure your printer is on and connected to the same WiFi network as your computer
2. Find your printer's IP address (check your router's device list, or the printer's network settings)
3. Enter the IP in the tool and click **Check Printer**
4. Review the waste ink counter levels
5. Click **Reset Waste Ink Counters**
6. **Power cycle the printer** (unplug, wait 30 seconds, plug back in)

## Finding Your Printer's IP Address

- **From the printer:** Navigate to Settings > Network Settings > Print Network Status
- **From your router:** Look for a device named "EPSON" in the connected devices list
- **Common default:** Many Epson printers are at `192.168.1.x` or `192.168.0.x`

## Important Notes

- This tool resets the **software counter**, not the physical ink pad. If your waste ink pad is genuinely full and saturated, you should clean or replace it to avoid ink leaks.
- Always power cycle the printer after resetting.
- The tool only works over your **local network** — your computer and printer must be on the same WiFi/LAN.
- Firewall: ensure UDP port 161 is not blocked on your computer.

## Building from Source

### Windows .exe

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name "EpsonReset" epson_reset.py
```

The .exe will be in the `dist/` folder.

### macOS .app

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name "EpsonReset" epson_reset.py
```

Or use py2app:

```bash
pip install py2app
python setup.py py2app
```

## How We Figured This Out

The Epson ET-2720 has no HTTP API for EEPROM access — only SNMP over UDP. Key discoveries:

- **SNMP GET (not SET)** is used for writes — the write command is encoded in the OID itself
- **Read key:** `[151, 7]` / **Write key:** `b'Maribaya'` (shared with ET-2700 family)
- **Maintenance level 94** is the threshold — values above 94 trigger the waste ink error
- USB through Parallels VMs returns empty reads — network SNMP is the reliable path

## Credits

- Protocol reverse-engineering: [epson_print_conf](https://github.com/Ircama/epson_print_conf) by Ircama
- WiFi reset implementation & GUI: this project

## License

MIT License — see [LICENSE](LICENSE).

## Disclaimer

This software is provided as-is. Resetting the waste ink counter does not physically empty the waste ink pad. Use at your own risk. The authors are not responsible for any damage to your printer.
