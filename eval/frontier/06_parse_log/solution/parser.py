"""Log parser."""

import re


def parse_log_line(line):
    """Parse a log line.

    Format: [LEVEL] timestamp=ISO key1=value1;key2=value2 ... message
    """
    match = re.match(r"\[(\w+)\]\s+timestamp=(\S+)\s+(.*)", line)
    if not match:
        raise ValueError("invalid log line")
    level, timestamp, rest = match.groups()
    parts = rest.split()
    fields = {}
    message_parts = []
    for part in parts:
        if ";" in part or "=" in part:
            for kv in part.split(";"):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    fields[k] = v
        else:
            message_parts.append(part)
    return {
        "level": level,
        "timestamp": timestamp,
        "fields": fields,
        "message": " ".join(message_parts),
    }
