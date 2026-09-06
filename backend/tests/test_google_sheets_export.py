import json
from types import SimpleNamespace

import httpx
import pytest

from app.services.google_sheets_export import (
    CRM_EXPORT_SPECS,
    ExportRow,
    GoogleSheetsConfigError,
    GoogleSheetsCredentialsError,
    GoogleSheetsQuotaError,
    GoogleSheetsTransportError,
    SheetSpec,
    _load_service_account_credentials,
    _send,
    call_with_quota_backoff,
    ensure_manual_notes_sheet,
    google_sheets_config_status,
    sync_sheet_rows,
)


class FakeSheetsClient:
    spreadsheet_id = "sheet_123"

    def __init__(self):
        self.values: dict[str, list[list[object]]] = {}
        self.updated_ranges: list[tuple[str, list[list[object]]]] = []
        self.batch_updates: list[list[dict[str, object]]] = []
        self.appends: list[tuple[str, list[list[object]]]] = []

    async def get_spreadsheet(self):
        return {"sheets": []}

    async def batch_update(self, requests):
        return {"replies": requests}

    async def get_values(self, range_name):
        return self.values.get(range_name, [])

    async def update_values(self, range_name, values):
        self.updated_ranges.append((range_name, values))
        return {"updatedRows": len(values)}

    async def batch_update_values(self, data):
        self.batch_updates.append(data)
        return {"totalUpdatedRows": len(data)}

    async def append_values(self, range_name, values):
        self.appends.append((range_name, values))
        return {"updates": {"updatedRows": len(values)}}


@pytest.mark.asyncio
async def test_sync_sheet_rows_updates_and_appends_by_stable_id_without_clear():
    client = FakeSheetsClient()
    client.values["'Users'!A2:A"] = [["user:1"], ["user:old"], ["user:1"]]
    spec = SheetSpec(name="Users", headers=("stable_id", "email", "status"))
    rows = [
        ExportRow(("user:1", "one@example.test", "active")),
        ExportRow(("user:2", "two@example.test", "pending")),
    ]

    summary = await sync_sheet_rows(client, spec, rows)

    assert summary.rows == 2
    assert summary.updated == 1
    assert summary.appended == 1
    assert summary.duplicate_existing_ids == ["user:1"]
    assert client.updated_ranges == [
        ("'Users'!A1:C1", [["stable_id", "email", "status"]])
    ]
    assert client.batch_updates == [
        [
            {
                "range": "'Users'!A2:C2",
                "values": [["user:1", "one@example.test", "active"]],
            }
        ]
    ]
    assert client.appends == [
        ("'Users'!A:C", [["user:2", "two@example.test", "pending"]])
    ]


@pytest.mark.asyncio
async def test_manual_notes_header_is_created_without_touching_note_rows():
    client = FakeSheetsClient()

    initialized = await ensure_manual_notes_sheet(client)

    assert initialized is True
    assert client.updated_ranges == [
        (
            "'Manual Notes'!A1:F1",
            [["record_type", "stable_id", "manual_note", "manual_status", "owner", "updated_at"]],
        )
    ]


@pytest.mark.asyncio
async def test_quota_backoff_retries_before_succeeding():
    calls = SimpleNamespace(count=0)
    sleeps: list[float] = []

    async def flaky():
        calls.count += 1
        if calls.count < 3:
            raise GoogleSheetsQuotaError("rate limit")
        return "ok"

    async def fake_sleep(delay):
        sleeps.append(delay)

    result = await call_with_quota_backoff(
        flaky,
        max_attempts=4,
        sleep=fake_sleep,
        base_delay=0.5,
        jitter=lambda: 0,
    )

    assert result == "ok"
    assert calls.count == 3
    assert sleeps == [0.5, 1.0]


def test_crm_export_sheets_cover_required_launch_crm_surfaces():
    sheet_names = {spec.name for spec in CRM_EXPORT_SPECS}

    assert {
        "Users",
        "Subscriptions",
        "Payments",
        "Referrals",
        "Referral Commissions",
        "Payouts",
        "Reconciliation Summary",
    }.issubset(sheet_names)


# ---------------------------------------------------------------------------
# GK-479: the export answered Grant with a bare 500 for ten weeks.
#
# The mounted key file had been deleted by the deploy after the one that
# created it, so `_load_service_account_credentials` raised `FileNotFoundError`
# — a type no handler in the router mentions. Nothing in the response said
# credential, or file, or path. These tests fix the shape of every way this can
# fail: each one names itself, and none escapes as an unhandled error.
# ---------------------------------------------------------------------------


VALID_KEY = {
    "client_email": "sheets@example.iam.gserviceaccount.com",
    "private_key": "-----BEGIN PRIVATE KEY-----\nMIIEv\n-----END PRIVATE KEY-----\n",
    "token_uri": "https://oauth2.googleapis.com/token",
}


def _settings(**kwargs):
    values = {
        "google_sheets_spreadsheet_id": "sheet_123",
        "google_sheets_service_account_json": "",
        "google_sheets_service_account_file": "",
    }
    values.update(kwargs)
    return SimpleNamespace(**values)


def test_the_deleted_key_file_names_the_path_instead_of_raising_filenotfound(tmp_path):
    """The GK-479 failure itself, in the shape the live host had it."""
    missing = tmp_path / "run" / "secrets" / "sa.json"

    with pytest.raises(GoogleSheetsCredentialsError) as caught:
        _load_service_account_credentials(
            _settings(google_sheets_service_account_file=str(missing))
        )

    message = str(caught.value)
    assert str(missing) in message
    assert "does not exist" in message
    # Still a config error, so the router keeps answering 400 rather than 500.
    assert isinstance(caught.value, GoogleSheetsConfigError)


def test_a_pem_pasted_where_the_whole_key_file_belongs_says_which_variable():
    broken = "-----BEGIN PRIVATE KEY-----\nMIIEv\n-----END PRIVATE KEY-----"

    with pytest.raises(GoogleSheetsCredentialsError) as caught:
        _load_service_account_credentials(
            _settings(google_sheets_service_account_json=broken)
        )

    message = str(caught.value)
    assert "GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON" in message
    assert "not valid JSON" in message
    # The secret itself must not be echoed back into an HTTP response.
    assert "MIIEv" not in message


def test_a_key_file_that_is_not_json_names_the_file_not_the_variable(tmp_path):
    path = tmp_path / "sa.json"
    path.write_text("not json at all", encoding="utf-8")

    with pytest.raises(GoogleSheetsCredentialsError) as caught:
        _load_service_account_credentials(
            _settings(google_sheets_service_account_file=str(path))
        )

    assert "not valid JSON" in str(caught.value)
    assert str(path) in str(caught.value)


@pytest.mark.parametrize("field", ["client_email", "private_key"])
def test_a_key_missing_a_required_field_names_the_field(field):
    payload = dict(VALID_KEY)
    payload.pop(field)

    with pytest.raises(GoogleSheetsCredentialsError) as caught:
        _load_service_account_credentials(
            _settings(google_sheets_service_account_json=json.dumps(payload))
        )

    assert field in str(caught.value)


def test_a_private_key_that_is_not_a_pem_block_is_caught_before_any_request():
    payload = dict(VALID_KEY, private_key="a1b2c3d4")

    with pytest.raises(GoogleSheetsCredentialsError) as caught:
        _load_service_account_credentials(
            _settings(google_sheets_service_account_json=json.dumps(payload))
        )

    assert "BEGIN PRIVATE KEY" in str(caught.value)


def test_neither_variable_set_says_there_is_no_service_account():
    with pytest.raises(GoogleSheetsCredentialsError) as caught:
        _load_service_account_credentials(_settings())

    assert "GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON" in str(caught.value)
    assert "GOOGLE_SHEETS_SERVICE_ACCOUNT_FILE" in str(caught.value)


def test_a_usable_key_still_loads():
    creds = _load_service_account_credentials(
        _settings(google_sheets_service_account_json=json.dumps(VALID_KEY))
    )

    assert creds.client_email == VALID_KEY["client_email"]
    assert creds.token_uri == "https://oauth2.googleapis.com/token"


@pytest.mark.asyncio
async def test_a_network_that_never_reaches_google_names_the_host():
    """DNS/connect/timeout becomes a 502 about the network, not a 500 about nothing."""

    class DeadClient:
        async def request(self, *_args, **_kwargs):
            raise httpx.ConnectError("[Errno -3] Temporary failure in name resolution")

    with pytest.raises(GoogleSheetsTransportError) as caught:
        await _send(DeadClient(), "POST", "https://oauth2.googleapis.com/token", data={})

    message = str(caught.value)
    assert "oauth2.googleapis.com" in message
    assert "ConnectError" in message
    # 502, not 503: a dead network is not a quota problem.
    assert not isinstance(caught.value, GoogleSheetsQuotaError)


def test_the_config_tile_reports_a_credential_that_does_not_load(tmp_path):
    missing = tmp_path / "gone.json"

    status = google_sheets_config_status(
        _settings(google_sheets_service_account_file=str(missing))
    )

    # Still "configured" — the variable is set, so the export button stays
    # pressable and the failure stays reachable. The error is what is new.
    assert status["service_account_configured"] is True
    assert status["service_account_error"] is not None
    assert str(missing) in status["service_account_error"]


def test_the_config_tile_stays_quiet_when_the_credential_loads():
    status = google_sheets_config_status(
        _settings(google_sheets_service_account_json=json.dumps(VALID_KEY))
    )

    assert status["service_account_configured"] is True
    assert status["service_account_error"] is None


@pytest.mark.asyncio
async def test_an_unhandled_error_reaches_the_panel_with_its_own_name(monkeypatch):
    """The regression guard for what Grant actually saw.

    Before this, any exception outside the three known families became the two
    words "Internal Server Error", and the traceback went to a container log
    nobody knew to read — which the next deploy then recreated, taking it away.
    """
    from fastapi import HTTPException

    from app.api.routers import crm_export as router

    async def boom(*_args, **_kwargs):
        raise FileNotFoundError(2, "No such file or directory", "/run/secrets/sa.json")

    monkeypatch.setattr(router, "export_crm_to_google_sheets", boom)

    with pytest.raises(HTTPException) as caught:
        await router.run_google_sheets_export(
            router.GoogleSheetsExportRequest(dry_run=False),
            None,
            SimpleNamespace(id=7),
            SimpleNamespace(),
        )

    assert caught.value.status_code == 500
    assert "FileNotFoundError" in caught.value.detail
    assert "/run/secrets/sa.json" in caught.value.detail
    assert caught.value.detail != "Internal Server Error"


@pytest.mark.asyncio
async def test_a_credential_failure_is_a_400_carrying_the_reason(monkeypatch):
    from fastapi import HTTPException

    from app.api.routers import crm_export as router

    async def boom(*_args, **_kwargs):
        raise GoogleSheetsCredentialsError("GOOGLE_SHEETS_SERVICE_ACCOUNT_FILE is gone")

    monkeypatch.setattr(router, "export_crm_to_google_sheets", boom)

    with pytest.raises(HTTPException) as caught:
        await router.run_google_sheets_export(
            router.GoogleSheetsExportRequest(dry_run=False),
            None,
            SimpleNamespace(id=7),
            SimpleNamespace(),
        )

    assert caught.value.status_code == 400
    assert "GOOGLE_SHEETS_SERVICE_ACCOUNT_FILE is gone" in caught.value.detail
