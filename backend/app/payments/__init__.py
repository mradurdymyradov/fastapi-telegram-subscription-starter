"""Payment provider abstraction.

Each provider exposes:
    async create_checkout(user, plan, gift_recipient_tg_id?) -> CheckoutResult
    handle_webhook(payload, signature?) -> WebhookEvent      # provider-specific
    async fulfill(session, payment) -> Subscription          # called once payment is succeeded
"""
from app.payments.base import CheckoutResult, PaymentEvent
from app.payments.lava_provider import LavaProvider
from app.payments.manual_provider import ManualProvider
from app.payments.stripe_provider import StripeProvider

__all__ = ["CheckoutResult", "PaymentEvent", "StripeProvider", "LavaProvider", "ManualProvider"]
