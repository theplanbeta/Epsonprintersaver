#!/usr/bin/env python3
"""
Epson EcoTank Waste Ink Pad Reset Tool
Resets waste ink counters on Epson ET-2720/2721/2700 series printers over WiFi.

No special drivers or USB connection needed - works over your local network via SNMP.
"""
import socket
import struct
import re
import time
import threading
import tkinter as tk
from tkinter import ttk, messagebox


# ─── Printer Configuration ───────────────────────────────────────────────────
# ET-2720 / ET-2700 family config (from epson_print_conf by Ircama)
READ_KEY = [151, 7]
WRITE_KEY = b'Maribaya'
BASE_OID = "1.3.6.1.4.1.1248.1.2.2.44.1.1.2.1"
SNMP_PORT = 161

# Waste ink EEPROM addresses and reset values
WASTE_INK_ADDRS = {
    48: ("Main waste counter (byte 0)", 0),
    49: ("Main waste counter (byte 1)", 0),
    47: ("Main waste counter (byte 2)", 0),
    50: ("Borderless waste counter (byte 0)", 0),
    51: ("Borderless waste counter (byte 1)", 0),
    52: ("Counter store (byte 0)", 0),
    53: ("Counter store (byte 1)", 0),
    54: ("Maintenance level (1st)", 94),
    55: ("Maintenance level (2nd)", 94),
    28: ("Auxiliary counter", 0),
}

# Waste ink counter composition
MAIN_WASTE_ADDRS = [48, 49, 47]      # byte0, byte1, byte2
BORDERLESS_WASTE_ADDRS = [50, 51, 47]  # byte0, byte1, byte2 (shares byte2)
MAIN_WASTE_DIVIDER = 63.45
BORDERLESS_WASTE_DIVIDER = 34.15
MAINTENANCE_THRESHOLD = 94


# ─── SNMP / EEPROM Protocol ──────────────────────────────────────────────────

def caesar(key):
    """Apply Caesar cipher to write key."""
    return [0 if b == 0 else b + 1 for b in key]


def epctrl_snmp_oid(command, payload):
    """Build Epson control OID with command and payload."""
    cmd = command.encode() + struct.pack('<H', len(payload)) + bytes(payload)
    return BASE_OID + "." + ".".join(str(int(i)) for i in cmd)


def eeprom_read_oid(addr):
    """Build OID for EEPROM read at given address."""
    return epctrl_snmp_oid("||", [
        READ_KEY[0], READ_KEY[1], 65, 190, 160, addr % 256, addr // 256
    ])


def eeprom_write_oid(addr, value):
    """Build OID for EEPROM write at given address."""
    return epctrl_snmp_oid("||", [
        READ_KEY[0], READ_KEY[1], 66, 189, 33, addr % 256, addr // 256, value
    ] + caesar(WRITE_KEY))


def encode_oid(oid_str):
    """Encode OID string to BER bytes."""
    parts = [int(x) for x in oid_str.split('.')]
    encoded = [40 * parts[0] + parts[1]]
    for p in parts[2:]:
        if p < 128:
            encoded.append(p)
        else:
            tmp = []
            while p > 0:
                tmp.append(p & 0x7F)
                p >>= 7
            tmp.reverse()
            for i in range(len(tmp) - 1):
                tmp[i] |= 0x80
            encoded.extend(tmp)
    return bytes(encoded)


def encode_length(length):
    """Encode ASN.1 length."""
    if length < 128:
        return bytes([length])
    elif length < 256:
        return bytes([0x81, length])
    else:
        return bytes([0x82, length >> 8, length & 0xff])


def build_snmp_get(oid_str, community=b'public', req_id=1):
    """Build complete SNMP GET request packet."""
    oid_encoded = encode_oid(oid_str)
    oid_tlv = bytes([0x06]) + encode_length(len(oid_encoded)) + oid_encoded
    null_val = bytes([0x05, 0x00])
    varbind = bytes([0x30]) + encode_length(len(oid_tlv) + len(null_val)) + oid_tlv + null_val
    varbindlist = bytes([0x30]) + encode_length(len(varbind)) + varbind
    req_id_bytes = bytes([0x02, 0x04]) + req_id.to_bytes(4, 'big')
    err_status = bytes([0x02, 0x01, 0x00])
    err_index = bytes([0x02, 0x01, 0x00])
    pdu_content = req_id_bytes + err_status + err_index + varbindlist
    pdu = bytes([0xA0]) + encode_length(len(pdu_content)) + pdu_content
    comm_tlv = bytes([0x04, len(community)]) + community
    version = bytes([0x02, 0x01, 0x00])
    msg_content = version + comm_tlv + pdu
    return bytes([0x30]) + encode_length(len(msg_content)) + msg_content


def snmp_query(ip, pkt, timeout=5):
    """Send SNMP packet and return response."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    sock.sendto(pkt, (ip, SNMP_PORT))
    try:
        data, _ = sock.recvfrom(4096)
        return data
    except socket.timeout:
        return None
    finally:
        sock.close()


def read_eeprom(ip, addr):
    """Read a single byte from printer EEPROM."""
    resp = snmp_query(ip, build_snmp_get(eeprom_read_oid(addr)))
    if resp:
        text = resp.decode('ascii', errors='replace')
        match = re.search(r'EE:([0-9A-Fa-f]{6})', text)
        if match:
            return int(match.group(1)[4:], 16)
    return None


def write_eeprom(ip, addr, value):
    """Write a single byte to printer EEPROM. Returns True/False/None."""
    # Epson uses SNMP GET (not SET) for writes - the command is encoded in the OID
    resp = snmp_query(ip, build_snmp_get(eeprom_write_oid(addr, value)))
    if resp:
        text = resp.decode('ascii', errors='replace')
        if ':OK;' in text:
            return True
        elif ':NA;' in text:
            return False
    return None


def get_printer_model(ip):
    """Query printer model name via SNMP."""
    resp = snmp_query(ip, build_snmp_get("1.3.6.1.4.1.1248.1.1.3.1.3.8.0"))
    if resp:
        text = resp.decode('ascii', errors='replace')
        matches = re.findall(r'ET-\d+[^\x00]*', text)
        if matches:
            return matches[0].strip()
    return None


def check_snmp_connectivity(ip):
    """Check basic SNMP connectivity."""
    resp = snmp_query(ip, build_snmp_get("1.3.6.1.2.1.1.1.0"), timeout=3)
    return resp is not None


# ─── GUI Application ─────────────────────────────────────────────────────────

class EpsonResetApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Epson EcoTank Ink Pad Reset")
        self.root.resizable(False, False)

        # Try to set icon (won't fail if not available)
        try:
            self.root.iconbitmap(default='')
        except Exception:
            pass

        self.printer_ip = tk.StringVar(value="192.168.2.200")
        self.is_running = False

        self._build_ui()
        self.root.after(100, self._center_window)

    def _center_window(self):
        self.root.update_idletasks()
        w = self.root.winfo_width()
        h = self.root.winfo_height()
        x = (self.root.winfo_screenwidth() // 2) - (w // 2)
        y = (self.root.winfo_screenheight() // 2) - (h // 2)
        self.root.geometry(f"+{x}+{y}")

    def _build_ui(self):
        # Main frame
        main = ttk.Frame(self.root, padding=20)
        main.grid(sticky="nsew")

        # Title
        title = ttk.Label(main, text="Epson EcoTank Ink Pad Reset",
                          font=("Segoe UI", 16, "bold"))
        title.grid(row=0, column=0, columnspan=3, pady=(0, 5))

        subtitle = ttk.Label(main, text="ET-2720 / ET-2721 / ET-2700 Series",
                             font=("Segoe UI", 10), foreground="gray")
        subtitle.grid(row=1, column=0, columnspan=3, pady=(0, 15))

        # IP input
        ip_frame = ttk.LabelFrame(main, text="Printer Connection", padding=10)
        ip_frame.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(0, 10))

        ttk.Label(ip_frame, text="Printer IP:").grid(row=0, column=0, padx=(0, 5))
        ip_entry = ttk.Entry(ip_frame, textvariable=self.printer_ip, width=20,
                             font=("Consolas", 11))
        ip_entry.grid(row=0, column=1, padx=(0, 10))

        self.btn_check = ttk.Button(ip_frame, text="Check Printer",
                                     command=self._on_check)
        self.btn_check.grid(row=0, column=2)

        # Printer info
        self.lbl_model = ttk.Label(ip_frame, text="", font=("Segoe UI", 9))
        self.lbl_model.grid(row=1, column=0, columnspan=3, pady=(5, 0))

        # Status display
        status_frame = ttk.LabelFrame(main, text="Waste Ink Status", padding=10)
        status_frame.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(0, 10))

        # Main waste counter
        ttk.Label(status_frame, text="Main waste counter:").grid(row=0, column=0, sticky="w")
        self.main_bar = ttk.Progressbar(status_frame, length=250, mode='determinate')
        self.main_bar.grid(row=0, column=1, padx=10)
        self.lbl_main = ttk.Label(status_frame, text="--", width=10)
        self.lbl_main.grid(row=0, column=2)

        # Borderless waste counter
        ttk.Label(status_frame, text="Borderless waste counter:").grid(row=1, column=0, sticky="w", pady=(5, 0))
        self.border_bar = ttk.Progressbar(status_frame, length=250, mode='determinate')
        self.border_bar.grid(row=1, column=1, padx=10, pady=(5, 0))
        self.lbl_border = ttk.Label(status_frame, text="--", width=10)
        self.lbl_border.grid(row=1, column=2, pady=(5, 0))

        # Maintenance levels
        ttk.Label(status_frame, text="Maintenance level (1st):").grid(row=2, column=0, sticky="w", pady=(5, 0))
        self.lbl_maint1 = ttk.Label(status_frame, text="--", width=10)
        self.lbl_maint1.grid(row=2, column=2, pady=(5, 0))

        ttk.Label(status_frame, text="Maintenance level (2nd):").grid(row=3, column=0, sticky="w", pady=(5, 0))
        self.lbl_maint2 = ttk.Label(status_frame, text="--", width=10)
        self.lbl_maint2.grid(row=3, column=2, pady=(5, 0))

        # Overall status
        self.lbl_status = ttk.Label(status_frame, text="", font=("Segoe UI", 11, "bold"))
        self.lbl_status.grid(row=4, column=0, columnspan=3, pady=(10, 0))

        # Buttons
        btn_frame = ttk.Frame(main)
        btn_frame.grid(row=4, column=0, columnspan=3, pady=(5, 10))

        self.btn_reset = ttk.Button(btn_frame, text="Reset Waste Ink Counters",
                                     command=self._on_reset)
        self.btn_reset.grid(row=0, column=0, padx=5)
        self.btn_reset.state(['disabled'])

        # Log area
        log_frame = ttk.LabelFrame(main, text="Log", padding=5)
        log_frame.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(0, 5))

        self.log_text = tk.Text(log_frame, height=10, width=65, font=("Consolas", 9),
                                state='disabled', bg='#1e1e1e', fg='#d4d4d4',
                                insertbackground='white')
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        # Configure text tags for colored output
        self.log_text.tag_configure('ok', foreground='#4ec9b0')
        self.log_text.tag_configure('error', foreground='#f44747')
        self.log_text.tag_configure('warn', foreground='#dcdcaa')
        self.log_text.tag_configure('info', foreground='#569cd6')

        # Footer
        footer = ttk.Label(main,
                           text="Based on epson_print_conf by Ircama. "
                                "Use at your own risk.",
                           font=("Segoe UI", 8), foreground="gray")
        footer.grid(row=6, column=0, columnspan=3)

    def log(self, msg, tag=None):
        """Append message to log (thread-safe)."""
        def _append():
            self.log_text.configure(state='normal')
            if tag:
                self.log_text.insert('end', msg + '\n', tag)
            else:
                self.log_text.insert('end', msg + '\n')
            self.log_text.see('end')
            self.log_text.configure(state='disabled')
        self.root.after(0, _append)

    def _set_buttons(self, checking=False, can_reset=False):
        def _update():
            if checking:
                self.btn_check.state(['disabled'])
                self.btn_reset.state(['disabled'])
            else:
                self.btn_check.state(['!disabled'])
                if can_reset:
                    self.btn_reset.state(['!disabled'])
                else:
                    self.btn_reset.state(['disabled'])
        self.root.after(0, _update)

    def _update_ui(self, main_pct, border_pct, maint1, maint2, is_over):
        def _update():
            self.main_bar['value'] = min(main_pct, 100)
            self.lbl_main.configure(text=f"{main_pct:.1f}%")
            self.border_bar['value'] = min(border_pct, 100)
            self.lbl_border.configure(text=f"{border_pct:.1f}%")
            self.lbl_maint1.configure(text=str(maint1) if maint1 is not None else "?")
            self.lbl_maint2.configure(text=str(maint2) if maint2 is not None else "?")
            if is_over:
                self.lbl_status.configure(text="WASTE INK PAD FULL - NEEDS RESET",
                                          foreground="red")
            else:
                self.lbl_status.configure(text="OK - Within limits",
                                          foreground="green")
        self.root.after(0, _update)

    def _set_model(self, text):
        self.root.after(0, lambda: self.lbl_model.configure(text=text))

    def _on_check(self):
        if self.is_running:
            return
        self.is_running = True
        threading.Thread(target=self._check_thread, daemon=True).start()

    def _check_thread(self):
        ip = self.printer_ip.get().strip()
        self._set_buttons(checking=True)
        self.log(f"Connecting to {ip}...", 'info')

        if not check_snmp_connectivity(ip):
            self.log(f"No response from {ip}. Check IP and network.", 'error')
            self._set_model("Not connected")
            self._set_buttons(checking=False, can_reset=False)
            self.is_running = False
            return

        self.log("SNMP connected!", 'ok')

        model = get_printer_model(ip)
        if model:
            self.log(f"Printer model: {model}", 'ok')
            self._set_model(f"Connected: {model}")
        else:
            self.log("Could not read model name", 'warn')
            self._set_model("Connected (unknown model)")

        self.log("Reading waste ink counters...", 'info')
        self._read_counters(ip)
        self.is_running = False

    def _read_counters(self, ip):
        # Read main waste counter
        main_bytes = []
        for addr in MAIN_WASTE_ADDRS:
            val = read_eeprom(ip, addr)
            main_bytes.append(val if val is not None else 0)
            self.log(f"  EEPROM[{addr}] = {val}", 'info')

        # Read borderless waste counter
        border_bytes = []
        for addr in BORDERLESS_WASTE_ADDRS:
            val = read_eeprom(ip, addr)
            border_bytes.append(val if val is not None else 0)
            if addr != 47:  # Don't log shared byte twice
                self.log(f"  EEPROM[{addr}] = {val}", 'info')

        main_value = main_bytes[0] + (main_bytes[1] << 8) + (main_bytes[2] << 16)
        border_value = border_bytes[0] + (border_bytes[1] << 8) + (border_bytes[2] << 16)
        main_pct = main_value / MAIN_WASTE_DIVIDER
        border_pct = border_value / BORDERLESS_WASTE_DIVIDER

        maint1 = read_eeprom(ip, 54)
        maint2 = read_eeprom(ip, 55)
        self.log(f"  Maintenance level 1st: {maint1}", 'info')
        self.log(f"  Maintenance level 2nd: {maint2}", 'info')

        is_over = (maint1 is not None and maint1 > MAINTENANCE_THRESHOLD)

        self.log(f"Main waste: {main_value} raw ({main_pct:.1f}%)")
        self.log(f"Borderless waste: {border_value} raw ({border_pct:.1f}%)")

        if is_over:
            self.log("WASTE INK PAD IS OVER LIMIT!", 'error')
        else:
            self.log("Waste ink levels are within limits.", 'ok')

        self._update_ui(main_pct, border_pct, maint1, maint2, is_over)
        self._set_buttons(checking=False, can_reset=True)

    def _on_reset(self):
        if self.is_running:
            return
        if not messagebox.askyesno(
            "Confirm Reset",
            "This will reset the waste ink pad counters to zero.\n\n"
            "Make sure you have emptied or replaced the waste ink pad "
            "if it is actually full.\n\n"
            "Continue?"
        ):
            return
        self.is_running = True
        threading.Thread(target=self._reset_thread, daemon=True).start()

    def _reset_thread(self):
        ip = self.printer_ip.get().strip()
        self._set_buttons(checking=True)
        self.log("=" * 50)
        self.log("RESETTING WASTE INK COUNTERS...", 'warn')

        success = True
        for addr, (label, value) in WASTE_INK_ADDRS.items():
            result = write_eeprom(ip, addr, value)
            if result is True:
                self.log(f"  EEPROM[{addr:3d}] = {value:3d} ... OK  ({label})", 'ok')
            else:
                status = "FAIL" if result is False else "NO RESPONSE"
                self.log(f"  EEPROM[{addr:3d}] = {value:3d} ... {status}  ({label})", 'error')
                success = False
            time.sleep(0.2)

        self.log("Verifying...", 'info')
        time.sleep(1)
        self._read_counters(ip)

        if success:
            self.log("=" * 50)
            self.log("RESET SUCCESSFUL!", 'ok')
            self.log("")
            self.log("Next steps:", 'warn')
            self.log("  1. Turn OFF the printer (unplug power)")
            self.log("  2. Wait 30 seconds")
            self.log("  3. Plug back in and turn ON")
            self.log("  4. Try printing a test page")
            self.root.after(0, lambda: messagebox.showinfo(
                "Reset Complete",
                "Waste ink counters have been reset!\n\n"
                "Please power cycle the printer:\n"
                "1. Unplug the printer\n"
                "2. Wait 30 seconds\n"
                "3. Plug back in and turn on\n"
                "4. Print a test page"
            ))
        else:
            self.log("Some writes failed. Try power cycling and running again.", 'error')

        self.is_running = False


def main():
    root = tk.Tk()
    app = EpsonResetApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
