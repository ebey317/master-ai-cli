"""Tiny app using Config."""

from config import Config


def run_app(config_path):
    cfg = Config(config_path).load()
    return f"db={cfg.data.get('db_url')} timeout={cfg.data.get('timeout')}"
