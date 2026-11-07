"""Project configuration loader."""

from pathlib import Path


class Config:
    def __init__(self, path: Path):
        self.path = path
        self.data = {}

    def load(self):
        """Load key=value lines, ignoring comments and blanks."""
        for line in self.path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            self.data[key.strip()] = value.strip()
        return self
