"""Runtime config."""


class RuntimeConfig:
    def __init__(self, name, shout=False):
        self.name = name
        self.shout = shout
        self.greeting = "Hello"

    def message(self):
        msg = f"{self.greeting}, {self.name}!"
        return msg.upper() if self.shout else msg
