from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# Known-default secrets that MUST NOT appear in any non-dev environment.
_DEFAULT_JWT_SECRET = "change-me-please-32-bytes-minimum-secret-key"
_DEFAULT_ADMIN_PASSWORD = "demo1234"
_DEFAULT_ADMIN_EMAIL = "admin@example.com"


def parse_access_start_floor(raw: str) -> datetime | None:
    """Parse ``ACCESS_START_FLOOR`` into an aware UTC instant (GK-439).

    Empty or whitespace means the mechanism is off. Anything else must be a
    valid ISO-8601 instant; a naive value is read as UTC and a trailing ``Z`` is
    accepted. Raises ``ValueError`` on anything else rather than shrugging to
    ``None`` — a typo that silently disables the floor would sell August access
    from the day of payment, which is exactly the thing this setting exists to
    prevent, and it would do it without a single log line.
    """
    value = (raw or "").strip()
    if not value:
        return None
    normalized = f"{value[:-1]}+00:00" if value[-1] in {"Z", "z"} else value
    parsed = datetime.fromisoformat(normalized)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def parse_tg_id_allowlist(raw: str) -> frozenset[int]:
    """Parse a comma-separated list of Telegram **user** ids (GK-446).

    Empty or whitespace means nobody, which is the safe reading here: an empty
    allowlist leaves the pre-launch hold covering everyone, exactly as GK-443
    built it. That is the opposite of ``ACCESS_START_FLOOR``, where "off" is the
    dangerous state — worth knowing before copying the pattern in either
    direction.

    Numeric ids only. A ``@username`` is not durable — the person can change it
    between the moment it is written into ``.env`` and the moment they open the
    bot — and resolving one would need a Telegram round trip at router-build
    time, before the bot has a running session. Negative ids are rejected too:
    those are chats, and pasting the channel id here is the plausible mistake.

    Raises ``ValueError`` on anything else, so a typo is found at startup by
    `validate_security` rather than by the one person who was supposed to be let
    through and instead met the hold.
    """
    value = (raw or "").strip()
    if not value:
        return frozenset()
    ids: set[int] = set()
    for chunk in value.split(","):
        entry = chunk.strip()
        if not entry:
            continue
        try:
            parsed = int(entry)
        except ValueError:
            raise ValueError(
                f"{entry!r} is not a Telegram user id — numeric ids only, not usernames"
            ) from None
        if parsed <= 0:
            raise ValueError(
                f"{parsed} is not a Telegram *user* id — negative ids are chats, "
                "and a channel id pasted here would let nobody through"
            )
        ids.add(parsed)
    return frozenset(ids)


def parse_tg_chat_id_list(raw: str) -> frozenset[int]:
    """Parse a comma-separated list of Telegram **chat** ids (GK-460).

    The mirror image of ``parse_tg_id_allowlist``: that one names people and
    rejects negatives, this one names rooms and expects them. Used by
    ``EXPIRY_REMOVALS_EXPECT_CHATS``, where the value is not a permission but a
    statement of intent — "these are the two communities I mean to remove people
    from" — checked against the chats the job would actually reach.

    ``0`` is rejected because it is what ``PRIVATE_CHANNEL_ID`` and
    ``PRACTICE_CHAT_ID`` say when they are *unset*; accepting it here would let
    "I named the target" be satisfied by naming nothing. Positive ids are
    accepted rather than refused: a user id pasted here fails the set comparison
    anyway, with a message that prints both sides, and a second weaker copy of
    that rule is one more thing to keep true.
    """
    value = (raw or "").strip()
    if not value:
        return frozenset()
    ids: set[int] = set()
    for chunk in value.split(","):
        entry = chunk.strip()
        if not entry:
            continue
        try:
            parsed = int(entry)
        except ValueError:
            raise ValueError(
                f"{entry!r} is not a Telegram chat id — numeric ids only, not @usernames"
            ) from None
        if parsed == 0:
            raise ValueError(
                "0 is not a chat id — it is what PRIVATE_CHANNEL_ID and "
                "PRACTICE_CHAT_ID read as when nothing is configured"
            )
        ids.add(parsed)
    return frozenset(ids)


def parse_fx_rates_to_usd(raw: str) -> dict[str, Decimal]:
    """Parse ``REFERRAL_FX_RATES_TO_USD`` into ``{CURRENCY: usd_per_unit}`` (GK-457).

    Format: ``RUB:0.0125,EUR:1.08`` — one USD-per-unit multiplier per currency,
    so ``1500 RUB * 0.0125 = 18.75 USD``. Written that way round on purpose: a
    multiplier reads the same direction as the arithmetic that uses it, while a
    "rate" like ``80`` is ambiguous about which way it divides, and the wrong
    guess is a 6400× error in somebody's payout.

    ``USD`` is refused rather than accepted-and-ignored: a line saying
    ``USD:1.1`` means the author believes this table can reprice dollars, and
    silently dropping it would leave that belief in place. USD is always 1.

    Raises ``ValueError`` on a malformed entry — `validate_security` is the
    startup gate. Shrugging to an empty table would be the quiet failure mode
    this setting exists to remove: every rouble commission would then be held
    back at zero, which is visible only to whoever reads the alert chat.
    """
    value = (raw or "").strip()
    if not value:
        return {}
    rates: dict[str, Decimal] = {}
    for chunk in value.split(","):
        entry = chunk.strip()
        if not entry:
            continue
        currency, separator, amount = entry.partition(":")
        if not separator:
            raise ValueError(f"{entry!r} is not CURRENCY:RATE, e.g. RUB:0.0125")
        code = currency.strip().upper()
        if not code.isalpha():
            raise ValueError(f"{currency.strip()!r} is not a currency code")
        if code == "USD":
            raise ValueError(
                "USD needs no rate — it is the ledger's own currency and is "
                "always 1. Remove the USD entry rather than setting it to 1."
            )
        try:
            rate = Decimal(amount.strip())
        except InvalidOperation:
            raise ValueError(f"{amount.strip()!r} is not a decimal rate for {code}") from None
        if rate <= 0:
            raise ValueError(f"{code} rate must be positive, got {rate}")
        rates[code] = rate
    return rates


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "membership_saas"
    app_env: str = "dev"  # dev | demo | prod

    bot_token: str = ""
    bot_username: str = "membership_bot"
    private_channel_id: int = 0
    private_channel_invite_link: str = ""
    practice_chat_id: int = 0
    practice_chat_invite_link: str = ""
    bot_update_mode: str = "polling"  # polling | webhook
    # ─── GK-443: pre-launch hold ────────────────────────────────────
    # While True the bot answers every private message, command and planted
    # keyboard tap with one approved sentence and registers no other handler —
    # no checkout, no portal link, no support flow — and the two member-facing
    # scheduled jobs (`kick_expired_job`, `remind_expiring_job`) skip their run.
    # Ops jobs, including the heartbeat, keep running.
    #
    # Default False on purpose: a hold that defaults to on is a trap for every
    # future deployment, and a deployment that silently holds is indistinguishable
    # from a broken one. It is turned on explicitly in the live `.env` in the same
    # action that returns the bot to service, and off again at launch.
    enable_prelaunch_hold: bool = False
    # ─── GK-446: who walks past the hold ────────────────────────────
    # Comma-separated Telegram user ids that get the *normal* bot while the hold
    # is on, so Grant can check the finished texts on a live flow while every
    # member still meets the заглушка. Empty — the default, and the state until
    # he sends the accounts — means nobody bypasses it and GK-443's guarantee
    # stands untouched: the selling handlers are absent from the dispatcher.
    #
    # Read that sentence the other way before setting it, because it is the
    # cost: with a non-empty allowlist those handlers are registered again,
    # behind a root-level filter. The named ids reach real checkouts and can
    # really pay. That is the point — a text check that stops before the payment
    # screen does not check the payment screen — but it is not a dry run.
    prelaunch_hold_allowlist: str = ""
    # ─── GK-460: arming the hourly removal sweep ────────────────────
    # `kick_expired_job` ban+unbans expired members out of every configured
    # Telegram resource, hourly, unattended. Until this task it was gated by
    # `ENABLE_PRELAUNCH_HOLD` alone — so the single `.env` edit that lifts the
    # hold on launch day also armed removals against whatever
    # `PRIVATE_CHANNEL_ID` and `PRACTICE_CHAT_ID` happened to point at, within
    # the hour, with nothing naming the intended target.
    #
    # `python -m app.ops.cutover` does the same thing to the same people and
    # refuses without an exact confirmation token **and** an `--expect-chats`
    # naming every resource a removal reaches. The scheduled job now asks for
    # the same second sentence: turning selling back on and arming removals are
    # two decisions, made separately, each written down. (GK-470 made the
    # cutover flag plural; the set-equality binding below is where that shape
    # was settled first.)
    #
    # Default False, like every flag here — off by omission and off by decision
    # look identical at runtime, which is why GK-436's guard makes every
    # `ENABLE_*` key must-declare.
    enable_expiry_removals: bool = False
    # The chats the operator means to remove people from, comma-separated. Must
    # equal — exactly, as a set — the chats an actual removal would reach
    # (`PRIVATE_CHANNEL_ID` and `PRACTICE_CHAT_ID`, ignoring unset ones). Not a
    # subset and not a prefix: a resource that appears without being named is as
    # much a surprise as one that was named and moved.
    #
    # Empty while `ENABLE_EXPIRY_REMOVALS` is on means armed-but-unbound, and
    # the job refuses and says so out loud rather than guessing.
    expiry_removals_expect_chats: str = ""
    tg_webhook_base_url: str = ""  # empty = public_base_url
    tg_webhook_path: str = "/tg-webhook/bot"
    tg_webhook_secret: str = ""
    tg_webhook_listen_host: str = "0.0.0.0"
    tg_webhook_listen_port: int = 8080

    public_base_url: str = "http://localhost:8000"
    admin_base_url: str = "http://localhost:3000"

    db_url_async: str = "postgresql+asyncpg://membership_saas:membership_saas@db:5432/membership_saas"
    db_url_sync: str = "postgresql+psycopg2://membership_saas:membership_saas@db:5432/membership_saas"
    redis_url: str = "redis://redis:6379/0"

    jwt_secret: str = _DEFAULT_JWT_SECRET
    jwt_algorithm: str = "HS256"
    # Shorter access tokens cap the blast radius of a stolen token.
    # 4h covers a normal admin session; longer needs re-login.
    jwt_expire_minutes: int = 60 * 4

    admin_default_email: str = _DEFAULT_ADMIN_EMAIL
    admin_default_password: str = _DEFAULT_ADMIN_PASSWORD
    # GK-442: whether the API creates the ADMIN_DEFAULT_* owner account at
    # startup if it is missing. Default **off**, because "missing" and "deleted
    # on purpose" are the same thing to a bootstrap that never asked: deleting
    # the seed admin used to hold only until the next API start. On with it, the
    # two ADMIN_DEFAULT_* values become required (see config_audit COUPLINGS);
    # off, they are never read, so they can leave the environment file entirely.
    seed_default_admin: bool = False

    # Hard cap on login attempts per IP per minute (Redis-backed).
    login_rate_limit_per_minute: int = 5
    # Allow legacy demo creds only when env=='demo'. In prod, bootstrap refuses to start.
    allow_default_admin_password: bool = False

    stripe_secret_key: str = ""
    stripe_publishable_key: str = ""
    stripe_webhook_secret: str = ""
    # GK-436: `stripe_test_mode` used to live here. Nothing read it, while the
    # live key is `sk_live_…` — a setting that reads like a safety switch and
    # switches nothing is worse than no setting. Test vs live is decided by
    # which secret key is configured, and nothing else.
    stripe_referral_coupon_id: str = "membership_saas-referral-monthly-once-20"
    stripe_price_monthly_id: str = ""
    stripe_price_6m_id: str = ""
    stripe_price_annual_id: str = ""
    # GK-377: gated like every other outbound provider mutation. Stripe's
    # cancel-at-period-end is the safe, reversible one of the two, but it still
    # touches a live billing object, and leaving it ungated meant a plain test
    # run reached api.stripe.com. While False the request is honest-fallback
    # only: the member is told a human must finish it, and it lands in the
    # admin cancellation queue. Flip after a test-mode smoke.
    enable_stripe_autorenew_cancellation: bool = False

    # ─── GK-439: paid time starts on a fixed date ───────────────────
    # Launch-only mechanism. While this holds a *future* UTC instant, a member's
    # FIRST paid period starts at it instead of at the moment of payment — money
    # taken on 19.08 buys 01.09 → 01.10, so an August payment covers September
    # whole and every member shares one renewal date. Renewals are untouched;
    # they already chain from `expires_at`.
    #
    # The same value moves Stripe's own clock: while it is set, checkout prepays
    # the first period as a one-time line item and defers the subscription's
    # first charge to the end of that period (`subscription_data.trial_end`), so
    # the provider's next-charge date and our `expires_at` cannot drift apart.
    # Lava has no equivalent field — its contract anchors to the payment moment
    # — so RUB charge dates stay per-member while access dates unify. Accepted
    # deliberately; see GK-439.
    #
    # Empty (the default) disables all of it and checkout keeps today's shape.
    # Emptying it after launch is how the mechanism removes itself instead of
    # lingering as a trap. Format: ISO-8601, e.g. 2026-09-01T00:00:00Z.
    access_start_floor: str = ""

    # GK-486: accounts that must never be able to start a payment. Grant asked
    # for Owner to walk the real bot; the only thing then standing between him
    # and a live charge on his own card was being asked not to press the button,
    # and his account already carried five payment attempts from June. A person
    # is not a safety mechanism.
    #
    # Deliberately NOT scoped to the pre-launch hold. Tying it to
    # `ENABLE_PRELAUNCH_HOLD` would delete the protection the moment the hold
    # lifts — which is launch day, the busiest day, when nobody is watching for
    # it. Staff should never be sold to, hold or no hold.
    #
    # Read the empty default the opposite way to `PRELAUNCH_HOLD_ALLOWLIST`:
    # there, empty means "nobody bypasses the hold", the safe state. Here empty
    # means "nobody is protected", the *unsafe* state. The two lists look alike
    # and fail in opposite directions, which is exactly the trap, so they are
    # never merged into one setting.
    no_charge_tg_ids: str = ""

    lava_api_key: str = ""
    lava_api_base_url: str = "https://gate.lava.top"
    # Verified membership_saas subscription offer. LAVA_SHOP_ID is retained
    # below for backward-compatible env parsing but is not used by Public API
    # v1.22.0 checkout creation.
    lava_offer_id: str = ""
    lava_shop_id: str = ""
    enable_lava_live_checkout: bool = False
    # GK-445: `enable_lava_live_refund` used to live here. It was retired, not
    # renamed — Lava publishes no refund endpoint (16 paths, zero refund) and
    # `LavaProvider` has no refund method, so there is nothing for a flag to
    # gate. Its only live effect was making the panel offer an "auto" Lava
    # refund that answered 400 every time. See RETIRED_SETTINGS in config_audit.
    # GK-377: same posture for autorenew cancellation. DELETE /api/v1/subscriptions/{id}
    # is in the official spec, but neither the request shape nor our API key's
    # permission has been exercised against the live account, and a wrong call here
    # touches real recurring contracts on personal cards. While False the bot still
    # accepts cancellation requests — they route to the manual queue and the user is
    # told plainly that a human has to finish it. Flip only after the contract smoke.
    enable_lava_autorenew_cancellation: bool = False
    # GK-418: rollback lever for the dead GK-412 direction. While False (the
    # default) RUB checkout creates an API invoice exactly like USD/EUR. While
    # True the bot links straight to the buyer-facing offer page instead —
    # which Lava confirmed in writing (2026-07-23) sends NO purchase webhook
    # and is invisible to the seller API, so a buyer pays and is granted
    # nothing. Do not enable it to work around the Smart Glocal "Оплатить"
    # widget hang: that failure happens on both flows.
    enable_lava_rub_offer_page: bool = False
    # Incoming Lava webhooks use either X-Api-Key or Basic auth. The launch
    # default is X-Api-Key, using a key generated on our side and entered in
    # the Lava dashboard. This is intentionally separate from LAVA_API_KEY,
    # which signs our outgoing requests to Lava.
    lava_webhook_auth_mode: str = "api_key"  # api_key | basic
    lava_webhook_api_key: str = ""
    lava_webhook_basic_username: str = ""
    lava_webhook_basic_password: str = ""

    zelle_recipient: str = "membership_saas@example.com"
    usdt_trc20_address: str = "TLotQJsvstVxCQdQNBoAUYn15ousqD75EC"
    usdt_erc20_address: str = "0x6f4C9BfDa3446265168c6567483feDca17643fc2"
    usdt_trc20_confirmations: int = 3
    usdt_erc20_confirmations: int = 3
    trongrid_base_url: str = "https://api.trongrid.io"
    trongrid_api_key: str = ""
    etherscan_base_url: str = "https://api.etherscan.io/v2/api"
    etherscan_chain_id: int = 1
    etherscan_api_key: str = ""

    openai_api_key: str = ""
    anthropic_api_key: str = ""
    support_ai_provider: str = "mock"  # mock | openai | anthropic
    # Optional human support target shown in bot copy, e.g. @GKcurators.
    # Empty means support is collected through SupportMessage/admin history only.
    support_contact: str = ""
    # GK-378: Bot-API-reachable chat the bot forwards inbound support tickets to
    # (a curator group/channel chat_id like -1001234567890, or an @username the
    # bot can post to). Empty = no auto-routing: messages are still persisted and
    # answered from the admin panel. A private @GKcurators user account cannot
    # receive bot-initiated messages, so it stays a direct-contact fallback in
    # support_contact, not a routing target. The real chat_id is pending from
    # Grant (BLK-016); routing activates the moment it is set.
    support_routing_chat_id: str = ""
    # Paid gift activation links are one-time and expire after this many days.
    gift_activation_ttl_days: int = 30

    # ─── Vimeo member portal (GK-091) ──────────────────────────────────
    # Read-only Vimeo API token for syncing archive metadata. Stored locally
    # in credentials/vimeo_api_token.txt; injected via .env, never committed.
    # Empty token disables sync gracefully (last-known rows keep rendering).
    vimeo_api_token: str = ""
    # Public base URL the bot puts in magic links and the portal redirects to.
    # Local dev: http://localhost:3001 ; prod: https://community.example.com
    portal_base_url: str = "http://localhost:3001"
    # Magic-link single-use token lifetime.
    portal_magic_link_ttl_minutes: int = 15
    # Portal session lifetime (sliding — bumped on activity, see portal_auth).
    portal_session_days: int = 30
    # Maximum simultaneous live browser sessions per account. Creating another
    # session transactionally revokes the oldest; expired/revoked rows do not
    # consume a slot. Independent from the active magic-link cap below.
    portal_session_max_active: int = 3
    # At most N live (unexpired, unused) magic links per user; issuing more
    # invalidates the oldest so the table can't be spammed.
    portal_magic_link_max_active: int = 3

    # Google Sheets CRM export (GK-080). Share the target spreadsheet with the
    # service account email. Keep raw JSON out of git; use either a mounted file
    # path or an environment-secret value.
    google_sheets_spreadsheet_id: str = ""
    google_sheets_service_account_json: str = ""
    google_sheets_service_account_file: str = ""
    google_sheets_quota_max_retries: int = 5

    # GK-436: `referral_bonus_days` / `referral_bonus_referee_days` used to live
    # here. The referral programme moved to the money ledger (a commission, per
    # the 2026-05-21 call) and neither value has been read by any code path
    # since. They stayed in `.env.example` and in the server's `.env`,
    # advertising a bonus-days model that no longer exists.
    #
    # ─── GK-457: what a non-USD payment is worth to the ledger ──────
    # The commission ledger is denominated in dollars end to end — Grant's
    # approved partner copy promises «от $100 накопленных начислений» and the
    # payout threshold is $100 — while the audience's main payment method is
    # roubles. Something has to turn one into the other, and until GK-457 the
    # answer was "nothing": a 1500 ₽ payment wrote 300 into a column summed as
    # USD.
    #
    # USD-per-unit multipliers, comma separated: `RUB:0.0125` means one rouble
    # is worth $0.0125, so the 1500 ₽ tariff accrues 20% of $18.75. That default
    # is not a market quote — it is read off our own price list, where the three
    # tariffs sell at 1500 ₽/$19, 7000 ₽/$79 and 10000 ₽/$129, implying 0.01267,
    # 0.01129 and 0.0129. 0.0125 sits inside that band, so a commission is worth
    # roughly the same to a partner whichever currency their invitee paid in.
    #
    # It is a *setting* and not a constant because it goes stale: revisit it
    # whenever the rouble prices change, and expect to. The rate actually used is
    # written onto each commission row (`fx_rate_to_usd`), so a stale entry is
    # visible per row in the panel and correctable by adjustment — it never
    # silently rewrites history.
    #
    # A currency with no entry here is NOT guessed at. See
    # `app/services/referral_ledger.py`: the commission is recorded at $0 with a
    # null rate and an ops alert, because a wrong number that looks payable is
    # worse than a zero somebody has to ask about.
    referral_fx_rates_to_usd: str = "RUB:0.0125"

    # ─── Launch feature flags (GK-120) ──────────────────────────────
    # Per the 2026-05-21 call and BLK-006/BLK-008, the production launch
    # surface intentionally hides Zelle, AmoCRM/Make/Zapier webhook
    # integrations, and any OpenAI/Anthropic-backed support replies.
    # Defaults are OFF so a fresh prod boot never accidentally exposes a
    # surface we have not committed to. Demo/dev can flip individual flags
    # back on.
    enable_zelle: bool = False
    enable_webhook_integrations: bool = False
    enable_ai_support: bool = False

    upload_dir: str = "/data/uploads"

    # CORS allowlist for the admin API. Comma-separated origins, or empty to
    # default to admin_base_url. "*" is explicitly rejected in production.
    cors_allowed_origins: str = ""

    # Shared secret for outgoing-webhook HMAC. If empty, signing is skipped
    # (back-compat) but the admin UI warns.
    outgoing_webhook_signing_secret: str = ""

    # ─── Observability (GK-040) ────────────────────────────────────
    # Structured JSON logging is the default for prod; humans on a TTY can
    # set LOG_FORMAT=console for color-friendly output.
    log_level: str = "INFO"
    log_format: str = "json"  # json | console
    # Empty DSN disables Sentry entirely — local dev does not need it.
    sentry_dsn: str = ""
    sentry_release: str = ""
    sentry_traces_sample_rate: float = 0.0
    # Telegram chat for ops alerts. Default production channel is
    # `@alert_membership_community` / `1003722846405` (Grant 2026-05-27).
    # Leave empty in dev/test so we don't spam the channel during local runs.
    alert_chat_id: str = ""
    # Cap how stale the bot heartbeat may be before /health flips to
    # "degraded". 180s = three missed 60s beats.
    bot_heartbeat_max_age_seconds: int = 180
    # GK-437: how old the newest *successful restore check* may be before the
    # bot says so. The canary runs daily; 36h absorbs one skipped run (a deploy,
    # a slow restore) and catches the second. Raising this is a decision to
    # find out later that backups stopped being provable.
    backup_verification_max_age_hours: int = 36

    @property
    def access_start_floor_at(self) -> datetime | None:
        """The parsed GK-439 floor, or ``None`` when unset **or unparseable**.

        Never raises: this is read on the payment path, and a member's checkout
        is the wrong place to discover a malformed environment value. A bad
        value is caught instead at startup by `validate_security`, which refuses
        to boot outside dev — so the only way to reach here with garbage is a
        laptop, where "off" is the safe reading.
        """
        try:
            return parse_access_start_floor(self.access_start_floor)
        except ValueError:
            return None

    @property
    def prelaunch_hold_allowlist_ids(self) -> frozenset[int]:
        """The parsed GK-446 allowlist, or empty when unset **or unparseable**.

        Never raises. This is read while the dispatcher is being built, and a
        bot that refuses to boot over a cosmetic `.env` typo during the launch
        window is worse than the failure it prevents — especially since the
        failure here is fail-safe by construction: an empty set means everyone
        meets the hold, nobody reaches a checkout. `validate_security` still
        refuses the boot in prod, so a malformed value is loud where loudness is
        free and harmless where it is not.
        """
        try:
            return parse_tg_id_allowlist(self.prelaunch_hold_allowlist)
        except ValueError:
            return frozenset()

    @property
    def expiry_removals_expect_chat_ids(self) -> frozenset[int]:
        """The parsed GK-460 target binding, or empty when unset **or malformed**.

        Never raises, and the fallback is the safe direction: an empty set while
        `ENABLE_EXPIRY_REMOVALS` is on can never equal the non-empty set of
        chats the sweep would reach, so a typo refuses the removals instead of
        aiming them somewhere nobody checked. `validate_security` still refuses
        the boot in prod, so the typo is loud as well as harmless.
        """
        try:
            return parse_tg_chat_id_list(self.expiry_removals_expect_chats)
        except ValueError:
            return frozenset()

    @property
    def is_prod(self) -> bool:
        return self.app_env.lower() in {"prod", "production"}

    @property
    def is_dev(self) -> bool:
        return self.app_env.lower() in {"dev", "development", "test"}

    def charges_blocked_for(self, tg_id: int | None) -> bool:
        """GK-486: may this Telegram user be charged? A predicate, not a set.

        Every call site asks the same question, and the two interesting answers
        are the ones a bare `frozenset` gets wrong if the caller is careless:

        * **No user.** A callback Telegram has only partially kept has no
          `from_user`. An event we cannot attribute to a person must not be able
          to spend money on that person's card, so `None` is blocked.
        * **A malformed setting.** This is the one that matters on this host.
          `validate_security` only *raises* when `is_prod`, and the live host
          runs `APP_ENV=demo` — so a typo in `NO_CHARGE_TG_IDS` there logs one
          warning line and the bot boots happily. If a bad value parsed to "the
          empty set" the result would be a bot quietly selling to everybody it
          was configured to protect, with no symptom until a charge appears.

        So a malformed value blocks **everybody**, which is the opposite of how
        `prelaunch_hold_allowlist_ids` treats its own garbage, and the asymmetry
        is deliberate: there, failing open means somebody meets the заглушка
        they were meant to skip — an inconvenience. Here, failing open means a
        real card is charged. A typo that stops all selling is discovered within
        minutes by anyone trying to buy and fixed by editing one line; a typo
        that silently removes the guard is discovered by a refund request.
        """
        if tg_id is None:
            return True
        try:
            blocked = parse_tg_id_allowlist(self.no_charge_tg_ids)
        except ValueError:
            return True
        return tg_id in blocked

    def validate_security(self) -> list[str]:
        """Return a list of fatal security misconfigurations.

        Called at process start; non-empty list means refuse to boot in prod.
        Demo/dev get warnings logged, not crashes.
        """
        errors: list[str] = []
        if self.jwt_secret == _DEFAULT_JWT_SECRET:
            errors.append("JWT_SECRET is the default placeholder — set a 32+ random hex value")
        if len(self.jwt_secret) < 32:
            errors.append("JWT_SECRET must be at least 32 characters")
        # Only meaningful when the seed path can actually run. GK-442 took these
        # two keys out of `REQUIRED_ALWAYS` so they could leave the environment
        # file — but this check was left reading the *pydantic default* they fall
        # back to, so removing them made `api` and `bot` refuse to start in prod
        # with a message about a password nobody had set. That is the same
        # deadlock GK-442 was filed to break, one layer down. With seeding off
        # neither value is ever read, so there is nothing to protect; with it on
        # the check is unchanged, and `_seed_default_admin` refuses the demo
        # password a second time regardless.
        if (
            self.seed_default_admin
            and self.admin_default_password == _DEFAULT_ADMIN_PASSWORD
            and not self.allow_default_admin_password
        ):
            errors.append(
                "SEED_DEFAULT_ADMIN is on and ADMIN_DEFAULT_PASSWORD is the demo "
                "default; set a strong ADMIN_DEFAULT_PASSWORD, or "
                "ALLOW_DEFAULT_ADMIN_PASSWORD=true only in demo env"
            )
        if (self.access_start_floor or "").strip():
            try:
                parse_access_start_floor(self.access_start_floor)
            except ValueError:
                errors.append(
                    "ACCESS_START_FLOOR is not a valid ISO-8601 instant "
                    "(e.g. 2026-09-01T00:00:00Z) — an unparseable floor reads as "
                    "no floor at all, and paid time would start on the day of payment"
                )
        if (self.prelaunch_hold_allowlist or "").strip():
            try:
                parse_tg_id_allowlist(self.prelaunch_hold_allowlist)
            except ValueError as exc:
                errors.append(
                    f"PRELAUNCH_HOLD_ALLOWLIST is malformed ({exc}) — the whole list "
                    "is then read as empty, and the people it names would meet the "
                    "hold instead of the bot they were asked to check"
                )
        if (self.expiry_removals_expect_chats or "").strip():
            try:
                parse_tg_chat_id_list(self.expiry_removals_expect_chats)
            except ValueError as exc:
                errors.append(
                    f"EXPIRY_REMOVALS_EXPECT_CHATS is malformed ({exc}) — the whole "
                    "binding is then read as empty, and the hourly removal sweep "
                    "refuses to run even though ENABLE_EXPIRY_REMOVALS says it should"
                )
        if (self.referral_fx_rates_to_usd or "").strip():
            try:
                parse_fx_rates_to_usd(self.referral_fx_rates_to_usd)
            except ValueError as exc:
                errors.append(
                    f"REFERRAL_FX_RATES_TO_USD is malformed ({exc}) — the whole "
                    "table is then read as empty, and every rouble commission "
                    "accrues $0 to a partner who earned one"
                )
        if self.is_prod and self.cors_allowed_origins.strip() == "*":
            errors.append("CORS_ALLOWED_ORIGINS='*' is forbidden in production")
        if self.is_prod:
            if not self.private_channel_id:
                errors.append("PRIVATE_CHANNEL_ID is required in production")
            if not self.practice_chat_id:
                errors.append("PRACTICE_CHAT_ID is required in production")
            if self.private_channel_id and self.practice_chat_id == self.private_channel_id:
                errors.append("PRIVATE_CHANNEL_ID and PRACTICE_CHAT_ID must be different")
        if (self.no_charge_tg_ids or "").strip():
            try:
                parse_tg_id_allowlist(self.no_charge_tg_ids)
            except ValueError as exc:
                errors.append(
                    f"NO_CHARGE_TG_IDS is malformed ({exc}) — every payment in the "
                    "bot is then refused, for everyone, until this is fixed"
                )
        bot_mode = (self.bot_update_mode or "polling").strip().lower()
        if bot_mode not in {"polling", "webhook"}:
            errors.append("BOT_UPDATE_MODE must be polling or webhook")
        if bot_mode == "webhook":
            if not self.tg_webhook_secret.strip():
                errors.append("BOT_UPDATE_MODE=webhook requires TG_WEBHOOK_SECRET")
            webhook_path = self.tg_webhook_path.strip()
            if webhook_path and not webhook_path.startswith("/"):
                webhook_path = f"/{webhook_path}"
            if not webhook_path.startswith("/tg-webhook/"):
                errors.append("TG_WEBHOOK_PATH must start with /tg-webhook/")
            webhook_base_url = (self.tg_webhook_base_url or self.public_base_url).strip()
            if not webhook_base_url:
                errors.append("BOT_UPDATE_MODE=webhook requires TG_WEBHOOK_BASE_URL or PUBLIC_BASE_URL")
            if self.is_prod and not webhook_base_url.lower().startswith("https://"):
                errors.append("BOT_UPDATE_MODE=webhook requires an https webhook base URL in production")
        if self.enable_lava_live_checkout:
            if not self.lava_api_key:
                errors.append("ENABLE_LAVA_LIVE_CHECKOUT=true requires LAVA_API_KEY")
            if not self.lava_offer_id:
                errors.append("ENABLE_LAVA_LIVE_CHECKOUT=true requires LAVA_OFFER_ID")
            if not self.lava_api_base_url.lower().startswith("https://"):
                errors.append("LAVA_API_BASE_URL must use https")
            lava_auth_mode = (self.lava_webhook_auth_mode or "api_key").strip().lower()
            if lava_auth_mode in {"api_key", "x_api_key", "x-api-key"}:
                if not self.lava_webhook_api_key:
                    errors.append(
                        "ENABLE_LAVA_LIVE_CHECKOUT=true requires LAVA_WEBHOOK_API_KEY "
                        "for incoming Lava webhook authentication"
                    )
            elif lava_auth_mode == "basic":
                if not self.lava_webhook_basic_username or not self.lava_webhook_basic_password:
                    errors.append(
                        "ENABLE_LAVA_LIVE_CHECKOUT=true with Basic auth requires "
                        "LAVA_WEBHOOK_BASIC_USERNAME and LAVA_WEBHOOK_BASIC_PASSWORD"
                    )
            else:
                errors.append("LAVA_WEBHOOK_AUTH_MODE must be api_key or basic")
        if self.is_prod and self.stripe_secret_key:
            if not self.stripe_price_monthly_id:
                errors.append("STRIPE_PRICE_MONTHLY_ID is required when Stripe is configured in production")
            if not self.stripe_price_6m_id:
                errors.append("STRIPE_PRICE_6M_ID is required when Stripe is configured in production")
            if not self.stripe_price_annual_id:
                errors.append("STRIPE_PRICE_ANNUAL_ID is required when Stripe is configured in production")
        return errors


@lru_cache
def get_settings() -> Settings:
    return Settings()
