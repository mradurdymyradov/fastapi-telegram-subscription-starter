"""USDT transaction-hash verification for launch TRC20/ERC20 payments.

The service verifies one user-submitted tx hash against the expected network,
official USDT contract, receiving wallet, amount, success status, and
confirmation threshold. Valid payments are then fulfilled through the shared
``fulfill_payment`` pipeline so subscriptions/referrals/webhooks stay
idempotent in one place.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

import httpx
from aiogram import Bot
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Payment, utcnow
from app.payments.fulfillment import fulfill_payment

settings = get_settings()

USDT_DECIMALS = 6
USDT_MINOR_UNITS = 10**USDT_DECIMALS
NETWORK_TRC20 = "TRC20"
NETWORK_ERC20 = "ERC20"
SUPPORTED_NETWORKS = {NETWORK_TRC20, NETWORK_ERC20}

# Tether's official launch-scope contracts.
TRC20_USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
ERC20_USDT_CONTRACT = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
ERC20_TRANSFER_TOPIC = "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

_TX_HASH_RE = re.compile(r"(?:0x)?([0-9a-fA-F]{64})")
_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


@dataclass(frozen=True)
class USDTTransferObservation:
    contract_address: str
    to_address: str
    amount_minor: int


@dataclass(frozen=True)
class USDTExplorerTx:
    network: str
    tx_hash: str
    success: bool
    confirmations: int | None
    confirmed_at: datetime | None
    transfers: list[USDTTransferObservation]


@dataclass(frozen=True)
class USDTVerificationResult:
    decision: str  # valid | manual_review | rejected
    reason: str
    message: str
    network: str | None = None
    tx_hash: str | None = None
    confirmations: int | None = None
    expected_amount_minor: int | None = None
    observed_amount_minor: int | None = None
    confirmed_at: datetime | None = None
    subscription_id: int | None = None
    invite_link: str | None = None

    @property
    def is_valid(self) -> bool:
        return self.decision == "valid"

    @property
    def needs_manual_review(self) -> bool:
        return self.decision == "manual_review"


class USDTExplorerError(RuntimeError):
    """Explorer returned an unusable response or could not be reached."""


class USDTExplorer(Protocol):
    async def fetch_transaction(self, network: str, tx_hash: str) -> USDTExplorerTx | None:
        ...


class USDTExplorerClient:
    def __init__(self) -> None:
        self._timeout = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)

    async def fetch_transaction(self, network: str, tx_hash: str) -> USDTExplorerTx | None:
        if network == NETWORK_TRC20:
            return await self._fetch_trc20(tx_hash)
        if network == NETWORK_ERC20:
            return await self._fetch_erc20(tx_hash)
        raise USDTExplorerError(f"unsupported network: {network}")

    async def _fetch_trc20(self, tx_hash: str) -> USDTExplorerTx | None:
        base_url = settings.trongrid_base_url.rstrip("/")
        headers = {}
        if settings.trongrid_api_key:
            headers["TRON-PRO-API-KEY"] = settings.trongrid_api_key

        try:
            async with httpx.AsyncClient(timeout=self._timeout, headers=headers) as client:
                events_resp = await client.get(
                    f"{base_url}/v1/transactions/{tx_hash}/events",
                    params={"only_confirmed": "false"},
                )
                events_resp.raise_for_status()
                info_resp = await client.post(
                    f"{base_url}/wallet/gettransactioninfobyid",
                    json={"value": tx_hash},
                )
                info_resp.raise_for_status()
                block_resp = await client.post(f"{base_url}/walletsolidity/getnowblock", json={})
                block_resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise USDTExplorerError(f"trongrid request failed: {exc}") from exc

        return parse_trc20_explorer_payloads(
            tx_hash=tx_hash,
            events_payload=events_resp.json(),
            info_payload=info_resp.json(),
            latest_block_payload=block_resp.json(),
        )

    async def _fetch_erc20(self, tx_hash: str) -> USDTExplorerTx | None:
        tx_hash_with_prefix = f"0x{tx_hash}"
        base_url = settings.etherscan_base_url
        receipt_params: dict[str, str | int] = {
            "chainid": settings.etherscan_chain_id,
            "module": "proxy",
            "action": "eth_getTransactionReceipt",
            "txhash": tx_hash_with_prefix,
        }
        latest_params: dict[str, str | int] = {
            "chainid": settings.etherscan_chain_id,
            "module": "proxy",
            "action": "eth_blockNumber",
        }
        if settings.etherscan_api_key:
            receipt_params["apikey"] = settings.etherscan_api_key
            latest_params["apikey"] = settings.etherscan_api_key

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                receipt_resp = await client.get(base_url, params=receipt_params)
                receipt_resp.raise_for_status()
                block_resp = await client.get(base_url, params=latest_params)
                block_resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise USDTExplorerError(f"etherscan request failed: {exc}") from exc

        return parse_erc20_receipt_payloads(
            tx_hash=tx_hash,
            receipt_payload=receipt_resp.json(),
            latest_block_payload=block_resp.json(),
        )


def extract_tx_hash(text: str | None) -> str | None:
    if not text:
        return None
    match = _TX_HASH_RE.search(text.strip())
    if not match:
        return None
    return match.group(1).lower()


def normalize_network(raw: str | None) -> str | None:
    value = (raw or "").strip().upper().replace("-", "_")
    aliases = {
        "TRC20": NETWORK_TRC20,
        "TRON": NETWORK_TRC20,
        "USDT_TRC20": NETWORK_TRC20,
        "ERC20": NETWORK_ERC20,
        "ETH": NETWORK_ERC20,
        "ETHEREUM": NETWORK_ERC20,
        "USDT_ERC20": NETWORK_ERC20,
    }
    return aliases.get(value)


def network_from_payment(payment: Payment) -> str | None:
    if payment.tx_network:
        return normalize_network(payment.tx_network)
    note = payment.note or ""
    if "method=usdt_trc20" in note:
        return NETWORK_TRC20
    if "method=usdt_erc20" in note:
        return NETWORK_ERC20
    return None


async def verify_usdt_transaction(
    network: str | None,
    tx_hash: str | None,
    expected_amount: Decimal | int | float | str,
    *,
    client: USDTExplorer | None = None,
) -> USDTVerificationResult:
    normalized_network = normalize_network(network)
    normalized_hash = extract_tx_hash(tx_hash)
    expected_amount_minor = amount_to_usdt_minor(expected_amount)

    if normalized_network not in SUPPORTED_NETWORKS:
        return _result(
            "rejected",
            "unsupported_network",
            "Поддерживаются только USDT в сетях TRC20 и ERC20.",
            network=normalized_network,
            tx_hash=normalized_hash,
            expected_amount_minor=expected_amount_minor,
        )
    if normalized_hash is None:
        return _result(
            "rejected",
            "invalid_tx_hash",
            "Пришлите хэш транзакции из 64 символов.",
            network=normalized_network,
            expected_amount_minor=expected_amount_minor,
        )

    explorer = client or USDTExplorerClient()
    try:
        tx = await explorer.fetch_transaction(normalized_network, normalized_hash)
    except USDTExplorerError as exc:
        return _result(
            "manual_review",
            "explorer_error",
            str(exc),
            network=normalized_network,
            tx_hash=normalized_hash,
            expected_amount_minor=expected_amount_minor,
        )

    if tx is None:
        return _result(
            "manual_review",
            "tx_not_found",
            "Обозреватель сети пока не нашёл эту транзакцию.",
            network=normalized_network,
            tx_hash=normalized_hash,
            expected_amount_minor=expected_amount_minor,
        )
    if not tx.success:
        return _result(
            "rejected",
            "failed_tx",
            "Обозреватель сети отмечает эту транзакцию как неуспешную.",
            tx=tx,
            expected_amount_minor=expected_amount_minor,
        )

    threshold = confirmation_threshold(normalized_network)
    if tx.confirmations is None or tx.confirmations < threshold:
        return _result(
            "manual_review",
            "insufficient_confirmations",
            f"У транзакции меньше {threshold} подтверждений.",
            tx=tx,
            expected_amount_minor=expected_amount_minor,
        )

    official_transfers = [
        transfer
        for transfer in tx.transfers
        if _is_official_usdt_contract(normalized_network, transfer.contract_address)
    ]
    if not official_transfers:
        return _result(
            "rejected",
            "wrong_contract",
            "Транзакция не переводит официальный USDT в выбранной сети.",
            tx=tx,
            expected_amount_minor=expected_amount_minor,
        )

    expected_wallet = receiving_wallet(normalized_network)
    wallet_transfers = [
        transfer
        for transfer in official_transfers
        if _same_address(normalized_network, transfer.to_address, expected_wallet)
    ]
    if not wallet_transfers:
        return _result(
            "rejected",
            "wrong_destination",
            "Транзакция отправляет USDT на другой кошелёк.",
            tx=tx,
            expected_amount_minor=expected_amount_minor,
        )

    exact_transfers = [
        transfer for transfer in wallet_transfers if transfer.amount_minor == expected_amount_minor
    ]
    if not exact_transfers:
        observed = wallet_transfers[0].amount_minor if wallet_transfers else None
        return _result(
            "rejected",
            "wrong_amount",
            "Сумма транзакции не совпадает с суммой счёта.",
            tx=tx,
            expected_amount_minor=expected_amount_minor,
            observed_amount_minor=observed,
        )

    return _result(
        "valid",
        "valid",
        "Транзакция USDT подтверждена.",
        tx=tx,
        expected_amount_minor=expected_amount_minor,
        observed_amount_minor=exact_transfers[0].amount_minor,
    )


async def verify_and_apply_usdt_payment(
    session: AsyncSession,
    payment: Payment,
    tx_hash: str,
    *,
    bot: Bot | None = None,
    network: str | None = None,
    client: USDTExplorer | None = None,
) -> USDTVerificationResult:
    if payment.provider != "usdt":
        return _result(
            "rejected",
            "not_usdt_payment",
            "Этот платёж не является заявкой на оплату USDT.",
        )
    if payment.status not in {"awaiting_review", "pending"}:
        return _result(
            "rejected",
            "payment_not_pending",
            "Платёж не ожидает проверки.",
        )

    normalized_network = normalize_network(network) or network_from_payment(payment)
    normalized_hash = extract_tx_hash(tx_hash)
    if normalized_network and normalized_hash:
        duplicate_id = await _find_existing_tx_claim(
            session,
            payment_id=payment.id,
            network=normalized_network,
            tx_hash=normalized_hash,
        )
        if duplicate_id is not None:
            return _result(
                "rejected",
                "duplicate_tx",
                "Этот хэш транзакции уже был использован.",
                network=normalized_network,
                tx_hash=normalized_hash,
            )

    result = await verify_usdt_transaction(
        normalized_network,
        normalized_hash,
        payment.amount,
        client=client,
    )

    if result.network:
        payment.tx_network = result.network
    if result.tx_hash:
        payment.tx_hash = result.tx_hash
    payment.note = _append_note(
        payment.note,
        f"[usdt:{result.reason}] tx={result.tx_hash or 'n/a'} "
        f"network={result.network or 'n/a'} confirmations={result.confirmations}",
    )

    if result.is_valid:
        # GK-401: reserve the (tx_network, tx_hash) claim atomically BEFORE any
        # external fulfillment side effect. The pending tx_hash assignment above
        # is flushed inside a SAVEPOINT; the partial unique index
        # `uq_pay_tx_network_hash` makes a concurrent duplicate's flush raise
        # IntegrityError, so the loser is rejected here — never producing an
        # invite — instead of only being caught at the too-late final commit.
        if not await _reserve_tx_claim(session):
            payment.tx_hash = None
            payment.status = "failed"
            payment.note = _append_note(
                payment.note,
                "[usdt:duplicate_tx] claim lost to a concurrent payment",
            )
            return _result(
                "rejected",
                "duplicate_tx",
                "Этот хэш транзакции уже был использован.",
                network=result.network,
                tx_hash=result.tx_hash,
            )
        payment.status = "succeeded"
        payment.tx_confirmed_at = result.confirmed_at or utcnow()
        payment.provider_event_id = payment.provider_event_id or (
            f"usdt:{result.network}:{result.tx_hash}"
        )
        subscription = await fulfill_payment(session, bot, payment)
        return USDTVerificationResult(
            **{
                **result.__dict__,
                "subscription_id": getattr(subscription, "id", None),
                "invite_link": getattr(subscription, "invite_link", None),
            }
        )

    if result.needs_manual_review:
        payment.status = "awaiting_review"
    else:
        payment.status = "failed"
    return result


async def _reserve_tx_claim(session: AsyncSession) -> bool:
    """Reserve the pending ``(tx_network, tx_hash)`` via the partial unique index.

    Flushes the already-assigned payment fields inside a SAVEPOINT so a unique
    conflict — a concurrent payment that committed the same ``(network, hash)``
    claim — rolls back only the savepoint, leaving the caller's transaction
    usable. Returns ``True`` when this payment holds the claim, ``False`` when it
    was lost to a concurrent winner (caller must not fulfil).
    """
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        return False
    return True


async def _find_existing_tx_claim(
    session: AsyncSession,
    *,
    payment_id: int,
    network: str,
    tx_hash: str,
) -> int | None:
    candidates = [tx_hash, f"0x{tx_hash}"]
    return (
        await session.execute(
            select(Payment.id)
            .where(
                Payment.id != payment_id,
                Payment.tx_network == network,
                Payment.tx_hash.is_not(None),
                func.lower(Payment.tx_hash).in_(candidates),
            )
            .limit(1)
        )
    ).scalar_one_or_none()


def amount_to_usdt_minor(amount: Decimal | int | float | str) -> int:
    try:
        decimal_amount = Decimal(str(amount))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid USDT amount: {amount!r}") from exc
    minor = decimal_amount * USDT_MINOR_UNITS
    if minor != minor.to_integral_value():
        raise ValueError(f"USDT amount has more than {USDT_DECIMALS} decimals: {amount!r}")
    return int(minor)


def confirmation_threshold(network: str) -> int:
    if network == NETWORK_TRC20:
        return settings.usdt_trc20_confirmations
    if network == NETWORK_ERC20:
        return settings.usdt_erc20_confirmations
    raise ValueError(f"unsupported network: {network}")


def receiving_wallet(network: str) -> str:
    if network == NETWORK_TRC20:
        return settings.usdt_trc20_address
    if network == NETWORK_ERC20:
        return settings.usdt_erc20_address
    raise ValueError(f"unsupported network: {network}")


def parse_trc20_explorer_payloads(
    *,
    tx_hash: str,
    events_payload: dict[str, Any],
    info_payload: dict[str, Any],
    latest_block_payload: dict[str, Any],
) -> USDTExplorerTx | None:
    if _is_empty_tron_response(info_payload) and not events_payload.get("data"):
        return None

    tx_block = _first_int(
        info_payload.get("blockNumber"),
        _first_event_value(events_payload, "block_number"),
        _first_event_value(events_payload, "blockNumber"),
    )
    latest_block = _first_int(
        latest_block_payload.get("block_header", {}).get("raw_data", {}).get("number"),
        latest_block_payload.get("blockID"),
    )
    confirmations = _confirmation_count(tx_block, latest_block)
    confirmed_at = _timestamp_from_ms(
        _first_int(
            info_payload.get("blockTimeStamp"),
            _first_event_value(events_payload, "block_timestamp"),
            _first_event_value(events_payload, "block_timestamp_ms"),
        )
    )
    success = _tron_success(info_payload)
    transfers = _parse_tron_events(events_payload)
    if not transfers:
        transfers = _parse_tron_receipt_logs(info_payload)

    return USDTExplorerTx(
        network=NETWORK_TRC20,
        tx_hash=extract_tx_hash(tx_hash) or tx_hash.lower(),
        success=success,
        confirmations=confirmations,
        confirmed_at=confirmed_at,
        transfers=transfers,
    )


def parse_erc20_receipt_payloads(
    *,
    tx_hash: str,
    receipt_payload: dict[str, Any],
    latest_block_payload: dict[str, Any],
) -> USDTExplorerTx | None:
    receipt = _etherscan_result(receipt_payload)
    if receipt is None:
        return None

    latest_hex = _etherscan_result(latest_block_payload)
    latest_block = _hex_to_int(latest_hex)
    tx_block = _hex_to_int(receipt.get("blockNumber"))
    confirmations = _confirmation_count(tx_block, latest_block)
    status = (receipt.get("status") or "").lower()
    success = status != "0x0"
    transfers: list[USDTTransferObservation] = []

    for log in receipt.get("logs") or []:
        topics = [str(topic).lower().removeprefix("0x") for topic in (log.get("topics") or [])]
        if len(topics) < 3 or topics[0] != ERC20_TRANSFER_TOPIC:
            continue
        amount_minor = _hex_to_int(log.get("data"))
        if amount_minor is None:
            continue
        transfers.append(
            USDTTransferObservation(
                contract_address=str(log.get("address") or ""),
                to_address=topics[2],
                amount_minor=amount_minor,
            )
        )

    return USDTExplorerTx(
        network=NETWORK_ERC20,
        tx_hash=extract_tx_hash(tx_hash) or tx_hash.lower().removeprefix("0x"),
        success=success,
        confirmations=confirmations,
        confirmed_at=None,
        transfers=transfers,
    )


def _result(
    decision: str,
    reason: str,
    message: str,
    *,
    tx: USDTExplorerTx | None = None,
    network: str | None = None,
    tx_hash: str | None = None,
    expected_amount_minor: int | None = None,
    observed_amount_minor: int | None = None,
) -> USDTVerificationResult:
    return USDTVerificationResult(
        decision=decision,
        reason=reason,
        message=message,
        network=network or (tx.network if tx else None),
        tx_hash=tx_hash or (tx.tx_hash if tx else None),
        confirmations=tx.confirmations if tx else None,
        expected_amount_minor=expected_amount_minor,
        observed_amount_minor=observed_amount_minor,
        confirmed_at=tx.confirmed_at if tx else None,
    )


def _parse_tron_events(payload: dict[str, Any]) -> list[USDTTransferObservation]:
    transfers: list[USDTTransferObservation] = []
    for event in payload.get("data") or []:
        if str(event.get("event_name") or event.get("event") or "").lower() != "transfer":
            continue
        result = event.get("result") or {}
        amount = _first_int(result.get("value"), result.get("_value"))
        to_address = result.get("to") or result.get("_to")
        contract = event.get("contract_address") or event.get("contract")
        if amount is None or not to_address or not contract:
            continue
        transfers.append(
            USDTTransferObservation(
                contract_address=str(contract),
                to_address=str(to_address),
                amount_minor=amount,
            )
        )
    return transfers


def _parse_tron_receipt_logs(payload: dict[str, Any]) -> list[USDTTransferObservation]:
    transfers: list[USDTTransferObservation] = []
    for log in payload.get("log") or []:
        topics = [str(topic).lower().removeprefix("0x") for topic in (log.get("topics") or [])]
        if len(topics) < 3 or topics[0] != ERC20_TRANSFER_TOPIC:
            continue
        amount = _hex_to_int(log.get("data"))
        if amount is None:
            continue
        transfers.append(
            USDTTransferObservation(
                contract_address=str(log.get("address") or ""),
                to_address=topics[2],
                amount_minor=amount,
            )
        )
    return transfers


def _tron_success(payload: dict[str, Any]) -> bool:
    receipt = payload.get("receipt") or {}
    result = str(receipt.get("result") or "").upper()
    if result and result != "SUCCESS":
        return False
    for item in payload.get("ret") or []:
        contract_ret = str(item.get("contractRet") or "").upper()
        if contract_ret and contract_ret != "SUCCESS":
            return False
    return True


def _is_empty_tron_response(payload: dict[str, Any]) -> bool:
    return not payload or (set(payload.keys()) <= {"Error"} and bool(payload.get("Error")))


def _etherscan_result(payload: dict[str, Any]) -> Any:
    message = str(payload.get("message") or "").lower()
    if payload.get("status") == "0" and message not in {"", "ok"}:
        raise USDTExplorerError(payload.get("result") or payload.get("message") or "etherscan error")
    return payload.get("result")


def _is_official_usdt_contract(network: str, value: str) -> bool:
    if network == NETWORK_TRC20:
        return _same_address(network, value, TRC20_USDT_CONTRACT)
    if network == NETWORK_ERC20:
        return _same_address(network, value, ERC20_USDT_CONTRACT)
    return False


def _same_address(network: str, actual: str, expected: str) -> bool:
    if network == NETWORK_ERC20:
        return _normalize_eth_address(actual) == _normalize_eth_address(expected)
    if network == NETWORK_TRC20:
        return _normalize_tron_address(actual) == _normalize_tron_address(expected)
    return False


def _normalize_eth_address(value: str) -> str:
    raw = (value or "").strip().lower().removeprefix("0x")
    if len(raw) > 40:
        raw = raw[-40:]
    return raw


def _normalize_tron_address(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if raw.startswith("T"):
        try:
            return _tron_base58check_to_hex(raw).lower()
        except ValueError:
            return raw.lower()
    raw_hex = raw.lower().removeprefix("0x")
    if len(raw_hex) == 40:
        raw_hex = f"41{raw_hex}"
    if len(raw_hex) > 42:
        raw_hex = f"41{raw_hex[-40:]}"
    return raw_hex


def _tron_base58check_to_hex(address: str) -> str:
    num = 0
    for char in address:
        num *= 58
        try:
            num += _BASE58_ALPHABET.index(char)
        except ValueError as exc:
            raise ValueError(f"invalid TRON address: {address}") from exc

    combined = num.to_bytes((num.bit_length() + 7) // 8, "big")
    leading_zeroes = len(address) - len(address.lstrip("1"))
    combined = (b"\x00" * leading_zeroes) + combined
    if len(combined) != 25:
        raise ValueError(f"invalid TRON address length: {address}")
    payload, checksum = combined[:-4], combined[-4:]
    digest = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    if checksum != digest:
        raise ValueError(f"invalid TRON address checksum: {address}")
    return payload.hex()


def _confirmation_count(tx_block: int | None, latest_block: int | None) -> int | None:
    if tx_block is None or latest_block is None or latest_block < tx_block:
        return None
    return latest_block - tx_block + 1


def _timestamp_from_ms(value: int | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def _first_event_value(payload: dict[str, Any], key: str) -> Any:
    for event in payload.get("data") or []:
        if key in event:
            return event[key]
    return None


def _first_int(*values: Any) -> int | None:
    for value in values:
        parsed = _parse_int(value)
        if parsed is not None:
            return parsed
    return None


def _parse_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    if text.lower().startswith("0x"):
        return _hex_to_int(text)
    try:
        return int(text)
    except ValueError:
        return None


def _hex_to_int(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip().lower().removeprefix("0x")
    if not text:
        return None
    try:
        return int(text, 16)
    except ValueError:
        return None


def _append_note(note: str | None, line: str) -> str:
    prefix = (note or "").rstrip()
    stamp = utcnow().isoformat()
    entry = f"{stamp} {line}"
    return f"{prefix}\n{entry}" if prefix else entry
