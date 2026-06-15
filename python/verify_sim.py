"""Verify lin_ecu_sim against a lin_tp tester (mirrors the viewer's UDS tab)."""
import sys, time
from uccbviewer.usbtin import USBtin, OpenMode
from uccbviewer.can_message import CANMessage
from uccbviewer import lin_tp
from lin_ecu_sim import LinEcuSim, hx

SIM_PORT = sys.argv[1] if len(sys.argv) > 1 else "COM30"
TST_PORT = sys.argv[2] if len(sys.argv) > 2 else "COM3"

# --- simulator on SIM_PORT (manages its own channel/mode) ---
sim_tin = USBtin(); sim_tin.connect(SIM_PORT)
sim_tin._serial.timeout = 0.1
sim = LinEcuSim(sim_tin, 0x01); sim_tin.add_message_listener(sim.on_frame); sim.start()
time.sleep(1.0)

# --- tester on TST_PORT (same stack the viewer uses) ---
tst_tin = USBtin(); tst_tin.connect(TST_PORT)
tst_tin.open_can_channel(100000, OpenMode.LOOPBACK); time.sleep(0.05)
conn = lin_tp.LinTpConnection(tst_tin, nad=0x01); conn.open()

results = []
def req(name, data, expect):
    conn.empty_rxqueue()
    conn.send(bytes(data))
    try:
        resp = conn.wait_frame(timeout=2.0, exception=True)
    except Exception:
        resp = b""
    ok = bytes(resp[:len(expect)]) == bytes(expect)
    results.append(ok)
    print("  [%s] %-26s req=%s  resp=%s" %
          ("PASS" if ok else "FAIL", name, hx(bytes(data)), hx(bytes(resp)) or "(none)"))
    time.sleep(1.5)   # let the sim flip back to MONITOR before the next request

print("Testing simulator (%s) <- tester (%s)\n" % (SIM_PORT, TST_PORT))
req("DiagSession 10 03",  [0x10, 0x03],         [0x50, 0x03, 0x00, 0x32, 0x01, 0xF4])
req("TesterPresent 3E 00",[0x3E, 0x00],         [0x7E, 0x00])
req("ECUReset 11 01",     [0x11, 0x01],         [0x51, 0x01])
req("ClearDTC 14",        [0x14, 0xFF,0xFF,0xFF],[0x54])
req("ReadDID F195",       [0x22, 0xF1, 0x95],   [0x62, 0xF1, 0x95, 0x01, 0x00])
req("ReadDID F190 (stub)",[0x22, 0xF1, 0x90],   [0x62, 0xF1, 0x90] + list(b"WVW"))
req("ReadDTC 19 02",      [0x19, 0x02, 0xFF],   [0x59, 0x02, 0xFF])
req("SuppressPosRsp 3E80",[0x3E, 0x80],         [])     # expect no response
req("Unsupported 2F",     [0x2F, 0x00],         [0x7F, 0x2F, 0x11])
req("UnknownDID 9999",    [0x22, 0x99, 0x99],   [0x7F, 0x22, 0x31])

print("\n%d/%d service checks passed" % (sum(results), len(results)))

conn.close()
sim.stop()
sim_tin.remove_message_listener(sim.on_frame)
try: tst_tin.close_can_channel(); tst_tin.disconnect()
except Exception: pass
try: sim_tin.disconnect()
except Exception: pass
sys.exit(0 if all(results) else 1)
