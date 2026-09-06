from datetime import UTC, datetime

from sqlalchemy.dialects import postgresql

from app.api.routers.metrics import (
    _recognized_revenue_by_currency_query,
    _recognized_revenue_by_day_query,
    _recognized_revenue_query,
)


def _sql(query) -> str:
    return str(
        query.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()


def test_recognized_revenue_deducts_only_confirmed_refund_total():
    sql = _sql(_recognized_revenue_query(datetime(2026, 6, 1, tzinfo=UTC)))

    assert "payments.amount - coalesce(payments.refunded_amount" in sql
    assert "payments.status in ('succeeded', 'refunded')" in sql
    assert "payments.created_at >=" in sql


def test_daily_revenue_uses_same_net_confirmed_cash_expression():
    sql = _sql(_recognized_revenue_by_day_query(datetime(2026, 6, 1, tzinfo=UTC)))

    assert "date_trunc('day', payments.created_at)" in sql
    assert "payments.amount - coalesce(payments.refunded_amount" in sql
    assert "payments.status in ('succeeded', 'refunded')" in sql
    assert "group by d" in sql
    # GK-414: no currency filter unless one is requested — the default series is unfiltered.
    assert "payments.currency =" not in sql


def test_daily_revenue_filters_to_one_currency_when_requested():
    sql = _sql(
        _recognized_revenue_by_day_query(datetime(2026, 6, 1, tzinfo=UTC), currency="RUB")
    )

    # GK-414: a single-currency series so the chart never mixes RUB and USD.
    assert "payments.currency = 'rub'" in sql
    assert "payments.amount - coalesce(payments.refunded_amount" in sql


def test_revenue_by_currency_groups_per_currency_and_never_mixes():
    sql = _sql(_recognized_revenue_by_currency_query(datetime(2026, 6, 1, tzinfo=UTC)))

    # GK-414: revenue is broken out per currency (RUB not summed into USD).
    assert "payments.currency" in sql
    assert "payments.amount - coalesce(payments.refunded_amount" in sql
    assert "payments.status in ('succeeded', 'refunded')" in sql
    assert "group by payments.currency" in sql
