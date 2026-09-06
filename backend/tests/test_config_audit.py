"""GK-436: the guard for the class of bug that hid GK-433 for sixteen days.

`ENABLE_STRIPE_AUTORENEW_CANCELLATION` was absent from the live `.env`. Pydantic
supplied `False`, the code took its honest-fallback branch, and the only place
that fact ever surfaced was a member pressing a button and being told a human
would finish it. No log line, no alert, no panel row.

The first test below is that exact configuration. The rest cover the other two
shapes drift takes — a value that is required and missing, and a key that is set
but read by nothing — plus the wiring, because an unregistered guard is the same
amount of protection as no guard at all.
"""
from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app import config_audit
from app.config import Settings
from app.config_audit import (
    ERROR,
    NON_SETTINGS_CONSUMERS,
    WARNING,
    audit_configuration,
    enforce_configuration,
    settings_env_names,
)

# A configuration with nothing wrong with it, used as the base for every case.
# Anything a test does not care about is declared, so a finding in the result is
# always caused by the thing the test changed.
CLEAN_VALUES: dict[str, str] = {
    "APP_ENV": "demo",
    "BOT_TOKEN": "123:abc",
    "JWT_SECRET": "x" * 48,
    "DB_URL_ASYNC": "postgresql+asyncpg://u:p@db:5432/d",
    "DB_URL_SYNC": "postgresql+psycopg2://u:p@db:5432/d",
    "REDIS_URL": "redis://redis:6379/0",
    "PUBLIC_BASE_URL": "https://example.test",
    "ADMIN_BASE_URL": "https://admin.example.test",
    "PORTAL_BASE_URL": "https://portal.example.test",
    "ADMIN_DEFAULT_EMAIL": "admin@example.test",
    "ADMIN_DEFAULT_PASSWORD": "not-the-demo-one",
    "PRIVATE_CHANNEL_ID": "-1003926731850",
    "PRACTICE_CHAT_ID": "-1003837574500",
    "ALERT_CHAT_ID": "-1003722846405",
    "BOT_UPDATE_MODE": "polling",
    "SUPPORT_AI_PROVIDER": "mock",
    "LAVA_WEBHOOK_AUTH_MODE": "api_key",
    "ALLOW_DEFAULT_ADMIN_PASSWORD": "false",
    "SEED_DEFAULT_ADMIN": "false",
    "USDT_ERC20_ADDRESS": "",
}


def _env(**overrides: str) -> dict[str, str]:
    """The declared environment: CLEAN_VALUES plus every flag written down."""
    env = dict(CLEAN_VALUES)
    env.update({name: "false" for name in settings_env_names() if name.startswith("ENABLE_")})
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def _settings(env: dict[str, str]) -> Settings:
    """A Settings built only from `env`.

    Every field is passed explicitly, including the ones the test does not care
    about: `_env_file=None` stops pydantic reading a dotenv file but NOT the
    process environment, and the test container is started with the real
    `deploy/.env`. Without this the result of a test depends on whose laptop it
    runs on — which is the same class of problem this module exists to catch.
    """
    fields = settings_env_names()
    kwargs: dict[str, object] = {
        name: field.default for name, field in Settings.model_fields.items()
    }
    kwargs.update({fields[k]: v for k, v in env.items() if k in fields})
    return Settings(_env_file=None, **kwargs)


def _audit(env: dict[str, str]) -> list[config_audit.Finding]:
    return audit_configuration(_settings(env), environ=env, dotenv_path="")


def _named(findings: list[config_audit.Finding], env_name: str) -> list[config_audit.Finding]:
    return [f for f in findings if f.env == env_name]


# ---------------------------------------------------------------------------
# the bug that started this
# ---------------------------------------------------------------------------


def test_a_stripe_deployment_missing_the_cancellation_flag_is_an_error():
    """GK-433, exactly as it sat on the live server for sixteen days."""
    env = _env(STRIPE_SECRET_KEY="sk_test_x")
    del env["ENABLE_STRIPE_AUTORENEW_CANCELLATION"]

    findings = _audit(env)
    hits = _named(findings, "ENABLE_STRIPE_AUTORENEW_CANCELLATION")

    assert hits, "the exact live configuration must not audit clean"
    assert any(f.severity == ERROR for f in hits)
    assert any(f.code == "coupling" for f in hits)


def test_writing_the_flag_down_as_false_is_not_drift():
    """Off-by-decision is fine. Off-by-omission is the defect."""
    env = _env(STRIPE_SECRET_KEY="sk_test_x", ENABLE_STRIPE_AUTORENEW_CANCELLATION="false")

    assert _named(_audit(env), "ENABLE_STRIPE_AUTORENEW_CANCELLATION") == []


def test_a_deployment_with_no_stripe_key_is_not_nagged_about_stripe_flags():
    env = _env(STRIPE_SECRET_KEY="")
    del env["ENABLE_STRIPE_AUTORENEW_CANCELLATION"]

    assert not any(f.severity == ERROR for f in _audit(env))


# ---------------------------------------------------------------------------
# missing values
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(config_audit.REQUIRED_ALWAYS))
def test_every_required_setting_is_an_error_when_absent(name: str):
    env = _env()
    del env[name]

    assert any(f.severity == ERROR and f.env == name for f in _audit(env))


def test_alert_chat_missing_is_an_error_on_a_server_and_silence_on_a_laptop():
    """An unset alert chat is why a failed backup and a refused cancellation
    both went to the same place: nowhere."""
    server = _env()
    del server["ALERT_CHAT_ID"]
    assert any(f.severity == ERROR and f.env == "ALERT_CHAT_ID" for f in _audit(server))

    laptop = _env(APP_ENV="dev")
    del laptop["ALERT_CHAT_ID"]
    assert _named(_audit(laptop), "ALERT_CHAT_ID") == []


def test_an_undeclared_flag_is_a_warning_even_with_nothing_coupled_to_it():
    env = _env()
    del env["ENABLE_ZELLE"]

    hits = _named(_audit(env), "ENABLE_ZELLE")
    assert [f.severity for f in hits] == [WARNING]
    assert hits[0].code == "undeclared"
    assert "False" in hits[0].detail  # says what it is silently running as


# ---------------------------------------------------------------------------
# couplings
# ---------------------------------------------------------------------------


def test_a_deployment_that_seeds_no_admin_may_drop_the_seed_credentials():
    """GK-442: the whole point — with seeding off, the two keys can leave `.env`."""
    env = _env(SEED_DEFAULT_ADMIN="false", ADMIN_DEFAULT_EMAIL=None, ADMIN_DEFAULT_PASSWORD=None)

    findings = _audit(env)

    assert _named(findings, "ADMIN_DEFAULT_EMAIL") == []
    assert _named(findings, "ADMIN_DEFAULT_PASSWORD") == []
    assert not any(f.severity == ERROR for f in findings)


@pytest.mark.parametrize("missing", ["ADMIN_DEFAULT_EMAIL", "ADMIN_DEFAULT_PASSWORD"])
def test_a_deployment_that_does_seed_still_needs_both_values(missing: str):
    env = _env(SEED_DEFAULT_ADMIN="true", **{missing: None})

    hits = _named(_audit(env), missing)

    assert any(f.severity == ERROR and f.code == "coupling" for f in hits)


def test_whether_a_deployment_seeds_an_owner_must_be_written_down():
    env = _env(SEED_DEFAULT_ADMIN=None)

    hits = _named(_audit(env), "SEED_DEFAULT_ADMIN")

    assert [f.code for f in hits] == ["undeclared"]


def test_live_lava_checkout_without_an_offer_id_is_an_error():
    env = _env(ENABLE_LAVA_LIVE_CHECKOUT="true", LAVA_API_KEY="k", LAVA_OFFER_ID="")

    assert any(f.severity == ERROR and f.env == "LAVA_OFFER_ID" for f in _audit(env))


def test_sentry_with_an_empty_release_is_flagged():
    """Measured on the live stand: DSN set, SENTRY_RELEASE empty, so every event
    arrives with no version and no way to tell if a deploy fixed it."""
    env = _env(SENTRY_DSN="https://k@o1.ingest.sentry.io/1", SENTRY_RELEASE="")

    hits = _named(_audit(env), "SENTRY_RELEASE")
    assert [f.severity for f in hits] == [WARNING]


def test_an_advertised_erc20_address_without_an_explorer_key_is_flagged():
    """GK-431: the buyer is shown an address whose payment nothing can verify."""
    env = _env(USDT_ERC20_ADDRESS="0xabc", ETHERSCAN_API_KEY="")

    assert any(f.env == "ETHERSCAN_API_KEY" for f in _audit(env))


def test_webhook_mode_without_a_secret_is_an_error():
    env = _env(BOT_UPDATE_MODE="webhook", TG_WEBHOOK_SECRET="")

    assert any(f.severity == ERROR and f.env == "TG_WEBHOOK_SECRET" for f in _audit(env))


# ---------------------------------------------------------------------------
# GK-479: two credentials, either of which is enough
# ---------------------------------------------------------------------------


def test_the_sheets_key_may_be_inline_json_with_no_file(tmp_path):
    """The live host's shape after 2026-08-23.

    The key was moved out of a mounted file and into the `.env`, because a file
    stored inside a release directory is deleted by the next deploy. The rule
    named only the file, so a correctly-configured host would have warned on
    every startup forever — and a warning that is always wrong is a warning
    nobody reads.
    """
    env = _env(
        GOOGLE_SHEETS_SPREADSHEET_ID="15r_eTJC",
        GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON='{"client_email":"a@b.iam.gserviceaccount.com"}',
        GOOGLE_SHEETS_SERVICE_ACCOUNT_FILE="",
    )

    findings = _audit(env)
    assert not [f for f in findings if f.code == "coupling" and "google_sheets" in f.detail]


def test_a_sheets_spreadsheet_with_neither_credential_is_still_flagged():
    env = _env(
        GOOGLE_SHEETS_SPREADSHEET_ID="15r_eTJC",
        GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON="",
        GOOGLE_SHEETS_SERVICE_ACCOUNT_FILE="",
    )

    hits = [f for f in _audit(env) if f.code == "coupling" and "google_sheets" in f.detail]
    assert len(hits) == 1
    assert "GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON" in hits[0].detail
    assert "GOOGLE_SHEETS_SERVICE_ACCOUNT_FILE" in hits[0].detail


def test_a_file_setting_pointing_at_nothing_is_drift_not_configuration():
    """GK-479's whole failure, catchable at startup.

    `GOOGLE_SHEETS_SERVICE_ACCOUNT_FILE` kept naming a path whose bind mount had
    been deleted ten weeks earlier. Every check that existed said "configured",
    because a non-empty string is what they all looked at.
    """
    env = _env(
        GOOGLE_SHEETS_SPREADSHEET_ID="15r_eTJC",
        GOOGLE_SHEETS_SERVICE_ACCOUNT_FILE="/run/secrets/deleted-by-the-next-deploy.json",
    )

    hits = _named(_audit(env), "GOOGLE_SHEETS_SERVICE_ACCOUNT_FILE")
    assert [f.code for f in hits] == ["missing_file"]
    assert hits[0].severity == WARNING
    assert "deleted-by-the-next-deploy.json" in hits[0].detail


def test_a_file_setting_that_resolves_is_not_reported(tmp_path):
    key = tmp_path / "sa.json"
    key.write_text("{}", encoding="utf-8")
    env = _env(
        GOOGLE_SHEETS_SPREADSHEET_ID="15r_eTJC",
        GOOGLE_SHEETS_SERVICE_ACCOUNT_FILE=str(key),
    )

    assert not [f for f in _audit(env) if f.code == "missing_file"]


# ---------------------------------------------------------------------------
# keys that are set and read by nothing
# ---------------------------------------------------------------------------


def test_a_misspelled_flag_is_reported_rather_than_ignored():
    """`ENABLE_STRIPE_AUTO_RENEW_CANCELLATION` would have been just as silent as
    leaving it out — pydantic ignores unknown keys."""
    env = _env(ENABLE_STRIPE_AUTO_RENEW_CANCELLATION="true")

    hits = _named(_audit(env), "ENABLE_STRIPE_AUTO_RENEW_CANCELLATION")
    assert [f.code for f in hits] == ["orphan"]


def test_a_setting_that_was_removed_from_the_code_says_so_instead_of_looking_like_a_typo():
    """Found by this module's own dead-setting scan: REFERRAL_BONUS_DAYS is set
    on the live server, documented in the shape file, and read by nothing since
    the referral programme moved to a ledger commission. Listing it next to
    genuine typos would tell an operator nothing."""
    env = _env(REFERRAL_BONUS_DAYS="7")

    hits = _named(_audit(env), "REFERRAL_BONUS_DAYS")
    assert [f.code for f in hits] == ["retired"]
    assert "ledger commission" in hits[0].detail


def test_a_host_that_still_carries_the_retired_lava_refund_flag_still_starts():
    """GK-445: the flag is deleted from the code, and every live host still has
    `ENABLE_LAVA_LIVE_REFUND=false` in its `.env`. Deleting a setting must not
    turn the next deploy into a boot failure — the operator is told the key is
    safe to remove, and the API starts."""
    env = _env(ENABLE_LAVA_LIVE_REFUND="false")

    hits = _named(_audit(env), "ENABLE_LAVA_LIVE_REFUND")

    assert [f.code for f in hits] == ["retired"]
    assert [f.severity for f in hits] == [WARNING]
    assert "no refund endpoint" in hits[0].detail
    assert not [f for f in _audit(env) if f.severity == ERROR]


@pytest.mark.parametrize("name", sorted(config_audit.RETIRED_SETTINGS))
def test_retired_settings_stay_removed(name: str):
    """A retired setting that quietly reappears in `Settings` gets a default,
    and a default is how the last one hid."""
    assert name not in settings_env_names()


def test_variables_belonging_to_compose_caddy_and_the_backup_sidecar_are_not_orphans():
    env = _env(**{name: "x" for name in NON_SETTINGS_CONSUMERS})

    assert [f for f in _audit(env) if f.code == "orphan"] == []


def test_the_operating_system_environment_is_not_our_business():
    env = _env(PATH="/usr/bin", HOSTNAME="api", LANG="C.UTF-8", PYTHONUNBUFFERED="1", HOME="/app")

    assert [f for f in _audit(env) if f.code == "orphan"] == []


# ---------------------------------------------------------------------------
# the startup gate
# ---------------------------------------------------------------------------


@pytest.fixture
def alerts(monkeypatch):
    sent = AsyncMock(return_value=True)
    import app.observability as observability

    monkeypatch.setattr(observability, "send_ops_alert", sent)
    return sent


@pytest.mark.asyncio
async def test_a_clean_environment_starts_silently(alerts):
    findings = await enforce_configuration("api", _settings(_env()), environ=_env())

    assert findings == []
    alerts.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_error_refuses_to_start_and_says_why_first(alerts):
    """The alert goes out *before* the raise, or a process that dies on a bad
    config dies without telling anybody — which is where we came in."""
    env = _env()
    del env["JWT_SECRET"]

    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        await enforce_configuration("api", _settings(env), environ=env)

    alerts.assert_awaited_once()
    assert "JWT_SECRET" in alerts.await_args.args[0]
    assert alerts.await_args.kwargs["key"] == "config_audit:api"


@pytest.mark.asyncio
async def test_warnings_alert_but_do_not_stop_the_deployment(alerts):
    env = _env()
    del env["ENABLE_ZELLE"]

    findings = await enforce_configuration("bot", _settings(env), environ=env)

    assert findings and not config_audit.has_errors(findings)
    alerts.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_laptop_is_told_but_not_blocked(alerts):
    env = _env(APP_ENV="dev")
    del env["JWT_SECRET"]

    findings = await enforce_configuration("api", _settings(env), environ=env)

    assert config_audit.has_errors(findings)  # still reported, just not fatal


@pytest.mark.asyncio
async def test_a_dead_alert_channel_does_not_rescue_a_broken_config(monkeypatch):
    """Failing to alert must not turn a fatal config into a silent start."""
    import app.observability as observability

    monkeypatch.setattr(
        observability, "send_ops_alert", AsyncMock(side_effect=RuntimeError("telegram down"))
    )
    env = _env()
    del env["JWT_SECRET"]

    with pytest.raises(RuntimeError, match="refusing to start"):
        await enforce_configuration("api", _settings(env), environ=env)


# ---------------------------------------------------------------------------
# wiring — GK-421's lesson: an unregistered guard guards nothing
# ---------------------------------------------------------------------------


def test_both_processes_actually_run_the_audit_at_startup():
    from app.api import main as api_main
    from app.bot import main as bot_main

    assert "enforce_configuration(" in inspect.getsource(api_main._bootstrap)
    assert "enforce_configuration(" in inspect.getsource(bot_main.main)


def test_the_setting_list_comes_from_the_model_and_not_from_a_hand_written_list():
    names = settings_env_names()

    assert len(names) == len(Settings.model_fields)
    assert "ENABLE_STRIPE_AUTORENEW_CANCELLATION" in names
    assert names["ENABLE_STRIPE_AUTORENEW_CANCELLATION"] == "enable_stripe_autorenew_cancellation"


# ---------------------------------------------------------------------------
# `.env.example` is the shape document; it is allowed to be incomplete only on
# purpose. Needs the repo checkout: CI has it, the test image does not (its
# build context is `backend/`), so it skips there with a reason rather than
# passing vacuously.
# ---------------------------------------------------------------------------

_EXAMPLE_CANDIDATES = (
    Path(__file__).resolve().parents[2] / "deploy" / ".env.example",
    Path("/repo/deploy/.env.example"),
)

# Read by the code but deliberately absent from the shape document: internal
# tuning with no operational meaning to whoever fills in a server .env.
EXAMPLE_EXEMPT: frozenset[str] = frozenset(
    {
        "APP_NAME",
        "JWT_ALGORITHM",
        "STRIPE_REFERRAL_COUPON_ID",
        "GIFT_ACTIVATION_TTL_DAYS",
        "PORTAL_MAGIC_LINK_MAX_ACTIVE",
        "TG_WEBHOOK_LISTEN_HOST",
        "TG_WEBHOOK_LISTEN_PORT",
        "UPLOAD_DIR",
        "GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON",
    }
)


def _example_path() -> Path:
    for candidate in _EXAMPLE_CANDIDATES:
        if candidate.is_file():
            return candidate
    pytest.skip("deploy/.env.example is outside this build context")


def _example_keys(path: Path) -> set[str]:
    keys: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        # Commented-out keys still document the shape (e.g. PORTAL_COOKIE_SECURE).
        if line.startswith("#"):
            line = line.lstrip("#").strip()
        if "=" not in line:
            continue
        name = line.split("=", 1)[0].strip()
        if name.isupper() and name.replace("_", "").isalnum():
            keys.add(name)
    return keys


def test_every_setting_the_code_reads_is_in_the_shape_document():
    keys = _example_keys(_example_path())
    missing = sorted(set(settings_env_names()) - keys - EXAMPLE_EXEMPT)

    assert not missing, (
        "deploy/.env.example does not document settings the code reads: "
        f"{missing}. That file is how a new environment gets built; a setting "
        "missing from it is a setting that will be missing from the server."
    )


def test_the_shape_document_has_no_keys_that_nothing_reads():
    keys = _example_keys(_example_path())
    orphans = sorted(keys - set(settings_env_names()) - NON_SETTINGS_CONSUMERS)

    assert not orphans, f"documented but read by nothing: {orphans}"


def test_the_shape_document_does_not_still_advertise_retired_settings():
    still_there = sorted(_example_keys(_example_path()) & set(config_audit.RETIRED_SETTINGS))

    assert not still_there, (
        f"{still_there} were removed from the code but are still offered to "
        "whoever builds the next environment from this file"
    )


def test_no_setting_in_the_model_is_read_by_nothing():
    """The mirror of the orphan check, and the scan that found the retired
    three: a setting the code declares but never reads is a promise to an
    operator that changing it will do something."""
    import re

    backend = Path(__file__).resolve().parents[1]
    sources = [
        path
        for directory in ("app", "alembic")
        for path in (backend / directory).rglob("*.py")
        if path.name != "config.py"
    ]
    if not sources:
        pytest.skip("no sources to scan")
    blob = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in sources)
    # `config.py` used to be excluded whole, so that a field's own declaration
    # could not count as a read. That also hid the settings whose real consumer
    # is a *property in the same file* — `access_start_floor` only ever passed
    # because an unrelated function elsewhere happens to share its name, and
    # GK-446's `prelaunch_hold_allowlist`, read by `prelaunch_hold_allowlist_ids`
    # and by `validate_security`, had no such accident to rely on. So scan it
    # too, with declarations and comments stripped: `self.<field>` in a property
    # is a read, and a genuinely unread field still appears nowhere but the
    # declaration line this removes.
    config_body = "\n".join(
        line
        for line in (backend / "app" / "config.py").read_text(encoding="utf-8").splitlines()
        # `[a-z_][a-z0-9_]*`, not `[a-z_]+`: `stripe_price_6m_id` and the four
        # `usdt_*20_*` fields have digits in their names, and a declaration this
        # regex fails to strip is a field that passes the check by quoting itself.
        if not re.match(r"\s*[a-z_][a-z0-9_]*\s*:\s*[^=]+=", line)
        and not line.lstrip().startswith("#")
    )
    blob = f"{blob}\n{config_body}"

    # Settings whose only consumer is a human reading the file: documented on
    # purpose, kept on purpose, and each one is a deliberate exception.
    deliberate = {
        "lava_shop_id",  # retained for backward-compatible env parsing (see config.py)
        "stripe_publishable_key",  # handed to the operator for the Stripe dashboard
        "upload_dir",  # container path, referenced from compose volumes
    }
    unread = sorted(
        name
        for name in Settings.model_fields
        if name not in deliberate and not re.search(rf"\b{re.escape(name)}\b", blob)
    )

    assert not unread, (
        f"declared in Settings but read nowhere: {unread}. Either wire it up, "
        "retire it into RETIRED_SETTINGS, or add it to `deliberate` with a reason."
    )


def _referenced_outside_settings(deploy: Path) -> set[str]:
    import re

    referenced: set[str] = set()
    for name in ("docker-compose.yml", "Caddyfile"):
        path = deploy / name
        if path.is_file():
            referenced |= set(
                re.findall(r"\$\{([A-Z][A-Z0-9_]*)", path.read_text(encoding="utf-8"))
            )
    # The backup sidecar reads its own variables straight from the environment,
    # so compose never names them.
    for path in sorted(deploy.glob("backup/*.sh")):
        referenced |= set(
            re.findall(
                r"\$\{?(BACKUP_[A-Z0-9_]+|POSTGRES_[A-Z0-9_]+)", path.read_text(encoding="utf-8")
            )
        )
    # …and so do the two Next.js apps, via process.env.
    for app in ("admin", "portal"):
        root = deploy.parent / app / "app"
        if not root.is_dir():
            continue
        for path in root.rglob("*.ts*"):
            referenced |= set(
                re.findall(r"process\.env\.([A-Z][A-Z0-9_]*)", path.read_text(encoding="utf-8"))
            )
    return referenced


def test_non_settings_consumers_matches_what_the_other_services_reference():
    """The allowlist that stops compose/backup variables being reported as
    orphans has to track those files, or it becomes its own piece of drift."""
    referenced = _referenced_outside_settings(_example_path().parent)

    unlisted = sorted(referenced - set(settings_env_names()) - NON_SETTINGS_CONSUMERS)
    assert not unlisted, (
        f"compose/Caddy/backup reference {unlisted}, which NON_SETTINGS_CONSUMERS does not "
        "list — they would be reported as orphans on every start"
    )


# The mirror of the test above — "is anything on the allowlist read by nothing?"
# — is deliberately NOT a test. It was run by hand and it earned its keep: it
# asked the right question about `BACKUP_RCLONE_CONF`, whose only consumer is a
# `${VAR:-default}` in compose on a branch that had not merged yet. As a
# standing assertion it would fail or pass depending on merge order rather than
# on whether the configuration is correct. Re-run it by hand when the allowlist
# grows.


def test_the_audit_runs_against_this_process_without_blowing_up():
    """Smoke: the real environment, whatever it is, must produce a report rather
    than an exception. `python -m app.config_audit` is the on-server command."""
    report = config_audit.format_report(audit_configuration(Settings(_env_file=None)))

    assert "Configuration audit" in report
