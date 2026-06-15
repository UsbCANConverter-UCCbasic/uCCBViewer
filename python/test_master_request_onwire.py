"""
On-wire check: does uCCBViewer actually transmit the LIN master request (0x3C)
*with its data bytes* when it issues a UDS request?

Setup (two dongles wired together, same 0483:5740 firmware):
  * MONITOR  node -- ucanlintools (C:\\Users\\LJ\\LinUSBConverter\\python_lib),
                     passive sniffer, prints every frame seen on the bus.
  * MASTER   node -- uCCBViewer's LinTpConnection, sends DiagnosticSessionControl
                     (10 03) exactly as the UDS tab does.

Both run at LIN S1 (19200): ucanlintools highSpeed() and uCCBViewer
open_can_channel(20000) both emit "S1", so the baud matches.

    python.exe test_master_request_onwire.py [MASTER_PORT] [MONITOR_PORT]
    e.g.  python.exe test_master_request_onwire.py COM33 COM3
"""

import sys
import time
import threading

# --- make ucanlintools importable on Windows ---
sys.path.insert(0, r"C:\Users\LJ\LinUSBConverter\python_lib")
from ucanlintools.LUC import LUC

from uccbviewer.usbtin import USBtin, OpenMode
from uccbviewer.lin_tp import LinTpConnection

MASTER_PORT = sys.argv[1] if len(sys.argv) > 1 else "COM33"
MONITOR_PORT = sys.argv[2] if len(sys.argv) > 2 else "COM3"


def hx(b):
    return " ".join("%02X" % x for x in bytes(b))


# captured (timestamp, id, data) seen by the passive monitor
seen = []
seen_lock = threading.Lock()
t0 = time.time()


def on_monitor_frame(f):
    with seen_lock:
        seen.append((time.time() - t0, f.id, bytes(f.data)))
    print("  [monitor] %7.3f  id=0x%02X  data=[%s]"
          % (time.time() - t0, f.id, hx(f.data)))


def main():
    print("=== MONITOR (ucanlintools) on %s ===" % MONITOR_PORT)
    mon = LUC(MONITOR_PORT, baudrate=19200)
    mon.set_frame_rx_handler(on_monitor_frame)
    mon.close()
    mon.highSpeed()            # S1 == 19200
    mon.openAsMonitor()
    mon.enable()
    time.sleep(0.5)

    print("=== MASTER (uCCBViewer LinTpConnection) on %s ===" % MASTER_PORT)
    tin = USBtin()
    tin.connect(MASTER_PORT)
    try:
        tin._serial.timeout = 0.1
    except AttributeError:
        pass
    print("connected: FW%s HW%s SN %s"
          % (tin.get_firmware_version(), tin.get_hardware_version(),
             tin.get_serial_number()))
    # S1 (20000 -> "S1") to match the monitor's 19200, LOOPBACK == LIN MONITOR
    tin.open_can_channel(20000, OpenMode.LOOPBACK)

    conn = LinTpConnection(tin, nad=0x01)
    conn.open()
    time.sleep(0.3)

    req = bytes([0x10, 0x03])     # DiagnosticSessionControl -> extended session
    print("\n>>> sending UDS request on 0x3C: %s "
          "(expect bus frame 01 02 10 03 FF FF FF FF)\n" % hx(req))
    with seen_lock:
        seen.clear()
    conn.specific_send(req)

    # let it settle, then a couple of response polls (no real slave -> timeout)
    time.sleep(0.5)
    try:
        conn.specific_wait_frame(timeout=0.5)
    except Exception as e:
        print("   (no slave response, expected: %s)" % type(e).__name__)

    time.sleep(0.3)

    # --- verdict ---
    print("\n=== RESULT ===")
    with seen_lock:
        frames_3c = [(t, d) for (t, i, d) in seen if i == 0x3C]
    if not frames_3c:
        print("FAIL: monitor saw NO 0x3C frame at all -- master request never "
              "reached the bus (or baud mismatch).")
    else:
        for t, d in frames_3c:
            has_data = any(b != 0xFF for b in d) and len(d) >= 2
            tag = "HAS DATA" if (len(d) >= 4 and d[2:4] == req) else \
                  ("header-only / no data" if len(d) == 0 else "data=[%s]" % hx(d))
            print("0x3C @ %.3f: %s" % (t, tag))
        full = any(len(d) >= 4 and d[0] == 0x01 and d[1] == 0x02 and d[2:4] == req
                   for _, d in frames_3c)
        if full:
            print("PASS: master request WITH data bytes is on the bus.")
        else:
            print("PROBLEM: 0x3C header is on the bus but WITHOUT the UDS data "
                  "payload -> slave can never see the request.")

    # --- cleanup ---
    try:
        conn.close()
        tin.close_can_channel()
        tin.disconnect()
    except Exception:
        pass
    try:
        mon.disable()
        mon.deInitSerial()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
