"""Project configuration loader."""

from pathlib import Path


class Config:
    def __init__(self, path: Path):
        self.path = path
        self.data = {}

    def load(self):
        """Load key=value lines, ignoring comments and blanks."""

        def _coerce(v):
            v = v.strip()
            low = v.lower()
            if low == "true":
                return True
            if low == "false":
                return False
            try:
                return int(v)
            except ValueError:
                pass
            try:
                return float(v)
            except ValueError:
                pass
            return v

        for line in self.path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            self.data[key.strip()] = _coerce(value)
        return self
