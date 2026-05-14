"""
Background job that finishes the Delete + Drive + Mail transfer flow
started from the Directory page.

The Worker writes one entry per queued transfer to `pending-transfers.json`:

    {
      "source_email": "...",
      "target_email": "...",
      "drive_transfer_id": "...",
      "queued_at": "<ISO>",
      "stage": "queued",
      "tenant": "letme" | "togetherloans"
    }

Drive transfer itself is async on Google's side — the Worker already kicked
it off via Admin SDK Data Transfer when the entry was written. This script:

  1. For each entry where stage != "done":
       a. (stage 'queued' -> 'migrating-mail') Walk Gmail messages on the
          source mailbox and INSERT each one into the target mailbox via
          gmail.users.messages.insert. internalDateSource='dateHeader' so
          original timestamps are preserved.
       b. (stage 'migrating-mail' -> 'deleting') Call the Worker's
          delete-account action to remove the source user. The 20-day
          recovery window starts; Drive transfer continues in the background.
       c. (stage 'deleting' -> done) Remove the entry from
          pending-transfers.json.
  2. On any error: update the entry with stage='error' + last_error so the
     page surfaces it and an admin can inspect.

The Worker uses GitHub Contents API to write pending-transfers.json; this
script uses a regular local git commit (already inside the workflow
checkout). They never write the file at the same time because the workflow
runs serially and the page only reads.

Required env:
  WORKSPACE_SERVICE_ACCOUNT_JSON  — DWD-enabled SA JSON for letme
  WORKSPACE_DELEGATE_USER         — letme super-admin email
  WORKSPACE_SERVICE_ACCOUNT_JSON_TOGETHERLOANS (optional) — togetherloans SA
  WORKSPACE_DELEGATE_USER_TOGETHERLOANS         (optional) — togetherloans super-admin

DWD scopes needed in admin.google.com → API controls:
  https://www.googleapis.com/auth/gmail.readonly
  https://www.googleapis.com/auth/gmail.insert
  https://www.googleapis.com/auth/admin.directory.user

(The script DELETEs the source user directly via the Admin SDK rather than
calling back to the Cloudflare-Access-gated Worker — same DWD-impersonated
admin token as everything else, no service-token plumbing required.)
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import time
from pathlib import Path
from typing import Iterable

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


PENDING_PATH = Path("pending-transfers.json")
SCOPES_GMAIL_READ   = ["https://www.googleapis.com/auth/gmail.readonly"]
SCOPES_GMAIL_INSERT = ["https://www.googleapis.com/auth/gmail.insert"]
SCOPES_ADMIN_USER   = ["https://www.googleapis.com/auth/admin.directory.user"]


def env_opt(name: str) -> str:
    return os.environ.get(name) or ""


def env_req(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        sys.exit(f"error: {name} not set")
    return v


def sa_info_for(tenant: str) -> tuple[dict, str]:
    """Return (sa_json, delegate_email) for the tenant ('letme' default,
    'togetherloans' if env vars exist)."""
    if tenant == "togetherloans":
        sa = env_opt("WORKSPACE_SERVICE_ACCOUNT_JSON_TOGETHERLOANS") or env_opt("WORKSPACE_SERVICE_ACCOUNT_JSON")
        delegate = env_opt("WORKSPACE_DELEGATE_USER_TOGETHERLOANS") or env_opt("WORKSPACE_DELEGATE_USER")
    else:
        sa = env_opt("WORKSPACE_SERVICE_ACCOUNT_JSON")
        delegate = env_opt("WORKSPACE_DELEGATE_USER")
    if not sa or not delegate:
        sys.exit(f"error: SA JSON or delegate user missing for tenant={tenant!r}")
    return json.loads(sa), delegate


def gmail_service_as_user(tenant: str, user_email: str, scopes: list[str]):
    """Build a Gmail service impersonating `user_email` via DWD."""
    sa_info, _ = sa_info_for(tenant)
    creds = service_account.Credentials.from_service_account_info(
        sa_info, scopes=scopes, subject=user_email,
    )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def admin_service(tenant: str):
    """Build the Admin Directory service impersonating the tenant's
    super-admin — required for users.delete()."""
    sa_info, delegate = sa_info_for(tenant)
    creds = service_account.Credentials.from_service_account_info(
        sa_info, scopes=SCOPES_ADMIN_USER, subject=delegate,
    )
    return build("admin", "directory_v1", credentials=creds, cache_discovery=False)


def iter_messages(svc, user_email: str) -> Iterable[str]:
    """Yield every message id in the user's mailbox (all labels, including
    spam + trash so we don't silently drop anything)."""
    page_token = None
    while True:
        resp = svc.users().messages().list(
            userId=user_email,
            includeSpamTrash=True,
            maxResults=500,
            pageToken=page_token,
        ).execute()
        for m in (resp.get("messages") or []):
            yield m["id"]
        page_token = resp.get("nextPageToken")
        if not page_token:
            break


def migrate_mailbox(tenant: str, source_email: str, target_email: str) -> tuple[int, int]:
    """Copy every Gmail message from source_email's mailbox into
    target_email's mailbox. Returns (inserted, errors)."""
    src_svc = gmail_service_as_user(tenant, source_email, SCOPES_GMAIL_READ)
    tgt_svc = gmail_service_as_user(tenant, target_email, SCOPES_GMAIL_INSERT)

    inserted = 0
    errors = 0
    for msg_id in iter_messages(src_svc, source_email):
        try:
            raw = src_svc.users().messages().get(
                userId=source_email, id=msg_id, format="raw",
            ).execute()
            payload = {
                "raw": raw["raw"],   # already URL-safe-base64-encoded by the API
                "labelIds": raw.get("labelIds") or [],
            }
            tgt_svc.users().messages().insert(
                userId=target_email,
                internalDateSource="dateHeader",
                body=payload,
            ).execute()
            inserted += 1
            if inserted % 200 == 0:
                print(f"  …inserted {inserted} so far", flush=True)
            # Mild per-user rate-limit cushion (~250 quota units/user/sec;
            # insert is 25 units, so 10 msg/sec is the soft ceiling).
            time.sleep(0.05)
        except HttpError as e:
            errors += 1
            print(f"  ! msg {msg_id}: {e}", flush=True)
            if errors > 25:
                print("  ! too many errors — aborting this mailbox", flush=True)
                break
    return inserted, errors


def delete_user(tenant: str, email: str) -> None:
    """Delete the Workspace user directly via Admin SDK. License freed
    immediately; account stays recoverable for 20 days from admin.google.com."""
    svc = admin_service(tenant)
    svc.users().delete(userKey=email).execute()


def save_pending(data: dict) -> None:
    """Write pending-transfers.json with a stable, indented format so the
    GitHub Contents API diff diff stays small + the workflow's `git add`
    commit catches the change."""
    data["updated_at"] = datetime.datetime.utcnow().isoformat() + "Z"
    PENDING_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    if not PENDING_PATH.exists():
        print("pending-transfers.json missing — nothing to do")
        return
    data = json.loads(PENDING_PATH.read_text(encoding="utf-8"))
    entries = data.get("entries") or []
    if not entries:
        print("no pending transfers")
        return

    keep = []
    for entry in entries:
        src = entry.get("source_email") or ""
        tgt = entry.get("target_email") or ""
        tenant = (entry.get("tenant") or "letme").lower()
        stage = entry.get("stage") or "queued"
        if not src or not tgt:
            print(f"  ! skipping malformed entry: {entry!r}")
            keep.append(entry)
            continue

        print(f"--- {src} -> {tgt} (stage={stage}, tenant={tenant})", flush=True)

        try:
            if stage in ("queued", "migrating-mail"):
                entry["stage"] = "migrating-mail"
                save_pending(data)  # surface state change ASAP
                ins, errs = migrate_mailbox(tenant, src, tgt)
                entry["mail_inserted"] = ins
                entry["mail_errors"] = errs
                print(f"  ✓ Gmail migration done: inserted={ins} errors={errs}", flush=True)
                stage = entry["stage"] = "deleting"
                save_pending(data)

            if stage == "deleting":
                print(f"  deleting {src} via Admin SDK…", flush=True)
                delete_user(tenant, src)
                print(f"  ✓ deleted {src}", flush=True)
                stage = entry["stage"] = "done"

            if stage == "done":
                # Drop from kept list.
                print(f"  removing {src} from pending-transfers", flush=True)
                continue
        except Exception as e:
            entry["stage"] = "error"
            entry["last_error"] = (str(e) or "unknown error")[:500]
            entry["errored_at"] = datetime.datetime.utcnow().isoformat() + "Z"
            print(f"  X {src} errored: {e}", flush=True)

        keep.append(entry)

    data["entries"] = keep
    save_pending(data)
    print(f"wrote {PENDING_PATH} — {len(keep)} entries remain")


if __name__ == "__main__":
    main()
