"""Runtime config."""


class RuntimeConfig:
    def __init__(self, name):
        self.name = name
        self.greeting = "Hello"

    def message(self):
        return f"{self.greeting}, {self.name}!"
