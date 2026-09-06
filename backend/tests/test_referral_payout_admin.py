from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.api.routers import referrals as referrals_router
from app.api.routers.referrals import (
    PayoutBatchCreate,
    PayoutBatchTransition,
    transition_payout_batch,
)


def test_payout_batch_create_rejects_threshold_below_launch_floor():
    assert PayoutBatchCreate().threshold_usd == 100.0
    with pytest.raises(ValidationError):
        PayoutBatchCreate(threshold_usd=99.99)


@pytest.mark.asyncio
async def test_payout_transition_endpoint_audit_logs_action(monkeypatch):
    batch = SimpleNamespace(id=55, status="paid", total_amount=100, commission_count=2)
    admin = SimpleNamespace(id=7)
    request = SimpleNamespace()
    audit_calls = []

    async def fake_transition(db, batch_id, **kwargs):
        assert batch_id == batch.id
        assert kwargs["action"] == "paid"
        assert kwargs["admin_id"] == admin.id
        assert kwargs["tx_hash"] == "0xabc123"
        return batch

    async def fake_batch_out(db, result, *, include_commissions):
        assert result is batch
        assert include_commissions is True
        return result

    async def fake_audit_record(db, **kwargs):
        audit_calls.append((db, kwargs))

    monkeypatch.setattr(referrals_router, "transition_referral_payout_batch", fake_transition)
    monkeypatch.setattr(referrals_router, "_batch_out", fake_batch_out)
    monkeypatch.setattr(referrals_router, "audit_record", fake_audit_record)

    result = await transition_payout_batch(
        batch.id,
        PayoutBatchTransition(action="paid", tx_hash="0xabc123"),
        SimpleNamespace(),
        admin,
        request,
    )

    assert result is batch
    assert len(audit_calls) == 1
    assert audit_calls[0][1]["action"] == "referral_payout_batch.paid"
    assert audit_calls[0][1]["target_type"] == "referral_payout_batch"
    assert audit_calls[0][1]["target_id"] == batch.id
    assert audit_calls[0][1]["details"] == {
        "status": "paid",
        "tx_hash": "0xabc123",
        "note": None,
        "total_amount": 100.0,
        "commission_count": 2,
    }


def test_telegram_summary_text_matches_blk005_monthly_payout_wording():
    zero_counts = {
        referrals_router.PAYOUT_DRAFT: 1,
        referrals_router.PAYOUT_SENT: 0,
        referrals_router.PAYOUT_PAID: 0,
        referrals_router.PAYOUT_CANCELLED: 0,
    }
    zero_amounts = {
        referrals_router.PAYOUT_DRAFT: 120.0,
        referrals_router.PAYOUT_SENT: 0.0,
        referrals_router.PAYOUT_PAID: 0.0,
        referrals_router.PAYOUT_CANCELLED: 0.0,
    }

    text = referrals_router._telegram_summary_text(
        month_label="2026-06",
        ready_count=2,
        ready_amount=120.0,
        counts=zero_counts,
        amounts=zero_amounts,
    )

    assert "2026-06" in text
    # $100 payout threshold and monthly support payout window / processing wording.
    assert "$100.00" in text
    assert "ежемесячно через поддержку" in text
    assert "с 1 по 5 число" in text
    assert "обработка" in text and "дней" in text
