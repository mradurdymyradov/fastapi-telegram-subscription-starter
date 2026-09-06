"""Admin-facing config endpoint.

Exposes the boolean launch flags that gate UI surfaces (Zelle, webhook
integrations, AI-backed support). The values are read from `Settings` so the
admin UI can show "disabled for launch" banners without hard-coding the
defaults in two places.
"""
from fastapi import APIRouter
from pydantic import BaseModel

from app.api.deps import CurrentAdmin
from app.config import get_settings

router = APIRouter(prefix="/config", tags=["config"])
settings = get_settings()


class LaunchFlags(BaseModel):
    enable_zelle: bool
    enable_webhook_integrations: bool
    enable_ai_support: bool


@router.get("/launch-flags", response_model=LaunchFlags)
async def launch_flags(_: CurrentAdmin) -> LaunchFlags:
    """Return the GK-120 launch feature flags for admin UI gating."""
    return LaunchFlags(
        enable_zelle=settings.enable_zelle,
        enable_webhook_integrations=settings.enable_webhook_integrations,
        enable_ai_support=settings.enable_ai_support,
    )
