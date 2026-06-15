"""
Full UDS-over-LIN round-trip across TWO different libraries:

  * SLAVE  node -- ucanlintools (C:\\Users\\LJ\\LinUSBConverter\\python_lib) in
                   real slave mode, with a small UDS engine on top.  It receives
                   the master request on 0x3C and publishes the answer on 0x3D.
  * MASTER node -- uCCBViewer's LinTpConnection + udsoncan client, issuing real
                   UDS services (DiagnosticSessionControl, ReadDataByIdentifier).

Both nodes run at LIN S1 (19200).

    python.exe test_uds_roundtrip.py [MASTER_PORT] [SLAVE_PORT]
    e.g.  python.exe test_uds_roundtrip.py COM33 COM3
"""

import sys
import time

sys.path.insert(0, r"C:\Users\LJ\LinUSBConverter\python_lib")
from ucanlintools.LUC import LUC

from uccbviewer.usbtin import USBtin, OpenMode
from uccbviewer.lin_tp import (LinTpConnection, create_client,
                               segment_request, _Reassembler,
                               LIN_MASTER_REQUEST_ID, LIN_SLAVE_RESPONSE_ID)
import udsoncan

MASTER_PORT = sys.argv[1] if len(sys.argv) > 1 else "COM33"
SLAVE_PORT = sys.argv[2] if len(sys.argv) > 2 else "COM3"
NAD = 0x01

DID_DATA = {0xF190: b"WVW", 0xF195: bytes([0x01, 0x00])}


def hx(b):
    return " ".join("%02X" % x for x in bytes(b))


# ---------------------------------------------------------------------------
# UDS engine: request payload bytes -> response payload bytes (or None)
# ---------------------------------------------------------------------------
def uds_compute(req):
    sid = req[0]
    if sid == 0x10:                       # DiagnosticSessionControl
        if req[1] & 0x80:
            return None
        return bytes([0x50, req[1], 0x00, 0x32, 0x01, 0xF4])
    if sid == 0x11:                       # ECUReset
        return None if req[1] & 0x80 else bytes([0x51, req[1]])
    if sid == 0x3E:                       # TesterPresent
        return None if req[1] & 0x80 else bytes([0x7E, req[1]])
    if sid == 0x22 and len(req) >= 3:     # ReadDataByIdentifier
        did = (req[1] << 8) | req[2]
        data = DID_DATA.get(did)
        if data is None:
            return bytes([0x7F, 0x22, 0x31])
        return bytes([0x62, req[1], req[2]]) + data
    return bytes([0x7F, sid, 0x11])       # serviceNotSupported


# ---------------------------------------------------------------------------
# ucanlintools slave wiring
# ---------------------------------------------------------------------------
class UdsSlave:
    def __init__(self, lin, nad):
        self.lin = lin
        self.nad = nad
        self._reasm = _Reassembler()

    def on_frame(self, f):
        if f.id != LIN_MASTER_REQUEST_ID:
            return
        data = bytes(f.data)
        if not data or len(data) < 2 or data[1] == 0x00:
            return
        try:
            payload = self._reasm.feed(data)
        except ValueError:
            self._reasm = _Reassembler()
            return
        if payload is None:
            return
        nad = data[0]
        self._reasm = _Reassembler()
        if nad not in (self.nad, 0x7E, 0x7F):
            print("  [slave] req for NAD %02X ignored (%s)" % (nad, hx(payload)))
            return
        print("  [slave] <- req  %s" % hx(payload))
        resp = uds_compute(payload)
        if resp is None:
            print("  [slave]    (suppressed, no response)")
            return
        frame = segment_request(self.nad, resp)[0]
        print("  [slave] -> resp %s   (0x3D = %s)" % (hx(resp), hx(frame)))
        # update the 0x3D answer for the next master poll (fire-and-forget)
        self.lin.addTransmitFrameToTable(LIN_SLAVE_RESPONSE_ID, frame)


def main():
    print("=== SLAVE (ucanlintools, UDS engine) on %s, NAD 0x%02X ===\n"
          % (SLAVE_PORT, NAD))
    slave_lin = LUC(SLAVE_PORT, baudrate=19200)
    slave = UdsSlave(slave_lin, NAD)
    slave_lin.set_frame_rx_handler(slave.on_frame)
    slave_lin.close()
    slave_lin.highSpeed()                      # S1 == 19200
    ok = slave_lin.openAsSlave()
    # receive the master request, publish a (initially empty) response slot
    slave_lin.addReceptionFrameToTable(LIN_MASTER_REQUEST_ID, 8)
    slave_lin.addTransmitFrameToTable(LIN_SLAVE_RESPONSE_ID,
                                      bytes([0x00] * 8))   # "not ready" sentinel
    slave_lin.enable()
    print("slave openAsSlave ok=%s, listening on 0x3C, answering 0x3D\n" % ok)
    time.sleep(0.4)

    print("=== MASTER (uCCBViewer + udsoncan) on %s ===" % MASTER_PORT)
    tin = USBtin()
    tin.connect(MASTER_PORT)
    try:
        tin._serial.timeout = 0.1
    except AttributeError:
        pass
    print("connected: FW%s HW%s SN %s"
          % (tin.get_firmware_version(), tin.get_hardware_version(),
             tin.get_serial_number()))
    tin.open_can_channel(20000, OpenMode.LOOPBACK)   # S1, LIN MONITOR

    results = []
    with create_client(tin, nad=NAD) as client:
        # --- DiagnosticSessionControl (the "session switch") ---
        for attempt in range(1, 4):
            try:
                print("\n>>> change_session(0x03)  attempt %d" % attempt)
                r = client.change_session(0x03)
                print("    OK  positive=%s" % r.positive)
                results.append(("change_session", True))
                break
            except Exception as e:
                print("    %s: %s" % (type(e).__name__, e))
                if attempt == 3:
                    results.append(("change_session", False))
            time.sleep(0.5)

        # --- ReadDataByIdentifier 0xF190 (raw, via the transport under test) ---
        for attempt in range(1, 4):
            try:
                print("\n>>> ReadDataByIdentifier(0xF190) raw  attempt %d" % attempt)
                client.conn.empty_rxqueue()
                client.conn.specific_send(bytes([0x22, 0xF1, 0x90]))
                resp = client.conn.specific_wait_frame(timeout=2.0)
                print("    OK  response=%s" % hx(resp))
                ok = len(resp) >= 3 and resp[0] == 0x62
                results.append(("rdbi_F190", ok))
                break
            except Exception as e:
                print("    %s: %s" % (type(e).__name__, e))
                if attempt == 3:
                    results.append(("rdbi_F190", False))
            time.sleep(0.5)

    print("\n=== RESULT ===")
    for name, ok in results:
        print("  %-16s %s" % (name, "PASS" if ok else "FAIL"))
    overall = all(ok for _, ok in results) and len(results) == 2

    try:
        tin.close_can_channel(); tin.disconnect()
    except Exception:
        pass
    try:
        slave_lin.disable(); slave_lin.deInitSerial()
    except Exception:
        pass

    print("\n%s" % ("ALL PASS: full UDS round-trip works across both libraries."
                    if overall else
                    "Some steps failed -- see log above."))
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
