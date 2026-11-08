"""Plugin registry."""


class PluginRegistry:
    def __init__(self):
        self._plugins = []

    def register(self, name):
        self._plugins.append(name)

    def list(self):
        return self._plugins
