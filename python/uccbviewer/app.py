"""
UCCBViewer (LIN) - Tkinter GUI for the UsbCANConverterBasic (uCCB) adapter.

LIN-only Python port of USBtinViewer.java.  Provides the LIN schedule table,
trace and monitor views, file logging and the SLCAN protocol layer used to
drive the uCCB adapter in LIN MASTER / MONITOR / SLAVE mode.
"""

import os
import queue
import threading
import time
from datetime import datetime

import tkinter as tk
from tkinter import ttk

import serial.tools.list_ports

from . import lin_tp
from .can_message import CANMessage
from .log_message import LogMessage, MessageType
from .table_models import MonitorModel, format_id, format_data, format_dlc
from .usbtin import USBtin, USBtinException, OpenMode

VERSION = "2.6"

# Build date/time, baked in by uCCBViewer.spec at build time.  Falls back to
# "dev" when running straight from source (no _build_info.py present).
try:
    from ._build_info import BUILD_TIME
except ImportError:
    BUILD_TIME = "dev"

# UDS negative-response codes (ISO 14229) shown decoded in the diagnostics tab
_NRC_NAMES = {
    0x10: "generalReject",
    0x11: "serviceNotSupported",
    0x12: "subFunctionNotSupported",
    0x13: "incorrectMessageLengthOrInvalidFormat",
    0x22: "conditionsNotCorrect",
    0x31: "requestOutOfRange",
    0x33: "securityAccessDenied",
    0x35: "invalidKey",
    0x78: "requestCorrectlyReceived-ResponsePending",
    0x7E: "subFunctionNotSupportedInActiveSession",
    0x7F: "serviceNotSupportedInActiveSession",
}


def _hexspace(data):
    """Space-separated uppercase hex, e.g. b'\\x22\\xf1' -> '22 F1'."""
    return " ".join("%02X" % b for b in data)

ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")

# number of LIN schedule rows (matches the Java constants)
LIN_ROW_COUNT = 15
ROWS_PER_COLUMN = 5

# maximum trace rows before the log is cleared (as in the Java version)
MAX_TRACE_ROWS = 50000

# number of discrete colour steps for the per-nibble heat map (black -> red)
HEAT_LEVELS = 24

# minimum seconds between monitor-view repaints (coalesce bursts of frames)
_MONITOR_RENDER_INTERVAL = 0.15

# --- dark theme palette (Catppuccin-ish, near-black) ------------------------
BG = "#14141c"           # window background (near-black)
SURFACE = "#1e1e2e"      # cards / tables
FIELD = "#272739"        # inputs / table headings / hover
BORDER = "#313244"       # hairline borders
TEXT = "#cdd6f4"         # primary text
MUTED = "#9399b2"        # captions / secondary text
ACCENT = "#89b4fa"       # section headers / primary action
ACCENT_ACTIVE = "#74c7ec"
GREEN = "#a6e3a1"        # table values (default / "constant" nibble)
RED = "#f38ba8"          # errors / disconnected
SEL_BG = "#89b4fa"       # table selection
DISABLED_BG = "#181820"  # disabled control fill (sinks into the window)
DISABLED_FG = "#4b4b5e"  # disabled text/icons — clearly dimmed vs MUTED

# per-nibble heat map ramp: green (constant) -> red (changes often)
HEAT_LOW = (166, 227, 161)
HEAT_HIGH = (243, 90, 100)

BASE_FONT = ("Segoe UI", 9)
CAPTION_FONT = ("Segoe UI", 8)
BOLD_FONT = ("Segoe UI", 9, "bold")
MONO_FONT = ("Consolas", 10)


def current_millis():
    return int(time.time() * 1000)


def bytes_to_hex(data):
    """Uppercase hex string, matching USBtinViewer.bytesToHex()."""
    return "".join("%02X" % b for b in data)


class UCCBViewer(tk.Tk):

    def __init__(self):
        super().__init__()

        self.usbtin = USBtin()
        self.base_timestamp = 0
        self.disable_msg_update = False
        self.sending_is_active = False
        self.hex_display = True

        self.filelogging = False
        self.fileloggingCSV = False
        self.file_log = None

        self.id_scan = 0
        self._scan_after_id = None
        self._send_after_id = None
        self._conn_busy = False   # True while a connect/disconnect worker runs

        # On a single-wire LIN bus the adapter receives its own transmission,
        # so every frame we send comes straight back as an identical RX frame.
        # We remember each sent frame here and drop the first matching echo.
        self._pending_echoes = []

        # trace data (kept so HEX/DEC toggle can re-render)
        self.trace_messages = []
        self.monitor_model = MonitorModel()
        # the monitor view is re-rendered (coalesced) whenever this is set
        self._monitor_dirty = False
        self._last_monitor_render = 0.0

        # marshal RX frames from the reader thread to the Tk main loop
        self.rx_queue = queue.Queue()
        # marshal UDS worker-thread results to the Tk main loop
        self.uds_queue = queue.Queue()
        self._uds_busy = False
        # marshal connect/disconnect worker results to the Tk main loop.
        # (Tk calls such as .after() are not safe from a worker thread, so the
        # workers post here and the main-loop poll runs the completion handler.)
        self.conn_queue = queue.Queue()

        self.title("UCCBViewer LIN %s  (build %s)" % (VERSION, BUILD_TIME))
        self._apply_initial_geometry(760, 680)
        self.minsize(680, 600)
        self._setup_style()
        self._load_icons()
        try:
            self.iconphoto(True, self.icons["app"])
        except tk.TclError:
            pass

        self._build_ui()
        self._setup_context_menus()

        self.usbtin.add_message_listener(self._on_can_message_threadsafe)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # start RX queue polling and trigger initial field sync
        self.after(20, self._drain_rx_queue)
        self._msg_string_to_fields()

    # ==================================================================
    # Resources
    # ==================================================================
    def _load_icons(self):
        self.icons = {
            MessageType.INFO: tk.PhotoImage(file=os.path.join(ICON_DIR, "info.png")),
            MessageType.ERROR: tk.PhotoImage(file=os.path.join(ICON_DIR, "error.png")),
            MessageType.IN: tk.PhotoImage(file=os.path.join(ICON_DIR, "receive.png")),
            MessageType.OUT: tk.PhotoImage(file=os.path.join(ICON_DIR, "send.png")),
            "app": tk.PhotoImage(file=os.path.join(ICON_DIR, "usbtinviewer.png")),
        }

    # ==================================================================
    # Styling
    # ==================================================================
    def _apply_initial_geometry(self, w, h):
        """Size/position the window so it always fits on the current screen."""
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        h = min(h, sh - 80)          # leave room for title bar + taskbar
        w = min(w, sw - 40)
        x = max(0, (sw - w) // 2)
        self.geometry("%dx%d+%d+%d" % (w, h, x, 10))

    def _setup_style(self):
        self.configure(bg=BG)
        self.option_add("*Menu.font", BASE_FONT)
        self.option_add("*Menu.background", SURFACE)
        self.option_add("*Menu.foreground", TEXT)
        self.option_add("*Menu.activeBackground", ACCENT)
        self.option_add("*Menu.activeForeground", BG)
        # style the combobox dropdown lists (a classic Listbox under the hood)
        self.option_add("*TCombobox*Listbox.background", FIELD)
        self.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.option_add("*TCombobox*Listbox.selectForeground", BG)
        self.option_add("*TCombobox*Listbox.font", BASE_FONT)

        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(".", background=BG, foreground=TEXT, font=BASE_FONT,
                        bordercolor=BORDER, darkcolor=SURFACE, lightcolor=SURFACE,
                        troughcolor=BG, fieldbackground=FIELD)
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Caption.TLabel", background=BG, foreground=MUTED,
                        font=CAPTION_FONT)
        style.configure("Status.TLabel", background=SURFACE, foreground=MUTED,
                        font=CAPTION_FONT)

        # group boxes: cyan/blue title, hairline border
        style.configure("TLabelframe", background=BG, bordercolor=BORDER,
                        relief="solid", borderwidth=1, padding=(8, 4))
        style.configure("TLabelframe.Label", background=BG, foreground=ACCENT,
                        font=BOLD_FONT)

        # buttons -------------------------------------------------------
        style.configure("TButton", background=FIELD, foreground=TEXT,
                        bordercolor=BORDER, borderwidth=1, relief="flat",
                        padding=(9, 3), font=BASE_FONT, focuscolor=BG)
        style.map("TButton",
                  background=[("pressed", BORDER), ("active", "#34344a"),
                              ("disabled", DISABLED_BG)],
                  foreground=[("disabled", DISABLED_FG)],
                  bordercolor=[("disabled", BORDER),
                               ("focus", ACCENT), ("active", ACCENT)])
        # primary action: filled accent with dark text
        style.configure("Accent.TButton", background=ACCENT, foreground=BG,
                        bordercolor=ACCENT, borderwidth=0)
        style.map("Accent.TButton",
                  background=[("pressed", ACCENT_ACTIVE), ("active", ACCENT_ACTIVE),
                              ("disabled", DISABLED_BG)],
                  foreground=[("disabled", DISABLED_FG)])
        # "dirty" action: table edited but not yet sent — filled red to grab
        # attention so the user knows the Send Table button needs a click
        style.configure("Dirty.TButton", background=RED, foreground=BG,
                        bordercolor=RED, borderwidth=0)
        style.map("Dirty.TButton",
                  background=[("pressed", RED), ("active", RED),
                              ("disabled", DISABLED_BG)],
                  foreground=[("disabled", DISABLED_FG)])

        # check buttons -------------------------------------------------
        style.configure("TCheckbutton", background=BG, foreground=TEXT,
                        font=BASE_FONT, indicatorbackground=FIELD,
                        indicatorforeground=BG, bordercolor=BORDER, focuscolor=BG)
        style.map("TCheckbutton",
                  background=[("active", BG)],
                  foreground=[("disabled", DISABLED_FG)],
                  indicatorbackground=[("disabled", DISABLED_BG),
                                       ("selected", ACCENT),
                                       ("active", "#34344a")],
                  indicatorforeground=[("selected", BG)])

        # comboboxes ----------------------------------------------------
        style.configure("TCombobox", fieldbackground=FIELD, background=FIELD,
                        foreground=TEXT, bordercolor=BORDER, arrowcolor=TEXT,
                        arrowsize=13, padding=2)
        style.map("TCombobox",
                  fieldbackground=[("readonly", FIELD), ("disabled", DISABLED_BG)],
                  foreground=[("disabled", DISABLED_FG)],
                  bordercolor=[("focus", ACCENT)],
                  arrowcolor=[("disabled", DISABLED_FG)])

        # notebook ------------------------------------------------------
        style.configure("TNotebook", background=BG, borderwidth=0,
                        tabmargins=(2, 4, 2, 0))
        style.configure("TNotebook.Tab", background=SURFACE, foreground=MUTED,
                        padding=(12, 5), font=BASE_FONT, borderwidth=0)
        style.map("TNotebook.Tab",
                  background=[("selected", FIELD), ("disabled", DISABLED_BG)],
                  foreground=[("selected", ACCENT), ("disabled", DISABLED_FG)])

        # treeview (trace) ---------------------------------------------
        style.configure("Treeview", background=SURFACE, fieldbackground=SURFACE,
                        foreground=GREEN, rowheight=21, borderwidth=0,
                        font=MONO_FONT)
        style.configure("Treeview.Heading", background=FIELD, foreground=ACCENT,
                        relief="flat", padding=4, font=BOLD_FONT)
        style.map("Treeview.Heading", background=[("active", BORDER)])
        style.map("Treeview",
                  background=[("selected", SEL_BG)],
                  foreground=[("selected", BG)])

        # scrollbars ----------------------------------------------------
        style.configure("TScrollbar", background=FIELD, troughcolor=BG,
                        bordercolor=BG, arrowcolor=MUTED, relief="flat")
        style.map("TScrollbar", background=[("active", BORDER)])
        style.configure("TSeparator", background=BORDER)

    # ==================================================================
    # Right-click context menus (copy / paste)
    # ==================================================================
    def _setup_context_menus(self):
        """Attach a right-click copy/paste menu to every text field, and a
        copy menu to the trace/monitor tables and the UDS log.

        Binding per widget *class* covers every existing and future widget of
        that kind in one shot.  Copy works even on disabled fields (it falls
        back to the full field contents), which is what makes the read-only
        data/length boxes copyable.
        """
        for cls in ("Entry", "Spinbox", "TEntry", "TCombobox"):
            self.bind_class(cls, "<Button-3>", self._popup_entry_menu)
        self.bind_class("Treeview", "<Button-3>", self._popup_tree_menu)
        self.bind_class("Text", "<Button-3>", self._popup_text_menu)

    @staticmethod
    def _widget_editable(w):
        try:
            return str(w.cget("state")) not in ("disabled", "readonly")
        except tk.TclError:
            return True

    def _set_clipboard(self, text):
        if text:
            self.clipboard_clear()
            self.clipboard_append(text)

    def _popup_entry_menu(self, event):
        w = event.widget
        try:
            has_sel = bool(w.selection_present())
        except tk.TclError:
            has_sel = False
        editable = self._widget_editable(w)
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Cut",
                         state="normal" if (editable and has_sel) else "disabled",
                         command=lambda: self._entry_cut(w))
        menu.add_command(label="Copy", command=lambda: self._entry_copy(w))
        menu.add_command(label="Paste",
                         state="normal" if editable else "disabled",
                         command=lambda: self._entry_paste(w))
        menu.add_separator()
        menu.add_command(label="Select All",
                         command=lambda: self._entry_select_all(w))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _entry_copy(self, w):
        try:
            text = w.selection_get()
        except tk.TclError:
            try:
                text = w.get()
            except tk.TclError:
                return
        self._set_clipboard(text)

    def _entry_cut(self, w):
        self._entry_copy(w)
        try:
            if w.selection_present():
                w.delete("sel.first", "sel.last")
        except tk.TclError:
            pass

    def _entry_paste(self, w):
        try:
            data = self.clipboard_get()
        except tk.TclError:
            return
        try:
            if w.selection_present():
                w.delete("sel.first", "sel.last")
        except tk.TclError:
            pass
        try:
            w.insert("insert", data)
        except tk.TclError:
            pass

    @staticmethod
    def _entry_select_all(w):
        try:
            w.selection_range(0, "end")
            w.icursor("end")
        except tk.TclError:
            pass

    def _popup_tree_menu(self, event):
        tree = event.widget
        row = tree.identify_row(event.y)
        if row and row not in tree.selection():
            tree.selection_set(row)
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Copy", command=lambda: self._tree_copy(tree))
        menu.add_command(label="Select All",
                         command=lambda: tree.selection_set(tree.get_children()))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _tree_copy(self, tree):
        lines = []
        for item in tree.selection():
            vals = tree.item(item, "values")
            lines.append("\t".join(str(v) for v in vals))
        self._set_clipboard("\n".join(lines))

    def _popup_text_menu(self, event):
        w = event.widget
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Copy", command=lambda: self._text_copy(w))
        menu.add_command(label="Select All",
                         command=lambda: (w.tag_add("sel", "1.0", "end"), None)[1])
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _text_copy(self, w):
        try:
            text = w.get("sel.first", "sel.last")
        except tk.TclError:
            text = w.get("1.0", "end-1c")
        self._set_clipboard(text)

    @staticmethod
    def _caption(parent, text, col, row=0):
        ttk.Label(parent, text=text, style="Caption.TLabel").grid(
            row=row, column=col, sticky="w", pady=(0, 2))

    @staticmethod
    def _style_entry(entry, font=None):
        entry.configure(relief="flat", borderwidth=0, highlightthickness=1,
                        highlightbackground=BORDER, highlightcolor=ACCENT,
                        background=FIELD, foreground=TEXT,
                        disabledbackground=DISABLED_BG, disabledforeground=DISABLED_FG,
                        insertbackground=TEXT, font=font or BASE_FONT)

    @staticmethod
    def _style_spinbox(sb):
        sb.configure(relief="flat", borderwidth=0, highlightthickness=1,
                     highlightbackground=BORDER, highlightcolor=ACCENT,
                     background=FIELD, foreground=TEXT, buttonbackground=FIELD,
                     disabledbackground=DISABLED_BG, disabledforeground=DISABLED_FG,
                     insertbackground=TEXT, font=BASE_FONT)

    # ==================================================================
    # UI construction
    # ==================================================================
    def _build_ui(self):
        self._build_top_bar()
        self._build_status_bar()
        self._build_send_bar()
        self._build_tabs()

    @staticmethod
    def _inline_label(parent, text):
        ttk.Label(parent, text=text, style="Caption.TLabel").pack(
            side="left", padx=(0, 3))

    def _build_top_bar(self):
        top = ttk.Frame(self, padding=(8, 6, 8, 2))
        top.pack(side="top", fill="x")

        # --- Connection group (single row) ---
        conn = ttk.Labelframe(top, text="Connection")
        conn.pack(side="top", fill="x")

        self._inline_label(conn, "Port")
        self.serial_port = ttk.Combobox(conn, width=12,
                                        postcommand=self._refresh_ports)
        self._refresh_ports()
        if self.serial_port["values"]:
            self.serial_port.current(0)
        self.serial_port.pack(side="left", padx=(0, 10))

        self._inline_label(conn, "Baud")
        self.bit_rate1 = ttk.Combobox(conn, width=7, state="readonly",
                                      values=["19200", "9600"])
        self.bit_rate1.current(0)
        self.bit_rate1.pack(side="left", padx=(0, 10))

        self._inline_label(conn, "Mode")
        self.openmode = ttk.Combobox(conn, width=10, state="readonly",
                                     values=["MONITOR", "MASTER", "SLAVE"])
        self.openmode.current(0)
        self.openmode.pack(side="left", padx=(0, 10))

        self.connection_button = ttk.Button(conn, text="Connect",
                                            style="Accent.TButton",
                                            command=self._on_connection)
        self.connection_button.pack(side="left")

        # --- Display & Logging group (single row) ---
        disp = ttk.Labelframe(top, text="Display & Logging")
        disp.pack(side="top", fill="x", pady=(4, 0))

        self.format_button = ttk.Button(disp, text="HEX", width=5,
                                        command=self._on_toggle_format)
        self.format_button.pack(side="left", padx=(0, 6))

        self.follow_var = tk.BooleanVar(value=True)
        self.follow_button = ttk.Checkbutton(disp, text="Follow",
                                            variable=self.follow_var)
        self.follow_button.pack(side="left", padx=(0, 8))

        ttk.Separator(disp, orient="vertical").pack(side="left", fill="y",
                                                    padx=6, pady=2)

        self.log_to_file_button = ttk.Button(disp, text="LogToFile",
                                            command=self._on_log_to_file)
        self.log_to_file_button.pack(side="left", padx=(6, 6))

        self.file_format_button = ttk.Button(disp, text="SLCAN", width=7,
                                            command=self._on_toggle_file_format)
        self.file_format_button.pack(side="left", padx=(0, 6))

        self.clear_button = ttk.Button(disp, text="Clear",
                                       command=self._on_clear)
        self.clear_button.pack(side="left")

    def _build_status_bar(self):
        bar = tk.Frame(self, bg=SURFACE, highlightthickness=1,
                       highlightbackground=BORDER)
        bar.pack(side="bottom", fill="x")
        self.status_dot = tk.Canvas(bar, width=10, height=10, bg=SURFACE,
                                    highlightthickness=0)
        self.status_dot.pack(side="left", padx=(10, 6), pady=5)
        self._dot = self.status_dot.create_oval(1, 1, 9, 9,
                                                fill=RED, outline="")
        self.status_var = tk.StringVar(value="Disconnected")
        ttk.Label(bar, textvariable=self.status_var,
                  style="Status.TLabel").pack(side="left", pady=4)

    def _set_status(self, text, connected=False):
        self.status_var.set(text)
        self.status_dot.itemconfigure(
            self._dot, fill=GREEN if connected else RED)

    def _build_tabs(self):
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(side="top", fill="both", expand=True, padx=8,
                           pady=(2, 4))

        self.trace_frame = self._build_trace_tab()
        self.monitor_frame = self._build_monitor_tab()
        self.lin_frame = self._build_lin_tab()
        self.uds_frame = self._build_uds_tab()

        self.notebook.add(self.trace_frame, text="Trace")
        self.notebook.add(self.monitor_frame, text="Monitor")
        self.notebook.add(self.lin_frame, text="LIN Schedule Table")
        self.notebook.add(self.uds_frame, text="UDS / Diagnostics")

    def _build_trace_tab(self):
        frame = ttk.Frame(self.notebook, padding=(0, 4, 0, 0))
        columns = ("time", "id", "dlc", "data")
        tree = ttk.Treeview(frame, columns=columns, show="tree headings",
                            selectmode="extended")
        tree.heading("#0", text="Type")
        tree.column("#0", width=60, stretch=False, anchor="center")
        tree.heading("time", text="Time (ms)")
        tree.column("time", width=100, stretch=False, anchor="w")
        tree.heading("id", text="Id")
        tree.column("id", width=90, stretch=False, anchor="e")
        tree.heading("dlc", text="DLC")
        tree.column("dlc", width=40, stretch=False, anchor="center")
        tree.heading("data", text="Data")
        tree.column("data", width=370, anchor="w")

        # per-row colours by message type
        tree.tag_configure("error", foreground=RED)
        tree.tag_configure("info", foreground=MUTED)
        tree.tag_configure("io", foreground=GREEN)

        vsb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        tree.bind("<Button-3>", self._on_trace_context_menu)
        self.log_tree = tree
        return frame

    def _build_monitor_tab(self):
        frame = ttk.Frame(self.notebook, padding=(0, 4, 0, 0))

        # header (kept aligned with the monospace text rows below)
        header = tk.Label(frame, font=MONO_FONT, text=self._monitor_header_text(),
                          anchor="w", background=FIELD, foreground=ACCENT,
                          padx=8, pady=4)
        header.pack(side="top", fill="x", padx=0, pady=(0, 0))

        body = ttk.Frame(frame)
        body.pack(side="top", fill="both", expand=True)

        text = tk.Text(body, font=MONO_FONT, wrap="none",
                       state="disabled", height=10, cursor="arrow",
                       background=SURFACE, foreground=TEXT,
                       relief="flat", borderwidth=0, padx=8, pady=6,
                       highlightthickness=0, spacing1=2, spacing3=2)
        vsb = ttk.Scrollbar(body, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=vsb.set)
        text.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        # one foreground colour per heat level: green (constant) -> red (busy)
        for level in range(HEAT_LEVELS + 1):
            t = level / HEAT_LEVELS
            rgb = tuple(int(round(HEAT_LOW[i] + (HEAT_HIGH[i] - HEAT_LOW[i]) * t))
                        for i in range(3))
            text.tag_configure("heat%d" % level, foreground="#%02x%02x%02x" % rgb)

        self.monitor_text = text
        return frame

    @staticmethod
    def _monitor_header_text():
        # widths must match the format string used in _render_monitor()
        return "    %-8s %-8s %-10s %-4s %s" % (
            "Period", "Count", "Id", "DLC", "Data")

    def _build_lin_tab(self):
        frame = ttk.Frame(self.notebook, padding=(8, 6))

        ttk.Label(frame, text="Configure up to 15 LIN schedule slots, then "
                  "send the table to the adapter.",
                  style="Caption.TLabel").grid(row=0, column=0, columnspan=8,
                                               sticky="w", pady=(0, 4))

        grid = ttk.Frame(frame)
        grid.grid(row=1, column=0, columnspan=8, sticky="w")

        self.lin_id_entries = []
        self.lin_data_entries = []
        self.lin_len_entries = []
        self.lin_frame_vars = []

        # per-column-group headers
        for col in range((LIN_ROW_COUNT + ROWS_PER_COLUMN - 1) // ROWS_PER_COLUMN):
            base = col * 4
            self._caption(grid, "ID", base + 0)
            self._caption(grid, "Data (hex)", base + 1)
            self._caption(grid, "Len", base + 2)
            self._caption(grid, "Data fr.", base + 3)

        for i in range(LIN_ROW_COUNT):
            col = i // ROWS_PER_COLUMN
            sub = (i % ROWS_PER_COLUMN) + 1
            base = col * 4

            id_entry = tk.Entry(grid, width=4)
            self._style_entry(id_entry, MONO_FONT)
            id_entry.grid(row=sub, column=base + 0, padx=(10, 1), pady=2, ipady=1)

            data_entry = tk.Entry(grid, width=16)
            self._style_entry(data_entry, MONO_FONT)
            data_entry.grid(row=sub, column=base + 1, padx=1, pady=2, ipady=1)
            data_entry.configure(state="disabled")

            len_entry = tk.Entry(grid, width=4)
            self._style_entry(len_entry, MONO_FONT)
            len_entry.grid(row=sub, column=base + 2, padx=1, pady=2, ipady=1)

            frame_var = tk.BooleanVar(value=False)
            cb = ttk.Checkbutton(grid, variable=frame_var)
            cb.grid(row=sub, column=base + 3, padx=(2, 14), pady=2)

            self.lin_id_entries.append(id_entry)
            self.lin_data_entries.append(data_entry)
            self.lin_len_entries.append(len_entry)
            self.lin_frame_vars.append(frame_var)

            # auto length update when data changes
            data_entry.bind("<KeyRelease>",
                            lambda e, idx=i: self._lin_text_len_update(idx))
            # any edit to an id / length cell flags the table as dirty
            id_entry.bind("<KeyRelease>", lambda e: self._lin_mark_dirty())
            len_entry.bind("<KeyRelease>", lambda e: self._lin_mark_dirty())
            # frame type toggle enables data / disables len and vice versa
            cb.configure(command=lambda idx=i: self._lin_frame_type_toggle(idx))

        # pending auto-send timer handle for the debounce (None = idle)
        self._lin_dirty_after_id = None

        btns = ttk.Frame(frame)
        btns.grid(row=2, column=0, columnspan=8, sticky="e", pady=(10, 0))
        self.scan_button = ttk.Button(btns, text="SCAN",
                                      command=self._on_scan)
        self.scan_button.pack(side="left", padx=4)
        self.lin_autosend_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(btns, text="Auto-send on edit",
                        variable=self.lin_autosend_var,
                        command=self._lin_autosend_toggle).pack(side="left",
                                                                padx=(0, 12))
        ttk.Button(btns, text="Clear Table",
                   command=self._on_lin_clear_table).pack(side="left", padx=4)
        self.lin_send_button = ttk.Button(btns, text="Send Table",
                                          style="Accent.TButton",
                                          command=self._on_lin_send_table)
        self.lin_send_button.pack(side="left", padx=4)
        return frame

    def _build_uds_tab(self):
        frame = ttk.Frame(self.notebook, padding=(8, 6))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(4, weight=1)

        ttk.Label(frame, text="UDS (ISO 14229) over LIN transport. Connect in "
                  "MONITOR mode first — requests go out on 0x3C and "
                  "responses are polled on 0x3D.",
                  style="Caption.TLabel").grid(row=0, column=0, sticky="w",
                                               pady=(0, 6))

        # --- config row: NAD / P2* / DID ---
        cfg = ttk.Frame(frame)
        cfg.grid(row=1, column=0, sticky="w")
        self._inline_label(cfg, "NAD (hex)")
        self.uds_nad_var = tk.StringVar(value="01")
        e = tk.Entry(cfg, width=4, textvariable=self.uds_nad_var)
        self._style_entry(e, MONO_FONT)
        e.pack(side="left", padx=(0, 12), ipady=1)

        self._inline_label(cfg, "P2* (ms)")
        self.uds_p2_var = tk.StringVar(value="2000")
        e = tk.Entry(cfg, width=6, textvariable=self.uds_p2_var)
        self._style_entry(e, MONO_FONT)
        e.pack(side="left", padx=(0, 12), ipady=1)

        self._inline_label(cfg, "DID (hex)")
        self.uds_did_var = tk.StringVar(value="F190")
        e = tk.Entry(cfg, width=6, textvariable=self.uds_did_var)
        self._style_entry(e, MONO_FONT)
        e.pack(side="left", ipady=1)

        # --- preset buttons (fill the request field) ---
        presets = ttk.Frame(frame)
        presets.grid(row=2, column=0, sticky="w", pady=(6, 0))

        def mkbtn(text, cmd):
            ttk.Button(presets, text=text, command=cmd).pack(side="left", padx=2)

        mkbtn("Default Session", lambda: self._uds_fill("10 01"))
        mkbtn("Extended Session", lambda: self._uds_fill("10 03"))
        mkbtn("Tester Present", lambda: self._uds_fill("3E 00"))
        mkbtn("ECU Reset", lambda: self._uds_fill("11 01"))
        mkbtn("Read DID", lambda: self._uds_fill("22 " + self.uds_did_var.get()))
        mkbtn("Clear DTC", lambda: self._uds_fill("14 FF FF FF"))
        mkbtn("Read DTC", lambda: self._uds_fill("19 02 FF"))

        # --- request row ---
        reqrow = ttk.Frame(frame)
        reqrow.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        self._inline_label(reqrow, "Request (hex)")
        self.uds_req_var = tk.StringVar(value="22 F1 90")
        e = tk.Entry(reqrow, textvariable=self.uds_req_var)
        self._style_entry(e, MONO_FONT)
        e.pack(side="left", fill="x", expand=True, padx=(0, 8), ipady=2)
        e.bind("<Return>", lambda ev: self._on_uds_send())
        self.uds_send_button = ttk.Button(reqrow, text="Send Request",
                                          style="Accent.TButton",
                                          command=self._on_uds_send)
        self.uds_send_button.pack(side="left")

        # --- response log ---
        body = ttk.Frame(frame)
        body.grid(row=4, column=0, sticky="nsew", pady=(8, 0))
        txt = tk.Text(body, font=MONO_FONT, wrap="none", state="disabled",
                      height=12, background=SURFACE, foreground=TEXT,
                      relief="flat", borderwidth=0, padx=8, pady=6,
                      highlightthickness=0)
        vsb = ttk.Scrollbar(body, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=vsb.set)
        txt.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        txt.tag_configure("tx", foreground=ACCENT)
        txt.tag_configure("rx", foreground=GREEN)
        txt.tag_configure("err", foreground=RED)
        txt.tag_configure("note", foreground=MUTED)
        self.uds_text = txt

        if not lin_tp._HAVE_UDSONCAN:
            self._uds_log("udsoncan is not installed — run "
                          "`pip install udsoncan` to enable diagnostics.", "err")
        return frame

    def _build_send_bar(self):
        bar = ttk.Labelframe(self, text="Compose & Send", padding=(8, 4))
        bar.pack(side="bottom", fill="x", padx=8, pady=(0, 6))

        # --- fields row : id / len / data0..7 / No data ---
        fields = ttk.Frame(bar)
        fields.pack(fill="x")

        self._caption(fields, "ID (hex)", 0)
        self.msg_id_var = tk.StringVar(value="001")
        self.msg_id_entry = tk.Entry(fields, width=9, textvariable=self.msg_id_var)
        self._style_entry(self.msg_id_entry, MONO_FONT)
        self.msg_id_entry.grid(row=1, column=0, padx=(0, 12), sticky="w", ipady=1)
        self.msg_id_var.trace_add("write", lambda *a: self._msg_fields_to_string())

        self._caption(fields, "Len", 1)
        self.msg_length_var = tk.IntVar(value=0)
        self.msg_length = tk.Spinbox(fields, from_=0, to=8, width=3,
                                     textvariable=self.msg_length_var,
                                     command=self._msg_fields_to_string)
        self._style_spinbox(self.msg_length)
        self.msg_length.grid(row=1, column=1, padx=(0, 12), sticky="w", ipady=1)

        self._caption(fields, "Data bytes (hex)", 2)
        databox = ttk.Frame(fields)
        databox.grid(row=1, column=2, sticky="w")
        self.msg_data_vars = []
        self.msg_data_entries = []
        for i in range(8):
            var = tk.StringVar(value="00")
            entry = tk.Entry(databox, width=3, textvariable=var, state="disabled")
            self._style_entry(entry, MONO_FONT)
            entry.pack(side="left", padx=1, ipady=1)
            var.trace_add("write", lambda *a: self._msg_fields_to_string())
            self.msg_data_vars.append(var)
            self.msg_data_entries.append(entry)

        # "No data" produces a LIN header-only (remote) frame
        self.msg_rtr_var = tk.BooleanVar(value=False)
        self.msg_rtr = ttk.Checkbutton(fields, text="No data",
                                       variable=self.msg_rtr_var,
                                       command=self._msg_fields_to_string)
        self.msg_rtr.grid(row=1, column=3, padx=(14, 0), sticky="w")

        # "Classic checksum" is a LIN option, kept on the fields row (to the
        # right) so the send row below stays compact
        fields.columnconfigure(4, weight=1)
        self.classic_checksum_var = tk.BooleanVar(value=False)
        self.classic_checksum = ttk.Checkbutton(fields, text="Classic checksum",
                                                variable=self.classic_checksum_var)
        self.classic_checksum.grid(row=1, column=4, padx=(14, 0), sticky="e")

        # --- send row : raw message string + options + send ---
        sendrow = ttk.Frame(bar)
        sendrow.pack(fill="x", pady=(6, 0))
        sendrow.columnconfigure(0, weight=1)

        self._caption(sendrow, "SLCAN message", 0)
        self.send_message_var = tk.StringVar(value="t00181122334455667788")
        self.send_message_entry = tk.Entry(sendrow, width=40,
                                           textvariable=self.send_message_var)
        self._style_entry(self.send_message_entry, MONO_FONT)
        self.send_message_entry.grid(row=1, column=0, sticky="w", padx=(0, 12),
                                     ipady=2)
        self.send_message_var.trace_add("write",
                                        lambda *a: self._msg_string_to_fields())
        self.send_message_entry.bind("<Return>", self._on_send_message_enter)

        ctrl = ttk.Frame(sendrow)
        ctrl.grid(row=1, column=1, sticky="e")

        self.cb_repeat_var = tk.BooleanVar(value=False)
        self.cb_repeat = ttk.Checkbutton(ctrl, text="Repeat",
                                         variable=self.cb_repeat_var)
        self.cb_repeat.pack(side="left", padx=(0, 4))

        self.ms_repeat_var = tk.StringVar(value="1000")
        self.ms_repeat = tk.Entry(ctrl, width=6, textvariable=self.ms_repeat_var)
        self._style_entry(self.ms_repeat, MONO_FONT)
        self.ms_repeat.pack(side="left", ipady=1)
        ttk.Label(ctrl, text="ms").pack(side="left", padx=(4, 12))

        self.send_button = ttk.Button(ctrl, text="Send", style="Accent.TButton",
                                      state="disabled", command=self._on_send_button)
        self.send_button.pack(side="left")

    # ==================================================================
    # Serial port list
    # ==================================================================
    def _refresh_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        current = None
        try:
            current = self.serial_port.get()
        except Exception:
            pass
        self.serial_port["values"] = ports
        if current:
            self.serial_port.set(current)

    # ==================================================================
    # Connection
    # ==================================================================
    def _connection_string_to_mode(self, s):
        mapping = {
            "MASTER": OpenMode.ACTIVE,
            "MONITOR": OpenMode.LOOPBACK,
            "SLAVE": OpenMode.LISTENONLY,
        }
        return mapping.get(s, OpenMode.LOOPBACK)

    def _on_connection(self):
        # Connect/disconnect do blocking serial I/O (port open, the v/V/N/W2D00
        # handshake, channel open).  Run it on a worker thread so the window
        # stays responsive instead of appearing frozen for a second or two.
        if self._conn_busy:
            return
        if self.connection_button["text"] == "Disconnect":
            self._begin_disconnect()
        else:
            self._begin_connect()

    # ---- connect -----------------------------------------------------
    def _begin_connect(self):
        port = self.serial_port.get()
        modestr = self.openmode.get()
        try:
            gg = int(self.bit_rate1.get())
        except ValueError as e:
            self.log("Invalid bitrate: %s" % e, MessageType.ERROR)
            return
        gg = 100000 if gg == 19200 else 50000
        mode = self._connection_string_to_mode(modestr)
        classic = self.classic_checksum_var.get()

        self._conn_busy = True
        self.connection_button.configure(state="disabled")
        self._set_status("Connecting to %s…" % port, connected=False)
        threading.Thread(target=self._connect_worker,
                         args=(port, gg, mode, modestr, classic),
                         daemon=True).start()

    def _connect_worker(self, port, gg, mode, modestr, classic):
        try:
            self.usbtin.clearfifo_tx()
            self.usbtin.connect(port)
            self.usbtin.open_can_channel(gg, mode)
            time.sleep(0.05)  # let the channel settle before the first frame
            error = None
        except (USBtinException, ValueError) as e:
            error = e
        # hand the result back to the main loop (never touch Tk from here)
        self.conn_queue.put(("connect", error, modestr, classic))

    def _connect_done(self, error, modestr, classic):
        self._conn_busy = False
        self.connection_button.configure(state="normal")
        if error is not None:
            self.log(str(error), MessageType.ERROR)
            self._set_status("Disconnected", connected=False)
            return

        self.connection_button.configure(text="Disconnect")
        self.bit_rate1.configure(state="disabled")
        self.serial_port.configure(state="disabled")

        if modestr.upper() in ("MASTER", "SLAVE"):
            self._set_config_tab_state("normal")
        else:
            self._set_config_tab_state("disabled")
            self.send_button.configure(state="normal")

        self.openmode.configure(state="disabled")
        self.log("Connected to Device (FW%s/HW%s, SN: %s)" % (
            self.usbtin.get_firmware_version(),
            self.usbtin.get_hardware_version(),
            self.usbtin.get_serial_number()), MessageType.INFO)

        self.classic_checksum.configure(state="disabled")
        self._set_status("Connected — %s @ %s (%s)" % (
            self.serial_port.get(), self.bit_rate1.get(),
            self.openmode.get()), connected=True)

        if self.base_timestamp == 0:
            self.base_timestamp = current_millis()

        # classic / enhanced checksum select (a quick write, safe on the UI thread)
        self.send(CANMessage.from_string("r3100" if classic else "r3000"))

    # ---- disconnect --------------------------------------------------
    def _begin_disconnect(self):
        self.scan_button.configure(text="SCAN")
        self._stop_scan_timer()
        self._conn_busy = True
        self.connection_button.configure(state="disabled")
        self._set_status("Disconnecting…", connected=False)
        threading.Thread(target=self._disconnect_worker, daemon=True).start()

    def _disconnect_worker(self):
        try:
            self.usbtin.close_can_channel()
            self.usbtin.disconnect()
            err = None
        except USBtinException as e:
            err = e
        # hand the result back to the main loop (never touch Tk from here)
        self.conn_queue.put(("disconnect", err))

    def _disconnect_done(self, error):
        self._conn_busy = False
        # If the user is sitting on the LIN Schedule Table tab (which we are
        # about to disable), move them to Trace first.  Otherwise ttk would
        # auto-advance the selection to the next tab (UDS), which is jarring.
        try:
            if self.notebook.select() == str(self.lin_frame):
                self.notebook.select(self.trace_frame)
        except tk.TclError:
            pass
        self._set_config_tab_state("disabled")
        if error is not None:
            self.log(str(error), MessageType.ERROR)
        else:
            self.log("Disconnected", MessageType.INFO)
        self.connection_button.configure(text="Connect", state="normal")
        self.bit_rate1.configure(state="normal")
        self.serial_port.configure(state="normal")
        self.send_button.configure(state="disabled")
        self._stop_send_timer()
        self.send_button.configure(text="Send")
        self.sending_is_active = False
        self.classic_checksum.configure(state="normal")
        self.openmode.configure(state="readonly")
        self._set_status("Disconnected", connected=False)

    def _set_config_tab_state(self, state):
        """Enable/disable the LIN Schedule Table tab."""
        try:
            self.notebook.tab(2, state=state)
        except tk.TclError:
            pass

    # ==================================================================
    # Sending
    # ==================================================================
    def _on_send_button(self):
        if self.cb_repeat_var.get():
            if not self.sending_is_active:
                try:
                    interval = int(self.ms_repeat_var.get())
                    self._start_send_timer(interval)
                    self.send_button.configure(text="Stop")
                    self.sending_is_active = True
                except ValueError:
                    self._stop_send_timer()
                    self.send_button.configure(text="Send")
            else:
                self.sending_is_active = False
                self._stop_send_timer()
                self.send_button.configure(text="Send")
        else:
            self.send_current()

    def _on_send_message_enter(self, event=None):
        if str(self.send_button["state"]) != "disabled":
            if self.sending_is_active:
                self.send_current()

    def _start_send_timer(self, interval_ms):
        self._stop_send_timer()

        def tick():
            self.send_current()
            self._send_after_id = self.after(interval_ms, tick)

        self._send_after_id = self.after(interval_ms, tick)

    def _stop_send_timer(self):
        if self._send_after_id is not None:
            self.after_cancel(self._send_after_id)
            self._send_after_id = None

    def _send_sequence(self, commands, gap_ms, index=0, on_done=None):
        """Send a list of SLCAN command strings spaced ``gap_ms`` apart.

        Each frame is sent on the Tk thread and the next one is scheduled with
        ``after`` instead of ``time.sleep`` so the event loop keeps running and
        the UI never freezes while a multi-frame burst (LIN table / SCAN) goes
        out on the wire.
        """
        if index >= len(commands):
            if on_done is not None:
                on_done()
            return
        try:
            self.send(CANMessage.from_string(commands[index]))
        except (ValueError, IndexError):
            pass
        self.after(gap_ms,
                   lambda: self._send_sequence(commands, gap_ms, index + 1, on_done))

    def send_current(self):
        """Send the message currently in the message string field."""
        try:
            canmsg = CANMessage.from_string(self.send_message_var.get())
        except (ValueError, IndexError):
            self.log("Invalid message string", MessageType.ERROR)
            return
        self.send(canmsg)
        if self.filelogging:
            if self.fileloggingCSV:
                s = "T" if canmsg.is_extended() else "t"
                self._file_println("%d,%s,%d,%s" % (
                    current_millis(), s, canmsg.get_id(),
                    bytes_to_hex(canmsg.get_data())))
            else:
                self._file_println("%d: T %s" % (
                    current_millis(), self.send_message_var.get()))

    # echoes older than this (ms) are no longer treated as our own transmission
    _ECHO_WINDOW_MS = 200

    def send(self, canmsg):
        """Send a CANMessage and log it as an outgoing frame."""
        self.log_message(LogMessage(canmsg, None, MessageType.OUT,
                                    current_millis() - self.base_timestamp))
        # arm self-echo suppression: on a single-wire LIN bus the adapter
        # reflects our transmission back, either as a byte-identical copy or as
        # a header-only (empty, same-id) frame.
        self._pending_echoes.append((str(canmsg), canmsg.get_id(),
                                     current_millis()))
        try:
            self.usbtin.send(canmsg)
        except USBtinException as e:
            self.log(str(e), MessageType.ERROR)

    def _is_self_echo(self, canmsg):
        """True if ``canmsg`` is the loopback echo of a frame we just sent.

        Within the echo window, consumes the first pending frame that either
        matches byte-for-byte (full data reflection) or shares the id while the
        received frame is empty (header-only reflection).  A genuine slave
        response carries data on its id, so it is never swallowed.
        """
        now = current_millis()
        sig = str(canmsg)
        rid = canmsg.get_id()
        empty = len(canmsg.get_data()) == 0
        kept = []
        matched = False
        for esig, eid, ts in self._pending_echoes:
            if now - ts > self._ECHO_WINDOW_MS:
                continue  # expired
            if not matched and (esig == sig or (empty and eid == rid)):
                matched = True
                continue  # consume this echo
            kept.append((esig, eid, ts))
        self._pending_echoes = kept
        return matched

    # ==================================================================
    # Receiving (reader thread -> queue -> main loop)
    # ==================================================================
    def _on_can_message_threadsafe(self, canmsg):
        self.rx_queue.put(canmsg)

    def _drain_rx_queue(self):
        try:
            while True:
                canmsg = self.rx_queue.get_nowait()
                self._receive_can_message(canmsg)
        except queue.Empty:
            pass
        try:
            while True:
                self._uds_handle_result(self.uds_queue.get_nowait())
        except queue.Empty:
            pass
        try:
            while True:
                item = self.conn_queue.get_nowait()
                if item[0] == "connect":
                    self._connect_done(item[1], item[2], item[3])
                else:  # "disconnect"
                    self._disconnect_done(item[1])
        except queue.Empty:
            pass
        # The monitor repaint is expensive (full clear + per-nibble re-tag).
        # Coalesce it to ~6 fps so a flood of frames can't starve the rest of
        # the UI; the RX queue itself is still drained every 20 ms above.
        if self._monitor_dirty:
            now = time.time()
            if now - self._last_monitor_render >= _MONITOR_RENDER_INTERVAL:
                self._render_monitor()
                self._last_monitor_render = now
        self.after(20, self._drain_rx_queue)

    def _receive_can_message(self, canmsg):
        # the adapter loops our own transmissions back on a single-wire bus;
        # drop that echo so a Send shows one OUT frame, not OUT + IN
        if self._is_self_echo(canmsg):
            return
        self.log_message(LogMessage(canmsg, None, MessageType.IN,
                                    current_millis() - self.base_timestamp))
        if self.filelogging:
            if self.fileloggingCSV:
                s = "R" if canmsg.is_extended() else "r"
                self._file_println("%d,%s,%d,%s" % (
                    current_millis(), s, canmsg.get_id(),
                    bytes_to_hex(canmsg.get_data())))
            else:
                self._file_println("%d: R %s" % (current_millis(), str(canmsg)))

        # mirror received payloads back into the matching LIN schedule row
        id_hex = format(canmsg.get_id(), "x")
        for i in range(LIN_ROW_COUNT):
            if self.lin_id_entries[i].get() == id_hex:
                if not self.lin_frame_vars[i].get():
                    self._set_entry_text(self.lin_data_entries[i],
                                        bytes_to_hex(canmsg.get_data()))
                    break

    # ==================================================================
    # Logging (trace + monitor + file)
    # ==================================================================
    def log_message(self, message):
        self.trace_messages.append(message)
        self._append_trace_row(message)
        if self.follow_var.get():
            children = self.log_tree.get_children()
            if children:
                self.log_tree.see(children[-1])

        if message.type in (MessageType.OUT, MessageType.IN):
            self._monitor_add(message)

        if len(self.trace_messages) > MAX_TRACE_ROWS:
            self._clear_log_tables(reset_timestamp=False)

    def log(self, msg, type):
        message = LogMessage(None, msg, type,
                             current_millis() - self.base_timestamp)
        self.trace_messages.append(message)
        self._append_trace_row(message)
        if self.follow_var.get():
            children = self.log_tree.get_children()
            if children:
                self.log_tree.see(children[-1])

    _ROW_TAG = {MessageType.ERROR: "error", MessageType.INFO: "info",
                MessageType.IN: "io", MessageType.OUT: "io"}

    def _append_trace_row(self, message):
        icon = self.icons[message.get_type()]
        canmsg = message.get_canmsg()
        if canmsg is None:
            values = ("", "", "", message.get_message() or "")
        else:
            values = (
                message.get_timestamp(),
                format_id(canmsg, self.hex_display),
                format_dlc(canmsg),
                format_data(canmsg, self.hex_display),
            )
        self.log_tree.insert("", "end", image=icon, values=values,
                             tags=(self._ROW_TAG.get(message.get_type(), "io"),))

    def _monitor_add(self, logmessage):
        # fold into the aggregate model; the (coalesced) re-render happens in
        # the RX drain cycle so bursts of frames don't repaint per frame.
        self.monitor_model.add(logmessage)
        self._monitor_dirty = True

    @staticmethod
    def _heat_level(h):
        if h <= 0.0:
            return 0
        if h >= 1.0:
            return HEAT_LEVELS
        return int(round(h * HEAT_LEVELS))

    def _render_monitor(self):
        """Repaint the monitor view, colouring each data nibble by its heat."""
        txt = self.monitor_text
        txt.configure(state="normal")
        txt.delete("1.0", "end")
        for key in self.monitor_model.sorted_keys():
            mm = self.monitor_model.messages[key]
            logm = mm.get_last_log_message()
            canmsg = logm.get_canmsg()

            txt.image_create("end", image=self.icons[logm.get_type()])
            txt.insert("end", " %-8s %-8s %-10s %-4s " % (
                mm.get_period(), mm.get_count(),
                format_id(canmsg, self.hex_display), format_dlc(canmsg)))

            if canmsg.is_rtr():
                txt.insert("end", "Remote Transmission Request")
            elif self.hex_display:
                # colour every hex nibble individually by its change frequency
                heat = mm.get_heat()
                for bi, b in enumerate(canmsg.get_data()):
                    for ni, ch in enumerate("%02x" % b):
                        idx = bi * 2 + ni
                        h = heat[idx] if idx < len(heat) else 0.0
                        txt.insert("end", ch, "heat%d" % self._heat_level(h))
                    txt.insert("end", " ")
            else:
                # DEC view: nibbles aren't shown, so render plain
                txt.insert("end", format_data(canmsg, self.hex_display))
            txt.insert("end", "\n")
        txt.configure(state="disabled")
        self._monitor_dirty = False

    def _clear_log_tables(self, reset_timestamp):
        self.trace_messages.clear()
        self.log_tree.delete(*self.log_tree.get_children())
        if reset_timestamp:
            self.base_timestamp = current_millis()
        self.monitor_model.clear()
        self._render_monitor()

    def _rerender_tables(self):
        """Re-render trace + monitor after a HEX/DEC change."""
        self.log_tree.delete(*self.log_tree.get_children())
        for message in self.trace_messages:
            self._append_trace_row(message)
        if self.follow_var.get():
            children = self.log_tree.get_children()
            if children:
                self.log_tree.see(children[-1])

        self._render_monitor()

    # ==================================================================
    # Toolbar actions
    # ==================================================================
    def _on_clear(self):
        self._clear_log_tables(reset_timestamp=True)

    def _on_toggle_format(self):
        if self.hex_display:
            self.hex_display = False
            self.format_button.configure(text="DEC")
        else:
            self.hex_display = True
            self.format_button.configure(text="HEX")
        self._rerender_tables()

    def _on_toggle_file_format(self):
        if not self.fileloggingCSV:
            self.fileloggingCSV = True
            self.file_format_button.configure(text="CSV")
        else:
            self.fileloggingCSV = False
            self.file_format_button.configure(text="SLCAN")

    def _on_log_to_file(self):
        if self.filelogging:
            self._file_close()
            self.filelogging = False
            self.log_to_file_button.configure(text="LogToFile")
        else:
            filename = "%d.log" % current_millis()
            try:
                self.file_log = open(filename, "w", encoding="utf-8")
            except OSError as e:
                self.log("Can not open log file: %s" % e, MessageType.ERROR)
                return
            self.filelogging = True
            self.log_to_file_button.configure(text="StopLogging")
            self._file_println("-----Log Start %s-----" %
                               datetime.now().strftime("%a %b %d %H:%M:%S %Y"))

    def _file_println(self, line):
        if self.file_log is not None:
            self.file_log.write(line + "\n")
            self.file_log.flush()

    def _file_close(self):
        if self.file_log is not None:
            try:
                self.file_log.close()
            finally:
                self.file_log = None

    # ==================================================================
    # Trace context menu (Resend / Copy)
    # ==================================================================
    def _on_trace_context_menu(self, event):
        row = self.log_tree.identify_row(event.y)
        if row and row not in self.log_tree.selection():
            self.log_tree.selection_set(row)
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Resend", command=self._context_resend)
        menu.add_command(label="Copy", command=self._context_copy)
        menu.tk_popup(event.x_root, event.y_root)

    def _selected_io_messages(self):
        result = []
        children = list(self.log_tree.get_children())
        index_by_iid = {iid: i for i, iid in enumerate(children)}
        for iid in self.log_tree.selection():
            idx = index_by_iid.get(iid)
            if idx is None or idx >= len(self.trace_messages):
                continue
            message = self.trace_messages[idx]
            if message.get_type() in (MessageType.IN, MessageType.OUT):
                result.append(message)
        return result

    def _context_resend(self):
        for message in self._selected_io_messages():
            self.send(message.get_canmsg())

    def _context_copy(self):
        lines = [str(m.get_canmsg()) for m in self._selected_io_messages()]
        if lines:
            self.clipboard_clear()
            self.clipboard_append(os.linesep.join(lines) + os.linesep)

    # ==================================================================
    # LIN schedule table
    # ==================================================================
    def _lin_text_len_update(self, idx):
        text = self.lin_data_entries[idx].get()
        self._set_entry_text(self.lin_len_entries[idx], str((len(text) + 1) // 2))
        self._lin_mark_dirty()

    def _lin_frame_type_toggle(self, idx):
        if self.lin_frame_vars[idx].get():
            self.lin_data_entries[idx].configure(state="normal")
            self.lin_len_entries[idx].configure(state="disabled")
        else:
            self.lin_data_entries[idx].configure(state="disabled")
            self.lin_len_entries[idx].configure(state="normal")
        self._lin_mark_dirty()

    def _lin_mark_dirty(self):
        """Called after any edit to the schedule table.

        With auto-send enabled, debounce 1 s after the last edit and then
        push the table to the adapter automatically.  Otherwise flag the
        Send Table button red so the user knows it needs a manual click.
        """
        if self._lin_dirty_after_id is not None:
            self.after_cancel(self._lin_dirty_after_id)
            self._lin_dirty_after_id = None
        if self.lin_autosend_var.get():
            self._lin_dirty_after_id = self.after(1000, self._lin_autosend_fire)
        else:
            self.lin_send_button.configure(style="Dirty.TButton")

    def _lin_autosend_fire(self):
        self._lin_dirty_after_id = None
        self._on_lin_send_table()

    def _lin_autosend_toggle(self):
        """React to the auto-send checkbox flipping."""
        if self.lin_autosend_var.get():
            # turned on while edits are still pending — honor them now
            if str(self.lin_send_button.cget("style")) == "Dirty.TButton":
                self._lin_mark_dirty()
        elif self._lin_dirty_after_id is not None:
            # turned off — drop any queued auto-send
            self.after_cancel(self._lin_dirty_after_id)
            self._lin_dirty_after_id = None

    def _on_lin_clear_table(self):
        self.send(CANMessage.from_string("r2FF0"))  # reset table

    def _on_lin_send_table(self):
        # Build the SLCAN command sequence on the UI thread (it reads widgets),
        # then stream it out paced by the event loop instead of blocking the UI
        # with time.sleep between every frame.

        # sending clears the "dirty" state: cancel any pending auto-send and
        # restore the button to its normal accent style
        if self._lin_dirty_after_id is not None:
            self.after_cancel(self._lin_dirty_after_id)
            self._lin_dirty_after_id = None
        self.lin_send_button.configure(style="Accent.TButton")

        rows = 0
        commands = ["r2FF0"]  # reset table
        try:
            for i in range(LIN_ROW_COUNT):
                rows += 1
                id_text = self.lin_id_entries[i].get()
                if len(id_text) > 0:
                    # header type
                    s = "t0" if self.lin_frame_vars[i].get() else "r0"
                    # id (padded to 2 hex digits)
                    if len(id_text) == 1:
                        s += "0" + id_text[0]
                    else:
                        s += id_text[0] + id_text[1]
                    # length / data
                    if self.lin_frame_vars[i].get():
                        byte_number = int(self.lin_len_entries[i].get())
                        s += str(byte_number)
                        s += self.lin_data_entries[i].get()
                        if len(self.lin_data_entries[i].get()) % 2 == 1:
                            s += "0"
                    else:
                        s += self.lin_len_entries[i].get()
                    commands.append(s)

                if self.filelogging:
                    self._file_println("%d: M %s" % (current_millis(), id_text))
        except (ValueError, IndexError):
            self.log("LIN schedule table send failed", MessageType.ERROR)
            return

        commands.append("r1FF0")  # start sending

        def done():
            if rows > 0:
                self.log("%d LIN schedule table send OK" % rows, MessageType.INFO)
            else:
                self.log("LIN schedule table send failed", MessageType.ERROR)

        self._send_sequence(commands, 50, on_done=done)

    # ==================================================================
    # LIN SCAN
    # ==================================================================
    def _on_scan(self):
        self.id_scan = 0
        if self.scan_button["text"] == "SCAN":
            self.scan_button.configure(text="SCAN OFF")
            self._start_scan_timer()
        else:
            self.scan_button.configure(text="SCAN")
            self._stop_scan_timer()

    def _start_scan_timer(self):
        self._stop_scan_timer()

        def tick():
            # reset table, probe one id, enable sending — paced by the event
            # loop (no blocking sleeps) so the rest of the UI stays responsive.
            self._send_sequence(["r2FF0",
                                 "r0" + "%02x" % self.id_scan + "%01x" % 8,
                                 "r1FF0"], 50)
            if self.id_scan >= 0x3F:
                self.id_scan = 0
                self._stop_scan_timer()
                self.scan_button.configure(text="SCAN")
                return
            self.id_scan += 1
            self._scan_after_id = self.after(1000, tick)

        self._scan_after_id = self.after(1000, tick)

    def _stop_scan_timer(self):
        if self._scan_after_id is not None:
            self.after_cancel(self._scan_after_id)
            self._scan_after_id = None

    # ==================================================================
    # UDS / Diagnostics (LIN transport via lin_tp.LinTpConnection)
    # ==================================================================
    def _uds_fill(self, text):
        self.uds_req_var.set(text)

    def _uds_log(self, line, tag="note"):
        self.uds_text.configure(state="normal")
        self.uds_text.insert("end", line + "\n", tag)
        self.uds_text.see("end")
        self.uds_text.configure(state="disabled")

    @staticmethod
    def _parse_hex_bytes(text):
        s = text.replace(" ", "").replace(",", "").strip()
        if not s or len(s) % 2 != 0:
            return None
        try:
            return bytes.fromhex(s)
        except ValueError:
            return None

    def _on_uds_send(self):
        if self._uds_busy:
            return
        if not lin_tp._HAVE_UDSONCAN:
            self._uds_log("udsoncan is not installed.", "err")
            return
        if self.connection_button["text"] != "Disconnect":
            self._uds_log("Not connected — connect in MONITOR mode first.", "err")
            return
        if self.openmode.get() != "MONITOR":
            self._uds_log("UDS needs MONITOR mode (reconnect with Mode=MONITOR).",
                          "err")
            return
        try:
            nad = int(self.uds_nad_var.get(), 16) & 0xFF
        except ValueError:
            self._uds_log("Invalid NAD.", "err")
            return
        req = self._parse_hex_bytes(self.uds_req_var.get())
        if not req:
            self._uds_log("Invalid / empty request bytes.", "err")
            return
        try:
            timeout = float(self.uds_p2_var.get()) / 1000.0
        except ValueError:
            timeout = lin_tp.DEFAULT_P2_STAR

        self._uds_busy = True
        self.uds_send_button.configure(state="disabled", text="…")
        self._uds_log(">> [NAD %02X] %s" % (nad, _hexspace(req)), "tx")
        threading.Thread(target=self._uds_worker, args=(nad, req, timeout),
                         daemon=True).start()

    def _uds_worker(self, nad, req, timeout):
        """Runs off the Tk thread: open a LIN-TP connection and exchange one
        request/response, posting the result back through ``uds_queue``."""
        result = {}
        conn = lin_tp.LinTpConnection(self.usbtin, nad, p2_star_timeout=timeout)
        try:
            conn.open()
            conn.empty_rxqueue()
            conn.send(req)
            pending = 0
            resp = conn.wait_frame(timeout=timeout, exception=True)
            # transparently keep polling while the slave answers NRC 0x78
            while (resp and len(resp) >= 3 and resp[0] == 0x7F
                   and resp[2] == 0x78):
                pending += 1
                resp = conn.wait_frame(timeout=timeout, exception=True)
            result["resp"] = bytes(resp) if resp else b""
            result["nad"] = conn.last_response_nad
            result["pending"] = pending
        except Exception as e:  # TimeoutException, USBtinException, ...
            result["error"] = "%s: %s" % (type(e).__name__, e)
        finally:
            try:
                conn.close()
            except Exception:
                pass
        self.uds_queue.put(result)

    def _uds_handle_result(self, result):
        self._uds_busy = False
        self.uds_send_button.configure(state="normal", text="Send Request")
        if "error" in result:
            self._uds_log("!! " + result["error"], "err")
            return
        if result.get("pending"):
            self._uds_log("   (NRC 0x78 response pending x%d)" %
                          result["pending"], "note")
        resp = result["resp"]
        nad = result.get("nad")
        prefix = "<< [NAD %02X] " % nad if nad is not None else "<< "
        if not resp:
            self._uds_log(prefix + "(empty response / timeout)", "err")
            return
        self._uds_log(prefix + _hexspace(resp), "rx")
        if resp[0] == 0x7F and len(resp) >= 3:
            self._uds_log("   NegativeResponse: service 0x%02X, NRC 0x%02X (%s)"
                          % (resp[1], resp[2],
                             _NRC_NAMES.get(resp[2], "unknown")), "err")
        else:
            self._uds_log("   Positive response (SID 0x%02X)" % resp[0], "note")

    # ==================================================================
    # Message string <-> input fields
    # ==================================================================
    def _msg_fields_to_string(self):
        if self.disable_msg_update:
            return
        self.disable_msg_update = True
        try:
            try:
                id_ = int(self.msg_id_var.get(), 16)
            except ValueError:
                id_ = 0

            try:
                length = int(self.msg_length_var.get())
            except (ValueError, tk.TclError):
                length = 0

            data = bytearray()
            for i in range(length):
                try:
                    data.append(int(self.msg_data_vars[i].get(), 16) & 0xFF)
                except ValueError:
                    data.append(0)

            canmsg = CANMessage(id_, bytes(data), False, self.msg_rtr_var.get())
            self.send_message_var.set(str(canmsg))

            for i in range(8):
                enabled = i < length and not self.msg_rtr_var.get()
                self.msg_data_entries[i].configure(
                    state="normal" if enabled else "disabled")
        finally:
            self.disable_msg_update = False

    def _msg_string_to_fields(self):
        if self.disable_msg_update:
            return
        self.disable_msg_update = True
        try:
            try:
                canmsg = CANMessage.from_string(self.send_message_var.get())
            except (ValueError, IndexError):
                return
            data = canmsg.get_data()

            self.msg_id_var.set("%03x" % canmsg.get_id())
            self.msg_length_var.set(len(data))
            self.msg_rtr_var.set(canmsg.is_rtr())

            for i in range(8):
                if i < len(data):
                    self.msg_data_vars[i].set("%02x" % data[i])
                enabled = i < len(data) and not canmsg.is_rtr()
                self.msg_data_entries[i].configure(
                    state="normal" if enabled else "disabled")
        finally:
            self.disable_msg_update = False

    # ==================================================================
    # Helpers
    # ==================================================================
    @staticmethod
    def _set_entry_text(entry, text):
        was_disabled = str(entry["state"]) == "disabled"
        if was_disabled:
            entry.configure(state="normal")
        entry.delete(0, "end")
        entry.insert(0, text)
        if was_disabled:
            entry.configure(state="disabled")

    def _on_close(self):
        self._stop_scan_timer()
        self._stop_send_timer()
        try:
            self.usbtin.disconnect()
        except USBtinException:
            pass
        self._file_close()
        self.destroy()


def main():
    app = UCCBViewer()
    app.mainloop()


if __name__ == "__main__":
    main()
