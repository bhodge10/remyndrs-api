"""
Unit tests for services/investment_health.py.

The three sanity tests come straight from the prompt's acceptance criteria —
they pin the verdict logic against known-good inputs.
"""

from services.investment_health import (
    evaluate,
    compute_derived,
    churn_status,
    growth_status,
    ltv_cac_status,
    cac_payback_status,
    breakeven_months_status,
    steady_state_status,
)


# ---------------------------------------------------------------------------
# Sanity tests from the prompt
# ---------------------------------------------------------------------------

def test_sanity_yellow_pivot():
    """users=250, net_now=40, net_3mo=30, churn=7, cac=60, arpu=8, burn=4167
    → Pivot before reinvesting (yellow)."""
    r = evaluate(
        current_users=250, net_now=40, net_3mo=30,
        churn_rate=7, cac=60, arpu=8, monthly_burn=4167,
    )
    assert r.verdict == "Pivot before reinvesting"
    assert r.verdict_color == "yellow"

    assert r.checks["churn"].status == "yellow"
    assert r.checks["ltv_cac"].status == "yellow"
    assert r.checks["growth_trend"].status == "green"
    assert r.checks["breakeven_months"].status == "green"

    # LTV/CAC ≈ 1.9x
    assert abs(r.derived["ltv_cac"] - (8 / 0.07) / 60) < 0.01
    # breakeven ≈ 521 users
    assert round(r.derived["breakeven_users"]) == 521
    # months_to_breakeven ≈ 9
    assert r.derived["months_to_breakeven"] is not None
    assert r.derived["months_to_breakeven"] <= 12


def test_sanity_green_continue():
    """users=500, net_now=50, net_3mo=40, churn=5, cac=40, arpu=8, burn=4167
    → Continue investing (green) with all 4 primary checks green."""
    r = evaluate(
        current_users=500, net_now=50, net_3mo=40,
        churn_rate=5, cac=40, arpu=8, monthly_burn=4167,
    )
    assert r.verdict == "Continue investing"
    assert r.verdict_color == "green"

    assert r.checks["churn"].status == "green"
    assert r.checks["growth_trend"].status == "green"
    assert r.checks["ltv_cac"].status == "green"
    assert r.checks["breakeven_months"].status == "green"


def test_sanity_red_stop():
    """users=200, net_now=15, net_3mo=35, churn=12, cac=80, arpu=8, burn=4167
    → Stop or restructure (red). Churn red, LTV/CAC red, breakeven unreachable."""
    r = evaluate(
        current_users=200, net_now=15, net_3mo=35,
        churn_rate=12, cac=80, arpu=8, monthly_burn=4167,
    )
    assert r.verdict == "Stop or restructure"
    assert r.verdict_color == "red"

    assert r.checks["churn"].status == "red"
    assert r.checks["ltv_cac"].status == "red"
    assert r.checks["breakeven_months"].status == "red"
    # Steady state below breakeven — can't reach
    assert r.derived["months_to_breakeven"] is None
    assert not r.derived["can_reach_breakeven"]


# ---------------------------------------------------------------------------
# Status threshold edge cases
# ---------------------------------------------------------------------------

def test_churn_thresholds():
    assert churn_status(6.99) == "green"
    assert churn_status(7.0) == "yellow"
    assert churn_status(10.0) == "yellow"
    assert churn_status(10.01) == "red"


def test_ltv_cac_thresholds():
    assert ltv_cac_status(3.0) == "green"
    assert ltv_cac_status(2.99) == "yellow"
    assert ltv_cac_status(1.5) == "yellow"
    assert ltv_cac_status(1.49) == "red"


def test_cac_payback_thresholds():
    assert cac_payback_status(11.99) == "green"
    assert cac_payback_status(12.0) == "yellow"
    assert cac_payback_status(24.0) == "yellow"
    assert cac_payback_status(24.01) == "red"


def test_breakeven_months_thresholds():
    assert breakeven_months_status(12) == "green"
    assert breakeven_months_status(13) == "yellow"
    assert breakeven_months_status(24) == "yellow"
    assert breakeven_months_status(25) == "red"
    assert breakeven_months_status(None) == "red"


def test_steady_state_thresholds():
    # ratio = 1.3+ green, 1.0-1.3 yellow, <1.0 red
    assert steady_state_status(130, 100) == "green"
    assert steady_state_status(100, 100) == "yellow"
    assert steady_state_status(99, 100) == "red"


def test_growth_status_negative_net_is_red():
    assert growth_status(0, 10) == "red"
    assert growth_status(-5, 10) == "red"
    assert growth_status(10, 10) == "green"
    assert growth_status(8, 10) == "green"     # delta -2 >= -3
    assert growth_status(5, 10) == "yellow"    # delta -5 < -3 but net_now > 0


# ---------------------------------------------------------------------------
# Edge cases from acceptance criteria
# ---------------------------------------------------------------------------

def test_zero_net_adds_means_never_reaches_breakeven():
    d = compute_derived(
        current_users=100, net_now=0, net_3mo=10,
        churn_rate=7, cac=60, arpu=8, monthly_burn=4167,
    )
    assert d["months_to_breakeven"] is None


def test_churn_clamped_to_one_percent():
    # churn=0 would divide by zero in LTV calc; should clamp to 1% floor
    d = compute_derived(
        current_users=100, net_now=10, net_3mo=10,
        churn_rate=0, cac=60, arpu=8, monthly_burn=4167,
    )
    # ltv = 8 / 0.01 = 800 (not infinity)
    assert d["ltv"] == 800.0


def test_already_at_breakeven_is_zero_months():
    d = compute_derived(
        current_users=10000, net_now=10, net_3mo=10,
        churn_rate=5, cac=60, arpu=8, monthly_burn=4167,
    )
    assert d["months_to_breakeven"] == 0


def test_simulation_caps_at_max_months():
    # Tiny growth with high churn — would take forever, must not hang
    d = compute_derived(
        current_users=10, net_now=1, net_3mo=1,
        churn_rate=10, cac=60, arpu=8, monthly_burn=4167,
    )
    # Either reachable in <240 mo or returns None — must not hang
    assert d["months_to_breakeven"] is None or d["months_to_breakeven"] < 240


def test_growth_delta_sign_in_display():
    r = evaluate(
        current_users=250, net_now=40, net_3mo=30,
        churn_rate=7, cac=60, arpu=8, monthly_burn=4167,
    )
    assert r.checks["growth_trend"].display.startswith("+")

    r2 = evaluate(
        current_users=250, net_now=20, net_3mo=30,
        churn_rate=7, cac=60, arpu=8, monthly_burn=4167,
    )
    assert r2.checks["growth_trend"].display.startswith("-")


def test_to_dict_serializable():
    r = evaluate(
        current_users=250, net_now=40, net_3mo=30,
        churn_rate=7, cac=60, arpu=8, monthly_burn=4167,
    )
    import json
    # Should round-trip through JSON without error (no infinities for these inputs)
    payload = json.dumps(r.to_dict())
    assert "verdict" in payload
    assert "checks" in payload
