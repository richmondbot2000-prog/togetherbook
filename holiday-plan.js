/**
 * Holiday-plan calculator (browser-side twin of scripts/holiday_plan.py).
 *
 * Inputs:
 *   - person     a Person record with optional fields { holiday_plan,
 *                start_date, end_date }
 *   - payroll    optional payroll record carrying { termination_date,
 *                start_date }; used as fallback when the Person hasn't
 *                been edited away from payroll's authoritative values
 *   - plansDoc   the parsed holiday-plans.json document
 *   - asOf       optional JS Date (defaults to now) — pass a fixed date
 *                in tests, otherwise leave undefined
 *
 * Returns { plan, plan_id, tax_year_start, service_start, service_end,
 *           months_worked, min_days, max_days, is_unlimited, reason }
 *
 * `reason` is a one-line human-readable explanation of why the numbers
 * came out as they did — surfaced in the UI tooltip so a non-technical
 * admin can sanity-check the figure without reading code.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.HolidayPlan = factory();
  }
}(typeof self !== "undefined" ? self : this, function () {

  const STATUTORY_MIN_DEFAULT = 2.333333;

  function parseDate(s) {
    if (!s) return null;
    if (s instanceof Date) return new Date(s.getTime());
    const m = String(s).match(/^(\d{4})-(\d{2})-(\d{2})/);
    if (!m) return null;
    return new Date(Date.UTC(+m[1], +m[2] - 1, +m[3]));
  }

  function fmtDate(d) {
    if (!(d instanceof Date) || isNaN(d.getTime())) return "";
    return d.toISOString().slice(0, 10);
  }

  // Most-recent April 1st on or before `asOf`. If `asOf` is in March,
  // returns the April 1st of the *previous* calendar year.
  function mostRecentTaxYearStart(asOf, month1, day1) {
    const y = asOf.getUTCFullYear();
    const candidate = new Date(Date.UTC(y, month1 - 1, day1));
    if (candidate.getTime() <= asOf.getTime()) return candidate;
    return new Date(Date.UTC(y - 1, month1 - 1, day1));
  }

  // Decimal months between two dates (a < b). Counts full calendar
  // months and pro-rates the partial month at either end as fraction
  // of that month's length, so 2026-04-15 → 2026-05-15 = exactly 1.0
  // and 2026-04-15 → 2026-05-30 ≈ 1.48 (the trailing 15 days are 15/31
  // of May, not 15/30).
  function decimalMonthsBetween(a, b) {
    if (!a || !b || b.getTime() <= a.getTime()) return 0;
    const yA = a.getUTCFullYear(), mA = a.getUTCMonth(), dA = a.getUTCDate();
    const yB = b.getUTCFullYear(), mB = b.getUTCMonth(), dB = b.getUTCDate();
    let months = (yB - yA) * 12 + (mB - mA);
    if (dB >= dA) {
      const daysInMonth = new Date(Date.UTC(yB, mB + 1, 0)).getUTCDate();
      months += (dB - dA) / daysInMonth;
    } else {
      months -= 1;
      const daysInPrev = new Date(Date.UTC(yB, mB, 0)).getUTCDate();
      months += (daysInPrev - dA + dB) / daysInPrev;
    }
    return Math.max(0, months);
  }

  function findPlan(plansDoc, planId) {
    const plans = (plansDoc && plansDoc.plans) || [];
    const target = String(planId || "").trim();
    return plans.find(p => p.id === target) || null;
  }

  function compute({ person, payroll, plansDoc, asOf } = {}) {
    const now = asOf instanceof Date ? asOf : new Date();
    const taxMonth = (plansDoc && plansDoc.tax_year_start_month) || 4;
    const taxDay = (plansDoc && plansDoc.tax_year_start_day) || 1;
    const defaultPlanId = (plansDoc && plansDoc.default_plan) || "OpsPlan1";
    const statutory = (plansDoc && plansDoc.statutory_min_days_per_month) || STATUTORY_MIN_DEFAULT;

    const taxYearStart = mostRecentTaxYearStart(now, taxMonth, taxDay);

    // Resolve the planned start date. Prefer the Person record's
    // explicit field; fall back to payroll's start_date if the Person
    // hasn't been edited away from payroll's value yet.
    const personStart = parseDate(person && person.start_date);
    const payrollStart = parseDate(payroll && payroll.start_date);
    const effectiveStart = personStart || payrollStart;

    // Resolve the end of the service window. Person's `end_date` wins;
    // payroll's `termination_date` is the secondary; otherwise today.
    const personEnd = parseDate(person && person.end_date);
    const payrollEnd = parseDate(payroll && payroll.termination_date);
    const effectiveEnd = personEnd || payrollEnd || now;

    // For this tax year, the service window is bounded by the tax-year
    // start at the lower end and today (or end date, whichever earlier)
    // at the upper end. If start_date is later than tax-year start, the
    // service window starts at start_date instead.
    let serviceStart = taxYearStart;
    if (effectiveStart && effectiveStart.getTime() > taxYearStart.getTime()) {
      serviceStart = effectiveStart;
    }
    let serviceEnd = effectiveEnd.getTime() < now.getTime() ? effectiveEnd : now;
    if (serviceEnd.getTime() < serviceStart.getTime()) {
      serviceEnd = serviceStart;   // pre-employment, zero months
    }

    const monthsWorked = decimalMonthsBetween(serviceStart, serviceEnd);

    const planId = (person && person.holiday_plan) || defaultPlanId;
    const plan = findPlan(plansDoc, planId) || findPlan(plansDoc, defaultPlanId);
    const annualMax = plan ? Number(plan.annual_max_days) || 0 : 0;

    const minDays = round2(monthsWorked * statutory);
    const maxDays = round2((annualMax * monthsWorked) / 12);

    // Build a human-readable reason for the UI tooltip.
    let reason;
    if (monthsWorked <= 0) {
      reason = `Service window has zero months (start ${fmtDate(serviceStart)} ≥ end ${fmtDate(serviceEnd)}).`;
    } else if (effectiveStart && effectiveStart.getTime() > taxYearStart.getTime()) {
      reason = `Started ${fmtDate(effectiveStart)} (after ${fmtDate(taxYearStart)}), so pro-rated: ${monthsWorked.toFixed(2)} months × (${annualMax}/12) = ${maxDays} max, × ${statutory} = ${minDays} statutory min.`;
    } else if (effectiveEnd && effectiveEnd.getTime() < now.getTime()) {
      reason = `Employment ended ${fmtDate(effectiveEnd)}; pro-rated: ${monthsWorked.toFixed(2)} months × (${annualMax}/12) = ${maxDays} max, × ${statutory} = ${minDays} statutory min.`;
    } else {
      reason = `${monthsWorked.toFixed(2)} months elapsed since ${fmtDate(taxYearStart)}. ${annualMax === 365 ? "Plan is uncapped." : `Plan max ${annualMax}/yr → ${maxDays} so far this year.`}`;
    }

    return {
      plan,
      plan_id: plan ? plan.id : planId,
      tax_year_start: fmtDate(taxYearStart),
      service_start: fmtDate(serviceStart),
      service_end: fmtDate(serviceEnd),
      months_worked: monthsWorked,
      min_days: minDays,
      max_days: maxDays,
      is_unlimited: !!(plan && plan.annual_max_days >= 365),
      reason,
    };
  }

  function round2(n) {
    if (!isFinite(n)) return 0;
    return Math.round(n * 100) / 100;
  }

  return { compute, mostRecentTaxYearStart, decimalMonthsBetween, parseDate };
}));
