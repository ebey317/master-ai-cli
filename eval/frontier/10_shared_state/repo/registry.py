"""Plugin registry."""


class PluginRegistry:
    _plugins = []  # BUG: mutable class-level state

    def __init__(self):
        pass

    def register(self, name):
        self._plugins.append(name)

    def list(self):
        return self._plugins
