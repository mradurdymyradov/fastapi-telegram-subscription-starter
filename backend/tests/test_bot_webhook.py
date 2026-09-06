from types import SimpleNamespace

import pytest

from app.bot import main as bot_main
from app.config import Settings


def webhook_config(**overrides):
    values = {
        "bot_update_mode": "webhook",
        "tg_webhook_base_url": "https://community.example.com/",
        "public_base_url": "https://fallback.example.com",
        "tg_webhook_path": "/tg-webhook/bot",
        "tg_webhook_secret": "secret",
        "is_prod": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_tg_webhook_url_uses_base_url_and_normalized_path():
    config = webhook_config(tg_webhook_path="tg-webhook/prod-bot")

    assert (
        bot_main._tg_webhook_url(config)
        == "https://community.example.com/tg-webhook/prod-bot"
    )


def test_tg_webhook_url_falls_back_to_public_base_url():
    config = webhook_config(tg_webhook_base_url="")

    assert bot_main._tg_webhook_url(config) == "https://fallback.example.com/tg-webhook/bot"


def test_tg_webhook_path_must_stay_under_caddy_route():
    config = webhook_config(tg_webhook_path="/telegram/bot")

    with pytest.raises(RuntimeError, match="TG_WEBHOOK_PATH"):
        bot_main._tg_webhook_path(config)


def test_webhook_mode_requires_secret_in_security_validation():
    settings = Settings(
        _env_file=None,
        app_env="prod",
        jwt_secret="x" * 32,
        admin_default_password="strong-password",
        bot_update_mode="webhook",
        public_base_url="https://community.example.com",
        tg_webhook_path="/tg-webhook/bot",
        tg_webhook_secret="",
    )

    assert "BOT_UPDATE_MODE=webhook requires TG_WEBHOOK_SECRET" in settings.validate_security()


def test_polling_mode_is_security_valid_without_webhook_secret():
    settings = Settings(
        _env_file=None,
        app_env="prod",
        jwt_secret="x" * 32,
        admin_default_password="strong-password",
        bot_update_mode="polling",
        public_base_url="https://community.example.com",
        tg_webhook_secret="",
    )

    assert "BOT_UPDATE_MODE=webhook requires TG_WEBHOOK_SECRET" not in settings.validate_security()


def test_security_validation_normalizes_webhook_path_slash():
    settings = Settings(
        _env_file=None,
        app_env="prod",
        jwt_secret="x" * 32,
        admin_default_password="strong-password",
        bot_update_mode="webhook",
        public_base_url="https://community.example.com",
        tg_webhook_path="tg-webhook/bot",
        tg_webhook_secret="secret",
    )

    assert "TG_WEBHOOK_PATH must start with /tg-webhook/" not in settings.validate_security()
