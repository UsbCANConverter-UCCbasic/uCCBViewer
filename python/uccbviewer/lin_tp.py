"""
LIN 2.1 transport layer (ISO 17987-2 / LIN-Spec 2.1 §3.2) exposed as a
``udsoncan`` connection, layered on the SLCAN :class:`~uccbviewer.usbtin.USBtin`
driver.

It lets you run the full ``udsoncan`` UDS (ISO 14229) service stack over LIN by
implementing only the LIN-specific bits:

* segmentation of a UDS request into Single / First / Consecutive frames sent
  on the **master request frame** (id ``0x3C``);
* polling of the **slave response frame** (id ``0x3D``) and reassembly of the
  UDS response.

Everything time-critical about LIN (sync break, header, parity, checksum) lives
in the adapter firmware; the coarse transport timing (P2 = 50..500 ms,
N_Cr = 1000 ms, STmin) is comfortably handled here on the host.

Adapter mode
------------
Open the adapter in **MONITOR** mode (the viewer's "MONITOR" / SLCAN ``l``).
In that mode the firmware routes ``t``/``r`` frames to its on-demand
single-shot header injector (``transmitStd``), which is what request/response
diagnostics need.  MASTER mode would instead queue frames into the autonomous
schedule table, which does not fit a request/response exchange.

Wire format
-----------
The diagnostic frames are addressed as standard LIN ids, which
:class:`CANMessage` serialises with the leading selector nibble the firmware
expects::

    CANMessage(0x3C, <8 bytes>)            -> "t03c8<16 hex>"   (master sends data)
    CANMessage(0x3D, b"\\x00"*8, rtr=True)  -> "r03d8"           (poll slave response)

Received slave responses arrive through the normal ``USBtin`` listener as
``CANMessage`` frames with id ``0x3D`` (an empty payload means "slave not ready,
keep polling").

Example
-------
::

    from uccbviewer.usbtin import USBtin, OpenMode
    from uccbviewer.lin_tp import LinTpConnection, create_client

    tin = USBtin()
    tin.connect("COM5")
    tin.open_can_channel(100000, OpenMode.LOOPBACK)   # LOOPBACK == LIN MONITOR

    with create_client(tin, nad=0x01) as client:
        client.change_session(0x03)                   # DiagnosticSessionControl
        resp = client.read_data_by_identifier(0xF190) # ReadDataByIdentifier
        print(resp.service_data.values)
"""

import queue
import time

try:
    from udsoncan.connections import BaseConnection
    from udsoncan.exceptions import TimeoutException
    _HAVE_UDSONCAN = True
except ImportError:                       # allow importing/segmenting without udsoncan
    _HAVE_UDSONCAN = False

    class BaseConnection:                  # minimal stand-in for the abstract base
        def __init__(self, name=None):
            self.name = name

    class TimeoutException(Exception):
        pass

from .can_message import CANMessage
from .usbtin import USBtinException

# ---------------------------------------------------------------------------
# LIN diagnostic constants (LIN-Spec 2.1)
# ---------------------------------------------------------------------------
LIN_MASTER_REQUEST_ID = 0x3C   # MRF: master -> slave  (§2.3.3.4)
LIN_SLAVE_RESPONSE_ID = 0x3D   # SRF: slave  -> master

# NAD ranges (§3.2.1.2)
NAD_SLEEP = 0x00
NAD_FUNCTIONAL = 0x7E          # functional request, no addressed response
NAD_BROADCAST = 0x7F

# PCI types: high nibble of the PCI byte (§3.2.2)
PCI_SF = 0x0   # single frame
PCI_FF = 0x1   # first frame
PCI_CF = 0x2   # consecutive frame

UNUSED = 0xFF  # padding for unused payload bytes (§3.2.2)

# Default transport timing in seconds (§3.2.5 / §5.6).
DEFAULT_P2 = 0.5         # budget for the first/only response frame (P2max)
DEFAULT_P2_STAR = 2.0    # extended budget after an NRC 0x78 (P2*max)
DEFAULT_STMIN = 0.01     # gap between consecutive frames / response poll pacing


# ===========================================================================
# Pure transport-layer framing (no I/O, unit-testable on its own)
# ===========================================================================
def segment_request(nad, payload):
    """Split a UDS request *payload* into a list of 8-byte LIN PDUs.

    ``payload`` is the application-layer message (SID followed by its
    parameters).  Frames shorter than 8 bytes are padded with ``0xFF``.
    """
    payload = bytes(payload)
    length = len(payload)
    if length == 0:
        raise ValueError("empty UDS payload")

    if length <= 6:
        # Single Frame: NAD | (0x0 << 4 | len) | data...
        pci = (PCI_SF << 4) | length
        frame = bytes([nad, pci]) + payload
        return [_pad(frame)]

    # First Frame: NAD | (0x1 << 4 | len_hi) | len_lo | data[0:5]
    frames = [bytes([nad, (PCI_FF << 4) | ((length >> 8) & 0x0F),
                     length & 0xFF]) + payload[:5]]
    remaining = payload[5:]
    counter = 1
    while remaining:
        chunk = remaining[:6]
        remaining = remaining[6:]
        # Consecutive Frame: NAD | (0x2 << 4 | counter) | data...
        pci = (PCI_CF << 4) | (counter & 0x0F)
        frames.append(_pad(bytes([nad, pci]) + chunk))
        counter = (counter + 1) & 0x0F
    return frames


def _pad(frame):
    """Pad a partial PDU out to the fixed 8 data bytes with ``0xFF``."""
    if len(frame) > 8:
        raise ValueError("LIN PDU longer than 8 bytes")
    return frame + bytes([UNUSED] * (8 - len(frame)))


class _Reassembler:
    """Stateful reassembly of a segmented UDS response across SRF polls."""

    def __init__(self):
        self.nad = None
        self._expected = None         # total payload length from the First Frame
        self._buf = bytearray()
        self._next_cf = 1             # expected Consecutive Frame counter

    def feed(self, frame):
        """Consume one 8-byte response PDU.

        Returns the complete UDS payload (``bytes``) once fully reassembled,
        or ``None`` if more Consecutive Frames are still required.
        """
        nad = frame[0]
        pci_type = (frame[1] >> 4) & 0x0F
        self.nad = nad

        if pci_type == PCI_SF:
            length = frame[1] & 0x0F
            return bytes(frame[2:2 + length])

        if pci_type == PCI_FF:
            self._expected = ((frame[1] & 0x0F) << 8) | frame[2]
            self._buf = bytearray(frame[3:8])
            self._next_cf = 1
            return self._finished()

        if pci_type == PCI_CF:
            if self._expected is None:
                raise ValueError("consecutive frame before first frame")
            counter = frame[1] & 0x0F
            if counter != (self._next_cf & 0x0F):
                raise ValueError("out-of-sequence consecutive frame "
                                 "(got %d, expected %d)"
                                 % (counter, self._next_cf & 0x0F))
            self._buf.extend(frame[2:8])
            self._next_cf = (self._next_cf + 1) & 0x0F
            return self._finished()

        raise ValueError("unknown PCI type 0x%X" % pci_type)

    def _finished(self):
        if self._expected is not None and len(self._buf) >= self._expected:
            return bytes(self._buf[:self._expected])
        return None


# ===========================================================================
# udsoncan connection
# ===========================================================================
class LinTpConnection(BaseConnection):
    """A ``udsoncan`` connection that speaks LIN-TP over an open ``USBtin``.

    The ``USBtin`` instance must already be connected with its channel open in
    LIN MONITOR mode; this object only borrows it for diagnostic frame I/O and
    never opens/closes the serial port itself.

    Parameters
    ----------
    usbtin:
        A connected :class:`~uccbviewer.usbtin.USBtin`.
    nad:
        Node address (NAD) of the addressed slave for requests.  Use
        :data:`NAD_FUNCTIONAL` for functional requests.
    p2_timeout, p2_star_timeout, st_min:
        Transport timing in seconds (see module constants).
    set_classic_checksum:
        Send ``r3100`` on :meth:`open` to force classic checksum.  The firmware
        already forces classic for 0x3C/0x3D, so this is belt-and-suspenders.
    """

    def __init__(self, usbtin, nad, *, name="lin",
                 p2_timeout=DEFAULT_P2, p2_star_timeout=DEFAULT_P2_STAR,
                 st_min=DEFAULT_STMIN, poll_interval=None,
                 set_classic_checksum=True):
        super().__init__(name)
        self.usbtin = usbtin
        self.nad = nad
        self.p2_timeout = p2_timeout
        self.p2_star_timeout = p2_star_timeout
        self.st_min = st_min
        self.poll_interval = poll_interval if poll_interval is not None else st_min
        self.set_classic_checksum = set_classic_checksum

        self._opened = False
        self._rxqueue = queue.Queue()
        self.last_response_nad = None

    # ------------------------------------------------------------------
    # BaseConnection lifecycle
    # ------------------------------------------------------------------
    def open(self):
        self.usbtin.add_message_listener(self._on_frame)
        if self.set_classic_checksum:
            self._send_raw("r3100")
        self._opened = True
        return self

    def close(self):
        if self._opened:
            try:
                self.usbtin.remove_message_listener(self._on_frame)
            except (ValueError, AttributeError):
                pass
            self._opened = False

    def is_open(self):
        return self._opened

    def empty_rxqueue(self):
        try:
            while True:
                self._rxqueue.get_nowait()
        except queue.Empty:
            pass

    # ------------------------------------------------------------------
    # BaseConnection payload I/O
    # ------------------------------------------------------------------
    def specific_send(self, payload):
        """Segment *payload* and transmit it on the master request frame."""
        if not self._opened:
            raise USBtinException("LIN connection is not open")
        self.empty_rxqueue()
        frames = segment_request(self.nad, payload)
        for i, frame in enumerate(frames):
            self._send_master_request(frame)
            if i != len(frames) - 1:
                time.sleep(self.st_min)

    def specific_wait_frame(self, timeout=DEFAULT_P2):
        """Poll the slave response frame and reassemble the UDS response.

        Raises :class:`TimeoutException` if nothing complete arrives within
        *timeout* seconds.  Negative responses (incl. NRC 0x78) are returned
        verbatim so the ``udsoncan`` client can apply its own P2* retry logic.
        """
        if not self._opened:
            raise USBtinException("LIN connection is not open")

        deadline = time.time() + timeout
        reasm = _Reassembler()
        while time.time() < deadline:
            frame = self._poll_slave_response(deadline)
            if frame is None:
                continue                     # slave not ready yet, keep polling
            payload = reasm.feed(frame)
            if payload is not None:
                self.last_response_nad = reasm.nad
                return payload
        raise TimeoutException(
            "No complete LIN slave response within %.3fs" % timeout)

    # ------------------------------------------------------------------
    # Frame I/O helpers
    # ------------------------------------------------------------------
    def _send_master_request(self, frame8):
        """Send one 8-byte PDU as a master-supplied data frame on 0x3C."""
        self.usbtin.send(CANMessage(LIN_MASTER_REQUEST_ID, frame8))

    def _request_slave_response(self):
        """Send a header-only request on 0x3D, asking for an 8-byte response."""
        self.usbtin.send(
            CANMessage(LIN_SLAVE_RESPONSE_ID, bytes(8), rtr=True))

    def _poll_slave_response(self, deadline):
        """Send one 0x3D header and return the response PDU, or ``None``.

        ``None`` means the slave did not answer this poll yet (empty frame or
        no frame); the caller keeps polling until its deadline.
        """
        self._request_slave_response()
        remaining = deadline - time.time()
        if remaining <= 0:
            return None
        try:
            frame = self._rxqueue.get(timeout=min(self.poll_interval, remaining))
        except queue.Empty:
            return None
        # "Slave not ready" looks like either a too-short frame or a single
        # frame whose PCI reports length 0 (NAD byte followed by 0x00, e.g.
        # "01 00 ..").  A real UDS response always carries at least the SID
        # byte, so a length-0 SF is never a valid response -- treat both as
        # "nothing yet" and keep polling until the deadline.
        if len(frame) < 2 or frame[1] == 0x00:
            time.sleep(self.st_min)
            return None
        return frame

    def _send_raw(self, slcan_str):
        """Send a raw SLCAN command string (e.g. the checksum select)."""
        self.usbtin.send(CANMessage.from_string(slcan_str))

    # ------------------------------------------------------------------
    # USBtin listener (runs on the reader thread)
    # ------------------------------------------------------------------
    def _on_frame(self, canmsg):
        if canmsg.get_id() == LIN_SLAVE_RESPONSE_ID:
            self._rxqueue.put(bytes(canmsg.get_data()))


# ===========================================================================
# Convenience factory
# ===========================================================================
def create_client(usbtin, nad, *, config=None, **conn_kwargs):
    """Build a ready-to-use ``udsoncan.client.Client`` over LIN-TP.

    Returns a client whose ``__enter__`` opens the connection.  ``config`` is
    merged onto ``udsoncan``'s default client config; extra keyword arguments
    are forwarded to :class:`LinTpConnection`.
    """
    if not _HAVE_UDSONCAN:
        raise ImportError(
            "udsoncan is required for create_client(); install it with "
            "`pip install udsoncan`")

    from udsoncan.client import Client
    from udsoncan import configs

    conn = LinTpConnection(usbtin, nad, **conn_kwargs)
    client_config = dict(configs.default_client_config)
    # LIN P2 timing is far longer than CAN; align the client timeouts.
    client_config["request_timeout"] = conn.p2_star_timeout + 1.0
    client_config["p2_timeout"] = conn.p2_timeout
    client_config["p2_star_timeout"] = conn.p2_star_timeout
    if config:
        client_config.update(config)
    return Client(conn, config=client_config)
