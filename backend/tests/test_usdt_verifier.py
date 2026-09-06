import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from app.payments import usdt_verifier
from app.payments.usdt_verifier import (
    ERC20_TRANSFER_TOPIC,
    ERC20_USDT_CONTRACT,
    NETWORK_ERC20,
    NETWORK_TRC20,
    TRC20_USDT_CONTRACT,
    USDTExplorerTx,
    USDTTransferObservation,
    amount_to_usdt_minor,
    parse_erc20_receipt_payloads,
    parse_trc20_explorer_payloads,
    verify_and_apply_usdt_payment,
    verify_usdt_transaction,
)

TX_HASH = "a" * 64


@pytest.fixture(autouse=True)
def usdt_settings(monkeypatch):
    monkeypatch.setattr(
        usdt_verifier,
        "settings",
        SimpleNamespace(
            usdt_trc20_address="TLotQJsvstVxCQdQNBoAUYn15ousqD75EC",
            usdt_erc20_address="0x6f4C9BfDa3446265168c6567483feDca17643fc2",
            usdt_trc20_confirmations=3,
            usdt_erc20_confirmations=3,
            trongrid_base_url="https://api.trongrid.io",
            trongrid_api_key="",
            etherscan_base_url="https://api.etherscan.io/v2/api",
            etherscan_chain_id=1,
            etherscan_api_key="",
        ),
    )


class ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _NoopNested:
    """Async context manager standing in for ``session.begin_nested()``."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, duplicate_id=None):
        self.duplicate_id = duplicate_id
        self.flushes = 0

    async def execute(self, _query):
        return ScalarResult(self.duplicate_id)

    async def flush(self):
        self.flushes += 1

    def begin_nested(self):
        return _NoopNested()


class ClaimingSession:
    """Session double modelling the partial unique index on (tx_network, tx_hash).

    The first ``flush`` to write a given (network, hash) key wins; every later
    flush of the same key raises ``IntegrityError`` — exactly how Postgres
    serialises concurrent inserts against ``uq_pay_tx_network_hash``. The set is
    shared across sessions to emulate distinct concurrent payments.
    """

    def __init__(self, claimed_keys, payment, duplicate_id=None):
        self.claimed_keys = claimed_keys
        self.payment = payment
        self.duplicate_id = duplicate_id
        self.flushes = 0

    async def execute(self, _query):
        return ScalarResult(self.duplicate_id)

    def begin_nested(self):
        return _NoopNested()

    async def flush(self):
        self.flushes += 1
        if self.payment.tx_hash is None:
            return
        key = (self.payment.tx_network, self.payment.tx_hash)
        if key in self.claimed_keys:
            raise IntegrityError("duplicate tx claim", {}, Exception("uq_pay_tx_network_hash"))
        self.claimed_keys.add(key)


class FakeExplorer:
    def __init__(self, tx):
        self.tx = tx
        self.calls = []

    async def fetch_transaction(self, network, tx_hash):
        self.calls.append((network, tx_hash))
        return self.tx


def make_payment(**overrides):
    data = {
        "id": 123,
        "provider": "usdt",
        "amount": Decimal("19.00"),
        "status": "awaiting_review",
        "note": "method=usdt_trc20",
        "tx_hash": None,
        "tx_network": "TRC20",
        "tx_confirmed_at": None,
        "provider_event_id": None,
        "approved_at": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def make_tx(**overrides):
    data = {
        "network": NETWORK_TRC20,
        "tx_hash": TX_HASH,
        "success": True,
        "confirmations": 3,
        "confirmed_at": datetime(2026, 5, 31, tzinfo=UTC),
        "transfers": [
            USDTTransferObservation(
                contract_address=TRC20_USDT_CONTRACT,
                to_address="TLotQJsvstVxCQdQNBoAUYn15ousqD75EC",
                amount_minor=19_000_000,
            )
        ],
    }
    data.update(overrides)
    return USDTExplorerTx(**data)


def test_trc20_parser_reads_mocked_trongrid_transfer_response():
    parsed = parse_trc20_explorer_payloads(
        tx_hash=TX_HASH,
        events_payload={
            "data": [
                {
                    "event_name": "Transfer",
                    "contract_address": TRC20_USDT_CONTRACT,
                    "block_number": 100,
                    "block_timestamp": 1_779_916_800_000,
                    "result": {
                        "to": "TLotQJsvstVxCQdQNBoAUYn15ousqD75EC",
                        "value": "19000000",
                    },
                }
            ]
        },
        info_payload={
            "blockNumber": 100,
            "blockTimeStamp": 1_779_916_800_000,
            "receipt": {"result": "SUCCESS"},
        },
        latest_block_payload={"block_header": {"raw_data": {"number": 102}}},
    )

    assert parsed is not None
    assert parsed.network == NETWORK_TRC20
    assert parsed.confirmations == 3
    assert parsed.success is True
    assert parsed.transfers[0].contract_address == TRC20_USDT_CONTRACT
    assert parsed.transfers[0].amount_minor == 19_000_000


def test_erc20_parser_reads_mocked_etherscan_receipt_response():
    parsed = parse_erc20_receipt_payloads(
        tx_hash=f"0x{TX_HASH}",
        receipt_payload={
            "jsonrpc": "2.0",
            "result": {
                "status": "0x1",
                "blockNumber": "0x64",
                "logs": [
                    {
                        "address": ERC20_USDT_CONTRACT,
                        "topics": [
                            f"0x{ERC20_TRANSFER_TOPIC}",
                            "0x" + "0" * 64,
                            "0x0000000000000000000000006f4c9bfda3446265168c6567483fedca17643fc2",
                        ],
                        "data": hex(19_000_000),
                    }
                ],
            },
        },
        latest_block_payload={"jsonrpc": "2.0", "result": "0x66"},
    )

    assert parsed is not None
    assert parsed.network == NETWORK_ERC20
    assert parsed.confirmations == 3
    assert parsed.success is True
    assert parsed.transfers[0].contract_address == ERC20_USDT_CONTRACT
    assert parsed.transfers[0].amount_minor == 19_000_000


@pytest.mark.asyncio
async def test_valid_trc20_hash_auto_fulfills_payment(monkeypatch):
    calls = []

    async def fake_fulfill(session, bot, payment):
        calls.append((session, bot, payment))
        payment.approved_at = datetime(2026, 5, 31, tzinfo=UTC)
        return SimpleNamespace(id=55, invite_link="invite-link")

    monkeypatch.setattr(usdt_verifier, "fulfill_payment", fake_fulfill)
    payment = make_payment()

    result = await verify_and_apply_usdt_payment(
        FakeSession(),
        payment,
        TX_HASH,
        bot=object(),
        client=FakeExplorer(make_tx()),
    )

    assert result.is_valid is True
    assert result.subscription_id == 55
    assert result.invite_link == "invite-link"
    assert payment.status == "succeeded"
    assert payment.tx_hash == TX_HASH
    assert payment.tx_network == NETWORK_TRC20
    assert payment.tx_confirmed_at == datetime(2026, 5, 31, tzinfo=UTC)
    assert payment.provider_event_id == f"usdt:TRC20:{TX_HASH}"
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tx", "reason"),
    [
        (
            make_tx(
                transfers=[
                    USDTTransferObservation(
                        contract_address="TWrongContract111111111111111111111",
                        to_address="TLotQJsvstVxCQdQNBoAUYn15ousqD75EC",
                        amount_minor=19_000_000,
                    )
                ]
            ),
            "wrong_contract",
        ),
        (
            make_tx(
                transfers=[
                    USDTTransferObservation(
                        contract_address=TRC20_USDT_CONTRACT,
                        to_address="TWrongWallet11111111111111111111111",
                        amount_minor=19_000_000,
                    )
                ]
            ),
            "wrong_destination",
        ),
        (
            make_tx(
                transfers=[
                    USDTTransferObservation(
                        contract_address=TRC20_USDT_CONTRACT,
                        to_address="TLotQJsvstVxCQdQNBoAUYn15ousqD75EC",
                        amount_minor=18_000_000,
                    )
                ]
            ),
            "wrong_amount",
        ),
        (make_tx(success=False, transfers=[]), "failed_tx"),
    ],
)
async def test_rejects_wrong_usdt_transfer_shapes(tx, reason):
    result = await verify_usdt_transaction(
        NETWORK_TRC20,
        TX_HASH,
        Decimal("19.00"),
        client=FakeExplorer(tx),
    )

    assert result.decision == "rejected"
    assert result.reason == reason


@pytest.mark.asyncio
async def test_duplicate_hash_is_rejected_before_explorer_lookup():
    explorer = FakeExplorer(make_tx())
    payment = make_payment()

    result = await verify_and_apply_usdt_payment(
        FakeSession(duplicate_id=999),
        payment,
        TX_HASH,
        client=explorer,
    )

    assert result.decision == "rejected"
    assert result.reason == "duplicate_tx"
    assert explorer.calls == []
    assert payment.status == "awaiting_review"
    assert payment.tx_hash is None


@pytest.mark.asyncio
async def test_insufficient_confirmations_goes_to_manual_review():
    result = await verify_usdt_transaction(
        NETWORK_TRC20,
        TX_HASH,
        Decimal("19.00"),
        client=FakeExplorer(make_tx(confirmations=2)),
    )

    assert result.decision == "manual_review"
    assert result.reason == "insufficient_confirmations"


def test_amount_check_uses_integer_minor_units():
    assert amount_to_usdt_minor(Decimal("15.20")) == 15_200_000
    assert amount_to_usdt_minor("19.000001") == 19_000_001
    with pytest.raises(ValueError):
        amount_to_usdt_minor("19.0000001")


# ── atomic (network, tx_hash) claim before fulfillment (GK-401) ───────────────
@pytest.mark.asyncio
async def test_lost_claim_rejects_without_fulfilling(monkeypatch):
    # The pre-check passes (no committed duplicate), the explorer validates, but
    # a concurrent payment already committed the (network, hash) claim — so the
    # reservation flush conflicts and we must reject WITHOUT any fulfillment.
    fulfills = []

    async def fake_fulfill(session, bot, payment):
        fulfills.append(payment.id)
        return SimpleNamespace(id=1, invite_link="invite-link")

    monkeypatch.setattr(usdt_verifier, "fulfill_payment", fake_fulfill)

    payment = make_payment()
    claimed = {(NETWORK_TRC20, TX_HASH)}  # pre-seeded: the slot is already taken
    session = ClaimingSession(claimed, payment)

    result = await verify_and_apply_usdt_payment(
        session,
        payment,
        TX_HASH,
        bot=object(),
        client=FakeExplorer(make_tx()),
    )

    assert result.decision == "rejected"
    assert result.reason == "duplicate_tx"
    assert fulfills == []  # AC2: no Telegram side effect when the claim fails
    assert payment.status == "failed"
    assert payment.tx_hash is None  # claim released, not persisted


@pytest.mark.asyncio
async def test_concurrent_same_hash_claims_fulfill_exactly_once(monkeypatch):
    # AC4: 20+ concurrent distinct-payment claims for one hash → exactly one
    # fulfillment / one claim; every loser gets duplicate_tx and no invite.
    fulfills = []

    async def fake_fulfill(session, bot, payment):
        fulfills.append(payment.id)
        return SimpleNamespace(id=payment.id, invite_link=f"invite-{payment.id}")

    monkeypatch.setattr(usdt_verifier, "fulfill_payment", fake_fulfill)

    claimed: set = set()  # shared partial-unique-index emulation
    payments = [make_payment(id=1000 + i) for i in range(20)]
    sessions = [ClaimingSession(claimed, p) for p in payments]

    results = await asyncio.gather(
        *[
            verify_and_apply_usdt_payment(
                s, p, TX_HASH, bot=object(), client=FakeExplorer(make_tx())
            )
            for s, p in zip(sessions, payments, strict=True)
        ]
    )

    winners = [r for r in results if r.is_valid]
    losers = [r for r in results if r.reason == "duplicate_tx"]
    assert len(winners) == 1
    assert len(losers) == 19
    assert len(fulfills) == 1  # exactly one fulfillment
    assert len(claimed) == 1  # one (network, hash) claim committed
