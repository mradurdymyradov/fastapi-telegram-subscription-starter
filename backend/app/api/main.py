import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select, text
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from app.api.routers import (
    archive,
    audit,
    auth,
    broadcasts,
    crm_export,
    integrations,
    metrics,
    payments,
    plans,
    portal,
    promocodes,
    reconciliation,
    referrals,
    subscriptions,
    support,
    users,
    webhooks_in,
)
from app.api.routers import (
    config as config_router,
)
from app.config import get_settings
from app.config_audit import enforce_configuration
from app.db.models import AdminUser, Plan
from app.db.session import async_session
from app.observability import (
    bind_correlation_id,
    bind_log_context,
    clear_log_context,
    init_observability,
    ops_alert_config_status,
    seconds_since_last_heartbeat,
)
from app.services.security import hash_password

# Configure JSON logging + Sentry as early as possible so every subsequent
# logging.getLogger(...) call inherits the same handler.
init_observability(service="api")

logger = logging.getLogger(__name__)
settings = get_settings()


async def _bootstrap() -> None:
    # Fail-fast on misconfiguration BEFORE we touch the DB or serve traffic.
    errors = settings.validate_security()
    if errors:
        msg = "Security misconfiguration: " + " | ".join(errors)
        if settings.is_prod:
            raise RuntimeError(msg)
        logger.warning("STARTUP WARNING: %s (env=%s)", msg, settings.app_env)

    # GK-436: `validate_security` only knows about values it was told to look
    # at. This checks every setting the code reads against what the environment
    # declares — the class of drift that silently disabled Stripe cancellation.
    await enforce_configuration("api")

    async with async_session() as s:
        await _seed_default_admin(s)
        plan_count = (await s.execute(select(Plan))).scalars().first()
        if plan_count is None:
            s.add_all(
                [
                    Plan(
                        code="1m", name="1 месяц", description="Ежемесячная подписка", price_rub=1500, price_usd=19,
                        duration_days=30, sort_order=10,
                    ),
                    Plan(
                        code="6m", name="6 месяцев", description="Полугодовой тариф", price_rub=7000, price_usd=79,
                        duration_days=180, sort_order=20,
                    ),
                    Plan(
                        code="12m", name="12 месяцев", description="Годовой тариф", price_rub=10000, price_usd=129,
                        duration_days=365, sort_order=30,
                    ),
                ]
            )
        await s.commit()


#: Printed verbatim when a deployment has no admin at all. It has to be
#: copy-pasteable at the moment somebody is locked out, which is not the moment
#: to go looking for a runbook.
CREATE_ADMIN_COMMAND = (
    "docker compose -p membership_saas exec -T api python - <<'PY'\n"
    "import asyncio\n"
    "from app.db.models import AdminUser\n"
    "from app.db.session import async_session\n"
    "from app.services.security import hash_password\n"
    "async def main():\n"
    "    async with async_session() as s:\n"
    "        s.add(AdminUser(email='you@example.com',\n"
    "                        password_hash=hash_password('<a strong password>'),\n"
    "                        role='owner'))\n"
    "        await s.commit()\n"
    "asyncio.run(main())\n"
    "PY"
)


async def _seed_default_admin(s) -> None:
    """Create the ADMIN_DEFAULT_* owner — but only if this deployment asked for one.

    GK-442: this used to be unconditional, which made the seed account's own
    credentials unremovable. Deleting it succeeded and was undone by the next
    API start, and the two `ADMIN_DEFAULT_*` keys could not be taken out of the
    environment either, because GK-436's guard listed them as always-required
    and refused to boot without them. Two sensible mechanisms, one deadlock.

    With `SEED_DEFAULT_ADMIN` off — the default — nothing is created and neither
    value is read. The one thing that must not happen quietly is a deployment
    with no way in at all, so an empty `admin_users` says so at ERROR level with
    the command that fixes it.
    """
    if not settings.seed_default_admin:
        any_admin = (await s.execute(select(AdminUser.id).limit(1))).scalar_one_or_none()
        if any_admin is None:
            logger.error(
                "No admin accounts exist and SEED_DEFAULT_ADMIN is off, so nobody "
                "can sign in to the panel. Either set SEED_DEFAULT_ADMIN=true with "
                "ADMIN_DEFAULT_EMAIL/ADMIN_DEFAULT_PASSWORD for a first boot, or "
                "create one now:\n%s",
                CREATE_ADMIN_COMMAND,
            )
        return

    admin = (
        await s.execute(select(AdminUser).where(AdminUser.email == settings.admin_default_email))
    ).scalar_one_or_none()
    if admin is not None:
        return

    # Defence-in-depth: never seed the demo password unless explicitly allowed.
    if settings.admin_default_password == "demo1234" and not settings.allow_default_admin_password:
        logger.error(
            "Refusing to seed admin %s with the demo password. "
            "Set ADMIN_DEFAULT_PASSWORD to a strong value, or "
            "ALLOW_DEFAULT_ADMIN_PASSWORD=true in demo env.",
            settings.admin_default_email,
        )
        return

    s.add(
        AdminUser(
            email=settings.admin_default_email,
            password_hash=hash_password(settings.admin_default_password),
            role="owner",
        )
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    await _bootstrap()
    yield


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Defence-in-depth headers. Caddy also sets these; double-cover."""

    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
        response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        return response


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Bind a correlation id to every request for the duration of the handler.

    Honors an incoming `X-Request-Id` (set by Caddy/uptime checks) when
    present; otherwise mints a fresh hex id. Echoed back in the response so
    clients/log shippers can join request, response, and Sentry events.
    """

    async def dispatch(self, request: Request, call_next):
        incoming = request.headers.get("x-request-id") or request.headers.get("x-correlation-id")
        cid = bind_correlation_id(incoming if incoming and len(incoming) <= 64 else None)
        request.state.correlation_id = cid
        start = time.monotonic()
        try:
            response: Response = await call_next(request)
            response.headers["X-Request-Id"] = cid
            # Single structured access line per request — separate from uvicorn's
            # default text log so JSON consumers see latency + path + status.
            # Bind the fields to the structlog context (NOT stdlib `extra=`,
            # which ProcessorFormatter does not render) and emit the line while
            # the correlation id is still in scope, so the JSON carries
            # correlation_id + method/path/status/duration.
            bind_log_context(
                http_method=request.method,
                http_path=request.url.path,
                http_status=response.status_code,
                duration_ms=round((time.monotonic() - start) * 1000.0, 2),
            )
            logger.info("http_request")
            return response
        finally:
            clear_log_context()


def _cors_origins() -> list[str]:
    raw = settings.cors_allowed_origins.strip()
    if not raw:
        return [settings.admin_base_url] if settings.admin_base_url else []
    if raw == "*":
        # Forbidden in prod by validate_security; in dev still allow.
        return ["*"]
    return [o.strip() for o in raw.split(",") if o.strip()]


app = FastAPI(title="membership_saas Admin API", lifespan=lifespan)
# Order matters: CorrelationId wraps every other middleware so its log
# binding is in scope when SecurityHeaders / CORS run.
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(CorrelationIdMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

API = "/api"
for r in (
    auth,
    metrics,
    users,
    subscriptions,
    payments,
    referrals,
    broadcasts,
    config_router,
    crm_export,
    integrations,
    plans,
    promocodes,
    support,
    audit,
    portal,
    archive,
    reconciliation,
):
    app.include_router(r.router, prefix=API)
app.include_router(webhooks_in.router)  # /webhooks/stripe, /webhooks/lava — at root


@app.get("/health")
async def health() -> JSONResponse:
    """Liveness + lightweight readiness signal.

    Always returns 200 unless the DB ping fails or the bot heartbeat is
    older than `BOT_HEARTBEAT_MAX_AGE_SECONDS`. External monitors page on
    non-200; humans get the JSON for triage.
    """
    db_ok = True
    db_error: str | None = None
    try:
        async with async_session() as s:
            await s.execute(text("SELECT 1"))
    except Exception as e:  # noqa: BLE001
        db_ok = False
        db_error = type(e).__name__

    bot_age = await seconds_since_last_heartbeat()
    bot_stale = (
        bot_age is None or bot_age > settings.bot_heartbeat_max_age_seconds
    )
    ops_alerts = ops_alert_config_status(settings)
    ops_alerts_required = not settings.is_dev
    ops_alerts_ok = ops_alerts["configured"] or not ops_alerts_required

    body = {
        "status": "ok" if db_ok and not bot_stale and ops_alerts_ok else "degraded",
        "service": "api",
        "env": settings.app_env,
        "db": {"ok": db_ok, "error": db_error},
        "bot": {
            "heartbeat_age_seconds": bot_age,
            "max_age_seconds": settings.bot_heartbeat_max_age_seconds,
            "stale": bot_stale,
        },
        "ops_alerts": {
            **ops_alerts,
            "required": ops_alerts_required,
        },
    }
    return JSONResponse(
        body,
        status_code=200 if db_ok and not bot_stale and ops_alerts_ok else 503,
    )
