"""Holiday-plan calculator — Python twin of /holiday-plan.js.

Used by any Python tool that needs to know a Person's current min/max
holiday entitlement (e.g. a future "list everyone over their max" CLI
report). The browser-side calculator drives the live UI; this one
exists so the same formulas can be exercised in CI and from the
command line.

Single public function: `compute(person, payroll=None, plans_doc=None,
as_of=None)` returns a dict matching the JS twin's return shape.
"""

from __future__ import annotations

import json
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


STATUTORY_MIN_DEFAULT = 2.333333
DEFAULT_PLANS_PATH = Path(__file__).resolve().parent.parent / "holiday-plans.json"


def _parse_date(value: Any) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    s = str(value).strip()[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _most_recent_tax_year_start(as_of: date, month1: int, day1: int) -> date:
    candidate = date(as_of.year, month1, day1)
    if candidate <= as_of:
        return candidate
    return date(as_of.year - 1, month1, day1)


def _decimal_months_between(a: Optional[date], b: Optional[date]) -> float:
    """Decimal months between two dates (a < b). Counts full calendar
    months and pro-rates the partial month at the trailing end. Mirrors
    the JS twin: 2026-04-15 → 2026-05-15 returns exactly 1.0; the
    leftover days are scaled by the *trailing* month's length, not 30.
    """
    if not a or not b or b <= a:
        return 0.0
    months = (b.year - a.year) * 12 + (b.month - a.month)
    if b.day >= a.day:
        days_in_b = monthrange(b.year, b.month)[1]
        months += (b.day - a.day) / days_in_b
    else:
        months -= 1
        # Days_in_prev = previous month relative to b.
        prev_month = 12 if b.month == 1 else b.month - 1
        prev_year = b.year - 1 if b.month == 1 else b.year
        days_in_prev = monthrange(prev_year, prev_month)[1]
        months += (days_in_prev - a.day + b.day) / days_in_prev
    return max(0.0, months)


def _find_plan(plans_doc: Dict[str, Any], plan_id: str) -> Optional[Dict[str, Any]]:
    plans = (plans_doc or {}).get("plans") or []
    target = (plan_id or "").strip()
    for p in plans:
        if p.get("id") == target:
            return p
    return None


def _round2(n: float) -> float:
    return round(n * 100) / 100


def load_plans(path: Path = DEFAULT_PLANS_PATH) -> Dict[str, Any]:
    return json.loads(path.read_text())


def compute(
    person: Optional[Dict[str, Any]] = None,
    payroll: Optional[Dict[str, Any]] = None,
    plans_doc: Optional[Dict[str, Any]] = None,
    as_of: Optional[date] = None,
) -> Dict[str, Any]:
    plans_doc = plans_doc or load_plans()
    now = as_of or datetime.now(timezone.utc).date()
    tax_month = int(plans_doc.get("tax_year_start_month") or 4)
    tax_day = int(plans_doc.get("tax_year_start_day") or 1)
    default_plan_id = plans_doc.get("default_plan") or "OpsPlan1"
    statutory = float(plans_doc.get("statutory_min_days_per_month") or STATUTORY_MIN_DEFAULT)

    tax_year_start = _most_recent_tax_year_start(now, tax_month, tax_day)

    person_start = _parse_date((person or {}).get("start_date"))
    payroll_start = _parse_date((payroll or {}).get("start_date"))
    effective_start = person_start or payroll_start

    person_end = _parse_date((person or {}).get("end_date"))
    payroll_end = _parse_date((payroll or {}).get("termination_date"))
    effective_end = person_end or payroll_end or now

    service_start = tax_year_start
    if effective_start and effective_start > tax_year_start:
        service_start = effective_start

    service_end = effective_end if effective_end < now else now
    if service_end < service_start:
        service_end = service_start

    months_worked = _decimal_months_between(service_start, service_end)

    plan_id = (person or {}).get("holiday_plan") or default_plan_id
    plan = _find_plan(plans_doc, plan_id) or _find_plan(plans_doc, default_plan_id)
    annual_max = float((plan or {}).get("annual_max_days") or 0)

    min_days = _round2(months_worked * statutory)
    max_days = _round2((annual_max * months_worked) / 12)

    if months_worked <= 0:
        reason = f"Service window has zero months (start {service_start.isoformat()} ≥ end {service_end.isoformat()})."
    elif effective_start and effective_start > tax_year_start:
        reason = (
            f"Started {effective_start.isoformat()} (after {tax_year_start.isoformat()}), so pro-rated: "
            f"{months_worked:.2f} months × ({annual_max:.0f}/12) = {max_days} max, "
            f"× {statutory} = {min_days} statutory min."
        )
    elif effective_end and effective_end < now:
        reason = (
            f"Employment ended {effective_end.isoformat()}; pro-rated: "
            f"{months_worked:.2f} months × ({annual_max:.0f}/12) = {max_days} max, "
            f"× {statutory} = {min_days} statutory min."
        )
    else:
        if annual_max >= 365:
            reason = f"{months_worked:.2f} months elapsed since {tax_year_start.isoformat()}. Plan is uncapped."
        else:
            reason = (
                f"{months_worked:.2f} months elapsed since {tax_year_start.isoformat()}. "
                f"Plan max {annual_max:.0f}/yr → {max_days} so far this year."
            )

    return {
        "plan": plan,
        "plan_id": plan["id"] if plan else plan_id,
        "tax_year_start": tax_year_start.isoformat(),
        "service_start": service_start.isoformat(),
        "service_end": service_end.isoformat(),
        "months_worked": months_worked,
        "min_days": min_days,
        "max_days": max_days,
        "is_unlimited": bool(plan and float(plan.get("annual_max_days") or 0) >= 365),
        "reason": reason,
    }


if __name__ == "__main__":
    # Quick smoke-test from the CLI: prints results for the current
    # OpsPlan1 default with a fixed example start date.
    sample_person = {"holiday_plan": "OpsPlan1", "start_date": "2024-01-01"}
    print(json.dumps(compute(sample_person), indent=2))
    sample_late = {"holiday_plan": "OpsPlan1", "start_date": "2026-09-01"}
    print(json.dumps(compute(sample_late), indent=2))
    sample_unlimited = {"holiday_plan": "Unlimited", "start_date": "2024-01-01"}
    print(json.dumps(compute(sample_unlimited), indent=2))
