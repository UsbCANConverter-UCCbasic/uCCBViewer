"""
SLCAN (Lawicel / USBtin) protocol implementation over a serial port.

This is a reconstruction of the ``USBtin`` class from uCCBlib (a fork of
Thomas Fischl's open-source USBtinLIB).  The original jar was not available, so
the wire protocol below is reimplemented from the documented USBtin/SLCAN
command set and from how the GUI drives it.

Protocol summary
----------------
The device speaks a line based ASCII protocol at 115200 baud, 8N1.  Every
command is terminated with a carriage return (``\\r``, 0x0D).  The device
answers each command with either ``\\r`` (ok), a payload terminated by ``\\r``
or a BELL byte (0x07) on error.

    V        -> hardware version    (V<hh>)
    v        -> firmware version    (v<hh>)
    N        -> serial number       (N<xxxx>)
    Sx       -> set standard bitrate (x = 0..9, where 9 = 83.3 kbit/s)
    sxxyyyy  -> set custom bitrate registers (CNF/BRP, derived from FOSC)
    O / l / L-> open channel active / loopback / listen-only
    C        -> close channel
    t/T/r/R  -> transmit a CAN frame; received frames use the same notation

Incoming CAN frames (while the channel is open) are delivered asynchronously
through registered listeners.
"""

import threading
import time
from enum import Enum

import serial

from .can_message import CANMessage


class USBtinException(Exception):
    """Raised on any protocol or serial error."""
    pass


class OpenMode(Enum):
    ACTIVE = "ACTIVE"
    LOOPBACK = "LOOPBACK"
    LISTENONLY = "LISTENONLY"


# Control bytes
BELL = 0x07
CR = 0x0D

# Standard bitrate -> SLCAN 'S' code
_STANDARD_BITRATES = {
    10000: "0",
    20000: "1",
    50000: "2",
    100000: "3",
    125000: "4",
    250000: "5",
    500000: "6",
    800000: "7",
    1000000: "8",
    83300: "9",
}


class USBtin:
    """Driver for an USBtin compatible (uCCB) USB-to-CAN adapter."""

    def __init__(self):
        self._serial = None
        self.firmware_version = ""
        self.hardware_version = ""
        self.serial_number = ""

        self._listeners = []
        self._reader_thread = None
        self._reader_running = False
        # serialises access to the port between the reader thread and senders
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Listener registration
    # ------------------------------------------------------------------
    def add_message_listener(self, listener):
        """Register a callable ``listener(canmsg)`` invoked for each RX frame."""
        self._listeners.append(listener)

    def remove_message_listener(self, listener):
        self._listeners.remove(listener)

    def _fire_message(self, canmsg):
        for listener in self._listeners:
            listener(canmsg)

    # ------------------------------------------------------------------
    # Connection handling
    # ------------------------------------------------------------------
    def connect(self, port_name):
        """Open the serial port and query device information."""
        try:
            self._serial = serial.Serial(
                port=port_name,
                baudrate=115200,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=1,
            )
        except (serial.SerialException, ValueError) as e:
            raise USBtinException("Can not open serial port %s: %s" % (port_name, e))

        try:
            # Make sure the device is in a known state: flush any half-finished
            # command and close any open channel before querying it.
            self._serial.write(b"\rC\r")
            time.sleep(0.1)
            self._serial.reset_input_buffer()
            self._serial.reset_output_buffer()
            try:
                self._transmit("C")
            except USBtinException:
                pass  # channel was already closed (device answered with BELL)

            self.firmware_version = self._transmit("v")[1:]
            self.hardware_version = self._transmit("V")[1:]
            try:
                self.serial_number = self._transmit("N")[1:]
            except USBtinException:
                self.serial_number = ""

            # reset overflow error flags (W2D00), as the original driver does
            try:
                self._transmit("W2D00")
            except USBtinException:
                pass
        except USBtinException:
            self.disconnect()
            raise

    def disconnect(self):
        """Close the serial port."""
        self._stop_reader()
        if self._serial is not None:
            try:
                self._serial.close()
            except serial.SerialException as e:
                raise USBtinException("Can not close serial port: %s" % e)
            finally:
                self._serial = None

    def get_firmware_version(self):
        return self.firmware_version

    def get_hardware_version(self):
        return self.hardware_version

    def get_serial_number(self):
        return self.serial_number

    # ------------------------------------------------------------------
    # CAN channel handling
    # ------------------------------------------------------------------
    def open_can_channel(self, baudrate, mode):
        """Configure the bitrate, open the channel and start receiving."""
        # 1) bitrate
        if baudrate in _STANDARD_BITRATES:
            self._transmit("S" + _STANDARD_BITRATES[baudrate])
        else:
            self._transmit("s" + self._calculate_btr(baudrate))

        # 2) open channel in requested mode
        if mode == OpenMode.LISTENONLY:
            self._transmit("L")
        elif mode == OpenMode.LOOPBACK:
            self._transmit("l")
        else:  # ACTIVE
            self._transmit("O")

        # 3) start asynchronous reception
        self._start_reader()

    def close_can_channel(self):
        """Stop receiving and close the channel."""
        self._stop_reader()
        if self._serial is not None:
            self._transmit("C")

    @staticmethod
    def _calculate_btr(baudrate):
        """Compute the custom-bitrate register payload for the ``s`` command.

        Direct port of ``openCANChannel``'s fallback path in uCCBlib: it walks
        the possible CAN bit lengths (in time quanta) for a 24 MHz oscillator,
        picks the closest baudrate prescaler and looks up the matching CNF
        register values.  All bitrates the GUI offers map to ``S`` presets, so
        this is only used for arbitrary custom rates.
        """
        FOSC = 24000000
        xdesired = FOSC // baudrate
        xopt = 0
        diffopt = 0
        brpopt = 0

        # walk through possible CAN bit length (in TQ)
        for x in range(11, 24):
            # get next even value for baudrate factor
            xbrp = (xdesired * 10) // x
            m = xbrp % 20
            if m >= 10:
                xbrp += 20
            xbrp -= m
            xbrp //= 10

            # check bounds
            if xbrp < 2:
                xbrp = 2
            if xbrp > 128:
                xbrp = 128

            # calculate diff
            xist = x * xbrp
            diff = abs(xdesired - xist)

            # use this clock option if it is better than the previous one
            if xopt == 0 or diff <= diffopt:
                xopt = x
                diffopt = diff
                brpopt = xbrp // 2 - 1

        # mapping for CNF register values
        cnfvalues = [0x9203, 0x9303, 0x9B03, 0x9B04, 0x9C04, 0xA404, 0xA405,
                     0xAC05, 0xAC06, 0xAD06, 0xB506, 0xB507, 0xBD07]

        return "%02x%04x" % (brpopt | 0xC0, cnfvalues[xopt - 11])

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------
    def send(self, canmsg):
        """Transmit a CAN message (or raw SLCAN command frame)."""
        with self._lock:
            if self._serial is None:
                raise USBtinException("Not connected")
            try:
                self._serial.write((str(canmsg) + "\r").encode("ascii"))
            except serial.SerialException as e:
                raise USBtinException(str(e))

    def clearfifo_tx(self):
        """Clear the transmit FIFO.

        The hardware FIFO is flushed implicitly by closing the channel; nothing
        is buffered on the host side, so this is a no-op kept for API parity.
        """
        return

    # ------------------------------------------------------------------
    # Low level transport
    # ------------------------------------------------------------------
    def _transmit(self, cmd):
        """Write a command and synchronously read its response.

        Only used while the reader thread is *not* running (during connect and
        channel open/close).
        """
        with self._lock:
            if self._serial is None:
                raise USBtinException("Not connected")
            try:
                self._serial.write((cmd + "\r").encode("ascii"))
                return self._read_response()
            except serial.SerialException as e:
                raise USBtinException(str(e))

    def _read_response(self):
        response = []
        while True:
            b = self._serial.read(1)
            if not b:
                raise USBtinException("Timeout waiting for device response")
            value = b[0]
            if value == BELL:
                raise USBtinException("Command error (BELL)")
            if value == CR:
                break
            response.append(chr(value))
        return "".join(response)

    # ------------------------------------------------------------------
    # Asynchronous receiver
    # ------------------------------------------------------------------
    def _start_reader(self):
        if self._reader_running:
            return
        self._reader_running = True
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    def _stop_reader(self):
        self._reader_running = False
        thread = self._reader_thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._reader_thread = None

    def _reader_loop(self):
        buffer = []
        while self._reader_running and self._serial is not None:
            try:
                chunk = self._serial.read(1)
            except (serial.SerialException, TypeError):
                break
            if not chunk:
                continue
            value = chunk[0]
            if value == CR:
                line = "".join(buffer)
                buffer = []
                self._handle_line(line)
            elif value == BELL:
                buffer = []  # error response to a transmit, ignore
            else:
                buffer.append(chr(value))

    def _handle_line(self, line):
        if not line:
            return
        type_char = line[0]
        if type_char in ("t", "T", "r", "R"):
            try:
                canmsg = CANMessage.from_string(line)
            except (ValueError, IndexError):
                return
            self._fire_message(canmsg)
        # 'z'/'Z' transmit acknowledgements and other responses are ignored
