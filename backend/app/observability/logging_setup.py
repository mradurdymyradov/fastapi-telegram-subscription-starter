"""Structured JSON logging + Sentry bootstrap for API and bot.

We use structlog as the front-end and stdlib logging as the backend so that
every third-party log (uvicorn access, aiogram, SQLAlchemy, APScheduler,
Stripe webhooks, etc.) ends up in the same JSON stream and carries the same
context (`correlation_id`, `service`, `event_id` when bound).

Sentry is initialized once with the same `release`/`environment` for both
services. Missing DSN is a no-op — local dev does not need Sentry.
"""

from __future__ import annotations

import logging
import sys
import uuid
from contextvars import ContextVar

import structlog

# ContextVar so async tasks/FastAPI requests/aiogram handlers each get their
# own correlation id without locking. structlog's `merge_contextvars`
# processor pulls everything from the same `structlog.contextvars` store.
_CORRELATION_ID: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def get_correlation_id() -> str | None:
    return _CORRELATION_ID.get()


def bind_correlation_id(value: str | None = None) -> str:
    """Set correlation_id for the current async context and structlog binding.

    Returns the id actually used (generated if not provided)."""
    cid = value or uuid.uuid4().hex
    _CORRELATION_ID.set(cid)
    structlog.contextvars.bind_contextvars(correlation_id=cid)
    return cid


def bind_log_context(**kwargs: object) -> None:
    """Bind arbitrary key-value pairs to the structlog context for this task.

    Use sparingly: `provider`, `event_id`, `user_id`, `payment_id`. Anything
    bound here shows up on every JSON line emitted for the same async task.
    """
    structlog.contextvars.bind_contextvars(**kwargs)


def clear_log_context() -> None:
    structlog.contextvars.clear_contextvars()
    _CORRELATION_ID.set(None)


def _correlation_id_processor(_, __, event_dict: dict) -> dict:
    if "correlation_id" not in event_dict:
        cid = _CORRELATION_ID.get()
        if cid is not None:
            event_dict["correlation_id"] = cid
    return event_dict


def _service_processor(service: str):
    def add_service(_, __, event_dict: dict) -> dict:
        event_dict.setdefault("service", service)
        return event_dict

    return add_service


def _configure_structlog(service: str, level: int, json_output: bool) -> None:
    processors: list = [
        structlog.contextvars.merge_contextvars,
        _correlation_id_processor,
        _service_processor(service),
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if json_output:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=False))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Route stdlib logging through the same handler. Existing modules use
    # logging.getLogger(__name__); they'll inherit JSON output for free.
    formatter = structlog.stdlib.ProcessorFormatter(
        processor=(
            structlog.processors.JSONRenderer()
            if json_output
            else structlog.dev.ConsoleRenderer(colors=False)
        ),
        foreign_pre_chain=[
            structlog.contextvars.merge_contextvars,
            _correlation_id_processor,
            _service_processor(service),
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    # Replace existing handlers so we don't double-emit when uvicorn
    # configures its own basicConfig in reload mode.
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(level)

    # Quiet down chatty libraries at INFO; their WARN+ still flows through.
    # aiogram.event stays at INFO deliberately (GK-350): its per-update
    # "Update id=... is handled/not handled" lines are the only way to tell a
    # silently-ignored update from one that never arrived.
    for noisy in ("httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))


def _init_sentry(settings, service: str) -> None:
    if not settings.sentry_dsn:
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
        from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
    except Exception:  # pragma: no cover - sentry_sdk not installed in dev
        logging.getLogger(__name__).warning(
            "sentry_sdk import failed; SENTRY_DSN set but Sentry disabled"
        )
        return

    integrations = [
        LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
        SqlalchemyIntegration(),
    ]
    if service == "api":
        integrations.extend([StarletteIntegration(), FastApiIntegration()])

    try:
        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            environment=settings.app_env,
            release=settings.sentry_release or None,
            traces_sample_rate=settings.sentry_traces_sample_rate,
            profiles_sample_rate=0.0,
            send_default_pii=False,
            default_integrations=False,
            integrations=integrations,
        )
    except ImportError as exc:
        if service != "api" or "jinja2" not in str(exc).lower():
            raise
        logging.getLogger(__name__).warning(
            "sentry api integrations need optional jinja2; retrying with minimal integrations"
        )
        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            environment=settings.app_env,
            release=settings.sentry_release or None,
            traces_sample_rate=settings.sentry_traces_sample_rate,
            profiles_sample_rate=0.0,
            send_default_pii=False,
            default_integrations=False,
            integrations=[
                LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
                SqlalchemyIntegration(),
            ],
        )
    sentry_sdk.set_tag("service", service)


def init_observability(service: str) -> None:
    """Bootstrap logging + Sentry for the given service ("api" or "bot").

    Idempotent: safe to call multiple times. Reads settings via
    `app.config.get_settings()` so env overrides are picked up.
    """
    # Import here to avoid a circular when this module is imported from
    # app.config-adjacent code.
    from app.config import get_settings

    settings = get_settings()
    level_name = (settings.log_level or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    json_output = settings.log_format.lower() == "json"

    _configure_structlog(service, level=level, json_output=json_output)
    _init_sentry(settings, service=service)
