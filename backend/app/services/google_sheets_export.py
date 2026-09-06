from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol, TypeVar
from urllib.parse import quote

import httpx
from jose import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.config import Settings, get_settings
from app.db.models import (
    Payment,
    Plan,
    ReconciliationRun,
    Referral,
    ReferralCommission,
    ReferralPayoutBatch,
    Subscription,
    User,
)

logger = logging.getLogger(__name__)
T = TypeVar("T")

GOOGLE_SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
MANUAL_NOTES_SHEET = "Manual Notes"
MANUAL_NOTES_HEADERS = (
    "record_type",
    "stable_id",
    "manual_note",
    "manual_status",
    "owner",
    "updated_at",
)


class GoogleSheetsConfigError(RuntimeError):
    pass


class GoogleSheetsCredentialsError(GoogleSheetsConfigError):
    """The service account could not be loaded — before any request exists.

    GK-479: this is the class of failure that reached Grant as a bare 500. The
    mounted key file had been deleted by a deploy ten weeks earlier, so the
    loader raised `FileNotFoundError`, which is nobody's `except` clause. Every
    way the credential can be unusable — absent file, malformed JSON, missing
    fields, a private key that is not a key — now arrives here instead, saying
    which one it was. Subclasses `GoogleSheetsConfigError` so the router keeps
    answering 400: a credential the process cannot load is our configuration,
    not Google's fault.
    """


class GoogleSheetsApiError(RuntimeError):
    pass


class GoogleSheetsQuotaError(GoogleSheetsApiError):
    pass


class GoogleSheetsTransportError(GoogleSheetsApiError):
    """Google was never reached — DNS, TCP, TLS or a timeout.

    Distinct from `GoogleSheetsApiError` because the answer is different: a 403
    means fix the sharing, a `ConnectError` means fix the network. Both are 502
    to the caller, and the message says which.
    """


@dataclass(frozen=True)
class SheetSpec:
    name: str
    headers: tuple[str, ...]


@dataclass(frozen=True)
class ExportRow:
    values: tuple[Any, ...]

    @property
    def stable_id(self) -> str:
        return str(self.values[0])


@dataclass
class SheetSyncSummary:
    sheet: str
    rows: int
    updated: int
    appended: int
    duplicate_existing_ids: list[str] = field(default_factory=list)


@dataclass
class GoogleSheetsExportSummary:
    spreadsheet_id: str
    dry_run: bool
    manual_notes_sheet: str
    manual_notes_initialized: bool
    sheets: list[SheetSyncSummary]
    operations: list[str]

    @property
    def rows(self) -> int:
        return sum(sheet.rows for sheet in self.sheets)

    @property
    def updated(self) -> int:
        return sum(sheet.updated for sheet in self.sheets)

    @property
    def appended(self) -> int:
        return sum(sheet.appended for sheet in self.sheets)


class GoogleSheetsClient(Protocol):
    spreadsheet_id: str

    async def get_spreadsheet(self) -> dict[str, Any]:
        ...

    async def batch_update(self, requests: list[dict[str, Any]]) -> dict[str, Any]:
        ...

    async def get_values(self, range_name: str) -> list[list[Any]]:
        ...

    async def update_values(self, range_name: str, values: list[list[Any]]) -> dict[str, Any]:
        ...

    async def batch_update_values(self, data: list[dict[str, Any]]) -> dict[str, Any]:
        ...

    async def append_values(self, range_name: str, values: list[list[Any]]) -> dict[str, Any]:
        ...


CRM_EXPORT_SPECS: tuple[SheetSpec, ...] = (
    SheetSpec(
        name="Users",
        headers=(
            "stable_id",
            "user_id",
            "tg_id",
            "username",
            "first_name",
            "last_name",
            "language",
            "joined_at",
            "referral_code",
            "referrer_id",
            "is_banned",
            "bonus_days",
            "stripe_customer_id",
        ),
    ),
    SheetSpec(
        name="Subscriptions",
        headers=(
            "stable_id",
            "subscription_id",
            "user_id",
            "username",
            "plan_code",
            "plan_name",
            "status",
            "source",
            "provider",
            "provider_subscription_id",
            "provider_status",
            "started_at",
            "current_period_start",
            "current_period_end",
            "expires_at",
            "grace_ends_at",
            "cancel_at_period_end",
            "access_revoked_at",
            "invite_link",
            # GK-483: appended last on purpose. Every synced row is rewritten at
            # full header width each run, but rows for subscriptions that have
            # since left the DB are not — inserting a column mid-row would shift
            # those stale rows out of alignment with their own headers.
            "is_comp",
        ),
    ),
    SheetSpec(
        name="Payments",
        headers=(
            "stable_id",
            "payment_id",
            "user_id",
            "username",
            "plan_code",
            "plan_name",
            "provider",
            "amount",
            "currency",
            "status",
            "external_id",
            "stripe_checkout_session_id",
            "stripe_invoice_id",
            "lava_invoice_id",
            "lava_subscription_id",
            "provider_event_id",
            "tx_network",
            "tx_hash",
            "tx_confirmed_at",
            "is_renewal",
            "is_gift",
            "gift_recipient_id",
            "created_at",
            "approved_at",
            "approved_by",
        ),
    ),
    SheetSpec(
        name="Referrals",
        headers=(
            "stable_id",
            "referral_id",
            "referrer_id",
            "referrer_username",
            "referee_id",
            "referee_username",
            "first_payment_id",
            "bonus_days_granted",
            "created_at",
        ),
    ),
    SheetSpec(
        name="Referral Commissions",
        headers=(
            "stable_id",
            "commission_id",
            "referral_id",
            "referrer_id",
            "referrer_username",
            "referee_id",
            "referee_username",
            "source_payment_id",
            "source_provider",
            "source_invoice_id",
            "source_amount",
            "source_currency",
            "amount_usd",
            "status",
            "vests_at",
            "vested_at",
            "cancelled_at",
            "paid_at",
            "payout_batch_id",
            "cancellation_reason",
            "created_at",
            "updated_at",
        ),
    ),
    SheetSpec(
        name="Payouts",
        headers=(
            "stable_id",
            "payout_batch_id",
            "status",
            "currency",
            "threshold_amount",
            "total_amount",
            "commission_count",
            "note",
            "created_at",
            "sent_at",
            "paid_at",
            "cancelled_at",
        ),
    ),
    SheetSpec(
        name="Reconciliation Summary",
        headers=(
            "stable_id",
            "run_id",
            "status",
            "triggered_by",
            "provider_scope",
            "started_at",
            "finished_at",
            "items_count",
            "open_items_count",
            "error",
            "summary_json",
        ),
    ),
)


@dataclass(frozen=True)
class ServiceAccountCredentials:
    client_email: str
    private_key: str
    token_uri: str = GOOGLE_TOKEN_URI


class GoogleSheetsHttpClient:
    """Small Sheets API client using a Google service-account JWT flow."""

    def __init__(
        self,
        spreadsheet_id: str,
        credentials: ServiceAccountCredentials,
        *,
        max_retries: int = 5,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.spreadsheet_id = spreadsheet_id
        self.credentials = credentials
        self.max_retries = max(1, max_retries)
        self.timeout_seconds = timeout_seconds
        self._token: str | None = None
        self._token_expires_at = 0.0

    async def get_spreadsheet(self) -> dict[str, Any]:
        return await self._request(
            "GET",
            "",
            params={
                "fields": (
                    "sheets(properties(sheetId,title,"
                    "gridProperties(rowCount,columnCount,frozenRowCount)))"
                )
            },
        )

    async def batch_update(self, requests: list[dict[str, Any]]) -> dict[str, Any]:
        return await self._request("POST", ":batchUpdate", json={"requests": requests})

    async def get_values(self, range_name: str) -> list[list[Any]]:
        body = await self._request("GET", f"/values/{quote(range_name, safe='')}")
        return list(body.get("values") or [])

    async def update_values(self, range_name: str, values: list[list[Any]]) -> dict[str, Any]:
        return await self._request(
            "PUT",
            f"/values/{quote(range_name, safe='')}",
            params={"valueInputOption": "RAW"},
            json={"values": values},
        )

    async def batch_update_values(self, data: list[dict[str, Any]]) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/values:batchUpdate",
            json={"valueInputOption": "RAW", "data": data},
        )

    async def append_values(self, range_name: str, values: list[list[Any]]) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/values/{quote(range_name, safe='')}:append",
            params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
            json={"values": values},
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        async def send() -> dict[str, Any]:
            token = await self._access_token()
            url = f"https://sheets.googleapis.com/v4/spreadsheets/{self.spreadsheet_id}{path}"
            headers = {"Authorization": f"Bearer {token}"}
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await _send(client, method, url, headers=headers, **kwargs)
            if response.status_code == 401:
                self._token = None
                token = await self._access_token()
                headers = {"Authorization": f"Bearer {token}"}
                async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                    response = await _send(client, method, url, headers=headers, **kwargs)
            if response.status_code >= 400:
                if _is_quota_response(response):
                    raise GoogleSheetsQuotaError(_response_error_message(response))
                raise GoogleSheetsApiError(_response_error_message(response))
            if not response.content:
                return {}
            try:
                return response.json()
            except ValueError as exc:
                raise GoogleSheetsApiError(
                    f"Google Sheets API {response.status_code} returned a body that "
                    f"is not JSON: {response.text[:200]}"
                ) from exc

        return await call_with_quota_backoff(send, max_attempts=self.max_retries)

    async def _access_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_expires_at - 60:
            return self._token

        issued_at = int(now)
        try:
            assertion = jwt.encode(
                {
                    "iss": self.credentials.client_email,
                    "scope": GOOGLE_SHEETS_SCOPE,
                    "aud": self.credentials.token_uri,
                    "iat": issued_at,
                    "exp": issued_at + 3600,
                },
                self.credentials.private_key,
                algorithm="RS256",
            )
        except Exception as exc:  # noqa: BLE001 - jose raises several unrelated types
            raise GoogleSheetsCredentialsError(
                f"The service-account private_key for {self.credentials.client_email} "
                f"could not be used to sign the token request: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await _send(
                client,
                "POST",
                self.credentials.token_uri,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                },
            )
        if response.status_code >= 400:
            raise GoogleSheetsApiError(_response_error_message(response))
        try:
            body = response.json()
        except ValueError as exc:
            raise GoogleSheetsApiError(
                f"Google OAuth returned a body that is not JSON: {response.text[:200]}"
            ) from exc
        token = body.get("access_token")
        if not token:
            raise GoogleSheetsApiError("Google OAuth response did not include access_token")
        self._token = str(token)
        self._token_expires_at = now + int(body.get("expires_in") or 3600)
        return self._token


class DryRunGoogleSheetsClient:
    """In-memory Sheets client used for dry runs and fast unit tests."""

    def __init__(self, spreadsheet_id: str = "dry-run") -> None:
        self.spreadsheet_id = spreadsheet_id
        self.operations: list[str] = []
        self._sheets: dict[str, dict[str, Any]] = {}
        self._values: dict[str, list[list[Any]]] = {}
        self._next_sheet_id = 1

    async def get_spreadsheet(self) -> dict[str, Any]:
        self.operations.append("get_spreadsheet")
        return {
            "sheets": [
                {
                    "properties": {
                        "sheetId": props["sheetId"],
                        "title": title,
                        "gridProperties": {"frozenRowCount": 1},
                    }
                }
                for title, props in self._sheets.items()
            ]
        }

    async def batch_update(self, requests: list[dict[str, Any]]) -> dict[str, Any]:
        self.operations.append(f"batchUpdate:{len(requests)}")
        replies: list[dict[str, Any]] = []
        for request in requests:
            add_sheet = request.get("addSheet")
            if add_sheet:
                title = add_sheet.get("properties", {}).get("title")
                if title and title not in self._sheets:
                    sheet_id = self._next_sheet_id
                    self._next_sheet_id += 1
                    self._sheets[title] = {"sheetId": sheet_id}
                    self._values[title] = []
                    replies.append({"addSheet": {"properties": {"sheetId": sheet_id, "title": title}}})
            update_props = request.get("updateSheetProperties")
            if update_props:
                replies.append({"updateSheetProperties": update_props.get("properties", {})})
        return {"replies": replies}

    async def get_values(self, range_name: str) -> list[list[Any]]:
        self.operations.append(f"values.get:{range_name}")
        sheet, start_row, start_col, end_col = _parse_simple_a1(range_name)
        rows = self._values.get(sheet, [])
        out: list[list[Any]] = []
        for row in rows[max(0, start_row - 1):]:
            out.append(row[max(0, start_col - 1):end_col])
        return out

    async def update_values(self, range_name: str, values: list[list[Any]]) -> dict[str, Any]:
        self.operations.append(f"values.update:{range_name}:{len(values)}")
        self._write_range(range_name, values)
        return {"updatedRows": len(values)}

    async def batch_update_values(self, data: list[dict[str, Any]]) -> dict[str, Any]:
        self.operations.append(f"values.batchUpdate:{len(data)}")
        for item in data:
            self._write_range(str(item["range"]), list(item["values"]))
        return {"totalUpdatedRows": len(data)}

    async def append_values(self, range_name: str, values: list[list[Any]]) -> dict[str, Any]:
        sheet, _start_row, _start_col, _end_col = _parse_simple_a1(range_name)
        self.operations.append(f"values.append:{sheet}:{len(values)}")
        self._values.setdefault(sheet, [])
        self._values[sheet].extend([list(row) for row in values])
        return {"updates": {"updatedRows": len(values)}}

    def _write_range(self, range_name: str, values: list[list[Any]]) -> None:
        sheet, start_row, start_col, _end_col = _parse_simple_a1(range_name)
        self._values.setdefault(sheet, [])
        rows = self._values[sheet]
        while len(rows) < start_row - 1:
            rows.append([])
        for index, values_row in enumerate(values, start=start_row - 1):
            while len(rows) <= index:
                rows.append([])
            row = rows[index]
            while len(row) < start_col - 1:
                row.append("")
            for col_offset, value in enumerate(values_row, start=start_col - 1):
                while len(row) <= col_offset:
                    row.append("")
                row[col_offset] = value


async def export_crm_to_google_sheets(
    session: AsyncSession,
    *,
    dry_run: bool = False,
    settings: Settings | None = None,
    client: GoogleSheetsClient | None = None,
) -> GoogleSheetsExportSummary:
    cfg = settings or get_settings()

    # GK-479: the credential is loaded before the database is read, not after.
    # The 500 Grant saw took 120 ms — a full 336-row collection across seven
    # tables, and only then the local failure that made all of it pointless.
    if client is None:
        spreadsheet_id = cfg.google_sheets_spreadsheet_id.strip()
        if dry_run:
            client = DryRunGoogleSheetsClient(spreadsheet_id or "dry-run")
        else:
            if not spreadsheet_id:
                raise GoogleSheetsConfigError("GOOGLE_SHEETS_SPREADSHEET_ID is not configured")
            client = GoogleSheetsHttpClient(
                spreadsheet_id,
                _load_service_account_credentials(cfg),
                max_retries=cfg.google_sheets_quota_max_retries,
            )

    rows_by_sheet = await collect_crm_export_rows(session)

    await ensure_crm_sheet_layout(client)
    manual_notes_initialized = await ensure_manual_notes_sheet(client)

    sheet_summaries: list[SheetSyncSummary] = []
    for spec in CRM_EXPORT_SPECS:
        sheet_summaries.append(await sync_sheet_rows(client, spec, rows_by_sheet[spec.name]))

    operations = list(getattr(client, "operations", []))
    return GoogleSheetsExportSummary(
        spreadsheet_id=client.spreadsheet_id,
        dry_run=dry_run,
        manual_notes_sheet=MANUAL_NOTES_SHEET,
        manual_notes_initialized=manual_notes_initialized,
        sheets=sheet_summaries,
        operations=operations,
    )


async def collect_crm_export_rows(session: AsyncSession) -> dict[str, list[ExportRow]]:
    referrer = aliased(User)
    referee = aliased(User)
    commission_referrer = aliased(User)
    commission_referee = aliased(User)

    users = (await session.execute(select(User).order_by(User.id.asc()))).scalars().all()
    subscriptions = (
        await session.execute(
            select(Subscription, User, Plan)
            .join(User, User.id == Subscription.user_id)
            .outerjoin(Plan, Plan.id == Subscription.plan_id)
            .order_by(Subscription.id.asc())
        )
    ).all()
    payments = (
        await session.execute(
            select(Payment, User, Plan)
            .join(User, User.id == Payment.user_id)
            .outerjoin(Plan, Plan.id == Payment.plan_id)
            .order_by(Payment.id.asc())
        )
    ).all()
    referrals = (
        await session.execute(
            select(Referral, referrer, referee)
            .join(referrer, referrer.id == Referral.referrer_id)
            .join(referee, referee.id == Referral.referee_id)
            .order_by(Referral.id.asc())
        )
    ).all()
    commissions = (
        await session.execute(
            select(ReferralCommission, commission_referrer, commission_referee)
            .join(commission_referrer, commission_referrer.id == ReferralCommission.referrer_id)
            .join(commission_referee, commission_referee.id == ReferralCommission.referee_id)
            .order_by(ReferralCommission.id.asc())
        )
    ).all()
    payouts = (
        await session.execute(select(ReferralPayoutBatch).order_by(ReferralPayoutBatch.id.asc()))
    ).scalars().all()
    reconciliation_runs = (
        await session.execute(
            select(ReconciliationRun).order_by(ReconciliationRun.started_at.desc()).limit(500)
        )
    ).scalars().all()

    return {
        "Users": [
            _row(
                f"user:{user.id}",
                user.id,
                user.tg_id,
                user.username,
                user.first_name,
                user.last_name,
                user.language,
                user.joined_at,
                user.referral_code,
                user.referrer_id,
                user.is_banned,
                user.bonus_days,
                user.stripe_customer_id,
            )
            for user in users
        ],
        "Subscriptions": [
            _row(
                f"subscription:{sub.id}",
                sub.id,
                user.id,
                user.username,
                plan.code if plan else "",
                plan.name if plan else "",
                sub.status,
                sub.source,
                sub.provider,
                sub.provider_subscription_id,
                sub.provider_status,
                sub.started_at,
                sub.current_period_start,
                sub.current_period_end,
                sub.expires_at,
                sub.grace_ends_at,
                sub.cancel_at_period_end,
                sub.access_revoked_at,
                sub.invite_link,
                # GK-483: without this the sheet reproduces exactly the inflation
                # the panel now avoids — the team would read as paying members to
                # anyone counting rows in the CRM.
                bool(sub.is_comp),
            )
            for sub, user, plan in subscriptions
        ],
        "Payments": [
            _row(
                f"payment:{payment.id}",
                payment.id,
                user.id,
                user.username,
                plan.code if plan else "",
                plan.name if plan else "",
                payment.provider,
                payment.amount,
                payment.currency,
                payment.status,
                payment.external_id,
                payment.stripe_checkout_session_id,
                payment.stripe_invoice_id,
                payment.lava_invoice_id,
                payment.lava_subscription_id,
                payment.provider_event_id,
                payment.tx_network,
                payment.tx_hash,
                payment.tx_confirmed_at,
                payment.is_renewal,
                payment.is_gift,
                payment.gift_recipient_id,
                payment.created_at,
                payment.approved_at,
                payment.approved_by,
            )
            for payment, user, plan in payments
        ],
        "Referrals": [
            _row(
                f"referral:{referral.id}",
                referral.id,
                referral.referrer_id,
                referrer_user.username,
                referral.referee_id,
                referee_user.username,
                referral.first_payment_id,
                referral.bonus_days_granted,
                referral.created_at,
            )
            for referral, referrer_user, referee_user in referrals
        ],
        "Referral Commissions": [
            _row(
                f"commission:{commission.id}",
                commission.id,
                commission.referral_id,
                commission.referrer_id,
                referrer_user.username,
                commission.referee_id,
                referee_user.username,
                commission.source_payment_id,
                commission.source_provider,
                commission.source_invoice_id,
                commission.source_amount,
                commission.source_currency,
                commission.amount_usd,
                commission.status,
                commission.vests_at,
                commission.vested_at,
                commission.cancelled_at,
                commission.paid_at,
                commission.payout_batch_id,
                commission.cancellation_reason,
                commission.created_at,
                commission.updated_at,
            )
            for commission, referrer_user, referee_user in commissions
        ],
        "Payouts": [
            _row(
                f"payout:{payout.id}",
                payout.id,
                payout.status,
                payout.currency,
                payout.threshold_amount,
                payout.total_amount,
                payout.commission_count,
                payout.note,
                payout.created_at,
                payout.sent_at,
                payout.paid_at,
                payout.cancelled_at,
            )
            for payout in payouts
        ],
        "Reconciliation Summary": [
            _row(
                f"reconciliation_run:{run.id}",
                run.id,
                run.status,
                run.triggered_by,
                run.provider_scope,
                run.started_at,
                run.finished_at,
                run.items_count,
                run.open_items_count,
                run.error,
                run.summary or {},
            )
            for run in reconciliation_runs
        ],
    }


async def ensure_crm_sheet_layout(client: GoogleSheetsClient) -> None:
    metadata = await client.get_spreadsheet()
    existing: dict[str, dict[str, Any]] = {}
    for sheet in metadata.get("sheets") or []:
        props = sheet.get("properties") or {}
        title = props.get("title")
        if title:
            existing[str(title)] = props

    requests: list[dict[str, Any]] = []
    desired_titles = [spec.name for spec in CRM_EXPORT_SPECS] + [MANUAL_NOTES_SHEET]
    for title in desired_titles:
        props = existing.get(title)
        if props is None:
            requests.append(
                {
                    "addSheet": {
                        "properties": {
                            "title": title,
                            "gridProperties": {"frozenRowCount": 1},
                        }
                    }
                }
            )
        else:
            sheet_id = props.get("sheetId")
            if sheet_id is not None:
                requests.append(
                    {
                        "updateSheetProperties": {
                            "properties": {
                                "sheetId": sheet_id,
                                "gridProperties": {"frozenRowCount": 1},
                            },
                            "fields": "gridProperties.frozenRowCount",
                        }
                    }
                )

    if requests:
        await client.batch_update(requests)


async def ensure_manual_notes_sheet(client: GoogleSheetsClient) -> bool:
    header_range = _a1_range(MANUAL_NOTES_SHEET, 1, 1, 1, len(MANUAL_NOTES_HEADERS))
    existing = await client.get_values(header_range)
    if existing and any(str(cell).strip() for cell in existing[0]):
        return False
    await client.update_values(header_range, [list(MANUAL_NOTES_HEADERS)])
    return True


async def sync_sheet_rows(
    client: GoogleSheetsClient,
    spec: SheetSpec,
    rows: Sequence[ExportRow],
) -> SheetSyncSummary:
    header_range = _a1_range(spec.name, 1, 1, 1, len(spec.headers))
    await client.update_values(header_range, [list(spec.headers)])

    existing_ids = await client.get_values(_a1_column(spec.name, 1, start_row=2))
    row_by_stable_id: dict[str, int] = {}
    duplicate_existing_ids: list[str] = []
    for row_number, existing_row in enumerate(existing_ids, start=2):
        stable_id = str(existing_row[0]).strip() if existing_row else ""
        if not stable_id:
            continue
        if stable_id in row_by_stable_id:
            duplicate_existing_ids.append(stable_id)
            continue
        row_by_stable_id[stable_id] = row_number

    update_data: list[dict[str, Any]] = []
    append_rows: list[list[Any]] = []
    for export_row in rows:
        values = list(export_row.values)
        if len(values) != len(spec.headers):
            raise ValueError(
                f"{spec.name} row {export_row.stable_id} has {len(values)} cells; "
                f"expected {len(spec.headers)}"
            )
        row_number = row_by_stable_id.get(export_row.stable_id)
        if row_number is None:
            append_rows.append(values)
        else:
            update_data.append(
                {
                    "range": _a1_range(spec.name, row_number, 1, row_number, len(spec.headers)),
                    "values": [values],
                }
            )

    for chunk in _chunks(update_data, 500):
        await client.batch_update_values(chunk)
    for chunk in _chunks(append_rows, 500):
        await client.append_values(_a1_table(spec.name, len(spec.headers)), chunk)

    return SheetSyncSummary(
        sheet=spec.name,
        rows=len(rows),
        updated=len(update_data),
        appended=len(append_rows),
        duplicate_existing_ids=duplicate_existing_ids,
    )


async def call_with_quota_backoff(
    fn: Callable[[], Awaitable[Any]],
    *,
    max_attempts: int = 5,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    base_delay: float = 1.0,
    jitter: Callable[[], float] = random.random,
) -> Any:
    attempts = max(1, max_attempts)
    last_error: GoogleSheetsQuotaError | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except GoogleSheetsQuotaError as exc:
            last_error = exc
            if attempt >= attempts:
                break
            delay = min(30.0, base_delay * (2 ** (attempt - 1)))
            await sleep(delay + jitter())
    assert last_error is not None
    raise last_error


def _load_service_account_credentials(settings: Settings) -> ServiceAccountCredentials:
    """Load the service account, or say precisely why it cannot be loaded.

    GK-479: every raise below is a `GoogleSheetsCredentialsError` naming the
    source it came from, because the operator's next action differs per case
    and "Internal Server Error" told Grant none of them. Nothing here performs
    I/O against Google, so this is also safe to call from the config endpoint.
    """
    raw_json = settings.google_sheets_service_account_json.strip()
    file_path = settings.google_sheets_service_account_file.strip()
    if raw_json:
        source = "GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON"
        try:
            payload = json.loads(raw_json)
        except ValueError as exc:
            raise GoogleSheetsCredentialsError(
                f"{source} is set ({len(raw_json)} chars) but is not valid JSON: {exc}. "
                "A PEM private key pasted in place of the whole key file fails exactly "
                "this way; paste the entire downloaded JSON, on one line."
            ) from exc
    elif file_path:
        source = f"GOOGLE_SHEETS_SERVICE_ACCOUNT_FILE ({file_path})"
        try:
            text = Path(file_path).read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise GoogleSheetsCredentialsError(
                f"{source} names a file that does not exist in this container. "
                "A key stored inside a release directory is deleted by the next "
                "deploy — prefer GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON, which the "
                "deploy carries forward with the rest of the .env."
            ) from exc
        except OSError as exc:
            raise GoogleSheetsCredentialsError(
                f"{source} could not be read: {type(exc).__name__}: {exc}"
            ) from exc
        except UnicodeDecodeError as exc:
            raise GoogleSheetsCredentialsError(
                f"{source} is not UTF-8 text: {exc}"
            ) from exc
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise GoogleSheetsCredentialsError(
                f"{source} exists ({len(text)} chars) but is not valid JSON: {exc}"
            ) from exc
    else:
        raise GoogleSheetsCredentialsError(
            "Neither GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON nor "
            "GOOGLE_SHEETS_SERVICE_ACCOUNT_FILE is set, so there is no service "
            "account to export as."
        )

    if not isinstance(payload, dict):
        raise GoogleSheetsCredentialsError(
            f"{source} parsed as {type(payload).__name__}, not a JSON object — "
            "it is not a service-account key file."
        )

    client_email = str(payload.get("client_email") or "").strip()
    private_key = str(payload.get("private_key") or "").strip()
    token_uri = str(payload.get("token_uri") or GOOGLE_TOKEN_URI).strip()
    missing = [
        name
        for name, value in (("client_email", client_email), ("private_key", private_key))
        if not value
    ]
    if missing:
        raise GoogleSheetsCredentialsError(
            f"{source} is valid JSON but has no {' and no '.join(missing)}. "
            "Expected the service-account key file Google Cloud downloads, not "
            "an OAuth client or an API key."
        )
    # Cheap, and it separates two failures that otherwise look identical from
    # the panel: a value in the private_key field that is not a key at all
    # (a key id, a fingerprint, a placeholder) is caught here by name, while a
    # real PEM that the crypto layer still refuses is caught at `jwt.encode`.
    if "PRIVATE KEY" not in private_key:
        raise GoogleSheetsCredentialsError(
            f"{source} has a private_key that is not a PEM block — no "
            "'BEGIN PRIVATE KEY' header in it."
        )
    return ServiceAccountCredentials(
        client_email=client_email,
        private_key=private_key,
        token_uri=token_uri or GOOGLE_TOKEN_URI,
    )


def google_sheets_config_status(settings: Settings | None = None) -> dict[str, Any]:
    """GK-479: `Configured` used to mean "a non-empty string exists".

    Under that definition the panel showed a green tile for ten weeks while the
    file the string named had been deleted. `service_account_configured` keeps
    its old meaning — something is set, so the export button stays pressable and
    the failure stays reachable — and `service_account_error` carries what
    actually happens when that something is loaded. No network call: this only
    reads the value and the file, so a green tile still proves nothing about
    Google's side of it. See GK-480.
    """
    cfg = settings or get_settings()
    configured = bool(
        cfg.google_sheets_service_account_json.strip()
        or cfg.google_sheets_service_account_file.strip()
    )
    error: str | None = None
    if configured:
        try:
            _load_service_account_credentials(cfg)
        except GoogleSheetsConfigError as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 - a status endpoint may not raise
            logger.exception("google sheets credential probe failed unexpectedly")
            error = f"{type(exc).__name__}: {exc}"
    return {
        "spreadsheet_id_configured": bool(cfg.google_sheets_spreadsheet_id.strip()),
        "service_account_configured": configured,
        "service_account_error": error,
        "manual_notes_sheet": MANUAL_NOTES_SHEET,
        "sheets": [spec.name for spec in CRM_EXPORT_SPECS],
    }


def _row(*values: Any) -> ExportRow:
    return ExportRow(tuple(_cell(value) for value in values))


def _cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True, default=str)
    return value


def _col_name(index: int) -> str:
    if index < 1:
        raise ValueError("column index is 1-based")
    chars = []
    current = index
    while current:
        current, remainder = divmod(current - 1, 26)
        chars.append(chr(65 + remainder))
    return "".join(reversed(chars))


def _sheet_name(name: str) -> str:
    return "'" + name.replace("'", "''") + "'"


def _a1_range(
    sheet: str,
    start_row: int,
    start_col: int,
    end_row: int,
    end_col: int,
) -> str:
    return (
        f"{_sheet_name(sheet)}!{_col_name(start_col)}{start_row}:"
        f"{_col_name(end_col)}{end_row}"
    )


def _a1_column(sheet: str, column: int, *, start_row: int = 1) -> str:
    col = _col_name(column)
    return f"{_sheet_name(sheet)}!{col}{start_row}:{col}"


def _a1_table(sheet: str, column_count: int) -> str:
    return f"{_sheet_name(sheet)}!A:{_col_name(column_count)}"


def _chunks(items: Sequence[T], size: int) -> list[list[T]]:
    return [list(items[index:index + size]) for index in range(0, len(items), size)]


def _parse_simple_a1(range_name: str) -> tuple[str, int, int, int | None]:
    sheet_part, _, cells = range_name.partition("!")
    sheet = sheet_part.strip("'").replace("''", "'")
    start, _, end = cells.partition(":")
    start_col, start_row = _parse_cell(start)
    end_col = _parse_cell(end)[0] if end else start_col
    return sheet, start_row, start_col, end_col


def _parse_cell(cell: str) -> tuple[int, int]:
    letters = "".join(ch for ch in cell if ch.isalpha()).upper()
    digits = "".join(ch for ch in cell if ch.isdigit())
    col = 0
    for ch in letters or "A":
        col = col * 26 + (ord(ch) - 64)
    row = int(digits or "1")
    return col, row


async def _send(
    client: httpx.AsyncClient, method: str, url: str, **kwargs: Any
) -> httpx.Response:
    """GK-479: a request that never reaches Google is not an unhandled error.

    `httpx.TransportError` covers DNS, connect, TLS, read timeout and pool
    exhaustion. Untouched, each escapes the router and becomes a 500 that names
    nothing; here it becomes a 502 that names the host it could not reach.
    """
    try:
        return await client.request(method, url, **kwargs)
    except httpx.TransportError as exc:
        host = httpx.URL(url).host
        raise GoogleSheetsTransportError(
            f"Could not reach {host}: {type(exc).__name__}: {exc}"
        ) from exc


def _is_quota_response(response: httpx.Response) -> bool:
    if response.status_code == 429:
        return True
    if response.status_code != 403:
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    error = payload.get("error") or {}
    if str(error.get("status") or "").upper() == "RESOURCE_EXHAUSTED":
        return True
    for detail in error.get("errors") or []:
        reason = str(detail.get("reason") or "").lower()
        if reason in {"ratelimitexceeded", "userratelimitexceeded", "quotaexceeded"}:
            return True
    return False


def _response_error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"Google Sheets API {response.status_code}: {response.text[:500]}"
    error = payload.get("error") or {}
    message = error.get("message") or payload.get("error_description") or response.text[:500]
    return f"Google Sheets API {response.status_code}: {message}"


__all__ = [
    "CRM_EXPORT_SPECS",
    "MANUAL_NOTES_SHEET",
    "DryRunGoogleSheetsClient",
    "ExportRow",
    "GoogleSheetsApiError",
    "GoogleSheetsConfigError",
    "GoogleSheetsCredentialsError",
    "GoogleSheetsExportSummary",
    "GoogleSheetsHttpClient",
    "GoogleSheetsQuotaError",
    "GoogleSheetsTransportError",
    "SheetSpec",
    "SheetSyncSummary",
    "call_with_quota_backoff",
    "collect_crm_export_rows",
    "ensure_crm_sheet_layout",
    "ensure_manual_notes_sheet",
    "export_crm_to_google_sheets",
    "google_sheets_config_status",
    "sync_sheet_rows",
]
