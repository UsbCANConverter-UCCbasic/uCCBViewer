"""Log / monitor message containers (port of LogMessage.java and
MonitorMessage.java)."""

from enum import IntEnum


class MessageType(IntEnum):
    """Type of a log entry.

    The integer order matches the icon array order used in the table models:
    INFO, ERROR, IN, OUT.
    """
    INFO = 0
    ERROR = 1
    IN = 2
    OUT = 3


class LogMessage:
    """A single entry in the trace log."""

    def __init__(self, canmsg, message, type, timestamp):
        self.canmsg = canmsg
        self.message = message
        self.type = type
        self.timestamp = timestamp

    def get_type(self):
        return self.type

    def get_canmsg(self):
        return self.canmsg

    def get_message(self):
        return self.message

    def get_timestamp(self):
        return self.timestamp


class MonitorMessage:
    """Aggregated view of a repeating id for the Monitor tab.

    Besides period/count it tracks a per-nibble "heat": an exponential moving
    average of how often each data nibble changes from one frame to the next.
    A nibble that keeps changing tends towards 1.0 (rendered red); one that
    stays constant decays back towards 0.0 (rendered black / default).
    """

    # weight of the newest sample in the moving average (0..1); higher reacts
    # faster, lower smooths more.
    HEAT_ALPHA = 0.3

    def __init__(self, msg):
        self.last_log_message = msg
        self.count = 1
        self.period = 0
        self.nibbles = self._to_nibbles(msg.get_canmsg())
        self.heat = [0.0] * len(self.nibbles)

    @staticmethod
    def _to_nibbles(canmsg):
        out = []
        for b in canmsg.get_data():
            out.append((b >> 4) & 0xF)
            out.append(b & 0xF)
        return out

    def update(self, logmessage):
        """Fold a newly received frame for this id into the aggregate."""
        new_nibbles = self._to_nibbles(logmessage.get_canmsg())
        if len(new_nibbles) != len(self.nibbles):
            # DLC changed - restart the heat tracking for the new layout
            self.heat = [0.0] * len(new_nibbles)
        else:
            a = self.HEAT_ALPHA
            for i, nv in enumerate(new_nibbles):
                changed = 1.0 if nv != self.nibbles[i] else 0.0
                self.heat[i] = a * changed + (1.0 - a) * self.heat[i]
        self.nibbles = new_nibbles

        self.count += 1
        self.period = (logmessage.get_timestamp()
                       - self.last_log_message.get_timestamp())
        self.last_log_message = logmessage

    def get_last_log_message(self):
        return self.last_log_message

    def get_period(self):
        return self.period

    def get_count(self):
        return self.count

    def get_heat(self):
        """Per-nibble change intensity, parallel to the data nibbles."""
        return self.heat
