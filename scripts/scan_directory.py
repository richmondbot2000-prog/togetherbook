"""
Generate staff.json — the Google Workspace user directory for letme.co.uk.

Used by directory.html on the BOOK site (gated behind Cloudflare Access
+ @letme.co.uk login, so we can show internal-staff data here).

Authentication: a Google Cloud service account with domain-wide delegation
authorised in Workspace admin. The service account impersonates a super
admin to call the Admin SDK Directory API users.list endpoint.

Required env vars:
  WORKSPACE_SERVICE_ACCOUNT_JSON  — full service-account key JSON (string)
  WORKSPACE_DELEGATE_USER         — super-admin email to impersonate
  WORKSPACE_DOMAIN                — letme.co.uk (default if unset)
  OUT                             — output path (default: staff.json)
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from collections import Counter
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build


SCOPES = ['https://www.googleapis.com/auth/admin.directory.user.readonly']
PAGE_SIZE = 500   # API max
DOMAIN_DEFAULT = 'letme.co.uk'


def env(name: str, default: str | None = None) -> str:
    v = os.environ.get(name) or default
    if not v:
        sys.exit(f"error: {name} not set")
    return v


def build_service():
    raw = env('WORKSPACE_SERVICE_ACCOUNT_JSON')
    try:
        key_info = json.loads(raw)
    except json.JSONDecodeError as e:
        sys.exit(f"error: WORKSPACE_SERVICE_ACCOUNT_JSON is not valid JSON: {e}")

    delegate = env('WORKSPACE_DELEGATE_USER')
    creds = service_account.Credentials.from_service_account_info(
        key_info, scopes=SCOPES, subject=delegate)
    return build('admin', 'directory_v1', credentials=creds, cache_discovery=False)


def fetch_users(service, domain: str) -> list[dict]:
    """Page through users.list — Workspace caps at 500/page, we follow nextPageToken.

    Uses `customer='my_customer'` rather than `domain=` so we get every active
    user across all the tenant's domains (letme.co.uk + any aliases). The admin
    console's "59 active users" count is across the whole customer.
    """
    out: list[dict] = []
    page_token: str | None = None
    while True:
        kwargs = {
            'customer': 'my_customer',
            'maxResults': PAGE_SIZE,
            'orderBy': 'email',
            'projection': 'full',   # need 'full' for organizations + photos
            'viewType': 'admin_view',
        }
        if page_token:
            kwargs['pageToken'] = page_token
        resp = service.users().list(**kwargs).execute()
        out.extend(resp.get('users', []))
        page_token = resp.get('nextPageToken')
        if not page_token:
            break
    return out


def normalize(u: dict) -> dict:
    """Pull just the fields we'll display on the page; drop the noisy rest."""
    name = u.get('name') or {}
    orgs = (u.get('organizations') or [{}])
    primary_org = next((o for o in orgs if o.get('primary')), orgs[0]) if orgs else {}
    return {
        'email':       u.get('primaryEmail') or '',
        'name':        (name.get('fullName') or '').strip(),
        'given':       (name.get('givenName') or '').strip(),
        'family':      (name.get('familyName') or '').strip(),
        'title':       (primary_org.get('title') or '').strip(),
        'department':  (primary_org.get('department') or '').strip(),
        'photo_url':   u.get('thumbnailPhotoUrl') or '',
        'suspended':   bool(u.get('suspended')),
        'admin':       bool(u.get('isAdmin')),
    }


def main() -> None:
    started = datetime.datetime.utcnow()
    print(f"# scan_directory start ({started.isoformat()}Z)", flush=True)

    domain = env('WORKSPACE_DOMAIN', DOMAIN_DEFAULT)
    service = build_service()
    raw_users = fetch_users(service, domain)
    print(f"# fetched {len(raw_users)} users from {domain}", flush=True)

    # Drop suspended accounts (left employees, dormant test accounts) — they
    # shouldn't appear in a "who's who" directory.
    users = [normalize(u) for u in raw_users]
    active = [u for u in users if not u['suspended']]
    suspended_count = len(users) - len(active)

    # Sort by family name, then given name. Falls back to email if names empty.
    active.sort(key=lambda u: (u['family'].lower(), u['given'].lower(), u['email'].lower()))

    # Department breakdown for the front-end filter pills.
    by_department: dict[str, int] = dict(Counter(
        u['department'] or '(unspecified)' for u in active
    ))

    payload = {
        'schema_version': 1,
        'updated_at':     started.isoformat() + 'Z',
        'snapshot_date':  started.date().isoformat(),
        'domain':         domain,
        'totals': {
            'users':            len(active),
            'suspended_hidden': suspended_count,
            'by_department':    dict(sorted(by_department.items(), key=lambda kv: -kv[1])),
        },
        'users': active,
    }

    out_path = Path(os.environ.get('OUT', 'staff.json')).resolve()
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(
        f"# wrote {out_path} — {len(active)} active users "
        f"({suspended_count} suspended hidden, {len(by_department)} departments)",
        flush=True,
    )


if __name__ == '__main__':
    main()
