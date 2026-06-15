"""Formatting helpers and the Monitor aggregation model.

Ports the display logic of LogMessageTableModel.java and
MonitorMessageTableModel.java.  The Tkinter GUI renders the rows itself, so
these helpers only produce the cell strings and keep the per-id aggregation.
"""

from .log_message import MessageType, MonitorMessage


def format_id(canmsg, hex_display=True):
    """Format a CAN id exactly like the Java table models."""
    if canmsg.is_extended():
        if hex_display:
            return "%08xh" % canmsg.get_id()
        return "%09d" % canmsg.get_id()
    else:
        if hex_display:
            return "%03xh" % canmsg.get_id()
        return "%04d" % canmsg.get_id()


def format_data(canmsg, hex_display=True):
    """Format the data bytes (or the RTR marker)."""
    if canmsg.is_rtr():
        return "Remote Transmission Request"

    parts = []
    for b in canmsg.get_data():
        if hex_display:
            parts.append("%02x" % b)
        else:
            parts.append("%03d" % b)
    return " ".join(parts)


def format_dlc(canmsg):
    return len(canmsg.get_data())


class MonitorModel:
    """Aggregates messages per (id, extended, direction) like the Monitor tab.

    The key matches the Java implementation::

        key = (id << 2) | (extended ? 1 : 0) | (out ? 2 : 0)
    """

    def __init__(self):
        self.messages = {}  # key -> MonitorMessage

    def clear(self):
        self.messages.clear()

    @staticmethod
    def make_key(logmessage):
        canmsg = logmessage.get_canmsg()
        key = canmsg.get_id() << 2
        if canmsg.is_extended():
            key |= 1
        if logmessage.get_type() == MessageType.OUT:
            key |= 2
        return key

    def add(self, logmessage):
        """Add/update a message.

        Returns ``(key, monitor_message, is_new)``.
        """
        key = self.make_key(logmessage)
        if key in self.messages:
            message = self.messages[key]
            message.update(logmessage)
            return key, message, False
        else:
            message = MonitorMessage(logmessage)
            self.messages[key] = message
            return key, message, True

    def sorted_keys(self):
        return sorted(self.messages.keys())
