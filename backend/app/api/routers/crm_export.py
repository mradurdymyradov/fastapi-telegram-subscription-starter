from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from app.api.deps import DB, CurrentAdmin
from app.config import get_settings
from app.services.audit import record as audit_record
from app.services.google_sheets_export import (
    GoogleSheetsApiError,
    GoogleSheetsConfigError,
    GoogleSheetsQuotaError,
    export_crm_to_google_sheets,
    google_sheets_config_status,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/crm-export", tags=["crm-export"])
settings = get_settings()


class GoogleSheetsConfigOut(BaseModel):
    spreadsheet_id_configured: bool
    service_account_configured: bool
    # GK-479: `service_account_configured` only ever meant "a non-empty string
    # exists". This says whether that string yields a usable credential.
    service_account_error: str | None = None
    manual_notes_sheet: str
    sheets: list[str]


class GoogleSheetsExportRequest(BaseModel):
    dry_run: bool = False


class SheetExportOut(BaseModel):
    sheet: str
    rows: int
    updated: int
    appended: int
    duplicate_existing_ids: list[str]


class GoogleSheetsExportOut(BaseModel):
    spreadsheet_id: str
    dry_run: bool
    manual_notes_sheet: str
    manual_notes_initialized: bool
    rows: int
    updated: int
    appended: int
    sheets: list[SheetExportOut]
    operations: list[str]


@router.get("/google-sheets/config", response_model=GoogleSheetsConfigOut)
async def google_sheets_config(_: CurrentAdmin) -> GoogleSheetsConfigOut:
    return GoogleSheetsConfigOut(**google_sheets_config_status(settings))


@router.post("/google-sheets", response_model=GoogleSheetsExportOut)
async def run_google_sheets_export(
    payload: GoogleSheetsExportRequest,
    db: DB,
    admin: CurrentAdmin,
    request: Request,
) -> GoogleSheetsExportOut:
    try:
        result = await export_crm_to_google_sheets(
            db,
            dry_run=payload.dry_run,
            settings=settings,
        )
    except GoogleSheetsConfigError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except GoogleSheetsQuotaError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except GoogleSheetsApiError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - see GK-479
        # The three handlers above cover everything this export knows how to
        # fail at. Anything else is a defect in our code rather than in the
        # configuration or in Google, and until 2026-08-23 it reached the panel
        # as the words "Internal Server Error" and nothing else — which is how a
        # FileNotFoundError went unread for ten weeks. Name the exception, keep
        # the traceback in the log, and stay a 500 because that is what it is.
        logger.exception(
            "CRM Google Sheets export failed unexpectedly (dry_run=%s, admin=%s)",
            payload.dry_run,
            admin.id,
        )
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"CRM export failed with an unhandled {type(exc).__name__}: {exc}. "
            "The full traceback is in the api container log.",
        ) from exc

    await audit_record(
        db,
        actor_admin_id=admin.id,
        action="crm_export.google_sheets",
        target_type="google_sheet",
        target_id=result.spreadsheet_id,
        details={
            "dry_run": result.dry_run,
            "rows": result.rows,
            "updated": result.updated,
            "appended": result.appended,
            "manual_notes_initialized": result.manual_notes_initialized,
            "sheets": [
                {
                    "sheet": sheet.sheet,
                    "rows": sheet.rows,
                    "updated": sheet.updated,
                    "appended": sheet.appended,
                    "duplicate_existing_ids": sheet.duplicate_existing_ids,
                }
                for sheet in result.sheets
            ],
        },
        request=request,
    )
    return GoogleSheetsExportOut(
        spreadsheet_id=result.spreadsheet_id,
        dry_run=result.dry_run,
        manual_notes_sheet=result.manual_notes_sheet,
        manual_notes_initialized=result.manual_notes_initialized,
        rows=result.rows,
        updated=result.updated,
        appended=result.appended,
        sheets=[
            SheetExportOut(
                sheet=sheet.sheet,
                rows=sheet.rows,
                updated=sheet.updated,
                appended=sheet.appended,
                duplicate_existing_ids=sheet.duplicate_existing_ids,
            )
            for sheet in result.sheets
        ],
        operations=result.operations,
    )
