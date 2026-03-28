#!/usr/bin/env python3
"""
Epson EcoTank Waste Ink Pad Reset Tool
Resets waste ink counters on Epson ET-2720/2721/2700 series printers over WiFi.

No special drivers or USB connection needed - works over your local network via SNMP.
"""
import ipaddress
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

SUPPORTED_MODELS = ["ET-2720", "ET-2721", "ET-2700"]

# Waste ink EEPROM addresses and reset values
# Ordered so maintenance level bytes (the lock gate) are written LAST
WASTE_INK_RESET = [
    (48, "Main waste counter (byte 0)", 0),
    (49, "Main waste counter (byte 1)", 0),
    (47, "Main waste counter (byte 2)", 0),
    (50, "Borderless waste counter (byte 0)", 0),
    (51, "Borderless waste counter (byte 1)", 0),
    (52, "Counter store (byte 0)", 0),
    (53, "Counter store (byte 1)", 0),
    (28, "Auxiliary counter", 0),
    (54, "Maintenance level (1st)", 94),
    (55, "Maintenance level (2nd)", 94),
]

# Waste ink counter composition
MAIN_WASTE_ADDRS = [48, 49, 47]      # byte0, byte1, byte2
BORDERLESS_WASTE_ADDRS = [50, 51, 47]  # byte0, byte1, byte2 (shares byte2)
MAIN_WASTE_DIVIDER = 63.45
BORDERLESS_WASTE_DIVIDER = 34.15
MAINTENANCE_THRESHOLD = 94


# ─── Input Validation ────────────────────────────────────────────────────────

def validate_ip(ip_str):
    """Validate IP is a private IPv4 address. Returns (ip_str, error_msg)."""
    try:
        addr = ipaddress.IPv4Address(ip_str)
    except (ipaddress.AddressValueError, ValueError):
        return None, f"'{ip_str}' is not a valid IPv4 address."
    if addr.is_multicast:
        return None, "Multicast addresses are not allowed."
    if addr.is_loopback:
        return None, "Loopback addresses are not allowed."
    if str(addr) in ("0.0.0.0", "255.255.255.255"):
        return None, "Broadcast addresses are not allowed."
    if not addr.is_private:
        return None, (f"{addr} is a public IP. This tool is for local "
                      f"network printers only (e.g. 192.168.x.x).")
    return str(addr), None


def is_supported_model(model_str):
    """Check if the detected model is in the supported list."""
    if not model_str:
        return False
    for m in SUPPORTED_MODELS:
        if m in model_str:
            return True
    return False


# ─── SNMP / EEPROM Protocol ──────────────────────────────────────────────────

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
    ] + [0 if b == 0 else b + 1 for b in WRITE_KEY])


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
    try:
        sock.sendto(pkt, (ip, SNMP_PORT))
        data, _ = sock.recvfrom(4096)
        return data
    except (socket.timeout, socket.gaierror, OSError):
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

        try:
            self.root.iconbitmap(default='')
        except Exception:
            pass

        self.printer_ip = tk.StringVar(value="")
        self.is_running = False
        self._lock = threading.Lock()
        self._detected_model = None

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
        main = ttk.Frame(self.root, padding=20)
        main.grid(sticky="nsew")

        title = ttk.Label(main, text="Epson EcoTank Ink Pad Reset",
                          font=("Segoe UI", 16, "bold"))
        title.grid(row=0, column=0, columnspan=3, pady=(0, 5))

        subtitle = ttk.Label(main, text="ET-2720 / ET-2721 / ET-2700 Series",
                             font=("Segoe UI", 10), foreground="gray")
        subtitle.grid(row=1, column=0, columnspan=3, pady=(0, 15))

        ip_frame = ttk.LabelFrame(main, text="Printer Connection", padding=10)
        ip_frame.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(0, 10))

        ttk.Label(ip_frame, text="Printer IP:").grid(row=0, column=0, padx=(0, 5))
        self.ip_entry = ttk.Entry(ip_frame, textvariable=self.printer_ip, width=20,
                                  font=("Consolas", 11))
        self.ip_entry.grid(row=0, column=1, padx=(0, 10))
        self.ip_entry.insert(0, "")
        # Placeholder hint
        self.ip_entry.bind('<FocusIn>', self._clear_placeholder)
        self.ip_entry.bind('<FocusOut>', self._show_placeholder)
        self._show_placeholder(None)

        self.btn_check = ttk.Button(ip_frame, text="Check Printer",
                                     command=self._on_check)
        self.btn_check.grid(row=0, column=2)

        self.lbl_model = ttk.Label(ip_frame, text="", font=("Segoe UI", 9))
        self.lbl_model.grid(row=1, column=0, columnspan=3, pady=(5, 0))

        status_frame = ttk.LabelFrame(main, text="Waste Ink Status", padding=10)
        status_frame.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(0, 10))

        ttk.Label(status_frame, text="Main waste counter:").grid(row=0, column=0, sticky="w")
        self.main_bar = ttk.Progressbar(status_frame, length=250, mode='determinate')
        self.main_bar.grid(row=0, column=1, padx=10)
        self.lbl_main = ttk.Label(status_frame, text="--", width=10)
        self.lbl_main.grid(row=0, column=2)

        ttk.Label(status_frame, text="Borderless waste counter:").grid(row=1, column=0, sticky="w", pady=(5, 0))
        self.border_bar = ttk.Progressbar(status_frame, length=250, mode='determinate')
        self.border_bar.grid(row=1, column=1, padx=10, pady=(5, 0))
        self.lbl_border = ttk.Label(status_frame, text="--", width=10)
        self.lbl_border.grid(row=1, column=2, pady=(5, 0))

        ttk.Label(status_frame, text="Maintenance level (1st):").grid(row=2, column=0, sticky="w", pady=(5, 0))
        self.lbl_maint1 = ttk.Label(status_frame, text="--", width=10)
        self.lbl_maint1.grid(row=2, column=2, pady=(5, 0))

        ttk.Label(status_frame, text="Maintenance level (2nd):").grid(row=3, column=0, sticky="w", pady=(5, 0))
        self.lbl_maint2 = ttk.Label(status_frame, text="--", width=10)
        self.lbl_maint2.grid(row=3, column=2, pady=(5, 0))

        self.lbl_status = ttk.Label(status_frame, text="", font=("Segoe UI", 11, "bold"))
        self.lbl_status.grid(row=4, column=0, columnspan=3, pady=(10, 0))

        btn_frame = ttk.Frame(main)
        btn_frame.grid(row=4, column=0, columnspan=3, pady=(5, 10))

        self.btn_reset = ttk.Button(btn_frame, text="Reset Waste Ink Counters",
                                     command=self._on_reset)
        self.btn_reset.grid(row=0, column=0, padx=5)
        self.btn_reset.state(['disabled'])

        log_frame = ttk.LabelFrame(main, text="Log", padding=5)
        log_frame.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(0, 5))

        self.log_text = tk.Text(log_frame, height=10, width=65, font=("Consolas", 9),
                                state='disabled', bg='#1e1e1e', fg='#d4d4d4',
                                insertbackground='white')
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        self.log_text.tag_configure('ok', foreground='#4ec9b0')
        self.log_text.tag_configure('error', foreground='#f44747')
        self.log_text.tag_configure('warn', foreground='#dcdcaa')
        self.log_text.tag_configure('info', foreground='#569cd6')

        footer = ttk.Label(main,
                           text="Based on epson_print_conf by Ircama. "
                                "Use at your own risk.",
                           font=("Segoe UI", 8), foreground="gray")
        footer.grid(row=6, column=0, columnspan=3)

    def _show_placeholder(self, event):
        if not self.printer_ip.get():
            self.ip_entry.configure(foreground='gray')
            self.printer_ip.set("e.g. 192.168.1.100")

    def _clear_placeholder(self, event):
        if self.printer_ip.get() == "e.g. 192.168.1.100":
            self.printer_ip.set("")
            self.ip_entry.configure(foreground='black')

    def _get_ip(self):
        """Get and validate the IP from the input field."""
        raw = self.printer_ip.get().strip()
        if raw == "e.g. 192.168.1.100":
            raw = ""
        if not raw:
            self.log("Please enter your printer's IP address.", 'error')
            return None
        ip, err = validate_ip(raw)
        if err:
            self.log(f"Invalid IP: {err}", 'error')
            return None
        return ip

    def log(self, msg, tag=None):
        """Append message to log (thread-safe)."""
        def _append():
            if not self.root.winfo_exists():
                return
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
            if not self.root.winfo_exists():
                return
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
            if not self.root.winfo_exists():
                return
            self.main_bar['value'] = min(main_pct, 100)
            display_main = f"{main_pct:.1f}%" if main_pct <= 100 else f"100%+ ({main_pct:.0f}%)"
            self.lbl_main.configure(text=display_main)
            self.border_bar['value'] = min(border_pct, 100)
            display_border = f"{border_pct:.1f}%" if border_pct <= 100 else f"100%+ ({border_pct:.0f}%)"
            self.lbl_border.configure(text=display_border)
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
        def _update():
            if self.root.winfo_exists():
                self.lbl_model.configure(text=text)
        self.root.after(0, _update)

    def _on_check(self):
        with self._lock:
            if self.is_running:
                return
            self.is_running = True
        self.btn_check.state(['disabled'])
        self.btn_reset.state(['disabled'])
        threading.Thread(target=self._check_thread, daemon=True).start()

    def _check_thread(self):
        try:
            ip = self._get_ip()
            if not ip:
                self._set_buttons(checking=False, can_reset=False)
                return

            self._set_buttons(checking=True)
            self.log(f"Connecting to {ip}...", 'info')

            if not check_snmp_connectivity(ip):
                self.log(f"No response from {ip}. Check IP and network.", 'error')
                self.log("Ensure printer is on, connected to WiFi, and "
                         "UDP port 161 is not blocked.", 'warn')
                self._set_model("Not connected")
                self._set_buttons(checking=False, can_reset=False)
                return

            self.log("SNMP connected!", 'ok')

            model = get_printer_model(ip)
            self._detected_model = model
            if model:
                self.log(f"Printer model: {model}", 'ok')
                self._set_model(f"Connected: {model}")
                if not is_supported_model(model):
                    self.log(f"WARNING: {model} is not a tested model!", 'error')
                    self.log(f"Supported: {', '.join(SUPPORTED_MODELS)}", 'warn')
                    self.log("EEPROM addresses may differ. Reset could damage "
                             "an unsupported printer.", 'error')
            else:
                self.log("Could not read model name", 'warn')
                self._set_model("Connected (unknown model)")

            self.log("Reading waste ink counters...", 'info')
            read_ok = self._read_counters(ip)
            can_reset = read_ok and (model is None or is_supported_model(model))
            self._set_buttons(checking=False, can_reset=can_reset)
        finally:
            with self._lock:
                self.is_running = False

    def _read_counters(self, ip):
        """Read and display waste ink counters. Returns True if all reads succeeded."""
        read_failed = False

        main_bytes = []
        for addr in MAIN_WASTE_ADDRS:
            val = read_eeprom(ip, addr)
            if val is None:
                self.log(f"  EEPROM[{addr}] = READ FAILED", 'error')
                read_failed = True
                main_bytes.append(0)
            else:
                main_bytes.append(val)
                self.log(f"  EEPROM[{addr}] = {val}", 'info')

        border_bytes = []
        for addr in BORDERLESS_WASTE_ADDRS:
            val = read_eeprom(ip, addr)
            if val is None:
                if addr != 47:
                    self.log(f"  EEPROM[{addr}] = READ FAILED", 'error')
                read_failed = True
                border_bytes.append(0)
            else:
                border_bytes.append(val)
                if addr != 47:
                    self.log(f"  EEPROM[{addr}] = {val}", 'info')

        main_value = main_bytes[0] + (main_bytes[1] << 8) + (main_bytes[2] << 16)
        border_value = border_bytes[0] + (border_bytes[1] << 8) + (border_bytes[2] << 16)
        main_pct = main_value / MAIN_WASTE_DIVIDER
        border_pct = border_value / BORDERLESS_WASTE_DIVIDER

        maint1 = read_eeprom(ip, 54)
        maint2 = read_eeprom(ip, 55)
        if maint1 is None or maint2 is None:
            self.log("  Maintenance level read failed!", 'error')
            read_failed = True
        self.log(f"  Maintenance level 1st: {maint1}", 'info')
        self.log(f"  Maintenance level 2nd: {maint2}", 'info')

        is_over = (maint1 is not None and maint1 > MAINTENANCE_THRESHOLD)

        self.log(f"Main waste: {main_value} raw ({main_pct:.1f}%)")
        self.log(f"Borderless waste: {border_value} raw ({border_pct:.1f}%)")

        if read_failed:
            self.log("Some EEPROM reads failed. Values may be inaccurate.", 'error')
        elif is_over:
            self.log("WASTE INK PAD IS OVER LIMIT!", 'error')
        else:
            self.log("Waste ink levels are within limits.", 'ok')

        self._update_ui(main_pct, border_pct, maint1, maint2, is_over)
        return not read_failed

    def _on_reset(self):
        with self._lock:
            if self.is_running:
                return
            self.is_running = True

        # Model safety check
        if self._detected_model and not is_supported_model(self._detected_model):
            proceed = messagebox.askyesno(
                "Unsupported Model",
                f"Your printer ({self._detected_model}) is not in the tested "
                f"model list ({', '.join(SUPPORTED_MODELS)}).\n\n"
                f"EEPROM addresses may differ. Proceeding could damage your "
                f"printer.\n\n"
                f"Are you sure you want to continue?"
            )
            if not proceed:
                with self._lock:
                    self.is_running = False
                return

        if not messagebox.askyesno(
            "Confirm Reset",
            "This will reset the waste ink pad counters to zero.\n\n"
            "Make sure you have emptied or replaced the waste ink pad "
            "if it is actually full.\n\n"
            "Continue?"
        ):
            with self._lock:
                self.is_running = False
            return

        self.btn_check.state(['disabled'])
        self.btn_reset.state(['disabled'])
        threading.Thread(target=self._reset_thread, daemon=True).start()

    def _reset_thread(self):
        try:
            ip = self._get_ip()
            if not ip:
                self._set_buttons(checking=False, can_reset=True)
                return

            self._set_buttons(checking=True)
            self.log("=" * 50)
            self.log("RESETTING WASTE INK COUNTERS...", 'warn')

            success = True
            for addr, label, value in WASTE_INK_RESET:
                self.log(f"  Writing EEPROM[{addr:3d}] = {value:3d} ({label})...", 'info')
                result = write_eeprom(ip, addr, value)
                if result is True:
                    self.log(f"  EEPROM[{addr:3d}] = {value:3d} ... OK", 'ok')
                else:
                    status = "FAIL" if result is False else "NO RESPONSE"
                    self.log(f"  EEPROM[{addr:3d}] = {value:3d} ... {status}", 'error')
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
                def _show_success():
                    if self.root.winfo_exists():
                        messagebox.showinfo(
                            "Reset Complete",
                            "Waste ink counters have been reset!\n\n"
                            "Please power cycle the printer:\n"
                            "1. Unplug the printer\n"
                            "2. Wait 30 seconds\n"
                            "3. Plug back in and turn on\n"
                            "4. Print a test page"
                        )
                self.root.after(0, _show_success)
            else:
                self.log("Some writes failed. Try power cycling and running again.", 'error')

            self._set_buttons(checking=False, can_reset=True)
        finally:
            with self._lock:
                self.is_running = False


def main():
    root = tk.Tk()
    app = EpsonResetApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
