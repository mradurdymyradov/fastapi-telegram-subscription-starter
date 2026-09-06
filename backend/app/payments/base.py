from dataclasses import dataclass


@dataclass
class CheckoutResult:
    payment_id: int
    url: str | None  # link юзеру (Stripe Checkout / Lava). None для manual.
    instructions: str | None  # текст для manual (реквизиты Zelle/USDT)


@dataclass
class PaymentEvent:
    external_id: str
    status: str  # checkout_completed | invoice_paid | invoice_payment_failed | subscription_updated | subscription_deleted
    amount: float
    currency: str
    metadata: dict
