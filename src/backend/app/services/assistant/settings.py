from app.core.config import config


def settings():
    return getattr(config, "assistant", {})


def enabled():
    return settings().get("enabled", False) is True
