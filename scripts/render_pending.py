#!/usr/bin/env python3
"""Render `pending.yaml` into three targets so they stay in lockstep.

Reads:
  pending.yaml  (canonical, hand-edited)

Writes (splices between AUTO markers, leaves surrounding text alone):
  SPEC.md                            — §17 Pending / blocked work
  ../wiki/CLAUDE_CONTEXT.md          — §8 Pending / open work
  pending.html                       — full page on the site

Markers used:
  In SPEC.md / CLAUDE_CONTEXT.md:
    <!-- PENDING:BEGIN -->
    ...generated content...
    <!-- PENDING:END -->

  In pending.html: the entire file is regenerated from a template inside this
  script, no markers needed.

Run after editing pending.yaml:
  python3 scripts/render_pending.py

The script exits non-zero if it can't find the AUTO markers — that's a signal
that a file got hand-edited in the wrong place.
"""
from __future__ import annotations

import datetime
import html
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("error: pyyaml not installed. `pip install pyyaml`")


REPO_ROOT = Path(__file__).resolve().parent.parent
WIKI_ROOT = REPO_ROOT.parent / "wiki"

YAML_PATH = REPO_ROOT / "pending.yaml"
SPEC_PATH = REPO_ROOT / "SPEC.md"
CONTEXT_PATH = WIKI_ROOT / "CLAUDE_CONTEXT.md"
HTML_PATH = REPO_ROOT / "pending.html"

BEGIN_MARK = "<!-- PENDING:BEGIN -->"
END_MARK = "<!-- PENDING:END -->"

# Sort order: priority asc, status order, then created asc.
STATUS_ORDER = {
    "in-progress": 0,
    "shipped-pending-config": 1,
    "pending": 2,
    "blocked": 3,
    "parked": 4,
    "done": 5,
}

STATUS_LABEL = {
    "in-progress":             ("In progress", "ip"),
    "shipped-pending-config":  ("Shipped — awaits config", "sp"),
    "pending":                 ("Pending", "pe"),
    "blocked":                 ("Blocked", "bl"),
    "parked":                  ("Parked", "pa"),
    "done":                    ("Done", "do"),
}


def load_items():
    if not YAML_PATH.exists():
        sys.exit(f"error: {YAML_PATH} not found")
    data = yaml.safe_load(YAML_PATH.read_text())
    items = data.get("items") or []
    for it in items:
        for key in ("id", "title", "status", "priority", "created", "detail"):
            if key not in it:
                sys.exit(f"error: item missing `{key}`: {it.get('id') or it}")
        if it["status"] not in STATUS_ORDER:
            sys.exit(f"error: item {it['id']} has unknown status `{it['status']}`")
    items.sort(key=lambda x: (x["priority"], STATUS_ORDER[x["status"]], str(x["created"])))
    return items, data.get("updated_at", datetime.date.today().isoformat())


def render_md_table(items):
    """Markdown table for SPEC §17 and CLAUDE_CONTEXT §8."""
    lines = [
        "| ID | Title | Status | Priority | Blocker / detail |",
        "|---|---|---|---|---|",
    ]
    for it in items:
        status_label = STATUS_LABEL[it["status"]][0]
        detail = (it.get("blocker") or "").strip()
        if not detail:
            # First non-blank paragraph from detail, single line.
            detail = (it["detail"] or "").strip().split("\n\n")[0].replace("\n", " ")
        # Cell-safe: collapse pipes.
        detail = detail.replace("|", "\\|")
        title = it["title"].replace("|", "\\|")
        lines.append(
            f"| `{it['id']}` | {title} | {status_label} | P{it['priority']} | {detail} |"
        )
    return "\n".join(lines)


def splice(path: Path, body: str, section_intro: str | None = None) -> bool:
    """Replace the content between BEGIN_MARK and END_MARK in `path` with `body`.
    Inserts `section_intro` (plain markdown text) between the BEGIN marker and
    the body if supplied — useful for the "Source of truth" line."""
    if not path.exists():
        print(f"  · {path} missing — skipping", flush=True)
        return False
    text = path.read_text()
    pattern = re.compile(
        re.escape(BEGIN_MARK) + r".*?" + re.escape(END_MARK),
        re.DOTALL,
    )
    if not pattern.search(text):
        sys.exit(
            f"error: {path} has no {BEGIN_MARK}…{END_MARK} block. "
            f"Add the markers around the section you want this script to manage."
        )
    intro = f"\n\n{section_intro}\n\n" if section_intro else "\n\n"
    new_block = f"{BEGIN_MARK}{intro}{body}\n\n{END_MARK}"
    new_text = pattern.sub(new_block, text)
    if new_text == text:
        return False
    path.write_text(new_text)
    return True


def render_html(items, updated_at):
    """Full /pending.html page. Same nav + look as the rest of the site."""
    rows = []
    for it in items:
        status_label, status_cls = STATUS_LABEL[it["status"]]
        tags = " ".join(f'<span class="pl-tag">{html.escape(t)}</span>' for t in (it.get("tags") or []))
        blocker_html = ""
        if it.get("blocker"):
            blocker_html = f'<div class="pl-blocker"><b>Blocked by:</b> {html.escape(it["blocker"])}</div>'
        # detail is markdown-light; we render newlines as <br> and escape angle-brackets.
        detail = html.escape(it["detail"].strip()).replace("\n", "<br>")
        rows.append(f"""
      <article class="pl-card pl-status-{status_cls}" id="{html.escape(it['id'])}">
        <header class="pl-head">
          <div class="pl-title">
            <span class="pl-id">#{html.escape(it['id'])}</span>
            <h2>{html.escape(it['title'])}</h2>
          </div>
          <div class="pl-meta">
            <span class="pl-pri pl-pri-{it['priority']}">P{it['priority']}</span>
            <span class="pl-status pl-status-{status_cls}-pill">{html.escape(status_label)}</span>
            <span class="pl-created">created {html.escape(str(it['created']))}</span>
          </div>
        </header>
        {blocker_html}
        <div class="pl-detail">{detail}</div>
        <footer class="pl-foot">{tags}</footer>
      </article>""")

    # Counts per status, for the heading band.
    counts = {}
    for it in items:
        counts[it["status"]] = counts.get(it["status"], 0) + 1

    band = []
    for status in STATUS_ORDER:
        if status not in counts:
            continue
        label, cls = STATUS_LABEL[status]
        band.append(f'<span class="pl-band-cell pl-status-{cls}"><b>{counts[status]}</b> {html.escape(label)}</span>')

    body = "\n".join(rows)
    band_html = "".join(band)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BOOK — Pending</title>
<link rel="icon" href="site-icon.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&family=Newsreader:ital,wght@0,400;0,500;0,600;0,700;1,400;1,500;1,600&display=swap">
<link rel="stylesheet" href="style.css?v={int(datetime.datetime.utcnow().timestamp())}">
<link rel="stylesheet" href="quiet-tokens.css?v={int(datetime.datetime.utcnow().timestamp())}">
<link rel="stylesheet" href="quiet.css?v={int(datetime.datetime.utcnow().timestamp())}">
<link rel="stylesheet" href="quiet-extras.css?v={int(datetime.datetime.utcnow().timestamp())}">
<link rel="stylesheet" href="quiet-legacy.css?v={int(datetime.datetime.utcnow().timestamp())}">
<style>
  .pl-page {{ max-width: 920px; margin: 0 auto; padding: 22px 16px 60px; font-family: var(--font-display, 'Newsreader', serif); }}
  .pl-page h1 {{ font: 600 30px/1.2 var(--font-display, 'Newsreader', serif); margin: 0 0 6px; color: var(--ink-900); }}
  .pl-sub {{ font: italic 400 14px/1.4 var(--font-display, 'Newsreader', serif); color: var(--ink-500); margin: 0 0 22px; }}
  .pl-band {{ display: flex; flex-wrap: wrap; gap: 10px; margin: 0 0 28px; }}
  .pl-band-cell {{ padding: 6px 12px; border: 1px solid var(--ink-300); border-radius: 2px; font: 500 13px/1 var(--font-display, 'Newsreader', serif); color: var(--ink-700); }}
  .pl-band-cell b {{ font-weight: 700; color: var(--ink-900); margin-right: 4px; }}
  .pl-card {{ padding: 16px 18px; margin: 0 0 16px; background: var(--paper-100, #F6EFD9); border-left: 3px solid var(--ink-300); }}
  .pl-card.pl-status-ip {{ border-left-color: var(--brass-500, #B8923F); }}
  .pl-card.pl-status-sp {{ border-left-color: #6FA94A; }}
  .pl-card.pl-status-pe {{ border-left-color: var(--ink-500); }}
  .pl-card.pl-status-bl {{ border-left-color: var(--red-500, #b14040); }}
  .pl-card.pl-status-pa {{ border-left-color: var(--ink-300); opacity: 0.85; }}
  .pl-head {{ display: flex; justify-content: space-between; align-items: baseline; gap: 14px; flex-wrap: wrap; margin: 0 0 8px; }}
  .pl-title h2 {{ font: 600 18px/1.3 var(--font-display, 'Newsreader', serif); margin: 0; color: var(--ink-900); display: inline; }}
  .pl-id {{ font: 500 11px/1 ui-monospace, SFMono-Regular, Menlo, monospace; color: var(--brass-500, #B8923F); margin-right: 8px; }}
  .pl-meta {{ display: flex; gap: 10px; align-items: center; flex-wrap: wrap; font: 500 11px/1 var(--font-display, 'Newsreader', serif); color: var(--ink-500); }}
  .pl-pri {{ padding: 2px 6px; border: 1px solid var(--ink-300); border-radius: 2px; color: var(--ink-700); }}
  .pl-pri-1 {{ background: var(--red-500, #b14040); color: #fff; border-color: var(--red-500); }}
  .pl-pri-2 {{ background: var(--brass-300, #E2BF74); border-color: var(--brass-500, #B8923F); }}
  .pl-status {{ padding: 2px 6px; border-radius: 2px; color: var(--ink-900); }}
  .pl-status-ip-pill {{ background: var(--brass-300, #E2BF74); }}
  .pl-status-sp-pill {{ background: #C2E0AE; }}
  .pl-status-pe-pill {{ background: var(--paper-200, #ECDFB6); }}
  .pl-status-bl-pill {{ background: #F3BABA; }}
  .pl-status-pa-pill {{ background: #E0DCD0; color: var(--ink-500); }}
  .pl-blocker {{ font: 400 13px/1.4 var(--font-display, 'Newsreader', serif); color: var(--red-500, #b14040); margin: 8px 0; }}
  .pl-detail {{ font: 400 14px/1.55 var(--font-display, 'Newsreader', serif); color: var(--ink-700); margin: 8px 0 0; }}
  .pl-foot {{ display: flex; gap: 6px; flex-wrap: wrap; margin: 10px 0 0; }}
  .pl-tag {{ font: 500 10px/1 ui-monospace, SFMono-Regular, Menlo, monospace; padding: 3px 7px; background: var(--paper-200, #ECDFB6); color: var(--ink-700); border-radius: 2px; text-transform: uppercase; letter-spacing: 0.04em; }}
  .pl-stale {{ font: italic 400 12px/1.4 var(--font-display, 'Newsreader', serif); color: var(--ink-500); margin: 28px 0 0; padding: 12px; background: var(--paper-100, #F6EFD9); border-left: 2px dashed var(--ink-300); }}
</style>
</head>
<body>
<header class="qb-topbar">
  <a class="qb-brand" href="/">
    <img class="qb-brand-logo" src="togetherbook-logo.png?v={int(datetime.datetime.utcnow().timestamp())}" alt="TogetherBOOK">
  </a>
  <nav class="qb-nav">
    <a class="qb-nav-link" href="/wall.html">Wall</a>
    <a class="qb-nav-link" href="/directory.html" data-sub='[{{"href": "/directory.html", "label": "Directory"}}, {{"href": "/directory.html", "label": "Your Page", "data-yourpage": "1"}}, {{"href": "/holidays.html", "label": "Holidays"}}, {{"href": "/org-structure.html", "label": "Org Structure"}}]'>People</a>
    <a class="qb-nav-link" href="/reports.html" data-sub='[{{"href": "/yesterday.html", "label": "Payouts"}}, {{"href": "/brandwatch.html", "label": "Brandwatch"}}, {{"href": "/1stcontact.html", "label": "1st Contact"}}, {{"href": "/topups.html", "label": "Top Ups"}}, {{"href": "/pipeline.html", "label": "Pipeline"}}, {{"href": "/brokers.html", "label": "Brokers"}}, {{"href": "/comms.html", "label": "Comms"}}]'>Business</a>
    <a class="qb-nav-link is-active" href="/index.html" data-sub='[{{"href": "/database.html", "label": "Schema"}}, {{"href": "/stats.html", "label": "Code"}}, {{"href": "/pending.html", "label": "Pending"}}]'>System</a>
  </nav>
</header>

<main class="pl-page">
  <h1>Pending work</h1>
  <p class="pl-sub">Single source of truth. Edit <code>pending.yaml</code> then run <code>python3 scripts/render_pending.py</code> to regenerate this page, SPEC §17, and the wiki's CLAUDE_CONTEXT §8 in lockstep. Last rendered <b>{html.escape(updated_at)}</b>.</p>
  <div class="pl-band">{band_html}</div>
{body}
  <p class="pl-stale">If you spot a stale entry, change it in <code>pending.yaml</code>, don't hand-edit this page or the SPEC / CLAUDE_CONTEXT blocks — the next render clobbers hand edits.</p>
</main>

<script src="nav.js?v={int(datetime.datetime.utcnow().timestamp())}" defer></script>
</body>
</html>
"""


def main():
    items, updated_at = load_items()
    md_body = render_md_table(items)

    spec_intro = (
        "_Generated from `pending.yaml`. Don't hand-edit this block — change the YAML and run "
        "`python3 scripts/render_pending.py`. Full detail at `/pending.html` on the site._"
    )
    context_intro = (
        "_Generated from `togetherbook/pending.yaml`. Authoritative list — the SPEC §17 table "
        "and `/pending.html` on the site are rendered from the same source._"
    )

    spec_changed    = splice(SPEC_PATH,    md_body, spec_intro)
    context_changed = splice(CONTEXT_PATH, md_body, context_intro)
    html_text = render_html(items, str(updated_at))
    html_changed = False
    if not HTML_PATH.exists() or HTML_PATH.read_text() != html_text:
        HTML_PATH.write_text(html_text)
        html_changed = True

    print(f"items: {len(items)}", flush=True)
    print(f"  SPEC.md          {'updated' if spec_changed    else 'unchanged'}", flush=True)
    print(f"  CLAUDE_CONTEXT.md {'updated' if context_changed else 'unchanged'}", flush=True)
    print(f"  pending.html     {'updated' if html_changed    else 'unchanged'}", flush=True)


if __name__ == "__main__":
    main()
