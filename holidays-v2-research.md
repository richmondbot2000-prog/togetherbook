# Holidays v2 — research + design rationale

## Shortlist — three references, one trick each

- **GitHub contribution graph (7 × 52).** Stolen: a fixed-pixel lattice that the eye reads as one image, not 365 squares. v1 wasted vertical room on Week-of labels and forced scrolling. v2 collapses to 1 row per person × 52 columns × 5 day micro-bars, so the full fiscal year sits in one viewport.
- **Tufte small multiples + sparklines.** Stolen: stack identical strips, sort by name, scan vertically for outliers. Each row is a per-person working-year sparkline; the right-hand 5-dot meter is an inline sparkbar of allowance-used.
- **Linear / GitHub timeline column totals.** Stolen: a "cover risk" ribbon above the lattice — bar height per week = headcount off. This is the feature v1 lacked: glanceable "which weeks have a hole." Bars go brass at ≥10% of team off, red at ≥25%.

## Rejected

- **BambooHR / Hibob month-grid Who's Out.** Pretty for one month, useless for year planning — pushed v1 to bolt a team-strip on as a secondary tab. v2 makes year-strip the default.
- **Personio yearly-per-individual.** Right shape, wrong default — single person only. v2 inverts: year always-on, multi-person by default.
- **Vacation Tracker wallchart.** Good density, SaaS-blue palette. Restripped to Quiet Edition (sage / brass / clay / paper).
- **Linear / Gantt swimlanes.** Cells too wide; tuned for multi-week durations. Holidays are 1–5 day spans — micro-bars beat lozenges.

## What I built

A single year-at-a-glance lattice hitting the 1280 × 15 × 365 × 9 target by collapsing weekends (HR doesn't approve Saturdays) and rendering Mon–Fri as 5 micro-bars per week column. At target viewport: ~21 px per week, ~4 px per day; 9 statuses stay distinguishable because the palette differentiates by hue family (sage = working, brass = paid time, red = sick, paper = off). 15 rows × 22 px ≈ 330 px, leaving room for the cover-risk ribbon and month strip above the fold.

Editing stayed 1-tap: click any day → popover with the 9 statuses → POST `/api/holidays/set` → optimistic repaint of the row + totals. Clicking a name pins a detail panel below: a Mon..Fri 5-rail expanded view + KPI block (used / remaining / sick / halves). Self-tab uses the same lattice with one row.

## Trade-off

Weekends are visually collapsed in the team grid. They remain editable from the focus panel (which renders all 7 days). This was the only way to fit 365 working-day cells across an ~1100 px strip without sub-pixel artefacts.
