"""
Representation of a CAN message in SLCAN (Lawicel/USBtin) notation.

This is a faithful Python reimplementation of the ``CANMessage`` class from
uCCBlib (a fork of Thomas Fischl's USBtinLIB).  The string form follows the
SLCAN protocol:

    t<iii><l><dd..>      standard data frame   (3 hex id digits)
    T<iiiiiiii><l><dd..> extended data frame   (8 hex id digits)
    r<iii><l>            standard remote frame
    R<iiiiiiii><l>       extended remote frame

where ``l`` is a single hex digit holding the data length (DLC) and ``dd`` are
the data bytes as two hex digits each.
"""


class CANMessage:
    """A single CAN frame."""

    def __init__(self, id=0, data=b"", extended=False, rtr=False):
        self.id = id
        # store as an immutable bytes object
        self.data = bytes(data) if data is not None else b""
        self.extended = extended
        self.rtr = rtr

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    @classmethod
    def from_string(cls, message):
        """Parse a CAN message from its SLCAN string representation.

        Mirrors the uCCBlib/USBtinLIB ``CANMessage(String)`` constructor: for
        remote frames the data array is still allocated to ``length`` bytes (so
        the DLC is preserved) but left as zeros.
        """
        if not message:
            return cls()

        type_char = message[0]
        rtr = type_char in ("r", "R")
        extended = type_char in ("T", "R")

        index = 1
        if extended:
            id_ = int(message[index:index + 8], 16)
            index += 8
        else:
            id_ = int(message[index:index + 3], 16)
            index += 3

        length = int(message[index:index + 1], 16)
        if length > 8:
            length = 8
        index += 1

        if not rtr:
            data = bytearray()
            for _ in range(length):
                data.append(int(message[index:index + 2], 16))
                index += 2
            data = bytes(data)
        else:
            # keep DLC, no payload (matches USBtinLIB behaviour)
            data = bytes(length)

        return cls(id_, data, extended, rtr)

    # ------------------------------------------------------------------
    # Accessors (kept method-style for parity with the Java sources)
    # ------------------------------------------------------------------
    def get_id(self):
        return self.id

    def get_data(self):
        return self.data

    def is_extended(self):
        return self.extended

    def is_rtr(self):
        return self.rtr

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------
    def __str__(self):
        if self.rtr:
            s = "R" if self.extended else "r"
        else:
            s = "T" if self.extended else "t"

        if self.extended:
            s += "%08x" % self.id
        else:
            s += "%03x" % self.id

        s += "%01x" % len(self.data)

        if not self.rtr:
            for b in self.data:
                s += "%02x" % b
        return s

    def __repr__(self):
        return "CANMessage(%s)" % str(self)
