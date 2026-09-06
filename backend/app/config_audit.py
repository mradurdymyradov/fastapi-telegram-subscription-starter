"""GK-436: configuration drift is silent, and the silence broke a live feature.

`ENABLE_STRIPE_AUTORENEW_CANCELLATION` was never written into the server's
`.env`. Pydantic filled in the `False` default, the code took the honest
fallback branch, and every Stripe member who pressed «Отменить автопродление»
landed in the manual queue with `cancel_failure_reason = "Stripe autorenew
cancellation is not enabled for this deployment"`. Nothing logged at startup,
nothing alerted, nothing in the panel said the deployment disagreed with the
release notes. The defect was only visible at the moment a member pressed a
button — sixteen days after it shipped.

The patch was one line in a file. The guard is this module: every setting the
code reads is enumerated from `Settings` itself (so the list cannot drift from
the code), checked against what the environment actually declares, and the
findings are logged and alerted **at startup**.

Three distinct failure shapes, because they fail differently:

1. **missing_required** — a setting with no usable default is absent.
2. **undeclared** — the setting is absent and the default silently changes
   behaviour. A feature flag that is off *by omission* is indistinguishable at
   runtime from one that is off *by decision*; only one of those is a bug, and
   only the environment file knows which. Every `enable_*` flag is in this
   class by construction.
3. **coupling** — a capability is configured but a setting it depends on is
   missing or empty. `STRIPE_SECRET_KEY` present while
   `ENABLE_STRIPE_AUTORENEW_CANCELLATION` is undeclared is exactly the shape of
   the original bug, and it is a rule here.

Plus **orphan**: a key that looks like ours, is set in the environment, and is
read by nothing. A typo in a flag name is as silent as an absent one.

Run it by hand against any environment:

    python -m app.config_audit            # exit 1 if any error
    python -m app.config_audit --strict   # exit 1 if any warning too
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

ERROR = "error"
WARNING = "warning"
INFO = "info"

_SEVERITY_ORDER = {ERROR: 0, WARNING: 1, INFO: 2}


# ---------------------------------------------------------------------------
# what "absent" costs, per setting
# ---------------------------------------------------------------------------

# No usable default. Absent means the deployment is broken, not degraded.
REQUIRED_ALWAYS: frozenset[str] = frozenset(
    {
        "APP_ENV",
        "BOT_TOKEN",
        "JWT_SECRET",
        "DB_URL_ASYNC",
        "DB_URL_SYNC",
        "REDIS_URL",
        "PUBLIC_BASE_URL",
        "ADMIN_BASE_URL",
        "PORTAL_BASE_URL",
        # GK-442: ADMIN_DEFAULT_EMAIL / ADMIN_DEFAULT_PASSWORD used to be here.
        # Always-required plus an unconditional bootstrap meant the seed account
        # could not be deleted (the next API start recreated it) and the keys
        # could not be removed either (the audit refused to boot without them).
        # They are now a coupling: required exactly when SEED_DEFAULT_ADMIN is on.
    }
)

# Defaults exist and are safe for a laptop, never for a server.
REQUIRED_OUTSIDE_DEV: frozenset[str] = frozenset(
    {
        "PRIVATE_CHANNEL_ID",
        "PRACTICE_CHAT_ID",
        # An unset alert chat is why a failing scheduler, a failed backup and a
        # refused cancellation all went to the same place: nowhere.
        "ALERT_CHAT_ID",
    }
)

# Absent ≠ off-by-decision. These must appear in the environment file even when
# the value equals the default, so that "we chose this" is written down
# somewhere a human can read. Every `enable_*` flag is added automatically.
MUST_BE_DECLARED: frozenset[str] = frozenset(
    {
        "APP_ENV",
        "BOT_UPDATE_MODE",
        "SUPPORT_AI_PROVIDER",
        "LAVA_WEBHOOK_AUTH_MODE",
        "ALLOW_DEFAULT_ADMIN_PASSWORD",
        # GK-442. Whether a deployment creates an owner account out of the
        # environment file is not something to leave to a built-in default:
        # off-by-omission and off-by-decision differ by whether anyone can log
        # in on first boot.
        "SEED_DEFAULT_ADMIN",
    }
)

# Settings that were removed from the code. An operator whose `.env` still
# carries one deserves to be told it does nothing, rather than to see it in the
# orphan list next to their typos and guess which is which.
RETIRED_SETTINGS: dict[str, str] = {
    "STRIPE_TEST_MODE": (
        "removed in GK-436 — nothing ever read it. Test vs live is decided by "
        "whether STRIPE_SECRET_KEY is sk_test_ or sk_live_, and by nothing else."
    ),
    "REFERRAL_BONUS_DAYS": (
        "removed in GK-436 — the referral programme pays a ledger commission, "
        "not bonus days. Changing this value has had no effect for months."
    ),
    "REFERRAL_BONUS_REFEREE_DAYS": (
        "removed in GK-436 — same reason as REFERRAL_BONUS_DAYS."
    ),
    "ENABLE_LAVA_LIVE_REFUND": (
        "removed in GK-445 — it gated nothing that exists. Lava publishes no "
        "refund endpoint and LavaProvider has no refund method, so the flag's "
        "only effect was to make the admin panel offer an automatic Lava refund "
        "that answered HTTP 400 every time. Lava refunds are manual: refund in "
        "the Lava dashboard, then confirm it in the panel."
    ),
}

# Read by docker-compose, Caddy, the backup sidecar or the Next.js builds —
# never by `Settings`. Without this list every one of them reads as an orphan.
# `test_config_audit.py::test_non_settings_consumers_matches_compose` keeps it
# honest against the actual compose/Caddy files.
NON_SETTINGS_CONSUMERS: frozenset[str] = frozenset(
    {
        "DOMAIN",
        "PORTAL_DOMAIN",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DB",
        "POSTGRES_HOST",
        "PORTAL_COOKIE_SECURE",
        # Internal API base for the Next.js apps' server-side fetches. Set by
        # compose, read via process.env in admin/ and portal/ — never by the
        # backend, so `Settings` has no field for it. Found by CI rather than
        # locally: the test only sees the frontends when the whole repo is in
        # the build context.
        "BACKEND_URL",
        "ADMIN_SENTRY_DSN",
        "PORTAL_SENTRY_DSN",
        "SENTRY_ORG",
        "SENTRY_ADMIN_PROJECT",
        "SENTRY_PORTAL_PROJECT",
        "SENTRY_AUTH_TOKEN",
        "BACKUP_AGE_PUBLIC_KEY",
        "BACKUP_AGE_IDENTITY_FILE",
        "BACKUP_DIR",
        "BACKUP_RCLONE_REMOTE",
        # Referenced by compose as ${BACKUP_RCLONE_CONF:-…}, not by any script,
        # so a grep of backup/*.sh will not find it. GK-427.
        "BACKUP_RCLONE_CONF",
        "BACKUP_RETENTION_DAYS",
        # Remote pruning, read only by backup.sh. Deliberately optional: unset
        # means "never touch the remote", which is correct for S3/R2/B2 where a
        # bucket lifecycle rule does the job storage-side. GK-427.
        "BACKUP_REMOTE_RETENTION_DAYS",
        "BACKUP_INTERVAL_SECONDS",
        "BACKUP_HEALTHCHECK_URL",
        "BACKUP_ALERT_WEBHOOK_URL",
        # GK-437 restore canary. Read by verify.sh / verify-entrypoint.sh in the
        # backup image, and by compose for the key mount. Note the pair:
        # BACKUP_AGE_IDENTITY_HOST_FILE says where the private key lives on the
        # host, BACKUP_AGE_IDENTITY_FILE is its fixed path inside the container.
        "BACKUP_AGE_IDENTITY_HOST_FILE",
        "BACKUP_VERIFY_DB",
        "BACKUP_VERIFY_MAX_AGE_HOURS",
        "BACKUP_VERIFY_INTERVAL_SECONDS",
        "BACKUP_VERIFY_INITIAL_DELAY_SECONDS",
    }
)

# Anything matching one of these prefixes is ours; an unknown key with one of
# them is a typo, not somebody else's variable.
OURS_PREFIXES: tuple[str, ...] = (
    # GK-439. `ACCESS_START_FLOOR` is deliberately NOT in MUST_BE_DECLARED: it
    # is a launch-window mechanism whose correct state after launch is absent,
    # so warning about its absence forever would train people to ignore the
    # warning. What is worth catching is a typo — `ACCESS_START_FLOR=2026-09-01`
    # reads as no floor at all and would sell August access from August.
    "ACCESS_",
    "ADMIN_",
    "ALERT_",
    "ALLOW_",
    "ANTHROPIC_",
    "APP_",
    "BACKUP_",
    "BOT_",
    "CORS_",
    "DB_URL",
    "ENABLE_",
    "ETHERSCAN_",
    # GK-460. Not in MUST_BE_DECLARED for the same reason as the two above: its
    # correct state is absent right up until removals are armed, and a warning
    # that is correct for months is a warning nobody reads by the time it
    # matters. The prefix is here for the typo — EXPIRY_REMOVALS_EXPECT_CHAT
    # reads as an empty binding, which refuses the sweep rather than aiming it
    # wrongly, but refuses it silently to whoever thought they had armed it.
    "EXPIRY_",
    "GIFT_",
    "GOOGLE_SHEETS_",
    "JWT_",
    "LAVA_",
    "LOGIN_RATE",
    # GK-486. The typo this catches is the dangerous direction: NO_CHARGE_TG_ID
    # (singular) or NO_CHARGE_IDS reads as "nobody is protected", the bot sells
    # to the accounts it was configured to shield, and there is no symptom until
    # somebody's card is charged. Unlike PRELAUNCH_ above, a typo here fails
    # *unsafe*, which is precisely why the prefix earns its place.
    "NO_CHARGE",
    "OPENAI_",
    "OUTGOING_",
    "PORTAL_",
    "PRACTICE_",
    # GK-446. Like ACCESS_START_FLOOR this is deliberately NOT in
    # MUST_BE_DECLARED — its correct state after launch is absent. The prefix is
    # here for the typo: PRELAUNCH_HOLD_ALLOWLST reads as an empty allowlist,
    # which fails safe but silently, and the person it was meant to admit finds
    # out by meeting the hold.
    "PRELAUNCH_",
    "PRIVATE_CHANNEL",
    "PUBLIC_BASE",
    "REFERRAL_",
    "SENTRY_",
    "STRIPE_",
    "SUPPORT_",
    "TG_WEBHOOK",
    "TRONGRID_",
    "USDT_",
    "VIMEO_",
    "ZELLE_",
)


@dataclass(frozen=True)
class Coupling:
    """"If X is configured then Y must be too" — the class of bug that hid
    behind a working Stripe integration for sixteen days."""

    name: str
    when: Callable[[Settings], bool]
    why: str
    # Must be present in the environment at all (declared, any value).
    declares: tuple[str, ...] = ()
    # Must be present AND non-empty.
    requires: tuple[str, ...] = ()
    # GK-479: at least one of these must be non-empty. A capability with two
    # interchangeable ways to supply the same secret — a path or the value
    # inline — cannot use `requires`, which demands all of them and so turns
    # the unused alternative into a permanent warning about a working
    # deployment. The docstring above says what that costs.
    requires_any: tuple[str, ...] = ()
    severity: str = ERROR


def _truthy(value: Any) -> bool:
    return bool(str(value or "").strip())


COUPLINGS: tuple[Coupling, ...] = (
    Coupling(
        name="seed_default_admin_on",
        when=lambda s: s.seed_default_admin,
        why=(
            "GK-442: seeding is on, so the two values it seeds from must be real. "
            "They are required here rather than always, because with seeding off "
            "nothing reads them and a live deployment should be able to delete the "
            "seed account and the credentials that recreate it."
        ),
        # Both lists, deliberately. The built-in defaults are the demo pair
        # (`admin@example.com` / `demo1234`), which are truthy — so
        # `requires` alone would pass on an absent key and seed the demo account.
        # `declares` catches absent, `requires` catches declared-and-empty.
        declares=("ADMIN_DEFAULT_EMAIL", "ADMIN_DEFAULT_PASSWORD"),
        requires=("ADMIN_DEFAULT_EMAIL", "ADMIN_DEFAULT_PASSWORD"),
    ),
    Coupling(
        name="stripe_configured",
        when=lambda s: _truthy(s.stripe_secret_key),
        why=(
            "Stripe is wired up, so every Stripe-gated capability must be an "
            "explicit yes or no in the environment file. This is the exact rule "
            "that would have caught GK-433: the key was set, the cancellation "
            "flag was absent, and the feature silently no-opped."
        ),
        declares=("ENABLE_STRIPE_AUTORENEW_CANCELLATION",),
        requires=(
            "STRIPE_WEBHOOK_SECRET",
            "STRIPE_PRICE_MONTHLY_ID",
            "STRIPE_PRICE_6M_ID",
            "STRIPE_PRICE_ANNUAL_ID",
        ),
    ),
    Coupling(
        name="lava_configured",
        when=lambda s: _truthy(s.lava_api_key),
        why=(
            "Same shape as Stripe: an API key present with the capability flags "
            "absent means nobody wrote down whether live checkout and "
            "cancellation are meant to be on."
        ),
        # GK-445: ENABLE_LAVA_LIVE_REFUND was here too. Demanding a declaration
        # of a capability Lava does not offer only teaches people to write a
        # value for it; it is retired instead.
        declares=(
            "ENABLE_LAVA_LIVE_CHECKOUT",
            "ENABLE_LAVA_AUTORENEW_CANCELLATION",
            "ENABLE_LAVA_RUB_OFFER_PAGE",
        ),
    ),
    Coupling(
        name="lava_live_checkout_on",
        when=lambda s: s.enable_lava_live_checkout,
        why="Live checkout takes real money; the offer and the webhook key are not optional.",
        requires=("LAVA_OFFER_ID", "LAVA_API_KEY"),
    ),
    Coupling(
        name="expiry_removals_armed",
        when=lambda s: s.enable_expiry_removals,
        why=(
            "GK-460: the hourly ban+unban is armed, so the chats it may reach "
            "must be named. Armed with an empty binding is the worst of the "
            "three states — the operator believes removals run, and they do not."
        ),
        requires=("EXPIRY_REMOVALS_EXPECT_CHATS",),
    ),
    Coupling(
        name="ai_support_on",
        when=lambda s: s.enable_ai_support,
        why="AI support is on but no provider key is set — every reply would fail at request time.",
        requires=("OPENAI_API_KEY",),
        severity=WARNING,
    ),
    Coupling(
        name="zelle_on",
        when=lambda s: s.enable_zelle,
        why="Zelle is visible to buyers; the recipient must be a real address, not the placeholder.",
        requires=("ZELLE_RECIPIENT",),
    ),
    Coupling(
        name="sentry_configured",
        when=lambda s: _truthy(s.sentry_dsn),
        why=(
            "Sentry is receiving events but SENTRY_RELEASE is empty, so every "
            "event lands with no version attached and no way to tell whether a "
            "deploy fixed it."
        ),
        requires=("SENTRY_RELEASE",),
        severity=WARNING,
    ),
    Coupling(
        name="webhook_mode",
        when=lambda s: (s.bot_update_mode or "").strip().lower() == "webhook",
        why="Webhook mode without a secret accepts unauthenticated updates.",
        requires=("TG_WEBHOOK_SECRET",),
    ),
    Coupling(
        name="google_sheets_configured",
        when=lambda s: _truthy(s.google_sheets_spreadsheet_id),
        why=(
            "A spreadsheet id with no service-account credentials exports "
            "nothing. Either variable satisfies this; GK-479 moved the live "
            "host to the inline JSON because a key file stored inside a release "
            "directory is deleted by the next deploy."
        ),
        requires_any=(
            "GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON",
            "GOOGLE_SHEETS_SERVICE_ACCOUNT_FILE",
        ),
        severity=WARNING,
    ),
    Coupling(
        name="erc20_offered",
        when=lambda s: _truthy(s.usdt_erc20_address),
        why=(
            "GK-431: the ERC20 address is advertised to buyers, so the explorer "
            "key that verifies their transaction must exist. Without it every "
            "ERC20 payment falls back to a manual check nobody is watching."
        ),
        requires=("ETHERSCAN_API_KEY",),
        severity=WARNING,
    ),
)


# ---------------------------------------------------------------------------
# the audit
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    env: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.severity.upper()}] {self.env}: {self.detail}"


def settings_env_names(model: type[Settings] = Settings) -> dict[str, str]:
    """Every setting the code can read, as ENV_NAME -> field name.

    Derived from the model, never hand-maintained: adding a field to
    `Settings` puts it under the guard on the same commit.
    """
    prefix = (model.model_config.get("env_prefix") or "").upper()
    return {f"{prefix}{name.upper()}": name for name in model.model_fields}


def _flag_names(env_names: Iterable[str]) -> frozenset[str]:
    return frozenset(name for name in env_names if name.startswith("ENABLE_"))


def declared_keys(
    environ: Mapping[str, str] | None = None,
    dotenv_path: str | os.PathLike[str] | None = None,
) -> frozenset[str]:
    """Keys the operator actually wrote down.

    On the server compose injects `env_file:` into the process environment, so
    `os.environ` is the whole truth. On a laptop pydantic also reads a `.env`
    next to the CWD, and a key written there is just as declared.
    """
    keys = set((environ if environ is not None else os.environ).keys())
    if dotenv_path is None:
        configured = Settings.model_config.get("env_file")
        dotenv_path = configured if isinstance(configured, str) else None
    if dotenv_path:
        path = Path(dotenv_path)
        if path.is_file():
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                keys.add(line.split("=", 1)[0].strip().upper())
    return frozenset(keys)


def audit_configuration(
    settings: Settings | None = None,
    environ: Mapping[str, str] | None = None,
    dotenv_path: str | os.PathLike[str] | None = None,
) -> list[Finding]:
    """Compare what the code reads against what the environment declares."""
    settings = settings if settings is not None else get_settings()
    known = settings_env_names(type(settings))
    declared = declared_keys(environ, dotenv_path)
    must_declare = MUST_BE_DECLARED | _flag_names(known)
    findings: list[Finding] = []

    def value_of(env_name: str) -> Any:
        field = known.get(env_name)
        return getattr(settings, field, None) if field else None

    for env_name in sorted(known):
        is_declared = env_name in declared
        # Declared-and-empty is not better than absent: an `ALERT_CHAT_ID=` line
        # sends exactly as many alerts as no line at all.
        required = env_name in REQUIRED_ALWAYS or (
            env_name in REQUIRED_OUTSIDE_DEV and not settings.is_dev
        )
        # A required setting must be written down AND have a value. The built-in
        # default for these is a laptop placeholder ("http://localhost:8000"),
        # which is truthy and useless — so an undeclared one is still an error.
        if required and (not is_declared or not _truthy(value_of(env_name))):
            findings.append(
                Finding(
                    ERROR,
                    "missing_required",
                    env_name,
                    (
                        "declared but empty; the built-in default is a placeholder, not a value"
                        if is_declared
                        else "not set anywhere; the built-in default is a placeholder, not a value"
                    )
                    + ("" if env_name in REQUIRED_ALWAYS else f" (app_env={settings.app_env})"),
                )
            )
            continue
        if is_declared:
            continue
        if env_name in must_declare:
            findings.append(
                Finding(
                    WARNING,
                    "undeclared",
                    env_name,
                    f"not set anywhere — running on the built-in default "
                    f"{value_of(env_name)!r}. Absent and off-by-decision look "
                    f"identical at runtime; write it down.",
                )
            )

    # GK-479: a `*_FILE` setting is a promise that a file is there. The Google
    # Sheets key was mounted from inside a release directory, so the very next
    # deploy deleted it while leaving the setting pointing at it — configured,
    # green in the panel, and dead for ten weeks. A path is only configuration
    # if it resolves; otherwise it is drift, and this is the cheapest place to
    # notice. A warning rather than an error: a missing optional key must not
    # keep the whole API from booting.
    for env_name in sorted(known):
        if not env_name.endswith("_FILE"):
            continue
        raw = str(value_of(env_name) or "").strip()
        if not raw:
            continue
        try:
            present = Path(raw).is_file()
        except OSError:
            present = False
        if not present:
            findings.append(
                Finding(
                    WARNING,
                    "missing_file",
                    env_name,
                    f"points at {raw!r}, which is not a file in this container. "
                    "Whatever reads it will fail at the moment somebody uses the "
                    "feature, not now — a secret kept inside a release directory "
                    "is deleted by the next deploy.",
                )
            )

    for coupling in COUPLINGS:
        try:
            active = bool(coupling.when(settings))
        except Exception:  # noqa: BLE001 - a broken predicate must not hide the rest
            logger.warning("config coupling %s failed to evaluate", coupling.name, exc_info=True)
            continue
        if not active:
            continue
        for env_name in coupling.declares:
            if env_name not in declared:
                findings.append(
                    Finding(
                        coupling.severity,
                        "coupling",
                        env_name,
                        f"{coupling.name}: not declared. {coupling.why}",
                    )
                )
        for env_name in coupling.requires:
            if not _truthy(value_of(env_name)):
                findings.append(
                    Finding(
                        coupling.severity,
                        "coupling",
                        env_name,
                        f"{coupling.name}: missing or empty. {coupling.why}",
                    )
                )
        if coupling.requires_any and not any(
            _truthy(value_of(env_name)) for env_name in coupling.requires_any
        ):
            findings.append(
                Finding(
                    coupling.severity,
                    "coupling",
                    coupling.requires_any[0],
                    f"{coupling.name}: none of "
                    f"{', '.join(coupling.requires_any)} is set. {coupling.why}",
                )
            )

    for env_name in sorted(declared):
        if env_name in known or env_name in NON_SETTINGS_CONSUMERS:
            continue
        if env_name in RETIRED_SETTINGS:
            findings.append(
                Finding(WARNING, "retired", env_name, f"safe to delete: {RETIRED_SETTINGS[env_name]}")
            )
        elif env_name.startswith(OURS_PREFIXES):
            findings.append(
                Finding(
                    WARNING,
                    "orphan",
                    env_name,
                    "set in the environment but read by nothing — a misspelled "
                    "setting is as silent as an absent one",
                )
            )

    findings.sort(key=lambda f: (_SEVERITY_ORDER[f.severity], f.code, f.env))
    return findings


def has_errors(findings: Iterable[Finding]) -> bool:
    return any(f.severity == ERROR for f in findings)


def format_report(findings: list[Finding], settings: Settings | None = None) -> str:
    settings = settings if settings is not None else get_settings()
    errors = [f for f in findings if f.severity == ERROR]
    warnings = [f for f in findings if f.severity == WARNING]
    header = (
        f"Configuration audit (app_env={settings.app_env}): "
        f"{len(errors)} error(s), {len(warnings)} warning(s), "
        f"{len(settings_env_names(type(settings)))} settings read by the code"
    )
    if not findings:
        return header + "\nNo drift."
    return "\n".join([header, *(f"  {f}" for f in findings)])


def alert_text(findings: list[Finding], service: str, settings: Settings) -> str:
    import html

    errors = [f for f in findings if f.severity == ERROR]
    warnings = [f for f in findings if f.severity == WARNING]
    lines = [
        f"⚙️ КОНФИГУРАЦИЯ РАСХОДИТСЯ С КОДОМ ({html.escape(service)}, env={settings.app_env})",
        f"{len(errors)} ошибок, {len(warnings)} предупреждений.",
        "",
    ]
    for finding in findings[:15]:
        lines.append(f"• <code>{html.escape(finding.env)}</code> — {html.escape(finding.detail)}")
    if len(findings) > 15:
        lines.append(f"• …и ещё {len(findings) - 15}")
    lines += ["", "Файл: /opt/membership_saas/current/deploy/.env"]
    return "\n".join(lines)


async def enforce_configuration(
    service: str,
    settings: Settings | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[Finding]:
    """Startup gate. Log everything, alert once, then refuse to run if broken.

    The alert is sent *before* the raise on purpose: a process that dies on a
    bad config must still manage to say why, or we are back to a defect that is
    only visible when a member presses a button.
    """
    settings = settings if settings is not None else get_settings()
    findings = audit_configuration(settings, environ)

    for finding in findings:
        log = logger.error if finding.severity == ERROR else logger.warning
        log(
            "config drift: %s %s — %s",
            finding.code,
            finding.env,
            finding.detail,
            extra={"config_env": finding.env, "config_code": finding.code},
        )
    if not findings:
        logger.info("config audit clean (%s, env=%s)", service, settings.app_env)
        return findings

    try:
        from app.observability import send_ops_alert

        await send_ops_alert(
            alert_text(findings, service, settings),
            key=f"config_audit:{service}",
            rate_limit_seconds=3600,
            severity=ERROR if has_errors(findings) else WARNING,
        )
    except Exception:  # noqa: BLE001 - alerting must never be the thing that crashes us
        logger.warning("could not send config-audit ops alert", exc_info=True)

    if has_errors(findings) and not settings.is_dev:
        raise RuntimeError(
            "Configuration errors, refusing to start: "
            + " | ".join(str(f) for f in findings if f.severity == ERROR)
        )
    return findings


def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Report configuration drift for this environment.")
    parser.add_argument(
        "--strict", action="store_true", help="exit non-zero on warnings as well as errors"
    )
    parser.add_argument(
        "--env-file",
        help=(
            "path to the deployment's .env, mounted into this container. Compose "
            "bakes env_file into a container at CREATE time, so a line added to "
            "the file after the container was made is invisible to the process "
            "until `up -d --force-recreate`. This flag finds that gap."
        ),
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    findings = audit_configuration(settings)
    print(format_report(findings, settings))

    stale = False
    if args.env_file:
        on_disk = declared_keys({}, args.env_file)
        in_process = frozenset(os.environ)
        missing = sorted(on_disk - in_process)
        print(f"\nFile vs process ({args.env_file}): {len(on_disk)} keys on disk")
        if missing:
            stale = True
            print(
                "  This process was started before these lines were written; it is "
                "NOT running the file you are reading:"
            )
            for name in missing:
                print(f"    {name}")
            print("  Fix: docker compose -p membership_saas up -d --force-recreate")
        else:
            print("  No gap — the process environment matches the file.")

    if has_errors(findings) or stale:
        return 1
    if args.strict and findings:
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(_main())
