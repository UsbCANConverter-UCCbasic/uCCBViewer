"""
Minimal LIN diagnostic ECU simulator for testing uCCBViewer's UDS tab.

Run it on ONE dongle; point the viewer at the OTHER dongle, connect in MONITOR
mode, and use the UDS / Diagnostics tab.  The simulator plays a LIN slave that
answers UDS (ISO 14229) requests over the LIN transport (master request frame
0x3C, slave response frame 0x3D).

    python lin_ecu_sim.py [PORT] [NAD]
    e.g.  python lin_ecu_sim.py COM30 01

Why it switches modes
---------------------
The uCCB firmware partitions what the host can do per channel mode:

    MONITOR : sees the tester's 0x3C request, but cannot transmit 0x3D
    SLAVE   : answers the tester's 0x3D poll from a response table (reliably,
              in the same frame slot -- no bus collision), but does NOT hand
              the 0x3C request up to the host

So the simulator reads the request in MONITOR mode, then a control thread flips
the channel to SLAVE, loads the computed answer into the response table, holds
it long enough for the tester to read it (the viewer polls for ~2 s), and flips
back to MONITOR for the next request.  Responses are kept to a single LIN frame
(<= 6 UDS bytes), which is what the slave response table can deliver.

Implemented services (enough to exercise the viewer's buttons)
-------------------------------------------------------------
    0x10 DiagnosticSessionControl   -> 50 <sub> 00 32 01 F4
    0x11 ECUReset                   -> 51 <sub>
    0x3E TesterPresent              -> 7E <sub>
    0x14 ClearDiagnosticInformation -> 54
    0x22 ReadDataByIdentifier       -> 62 <did> <data>   (a few canned DIDs)
    0x19 ReadDTCInformation         -> 59 <sub> FF       (no DTCs stored)
    anything else                   -> 7F <sid> 11 (serviceNotSupported)
    unknown DID                     -> 7F 22 31 (requestOutOfRange)
The suppressPosRsp bit (sub-function bit 0x80) is honoured for 10/11/3E.

Note: responses are single-frame only, so values are short (e.g. DID 0xF190 is
a 3-byte stub, not a full 17-byte VIN).  That keeps the exchange reliable on a
two-dongle bench setup.

The very first request right after the viewer connects may be missed (these
adapters routinely drop the first frame on a freshly opened channel); just send
it again.  Allow ~1 s between requests so the simulator can flip back to MONITOR
to catch the next one.
"""

import os
import queue
import sys
import threading
import time

from uccbviewer.usbtin import USBtin, USBtinException, OpenMode
from uccbviewer.can_message import CANMessage
from uccbviewer.lin_tp import (segment_request, _Reassembler,
                               LIN_MASTER_REQUEST_ID, LIN_SLAVE_RESPONSE_ID)

_DBG = bool(os.environ.get("SIM_DEBUG"))

GG = 100000                     # firmware rate param (matches the viewer)
NRC_SERVICE_NOT_SUPPORTED = 0x11
NRC_REQUEST_OUT_OF_RANGE = 0x31

# Canned ReadDataByIdentifier values (DID -> data, kept <= 3 bytes for SF)
DID_DATA = {
    0xF190: b"WVW",                    # VIN stub (single-frame only)
    0xF187: b"1K0",                    # spare part number stub
    0xF195: bytes([0x01, 0x00]),       # application SW version
    0x0100: bytes([0xDE, 0xAD, 0xBE]),
}


def hx(b):
    return " ".join("%02X" % x for x in bytes(b))


class LinEcuSim:
    # how long to stay in SLAVE mode answering polls after a request
    HOLD = 0.8

    def __init__(self, tin, nad):
        self.tin = tin
        self.nad = nad
        self._req = _Reassembler()
        self._pending = queue.Queue()
        self._running = False
        self._ctrl = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    # A "slave not ready" sentinel (PCI byte 0x00).  The tester's transport
    # skips frames whose PCI length is 0, so when the previous response leaks
    # during the brief SLAVE-reopen window it is ignored instead of being
    # mistaken for the new answer (which would lag every reply by one request).
    _SENTINEL = bytes([0x00, 0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF])

    def start(self):
        self._running = True
        self._prime_table()              # leave a harmless sentinel, not a stale reply
        self._enter_monitor()
        self._ctrl = threading.Thread(target=self._ctrl_loop, daemon=True)
        self._ctrl.start()

    def _prime_table(self):
        # The response table survives close/reopen and even program restarts.
        # Load the sentinel so any leftover real response from a previous run
        # cannot answer the first request.
        try:
            self.tin.open_can_channel(GG, OpenMode.LISTENONLY)
            time.sleep(0.05)
            self._load_slave(self._SENTINEL)
            self.tin.close_can_channel()
            time.sleep(0.05)
        except USBtinException:
            pass

    def _load_slave(self, frame):
        """Replace the SLAVE response-table entry for 0x3D with *frame*."""
        self.tin.send(CANMessage.from_string("r2FF0")); time.sleep(0.03)
        self.tin.send(CANMessage(LIN_SLAVE_RESPONSE_ID, frame)); time.sleep(0.03)
        self.tin.send(CANMessage.from_string("r1FF0")); time.sleep(0.03)

    def stop(self):
        self._running = False
        self._pending.put(None)
        if self._ctrl is not None:
            self._ctrl.join(timeout=3.0)
        try:
            self.tin.close_can_channel()
        except USBtinException:
            pass

    def _enter_monitor(self):
        self.tin.open_can_channel(GG, OpenMode.LOOPBACK)
        time.sleep(0.1)
        self.tin.send(CANMessage.from_string("r3100"))   # classic checksum
        time.sleep(0.05)

    # ------------------------------------------------------------------
    # MONITOR: receive and reassemble the master request (reader thread)
    # ------------------------------------------------------------------
    def on_frame(self, canmsg):
        if canmsg.get_id() != LIN_MASTER_REQUEST_ID:
            return
        data = bytes(canmsg.get_data())
        if not data:
            return
        try:
            payload = self._req.feed(data)
        except ValueError:
            self._req = _Reassembler()
            return
        if payload is not None:
            nad = data[0]
            self._req = _Reassembler()
            self._dispatch_request(nad, payload)

    def _dispatch_request(self, nad, payload):
        addressed = nad in (self.nad, 0x7E, 0x7F)
        print("  >> [NAD %02X] %s%s" % (nad, hx(payload),
                                        "" if addressed else "  (not for me)"))
        if not addressed:
            return
        resp = self._compute(payload)
        if resp is None:
            print("     (no response / suppressed)")
            return
        if len(resp) > 6:
            print("     (response %d bytes > single-frame; truncated)" % len(resp))
            resp = resp[:6]
        print("     << [NAD %02X] %s" % (self.nad, hx(resp)))
        self._pending.put((nad, resp))

    # ------------------------------------------------------------------
    # control thread: deliver the response via a MONITOR->SLAVE->MONITOR flip
    # ------------------------------------------------------------------
    def _ctrl_loop(self):
        while self._running:
            item = self._pending.get()
            if item is None:
                break
            nad, resp = item
            # if several requests piled up, only the most recent matters
            while not self._pending.empty():
                nxt = self._pending.get_nowait()
                if nxt is None:
                    return
                nad, resp = nxt
            self._deliver(nad, resp)

    def _deliver(self, nad, resp):
        frame = segment_request(nad, resp)[0]    # single-frame response
        if _DBG:
            print("     [dbg] deliver via SLAVE: 3D = %s" % hx(frame))
        try:
            # MONITOR -> SLAVE.  The table still holds the sentinel from last
            # time, so the open-window polls answer "not ready" (ignored) until
            # we load the real response below.
            self.tin.close_can_channel()
            self.tin.open_can_channel(GG, OpenMode.LISTENONLY)
            time.sleep(0.03)
            self.tin.send(CANMessage.from_string("r3100")); time.sleep(0.02)
            self._load_slave(frame)              # publish the real response
            time.sleep(self.HOLD)                # tester reads it within this window
            # leave the sentinel behind so the next request's reopen window
            # cannot replay this (now stale) response
            self._load_slave(self._SENTINEL)
            self.tin.close_can_channel()
            self._enter_monitor()
        except USBtinException as e:
            print("     [error] delivery failed: %s" % e)

    # ------------------------------------------------------------------
    # UDS service logic (pure: request bytes -> response bytes or None)
    # ------------------------------------------------------------------
    def _compute(self, req):
        sid = req[0]
        if sid == 0x10:      # DiagnosticSessionControl
            return self._sub(req, bytes([0x50, req[1], 0x00, 0x32, 0x01, 0xF4]))
        if sid == 0x11:      # ECUReset
            return self._sub(req, bytes([0x51, req[1]]))
        if sid == 0x3E:      # TesterPresent
            return self._sub(req, bytes([0x7E, req[1]]))
        if sid == 0x14:      # ClearDiagnosticInformation
            return bytes([0x54])
        if sid == 0x22:      # ReadDataByIdentifier
            return self._read_did(req)
        if sid == 0x19:      # ReadDTCInformation
            sub = req[1] if len(req) >= 2 else 0x02
            return bytes([0x59, sub, 0xFF])   # availability mask, no DTCs stored
        return bytes([0x7F, sid, NRC_SERVICE_NOT_SUPPORTED])

    @staticmethod
    def _sub(req, positive):
        if len(req) >= 2 and (req[1] & 0x80):
            return None                       # suppressPosRsp
        return positive

    def _read_did(self, req):
        if len(req) < 3:
            return bytes([0x7F, 0x22, NRC_REQUEST_OUT_OF_RANGE])
        did = (req[1] << 8) | req[2]
        data = DID_DATA.get(did)
        if data is None:
            return bytes([0x7F, 0x22, NRC_REQUEST_OUT_OF_RANGE])
        return bytes([0x62, req[1], req[2]]) + data


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "COM30"
    nad = int(sys.argv[2], 16) if len(sys.argv) > 2 else 0x01

    tin = USBtin()
    print("LIN ECU simulator -- port %s, NAD 0x%02X" % (port, nad))
    try:
        tin.connect(port)
    except USBtinException as e:
        print("ERROR: %s" % e)
        return 1
    # short serial timeout -> fast reader-thread stop on each mode flip
    try:
        tin._serial.timeout = 0.1
    except AttributeError:
        pass

    print("connected (FW%s/HW%s, SN %s). Waiting for UDS requests... Ctrl+C to stop.\n"
          % (tin.get_firmware_version(), tin.get_hardware_version(),
             tin.get_serial_number()))

    sim = LinEcuSim(tin, nad)
    tin.add_message_listener(sim.on_frame)
    sim.start()
    try:
        while True:
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\nstopping...")
    finally:
        sim.stop()
        try:
            tin.disconnect()
        except USBtinException:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
