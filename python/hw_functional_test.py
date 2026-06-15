"""
Hardware-in-the-loop functional test for uCCBViewer.

Exercises the engine behind the viewer GUI against two real LIN adapters on
two serial ports (default COM3 + COM30).  The Tk GUI is a thin layer over the
modules tested here (USBtin, CANMessage, lin_tp), so this validates the actual
feature behaviour the viewer exposes:

    Tier 0  pure logic        CANMessage round-trip, self-echo signature,
                              UDS segmentation + reassembly         (no HW)
    Tier 1  single adapter    connect / versions / open every mode  (each port)
    Tier 2  two adapters      MONITOR send  ->  MONITOR receive (both directions)
                              MASTER schedule publish -> MONITOR receive
    Tier 3  UDS               request framing on the wire, and a best-effort
                              full round-trip with a synthetic slave response

Usage:
    python hw_functional_test.py [PORT_A] [PORT_B]
"""

import sys
import time

from uccbviewer.can_message import CANMessage
from uccbviewer.usbtin import USBtin, USBtinException, OpenMode
from uccbviewer import lin_tp


PORT_A = sys.argv[1] if len(sys.argv) > 1 else "COM3"
PORT_B = sys.argv[2] if len(sys.argv) > 2 else "COM30"
GG = 100000  # firmware rate param the viewer uses for the 19200 LIN selection

LIN_MRF = lin_tp.LIN_MASTER_REQUEST_ID   # 0x3C
LIN_SRF = lin_tp.LIN_SLAVE_RESPONSE_ID   # 0x3D


# ---------------------------------------------------------------------------
# tiny test harness
# ---------------------------------------------------------------------------
_results = []


def check(name, ok, detail=""):
    _results.append((name, ok))
    mark = "PASS" if ok else "FAIL"
    line = "  [%s] %s" % (mark, name)
    if detail:
        line += "  -- " + detail
    print(line)
    return ok


def section(title):
    print("\n=== %s ===" % title)


class Collector:
    """Listener that records received CANMessages."""

    def __init__(self):
        self.msgs = []

    def __call__(self, canmsg):
        self.msgs.append(canmsg)

    def find(self, id_, data=None, timeout_already_waited=True):
        for m in self.msgs:
            if m.get_id() == id_ and (data is None or bytes(m.get_data()) == bytes(data)):
                return m
        return None

    def clear(self):
        self.msgs = []


def open_adapter(port, modestr, classic=True):
    mode = {"MASTER": OpenMode.ACTIVE,
            "MONITOR": OpenMode.LOOPBACK,
            "SLAVE": OpenMode.LISTENONLY}[modestr]
    tin = USBtin()
    tin.clearfifo_tx()
    tin.connect(port)
    tin.open_can_channel(GG, mode)
    time.sleep(0.05)
    tin.send(CANMessage.from_string("r3100" if classic else "r3000"))
    time.sleep(0.05)
    return tin


# ===========================================================================
# Tier 0 -- pure logic, no hardware
# ===========================================================================
def tier0_logic():
    section("Tier 0: pure logic (no hardware)")

    # CANMessage round-trip for each frame type
    cases = ["t1233112233", "T000001044deadbeef", "r3008", "R000001023"]
    ok = True
    for s in cases:
        rt = str(CANMessage.from_string(s))
        if rt != s:
            ok = False
            check("CANMessage round-trip %r" % s, False, "got %r" % rt)
    check("CANMessage round-trip (t/T/r/R)", ok)

    # self-echo signature: a sent frame and its parsed echo share str()
    sent = CANMessage(0x123, bytes([1, 2, 3]))
    echo = CANMessage.from_string(str(sent))
    check("self-echo signature stable", str(sent) == str(echo))

    # UDS single-frame segmentation
    frames = lin_tp.segment_request(0x01, bytes([0x3E, 0x00]))
    check("UDS segment single-frame",
          len(frames) == 1 and frames[0][:4] == bytes([0x01, 0x02, 0x3E, 0x00]),
          detail=frames[0].hex())

    # UDS multi-frame segmentation (10 bytes -> FF + 1 CF)
    payload = bytes(range(0x10, 0x1A))  # 10 bytes
    frames = lin_tp.segment_request(0x01, payload)
    seg_ok = (len(frames) == 2
              and frames[0][0] == 0x01 and (frames[0][1] >> 4) == lin_tp.PCI_FF
              and frames[0][2] == 10
              and (frames[1][1] >> 4) == lin_tp.PCI_CF)
    check("UDS segment multi-frame (FF+CF)", seg_ok,
          detail="%s | %s" % (frames[0].hex(), frames[1].hex()))

    # UDS reassembly of that multi-frame response
    r = lin_tp._Reassembler()
    out = r.feed(frames[0])
    out = r.feed(frames[1]) if out is None else out
    check("UDS reassemble multi-frame", out == payload, detail=str(out))

    # UDS reassembly of a single frame
    r = lin_tp._Reassembler()
    out = r.feed(bytes([0x01, 0x02, 0x7E, 0x00, 0xFF, 0xFF, 0xFF, 0xFF]))
    check("UDS reassemble single-frame", out == bytes([0x7E, 0x00]),
          detail=str(out))

    # poll loop must SKIP a "slave not ready" frame ([NAD,00,..]) and keep
    # polling until the real response arrives, instead of returning empty.
    class _DummyTin:
        def add_message_listener(self, l): pass
        def remove_message_listener(self, l): pass
        def send(self, m): pass

    conn = lin_tp.LinTpConnection(_DummyTin(), nad=0x01,
                                  set_classic_checksum=False)
    conn.open()
    conn._rxqueue.put(bytes([0x01, 0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF]))
    conn._rxqueue.put(bytes([0x01, 0x06, 0x50, 0x03, 0x00, 0x32, 0x01, 0xF4]))
    try:
        payload = conn.specific_wait_frame(timeout=1.0)
    except lin_tp.TimeoutException:
        payload = None
    conn.close()
    check("UDS poll skips 'not ready' (01 00) frame, returns real response",
          payload == bytes([0x50, 0x03, 0x00, 0x32, 0x01, 0xF4]),
          detail=str(payload))


# ===========================================================================
# Tier 1 -- single adapter sanity, each port
# ===========================================================================
def tier1_single(port):
    section("Tier 1: single-adapter sanity on %s" % port)
    tin = USBtin()
    try:
        tin.connect(port)
    except USBtinException as e:
        check("connect %s" % port, False, str(e))
        return
    check("connect %s" % port, True)
    fw = tin.get_firmware_version()
    hw = tin.get_hardware_version()
    sn = tin.get_serial_number()
    check("query versions %s" % port, bool(fw) or bool(hw),
          detail="fw=%s hw=%s sn=%s" % (fw, hw, sn))

    ok = True
    for modestr, mode in (("MONITOR", OpenMode.LOOPBACK),
                          ("MASTER", OpenMode.ACTIVE),
                          ("SLAVE", OpenMode.LISTENONLY)):
        try:
            tin.open_can_channel(GG, mode)
            time.sleep(0.03)
            tin.close_can_channel()
            time.sleep(0.03)
        except USBtinException as e:
            ok = False
            check("open/close %s on %s" % (modestr, port), False, str(e))
    check("open + close every mode on %s" % port, ok)
    tin.disconnect()
    check("disconnect %s" % port, True)


# ===========================================================================
# Tier 2 -- two-adapter bus traffic
# ===========================================================================
def tier2_send_receive():
    section("Tier 2: two-adapter bus traffic (%s <-> %s)" % (PORT_A, PORT_B))

    a = open_adapter(PORT_A, "MONITOR")
    b = open_adapter(PORT_B, "MONITOR")
    col_b = Collector(); b.add_message_listener(col_b)
    col_a = Collector(); a.add_message_listener(col_a)
    try:
        # A -> B : single-shot data frame
        col_b.clear()
        frame = CANMessage(0x21, bytes([0xDE, 0xAD, 0xBE, 0xEF]))
        a.send(frame)
        time.sleep(0.4)
        got = col_b.find(0x21, [0xDE, 0xAD, 0xBE, 0xEF])
        check("A->B  MONITOR send received on %s" % PORT_B, got is not None,
              detail="rx=%s" % ([str(m) for m in col_b.msgs] or "none"))

        # B -> A : single-shot data frame (other direction)
        col_a.clear()
        frame = CANMessage(0x22, bytes([0x11, 0x22]))
        b.send(frame)
        time.sleep(0.4)
        got = col_a.find(0x22, [0x11, 0x22])
        check("B->A  MONITOR send received on %s" % PORT_A, got is not None,
              detail="rx=%s" % ([str(m) for m in col_a.msgs] or "none"))

        # different lengths -- paced like the viewer's schedule / repeat sender
        col_b.clear()
        a.send(CANMessage(0x30, bytes([0xAA])))
        time.sleep(0.1)
        a.send(CANMessage(0x31, bytes(range(8))))
        time.sleep(0.4)
        check("A->B  1-byte + 8-byte frames received",
              col_b.find(0x30, [0xAA]) is not None
              and col_b.find(0x31, bytes(range(8))) is not None,
              detail="rx=%s" % [str(m) for m in col_b.msgs])
    finally:
        a.remove_message_listener(col_a)
        b.remove_message_listener(col_b)
        a.disconnect(); b.disconnect()


def tier2_schedule():
    section("Tier 2: MASTER schedule table publish -> MONITOR receive")
    master = open_adapter(PORT_A, "MASTER")
    mon = open_adapter(PORT_B, "MONITOR")
    col = Collector(); mon.add_message_listener(col)
    try:
        # mimic _on_lin_send_table: reset, add one publish slot, start
        master.send(CANMessage.from_string("r2FF0"))
        time.sleep(0.05)
        # publish 0x15 = [CA,FE,BA,BE] on a 4-byte data slot
        master.send(CANMessage(0x15, bytes([0xCA, 0xFE, 0xBA, 0xBE])))
        time.sleep(0.05)
        master.send(CANMessage.from_string("r1FF0"))  # start schedule
        time.sleep(0.8)
        got = col.find(0x15, [0xCA, 0xFE, 0xBA, 0xBE])
        check("MASTER schedule frame seen by monitor", got is not None,
              detail="rx=%s" % ([str(m) for m in col.msgs][:6] or "none"))
    finally:
        try:
            master.send(CANMessage.from_string("r2FF0"))  # stop/reset schedule
        except USBtinException:
            pass
        mon.remove_message_listener(col)
        master.disconnect(); mon.disconnect()


# ===========================================================================
# Tier 3 -- UDS over LIN-TP
# ===========================================================================
def tier3_uds_request_framing():
    section("Tier 3: UDS request framing on the wire")
    tester = open_adapter(PORT_A, "MONITOR")
    mon = open_adapter(PORT_B, "MONITOR")
    col = Collector(); mon.add_message_listener(col)
    try:
        conn = lin_tp.LinTpConnection(tester, nad=0x01)
        conn.open()
        conn.empty_rxqueue()
        # send a Tester Present (3E 00) a few times, paced, so the isolated
        # master-request frame is reliably observed on the bus
        mrf = None
        for _ in range(5):
            col.clear()
            conn.specific_send(bytes([0x3E, 0x00]))
            time.sleep(0.2)
            cand = col.find(LIN_MRF)   # id 0x3C; payload is FF-padded to 8 bytes
            if cand is not None and bytes(cand.get_data())[:4] == bytes(
                    [0x01, 0x02, 0x3E, 0x00]):
                mrf = cand
                break
        ok = mrf is not None
        check("UDS master-request (0x3C) on wire carries [NAD,PCI,3E,00]", ok,
              detail="0x3C=%s rx=%s" % (str(mrf) if mrf else "none",
                                        [str(m) for m in col.msgs]))

        # the poll loop must emit slave-response headers (0x3D)
        col.clear()
        try:
            conn.specific_wait_frame(timeout=0.4)
        except lin_tp.TimeoutException:
            pass
        srf = col.find(LIN_SRF)
        check("UDS slave-response poll (0x3D) emitted on wire",
              srf is not None, detail="0x3D=%s" % (str(srf) if srf else "none"))
        conn.close()
    finally:
        mon.remove_message_listener(col)
        tester.disconnect(); mon.disconnect()


def tier3_uds_roundtrip():
    section("Tier 3: UDS full round-trip (synthetic slave response)")
    if not lin_tp._HAVE_UDSONCAN:
        check("udsoncan available", False, "not installed")
        return

    # B publishes a canned UDS positive response for Tester Present on 0x3D:
    #   data = [NAD=01, PCI=SF|len2, 0x7E, 0x00, FF FF FF FF]
    slave = open_adapter(PORT_B, "MASTER")
    tester = open_adapter(PORT_A, "MONITOR")
    try:
        resp = CANMessage(LIN_SRF,
                          bytes([0x01, 0x02, 0x7E, 0x00, 0xFF, 0xFF, 0xFF, 0xFF]))
        slave.send(CANMessage.from_string("r2FF0"))
        time.sleep(0.05)
        slave.send(resp)                       # schedule slot: publish on 0x3D
        time.sleep(0.05)
        slave.send(CANMessage.from_string("r1FF0"))  # start autonomous publish
        time.sleep(0.2)

        from uccbviewer.lin_tp import create_client
        ok = False
        detail = ""
        try:
            with create_client(tester, nad=0x01,
                               p2_star_timeout=2.0) as client:
                client.config["request_timeout"] = 4.0
                r = client.tester_present()
                ok = r.positive
                detail = "positive=%s" % r.positive
        except Exception as e:
            detail = "%s: %s" % (type(e).__name__, e)
        check("UDS TesterPresent round-trip (3E00 -> 7E00)", ok, detail)
    finally:
        try:
            slave.send(CANMessage.from_string("r2FF0"))
        except USBtinException:
            pass
        slave.disconnect(); tester.disconnect()


# ===========================================================================
def main():
    print("uCCBViewer hardware functional test")
    print("ports: A=%s  B=%s" % (PORT_A, PORT_B))

    tier0_logic()
    tier1_single(PORT_A)
    tier1_single(PORT_B)
    tier2_send_receive()
    tier2_schedule()
    tier3_uds_request_framing()
    tier3_uds_roundtrip()

    section("SUMMARY")
    passed = sum(1 for _, ok in _results if ok)
    total = len(_results)
    for name, ok in _results:
        print("  %-50s %s" % (name, "PASS" if ok else "FAIL"))
    print("\n%d/%d checks passed" % (passed, total))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
