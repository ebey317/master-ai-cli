# Task 10

`PluginRegistry` uses a class-level mutable list, so separate instances share state and tests are flaky. Make each `PluginRegistry` instance independent while keeping the public API identical.

Repo: `repo/`
