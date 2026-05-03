"""
Investment Health calculations.

Pure functions that turn 7 business inputs (users, growth, churn, CAC, ARPU,
burn) into SaaS unit economics + a green/yellow/red verdict for the admin
"keep investing?" dashboard.

No I/O — every input is passed in, every output is returned. Stripe and HTTP
live in admin_dashboard.py. Tests live in tests/test_investment_health.py.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional


# ---------------------------------------------------------------------------
# Defaults — used when Stripe is unreachable or returns no data, and as the
# initial values for the two manual-only inputs (CAC, burn).
# ---------------------------------------------------------------------------

DEFAULT_INPUTS = {
    "current_users": 0,
    "net_now": 0,
    "net_3mo": 0,
    "churn_rate": 7.0,        # percent
    "cac": 60.0,              # dollars (manual only)
    "arpu": 8.0,              # dollars (net of $1 SMS cost)
    "monthly_burn": 4167.0,   # dollars (manual only)
}

# SMS cost subtracted from gross ARPU when computing net revenue per user.
SMS_COST_PER_USER = 1.0

# Below this churn rate the LTV math explodes — clamp to keep numbers honest.
MIN_CHURN_PERCENT = 1.0

# Hard cap on the "months to breakeven" simulation loop.
MAX_BREAKEVEN_MONTHS = 240


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class HealthCheck:
    metric: str
    value: float
    display: str
    status: str       # "green" | "yellow" | "red"
    context: str = ""


@dataclass
class HealthReport:
    inputs: dict
    derived: dict
    checks: dict[str, HealthCheck]
    verdict: str          # "Continue investing" | "Pivot before reinvesting" | "Stop or restructure"
    verdict_color: str    # "green" | "yellow" | "red"
    message: str

    def to_dict(self) -> dict:
        return {
            "inputs": self.inputs,
            "derived": self.derived,
            "checks": {k: asdict(v) for k, v in self.checks.items()},
            "verdict": self.verdict,
            "verdict_color": self.verdict_color,
            "message": self.message,
        }


# ---------------------------------------------------------------------------
# Core math
# ---------------------------------------------------------------------------

def _clamp_churn(churn_percent: float) -> float:
    return max(churn_percent, MIN_CHURN_PERCENT)


def compute_derived(
    current_users: float,
    net_now: float,
    net_3mo: float,
    churn_rate: float,
    cac: float,
    arpu: float,
    monthly_burn: float,
) -> dict:
    """Compute everything downstream from the 7 inputs.

    `churn_rate` is a percent (e.g. 7 means 7%). All currency values are
    monthly dollars.
    """
    churn_d = _clamp_churn(churn_rate) / 100.0

    # Avoid divide-by-zero on the rare case where someone enters arpu=0 in
    # the UI. Treat 0 ARPU as "infinite payback / unreachable breakeven".
    if arpu <= 0:
        ltv = 0.0
        ltv_cac = 0.0
        cac_payback_months = float("inf")
        breakeven_users = float("inf")
    else:
        ltv = arpu / churn_d
        ltv_cac = ltv / cac if cac > 0 else float("inf")
        cac_payback_months = cac / arpu
        breakeven_users = monthly_burn / arpu

    # gross_adds = net adds + users that churned this month. Floor at 0 so
    # a shrinking month doesn't produce negative acquisition.
    gross_adds = max(0.0, net_now + current_users * churn_d)

    # Steady-state user base: where the population plateaus given current
    # acquisition rate and churn. gross_adds / churn_d.
    steady_state = gross_adds / churn_d if churn_d > 0 else float("inf")

    can_reach_breakeven = steady_state >= breakeven_users

    # Months to breakeven — simulate month-by-month because net adds
    # decline as the base grows (more users, more churn).
    months_to_breakeven: Optional[int]
    if can_reach_breakeven and net_now > 0 and current_users < breakeven_users:
        u = float(current_users)
        m = 0
        while u < breakeven_users and m < MAX_BREAKEVEN_MONTHS:
            u = u * (1 - churn_d) + gross_adds
            m += 1
        months_to_breakeven = m if m < MAX_BREAKEVEN_MONTHS else None
    elif current_users >= breakeven_users:
        months_to_breakeven = 0
    else:
        months_to_breakeven = None

    return {
        "ltv": ltv,
        "ltv_cac": ltv_cac,
        "cac_payback_months": cac_payback_months,
        "breakeven_users": breakeven_users,
        "gross_adds": gross_adds,
        "steady_state": steady_state,
        "can_reach_breakeven": can_reach_breakeven,
        "months_to_breakeven": months_to_breakeven,
        "growth_delta": net_now - net_3mo,
    }


# ---------------------------------------------------------------------------
# Status thresholds
# ---------------------------------------------------------------------------

def churn_status(churn_percent: float) -> str:
    if churn_percent < 7:
        return "green"
    if churn_percent <= 10:
        return "yellow"
    return "red"


def growth_status(net_now: float, net_3mo: float) -> str:
    """Green if growing, holding, or only mildly decelerating; red if net adds
    have stopped or gone negative."""
    if net_now <= 0:
        return "red"
    delta = net_now - net_3mo
    if delta >= -3:
        return "green"
    return "yellow"


def ltv_cac_status(ratio: float) -> str:
    if ratio >= 3.0:
        return "green"
    if ratio >= 1.5:
        return "yellow"
    return "red"


def cac_payback_status(months: float) -> str:
    if months < 12:
        return "green"
    if months <= 24:
        return "yellow"
    return "red"


def breakeven_months_status(months: Optional[int]) -> str:
    if months is None:
        return "red"
    if months <= 12:
        return "green"
    if months <= 24:
        return "yellow"
    return "red"


def steady_state_status(steady_state: float, breakeven_users: float) -> str:
    if breakeven_users <= 0 or breakeven_users == float("inf"):
        return "red"
    ratio = steady_state / breakeven_users
    if ratio >= 1.3:
        return "green"
    if ratio >= 1.0:
        return "yellow"
    return "red"


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _format_breakeven(months: Optional[int]) -> str:
    if months is None:
        return "Never"
    if months >= MAX_BREAKEVEN_MONTHS:
        return ">20 yr"
    return f"{months} mo"


def _format_currency(amount: float) -> str:
    return f"${round(amount):,}"


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------

def _verdict(checks: dict[str, HealthCheck]) -> tuple[str, str, str]:
    """Returns (verdict, color, message). Uses only the 4 primary checks:
    churn, growth_trend, ltv_cac, breakeven_months. CAC payback and
    steady-state are diagnostic only."""
    primary = [
        checks["churn"].status,
        checks["growth_trend"].status,
        checks["ltv_cac"].status,
        checks["breakeven_months"].status,
    ]
    red_count = sum(1 for s in primary if s == "red")
    green_count = sum(1 for s in primary if s == "green")

    critical_red = checks["churn"].status == "red" or checks["ltv_cac"].status == "red"

    if critical_red or red_count >= 2:
        return (
            "Stop or restructure",
            "red",
            f"{red_count} critical fail(s). Unit economics or product stickiness "
            f"won't fix themselves with more ad spend. Cut burn, fix retention or "
            f"pricing, or wind down before adding more capital.",
        )
    if green_count >= 3:
        return (
            "Continue investing",
            "green",
            f"{green_count} of 4 health checks pass. Slower ramp than original "
            f"target is fine — the underlying business works. Keep optimizing "
            f"creative, conversion, and the upgrade flow.",
        )
    return (
        "Pivot before reinvesting",
        "yellow",
        f"{green_count} of 4 pass, {red_count} fail. Fixable problem in one "
        f"dimension. Address the weak metric before adding more spend — "
        f"don't double down on a leak.",
    )


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

def evaluate(
    current_users: float,
    net_now: float,
    net_3mo: float,
    churn_rate: float,
    cac: float,
    arpu: float,
    monthly_burn: float,
) -> HealthReport:
    """Run the full pipeline: derive metrics, score each check, compute verdict."""
    derived = compute_derived(
        current_users, net_now, net_3mo, churn_rate, cac, arpu, monthly_burn
    )

    checks: dict[str, HealthCheck] = {}

    checks["churn"] = HealthCheck(
        metric="Monthly churn",
        value=churn_rate,
        display=f"{churn_rate:.1f}%",
        status=churn_status(churn_rate),
        context="trailing 90 days",
    )

    growth_delta = derived["growth_delta"]
    sign = "+" if growth_delta >= 0 else ""
    checks["growth_trend"] = HealthCheck(
        metric="Growth trend",
        value=growth_delta,
        display=f"{sign}{round(growth_delta)}",
        status=growth_status(net_now, net_3mo),
        context=f"{round(net_now)} now vs {round(net_3mo)} three months ago",
    )

    checks["ltv_cac"] = HealthCheck(
        metric="LTV / CAC",
        value=derived["ltv_cac"],
        display=f"{derived['ltv_cac']:.1f}x",
        status=ltv_cac_status(derived["ltv_cac"]),
        context=f"LTV {_format_currency(derived['ltv'])} / CAC {_format_currency(cac)}",
    )

    checks["cac_payback"] = HealthCheck(
        metric="CAC payback",
        value=derived["cac_payback_months"],
        display=(
            f"{derived['cac_payback_months']:.1f} mo"
            if derived["cac_payback_months"] != float("inf")
            else "∞"
        ),
        status=cac_payback_status(derived["cac_payback_months"]),
        context="months to recover acquisition cost",
    )

    checks["breakeven_months"] = HealthCheck(
        metric="Months to breakeven",
        value=derived["months_to_breakeven"] if derived["months_to_breakeven"] is not None else -1,
        display=_format_breakeven(derived["months_to_breakeven"]),
        status=breakeven_months_status(derived["months_to_breakeven"]),
        context=f"need {round(derived['breakeven_users']):,} paid users",
    )

    checks["steady_state"] = HealthCheck(
        metric="Steady-state ceiling",
        value=derived["steady_state"],
        display=f"{round(derived['steady_state']):,}",
        status=steady_state_status(derived["steady_state"], derived["breakeven_users"]),
        context=f"breakeven needs {round(derived['breakeven_users']):,}",
    )

    verdict, color, message = _verdict(checks)

    return HealthReport(
        inputs={
            "current_users": current_users,
            "net_now": net_now,
            "net_3mo": net_3mo,
            "churn_rate": churn_rate,
            "cac": cac,
            "arpu": arpu,
            "monthly_burn": monthly_burn,
        },
        derived=derived,
        checks=checks,
        verdict=verdict,
        verdict_color=color,
        message=message,
    )
