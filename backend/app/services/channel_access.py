"""Telegram access management for all subscription resources.

Uses aiogram Bot to issue one-time invite links on subscription creation,
and to kick users from every configured Telegram resource when access expires.

Kick = ban_chat_member + unban_chat_member, so the user can re-join after re-purchase.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# GK-434.1: an invite is already single-use (`member_limit=1`), but without an
# expiry an unused one stays redeemable forever — a link issued to somebody who
# never joined can be forwarded and used months later, by anybody. 30 days is
# the same window the gift flow uses, and it is at least as long as the shortest
# plan, so a buyer who is slow to join is never locked out of what they paid for.
INVITE_LINK_TTL_DAYS = 30

_ALREADY_REVOKED_MARKERS = (
    "member not found",
    "user not found",
    "participant_id_invalid",
    "user is deactivated",
)

_INVITE_ALREADY_REVOKED_MARKERS = (
    "invite link not found",
    "chat invite link not found",
    "invite_hash_invalid",
)

# GK-432: refusals no amount of retrying will change. `sub#2` sat at 647 failed
# attempts because Telegram will never let a bot remove a chat owner, and the
# hourly job had no way to tell that from a network blip. Everything here needs
# a *person* — demote the owner, restore the bot's admin rights, fix the chat id
# — so the correct response is to stop, record it, and say so once.
_PERMANENT_REFUSAL_MARKERS = (
    "can't remove chat owner",
    "cant remove chat owner",
    "user is an administrator of the chat",
    "user_admin_invalid",
    "not enough rights",
    "chat_admin_required",
    "need administrator rights",
    "chat not found",
    "bot is not a member",
    "method is available only for supergroups",
)


@dataclass(frozen=True)
class KickResult:
    success: bool
    retry_after: int | None = None
    error: str | None = None
    resource_results: tuple[KickResourceResult, ...] = ()
    #: GK-432: every failure here is one Telegram will keep giving. Retrying
    #: cannot help; only a human can.
    permanent: bool = False


@dataclass(frozen=True)
class TelegramAccessResource:
    key: str
    title: str
    chat_id: int = 0
    fallback_invite_link: str | None = None


@dataclass(frozen=True)
class InviteLinkResult:
    resource: TelegramAccessResource
    invite_link: str | None = None
    error: str | None = None

    @property
    def success(self) -> bool:
        return bool(self.invite_link)


@dataclass(frozen=True)
class InviteLinksResult:
    results: tuple[InviteLinkResult, ...]

    @property
    def all_success(self) -> bool:
        return bool(self.results) and all(result.success for result in self.results)

    @property
    def any_success(self) -> bool:
        return any(result.success for result in self.results)

    @property
    def storage_text(self) -> str | None:
        successful = [result for result in self.results if result.invite_link]
        if not successful:
            return None
        if len(successful) == 1 and len(self.results) == 1:
            return successful[0].invite_link
        return "\n".join(
            f"{result.resource.title}: {result.invite_link}"
            for result in successful
            if result.invite_link
        )

    @property
    def error_summary(self) -> str | None:
        failed = [
            f"{result.resource.key}: {result.error or 'invite link missing'}"
            for result in self.results
            if not result.success
        ]
        return "; ".join(failed) if failed else None


@dataclass(frozen=True)
class KickResourceResult:
    resource: TelegramAccessResource
    success: bool
    retry_after: int | None = None
    error: str | None = None
    permanent: bool = False


@dataclass(frozen=True)
class RevokeInviteLinkResult:
    resource: TelegramAccessResource | None
    invite_link: str
    success: bool
    retry_after: int | None = None
    error: str | None = None
    permanent: bool = False


@dataclass(frozen=True)
class RevokeInviteLinksResult:
    results: tuple[RevokeInviteLinkResult, ...]

    @property
    def success(self) -> bool:
        return bool(self.results) and all(result.success for result in self.results)

    @property
    def retry_after(self) -> int | None:
        values = [result.retry_after for result in self.results if result.retry_after is not None]
        return max(values) if values else None

    @property
    def error(self) -> str | None:
        if not self.results:
            return "no invite links parsed"
        failed = [
            f"{result.resource.key if result.resource else 'unknown'}: {result.error or 'failed'}"
            for result in self.results
            if not result.success
        ]
        return "; ".join(failed) if failed else None

    @property
    def permanent(self) -> bool:
        failed = [result for result in self.results if not result.success]
        return bool(failed) and all(result.permanent for result in failed)


@dataclass(frozen=True)
class SubscriptionAccessRevokeResult:
    success: bool
    retry_after: int | None = None
    error: str | None = None
    invite_result: RevokeInviteLinksResult | None = None
    kick_result: KickResult | None = None
    permanent: bool = False


def configured_access_resources() -> tuple[TelegramAccessResource, ...]:
    """Return Telegram resources that can grant or revoke subscription access."""
    resources = (
        TelegramAccessResource(
            key="community_channel",
            title="Community channel",
            chat_id=settings.private_channel_id,
            fallback_invite_link=_clean_link(settings.private_channel_invite_link),
        ),
        TelegramAccessResource(
            key="practice_chat",
            title="Practice chat",
            chat_id=settings.practice_chat_id,
            fallback_invite_link=_clean_link(settings.practice_chat_invite_link),
        ),
    )
    return tuple(
        resource
        for resource in resources
        if resource.chat_id or resource.fallback_invite_link
    )


async def create_invite_links(bot: Bot, name: str | None = None) -> InviteLinksResult:
    resources = configured_access_resources()
    if not resources:
        logger.warning("Telegram access resources not configured")
        return InviteLinksResult(())

    results = []
    for resource in resources:
        results.append(await _create_invite_link_for_resource(bot, resource, name=name))
    return InviteLinksResult(tuple(results))


async def create_invite_link(bot: Bot, name: str | None = None) -> str | None:
    """Backward-compatible wrapper returning a storable invite block."""
    result = await create_invite_links(bot, name=name)
    return result.storage_text


async def revoke_invite_links(bot: Bot, storage_text: str | None) -> RevokeInviteLinksResult:
    """Revoke subscription invite links stored on a subscription.

    Telegram kicks remove the user, but a previously issued invite link can let
    them rejoin. Revoking the stored links makes refund/cancel access removal
    complete while keeping future re-purchase possible through new links.
    """
    resources = configured_access_resources()
    targets = _invite_link_targets(storage_text, resources)
    if not targets:
        return RevokeInviteLinksResult(())

    results = []
    for resource, invite_link in targets:
        fallback_resource = _fallback_resource_for_link(invite_link, resources)
        if fallback_resource is not None:
            # A stored link that equals the configured shared fallback is a
            # channel-wide credential the bot cannot revoke per-user. Treat it as a
            # revocation FAILURE so a kick never "completes" while the former member
            # keeps a working link (GK-406).
            logger.error(
                "stored invite link for %s is the unrevocable configured fallback; "
                "reporting revoke failure",
                fallback_resource.key,
            )
            results.append(
                RevokeInviteLinkResult(
                    resource or fallback_resource,
                    invite_link,
                    False,
                    error="unrevocable configured fallback invite link",
                    permanent=True,
                )
            )
            continue
        if resource is None:
            results.append(
                RevokeInviteLinkResult(
                    resource,
                    invite_link,
                    False,
                    error="could not map invite link to configured resource",
                    permanent=True,
                )
            )
            continue
        try:
            results.append(await _revoke_invite_link_for_resource(bot, resource, invite_link))
        except Exception as e:
            logger.exception("revoke_invite_link crashed in %s", resource.key)
            results.append(RevokeInviteLinkResult(resource, invite_link, False, error=str(e)))
    return RevokeInviteLinksResult(tuple(results))


async def revoke_subscription_access(
    bot: Bot,
    tg_id: int,
    invite_link: str | None,
) -> SubscriptionAccessRevokeResult:
    """Revoke all stored access material for a subscription.

    The invite-link revoke closes the "unused one-time link" hole; the kick
    removes an already-joined member. Both are best-effort and combined into the
    durable access-revoke fields on the subscription row by callers.
    """
    invite_result: RevokeInviteLinksResult | None = None
    if (invite_link or "").strip():
        try:
            invite_result = await revoke_invite_links(bot, invite_link)
        except Exception as e:
            logger.exception("revoke_invite_links crashed for tg_id=%s", tg_id)
            invite_result = RevokeInviteLinksResult(
                (RevokeInviteLinkResult(None, invite_link.strip(), False, error=str(e)),)
            )

    try:
        kick_result = await kick_user(bot, tg_id)
    except Exception as e:
        # An unexpected crash is not a Telegram refusal; keep retrying it.
        logger.exception("kick_user crashed for tg_id=%s", tg_id)
        kick_result = KickResult(False, error=str(e))

    retry_after_values = [
        value
        for value in (
            invite_result.retry_after if invite_result is not None else None,
            kick_result.retry_after,
        )
        if value is not None
    ]
    errors = [
        error
        for error in (
            invite_result.error if invite_result is not None else None,
            kick_result.error,
        )
        if error
    ]
    success = (invite_result.success if invite_result is not None else True) and kick_result.success
    # GK-432: permanent only if nothing that failed could succeed on a retry.
    permanently_stuck = [
        part.permanent
        for part in (invite_result, kick_result)
        if part is not None and not part.success
    ]
    return SubscriptionAccessRevokeResult(
        success,
        retry_after=max(retry_after_values) if retry_after_values else None,
        error="; ".join(errors) if errors else None,
        invite_result=invite_result,
        kick_result=kick_result,
        permanent=bool(permanently_stuck) and all(permanently_stuck),
    )


def invite_link_expires_at(now: datetime | None = None) -> datetime:
    """When an invite issued right now stops being redeemable (GK-434.1)."""
    return (now or datetime.now(UTC)) + timedelta(days=INVITE_LINK_TTL_DAYS)


async def _create_invite_link_for_resource(
    bot: Bot,
    resource: TelegramAccessResource,
    *,
    name: str | None = None,
) -> InviteLinkResult:
    # A configured static fallback link is channel-wide and unrevocable, so it is
    # never handed to a subscriber (GK-406). If a per-user one-time link cannot be
    # issued, surface an error instead — the caller flags the payment for an admin
    # to regenerate rather than delivering a shared credential.
    if not resource.chat_id:
        logger.warning("%s chat_id not configured", resource.key)
        return InviteLinkResult(resource, error=f"{resource.key} chat_id not configured")
    try:
        link = await bot.create_chat_invite_link(
            chat_id=resource.chat_id,
            name=_invite_name(name, resource),
            member_limit=1,
            expire_date=invite_link_expires_at(),
            creates_join_request=False,
        )
        return InviteLinkResult(resource, invite_link=link.invite_link)
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        logger.error("create_invite_link failed for %s: %s", resource.key, e)
        return InviteLinkResult(resource, error=str(e))


async def _revoke_invite_link_for_resource(
    bot: Bot,
    resource: TelegramAccessResource,
    invite_link: str,
) -> RevokeInviteLinkResult:
    if not resource.chat_id:
        return RevokeInviteLinkResult(
            resource,
            invite_link,
            False,
            error=f"{resource.key} chat_id not configured",
            permanent=True,
        )
    try:
        await bot.revoke_chat_invite_link(chat_id=resource.chat_id, invite_link=invite_link)
        return RevokeInviteLinkResult(resource, invite_link, True)
    except TelegramRetryAfter as e:
        logger.warning(
            "revoke_invite_link rate-limited in %s: retry after %ss",
            resource.key,
            e.retry_after,
        )
        return RevokeInviteLinkResult(
            resource,
            invite_link,
            False,
            retry_after=e.retry_after,
            error=f"retry_after={e.retry_after}",
        )
    except TelegramBadRequest as e:
        error = str(e)
        if _is_invite_already_revoked(error):
            logger.info("invite link already revoked in %s: %s", resource.key, e)
            return RevokeInviteLinkResult(resource, invite_link, True, error=error)
        logger.warning("revoke_invite_link failed in %s: %s", resource.key, e)
        return RevokeInviteLinkResult(
            resource, invite_link, False, error=error, permanent=_is_permanent_refusal(error)
        )
    except TelegramForbiddenError as e:
        logger.warning("revoke_invite_link failed in %s: %s", resource.key, e)
        return RevokeInviteLinkResult(resource, invite_link, False, error=str(e), permanent=True)


async def kick_user(bot: Bot, tg_id: int) -> KickResult:
    resources = configured_access_resources()
    if not resources:
        return KickResult(False, error="Telegram access resources not configured")

    results = []
    for resource in resources:
        try:
            results.append(await _kick_user_from_resource(bot, tg_id, resource))
        except Exception as e:
            logger.exception("kick_user(%s) crashed in %s", tg_id, resource.key)
            results.append(KickResourceResult(resource, False, error=str(e)))

    success = all(result.success for result in results)
    retry_after_values = [
        result.retry_after for result in results if result.retry_after is not None
    ]
    retry_after = max(retry_after_values) if retry_after_values else None
    failed = [result for result in results if not result.success]
    return KickResult(
        success,
        retry_after=retry_after,
        error=None if success else _kick_error_summary(results),
        resource_results=tuple(results),
        # Only when *every* failure is permanent. One transient failure alongside
        # a permanent one is still worth another hour — the bound catches it.
        permanent=bool(failed) and all(result.permanent for result in failed),
    )


async def _kick_user_from_resource(
    bot: Bot,
    tg_id: int,
    resource: TelegramAccessResource,
) -> KickResourceResult:
    if not resource.chat_id:
        return KickResourceResult(
            resource,
            False,
            error=f"{resource.key} chat_id not configured",
            permanent=True,
        )
    try:
        await bot.ban_chat_member(chat_id=resource.chat_id, user_id=tg_id)
        await bot.unban_chat_member(chat_id=resource.chat_id, user_id=tg_id, only_if_banned=True)
        return KickResourceResult(resource, True)
    except TelegramRetryAfter as e:
        logger.warning(
            "kick_user(%s) rate-limited in %s: retry after %ss",
            tg_id,
            resource.key,
            e.retry_after,
        )
        return KickResourceResult(
            resource,
            False,
            retry_after=e.retry_after,
            error=f"retry_after={e.retry_after}",
        )
    except TelegramBadRequest as e:
        error = str(e)
        if _is_already_revoked(error):
            logger.info("kick_user(%s) already revoked in %s: %s", tg_id, resource.key, e)
            return KickResourceResult(resource, True, error=error)
        logger.warning("kick_user(%s) failed in %s: %s", tg_id, resource.key, e)
        return KickResourceResult(
            resource, False, error=error, permanent=_is_permanent_refusal(error)
        )
    except TelegramForbiddenError as e:
        # The bot was blocked or is no longer in the chat. Either way it will
        # never remove this member; a person has to restore access or accept it.
        logger.warning("kick_user(%s) failed in %s: %s", tg_id, resource.key, e)
        return KickResourceResult(resource, False, error=str(e), permanent=True)


def _is_already_revoked(error: str) -> bool:
    normalized = error.lower()
    return any(marker in normalized for marker in _ALREADY_REVOKED_MARKERS)


def _is_permanent_refusal(error: str) -> bool:
    normalized = (error or "").lower()
    return any(marker in normalized for marker in _PERMANENT_REFUSAL_MARKERS)


def _is_invite_already_revoked(error: str) -> bool:
    normalized = error.lower()
    return any(marker in normalized for marker in _INVITE_ALREADY_REVOKED_MARKERS)


def _clean_link(value: str | None) -> str | None:
    clean = (value or "").strip()
    return clean or None


def _invite_name(name: str | None, resource: TelegramAccessResource) -> str:
    prefix = name or "membership_saas invite"
    return f"{prefix} {resource.key}"[:32]


def _invite_link_targets(
    storage_text: str | None,
    resources: tuple[TelegramAccessResource, ...],
) -> list[tuple[TelegramAccessResource | None, str]]:
    text = (storage_text or "").strip()
    if not text:
        return []

    by_title = {resource.title.lower(): resource for resource in resources}
    by_key = {resource.key.lower(): resource for resource in resources}
    targets: list[tuple[TelegramAccessResource | None, str]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        label = ""
        link = line
        if line.startswith(("http://", "https://")):
            link = line
        elif ":" in line:
            label, link = (part.strip() for part in line.split(":", 1))
        if not link.startswith(("http://", "https://")):
            continue
        resource = by_title.get(label.lower()) or by_key.get(label.lower())
        if resource is None and len(resources) == 1 and not label:
            resource = resources[0]
        targets.append((resource, link))
    return targets


def _fallback_resource_for_link(
    invite_link: str,
    resources: tuple[TelegramAccessResource, ...],
) -> TelegramAccessResource | None:
    for resource in resources:
        if resource.fallback_invite_link == invite_link:
            return resource
    return None


def _kick_error_summary(results: list[KickResourceResult]) -> str | None:
    failed = [
        f"{result.resource.key}: {result.error or 'failed'}"
        for result in results
        if not result.success
    ]
    return "; ".join(failed) if failed else None
