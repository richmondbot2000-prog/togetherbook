"""
Merge Google Workspace Reports API activity (login, gmail, drive, meet,
chat, calendar, admin) into staff-activity-buckets.json.

For each `applications/<name>` audit log we walk the last 14 days,
extract (actor.email, id.time), bucket the time into a 15-min slot,
and merge into the per-user records the warehouse scanner already
produced.

Output side-effect: the existing `staff-activity-buckets.json` is
read, mutated, and written back. Each Google event becomes:
- a bit set in `by_user[email].buckets[date]`
- an entry in `by_user[email].events[date]` with shape
    { src: "google.<app>", kind: "google", bucket, first_at, last_at, writes: <count> }
  Multiple events in the same (user, date, bucket, app) are folded
  into a single entry (writes++, last_at = latest).

Required env vars (same as scan_directory.py):
  WORKSPACE_SERVICE_ACCOUNT_JSON  — full SA key JSON (string)
  WORKSPACE_DELEGATE_USER         — super-admin email to impersonate
                                    (any one tenant is enough — the
                                    Reports API spans the whole
                                    customer regardless)

DWD scope required (not currently granted; ask the user to add it):
  https://www.googleapis.com/auth/admin.reports.audit.readonly

If the scope is missing the script logs a warning and exits 0 so it
doesn't break the workflow.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except ImportError:
    print("# google-api-python-client not installed — skipping", flush=True)
    sys.exit(0)


SCOPES = ["https://www.googleapis.com/auth/admin.reports.audit.readonly"]

# Application names that the Reports API exposes. Order matters only
# for the log readability — each scan is independent.
APPLICATIONS = [
    "login",
    "gmail",
    "drive",
    "meet",
    "chat",
    "calendar",
    "admin",
]

PAGE_SIZE = 1000  # API max


def env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        sys.exit(f"error: {name} not set")
    return v


def build_service():
    raw = env("WORKSPACE_SERVICE_ACCOUNT_JSON")
    try:
        key_info = json.loads(raw)
    except json.JSONDecodeError as e:
        sys.exit(f"WORKSPACE_SERVICE_ACCOUNT_JSON not valid JSON: {e}")
    delegate = env("WORKSPACE_DELEGATE_USER")
    creds = service_account.Credentials.from_service_account_info(
        key_info, scopes=SCOPES, subject=delegate)
    return build("admin", "reports_v1", credentials=creds, cache_discovery=False)


def iso_to_bucket(t: str) -> tuple[str, int] | None:
    """Convert an RFC3339 timestamp from the API ('2026-05-15T08:32:14.123Z')
    into ('2026-05-15', bucket_index_0_to_95). Returns None if unparseable."""
    if not t: return None
    try:
        if t.endswith("Z"):
            d = datetime.datetime.fromisoformat(t[:-1] + "+00:00")
        else:
            d = datetime.datetime.fromisoformat(t)
        d = d.astimezone(datetime.timezone.utc)
        bucket = d.hour * 4 + d.minute // 15
        return (d.date().isoformat(), bucket)
    except Exception:
        return None


def pull_app(service, app: str, start: datetime.datetime, end: datetime.datetime):
    """Walk every event for `app` in [start, end). Yields tuples of
    (actor_email_lower, date_iso, bucket_idx, event_time_iso)."""
    print(f"# applications/{app}", flush=True)
    start_str = start.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end_str   = end.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    page_token = None
    seen = 0
    while True:
        try:
            resp = service.activities().list(
                userKey="all",
                applicationName=app,
                startTime=start_str,
                endTime=end_str,
                maxResults=PAGE_SIZE,
                pageToken=page_token,
            ).execute()
        except HttpError as e:
            status = getattr(e, "status_code", None) or (e.resp.status if hasattr(e, "resp") else None)
            if status in (400, 403):
                print(f"  ! {app}: HTTP {status} — scope likely not granted; skipping app", flush=True)
                return
            print(f"  ! {app}: HTTP {status} — {e}", flush=True)
            return
        except Exception as e:
            print(f"  ! {app}: {e}", flush=True)
            return

        items = resp.get("items") or []
        for it in items:
            actor = (it.get("actor") or {})
            email = (actor.get("email") or "").lower()
            if not email or "@" not in email:
                continue
            t = ((it.get("id") or {}).get("time") or "")
            parsed = iso_to_bucket(t)
            if not parsed: continue
            iso, bucket = parsed
            yield (email, iso, bucket, t)
            seen += 1
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    print(f"  - {seen} events", flush=True)


def parse_date(s: str | None):
    if not s: return None
    try:
        return datetime.datetime.strptime(s.strip(), "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
    except Exception:
        sys.exit(f"error: bad date '{s}' — expected YYYY-MM-DD")


def main():
    started = datetime.datetime.now(datetime.timezone.utc)
    today = started.date()
    yesterday = today - datetime.timedelta(days=1)
    start = parse_date(os.environ.get("ACTIVITY_START_DATE")) or datetime.datetime(
        yesterday.year, yesterday.month, yesterday.day, tzinfo=datetime.timezone.utc)
    end_inclusive = parse_date(os.environ.get("ACTIVITY_END_DATE")) or datetime.datetime(
        yesterday.year, yesterday.month, yesterday.day, tzinfo=datetime.timezone.utc)
    end_exclusive = end_inclusive + datetime.timedelta(days=1)

    print(f"# scan_google_workspace_activity start {started.isoformat()}", flush=True)
    print(f"# range: {start.date().isoformat()} → {end_inclusive.date().isoformat()} (inclusive)", flush=True)

    out_path = Path("staff-activity-buckets.json").resolve()
    if not out_path.exists():
        print("# staff-activity-buckets.json missing — run scan_staff_activity_buckets.py first", flush=True)
        sys.exit(0)
    doc = json.loads(out_path.read_text())
    by_user = doc.setdefault("by_email", {})

    # ISO dates inside the requested window.
    window_set = set()
    d = start.date()
    while d <= end_inclusive.date():
        window_set.add(d.isoformat())
        d = d + datetime.timedelta(days=1)

    try:
        service = build_service()
    except Exception as e:
        print(f"# could not build reports service: {e}", flush=True)
        sys.exit(0)

    # First — drop existing google.* events in the requested window
    # from every existing user, so a forced refresh is idempotent
    # without nuking warehouse rows on the same days.
    for email, rec in by_user.items():
        evs_by_date = rec.get("events") or {}
        for iso in list(evs_by_date.keys()):
            if iso not in window_set: continue
            kept = [e for e in evs_by_date[iso] if not (e.get("src", "").startswith("google."))]
            if kept:
                evs_by_date[iso] = kept
            else:
                del evs_by_date[iso]

    # event_agg keyed by (email, iso, bucket, app) → {writes, first_at, last_at}
    event_agg: dict[tuple[str, str, int, str], dict] = {}

    for app in APPLICATIONS:
        for email, iso, bucket, t in pull_app(service, app, start, end_exclusive):
            if iso not in window_set: continue
            k = (email, iso, bucket, app)
            cur = event_agg.get(k)
            if cur is None:
                event_agg[k] = {"writes": 1, "first_at": t, "last_at": t}
            else:
                cur["writes"] += 1
                if t < cur["first_at"]: cur["first_at"] = t
                if t > cur["last_at"]:  cur["last_at"]  = t

    added_events = 0
    added_buckets = 0
    for (email, iso, bucket, app), v in event_agg.items():
        rec = by_user.setdefault(email, {"buckets": {}, "events": {}})
        rec.setdefault("buckets", {})
        rec.setdefault("events", {})
        b = set(rec["buckets"].get(iso, []))
        if bucket not in b:
            b.add(bucket)
            rec["buckets"][iso] = sorted(b)
            added_buckets += 1
        evs = rec["events"].setdefault(iso, [])
        evs.append({
            "src": f"google.{app}",
            "bucket": bucket,
            "writes": v["writes"],
            "first_at": v["first_at"],
            "last_at":  v["last_at"],
            "kind": "google",
        })
        added_events += 1

    for rec in by_user.values():
        for iso in (rec.get("events") or {}):
            rec["events"][iso].sort(key=lambda e: (e.get("bucket") or 0, e.get("src") or ""))

    doc["google_workspace"] = {
        "last_pull_at": started.isoformat(),
        "applications": APPLICATIONS,
    }
    doc["active_count"] = len(by_user)
    pulled = doc.setdefault("pulled", {})
    # Tag each in-window date as having been pulled — the bucket
    # scanner already marks the warehouse side; we annotate google too
    # by leaving the existing pulled[iso] timestamp alone if it exists,
    # else writing the merge time.
    for iso in window_set:
        pulled.setdefault(iso, started.isoformat())
    out_path.write_text(json.dumps(doc, indent=2))
    print(f"# merged {added_events} Workspace event groups (+{added_buckets} new 15-min buckets)", flush=True)
    print(f"# wrote {out_path}", flush=True)


if __name__ == "__main__":
    main()
