"""Log parser stub."""


def parse_log_line(line):
    """Parse a log line.

    Format: [LEVEL] timestamp=ISO key1=value1;key2=value2 ... message
    Example: [WARN] timestamp=2024-01-01T00:00:00 user=alice;retries=3 connection timeout
    """
    raise NotImplementedError("implement me")
