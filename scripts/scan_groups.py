"""
Generate groups.json — the Google Workspace groups directory + per-group
member lists. Companion to scan_directory.py (which does users).

Authentication is identical: the same service account, impersonating the same
delegate. Scopes are read-only Group + Group-member, kept narrow so this
scanner can never accidentally write.

Required env vars:
  WORKSPACE_SERVICE_ACCOUNT_JSON  — full service-account key JSON (string)
  WORKSPACE_TENANTS               — same shape as scan_directory.py
                                     (or the legacy WORKSPACE_DELEGATE_USER /
                                     WORKSPACE_DOMAIN fallback)

Optional:
  OUT — output path (default: groups.json)

Output schema:
  {
    "schema_version": 1,
    "updated_at": "<ISO>",
    "totals": { "groups": N, "members": N },
    "tenants": [ "<tenant_name>", ... ],
    "groups": [
      {
        "email":        "finance@rgroup.co.uk",
        "name":         "Finance",
        "description":  "...",
        "tenant":       "letme.co.uk",
        "aliases":      ["finance@letme.com", ...],
        "member_count": 7,
        "members": [
          { "email": "...", "role": "MEMBER" | "MANAGER" | "OWNER",
            "type": "USER" | "GROUP" | "EXTERNAL",
            "status": "ACTIVE" | "SUSPENDED" | ... }
        ]
      },
      ...
    ]
  }
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build


SCOPES = [
    'https://www.googleapis.com/auth/admin.directory.group.readonly',
    'https://www.googleapis.com/auth/admin.directory.group.member.readonly',
]
PAGE_SIZE = 200


def env(name: str, default: str | None = None) -> str:
    v = os.environ.get(name) or default
    if not v:
        sys.exit(f"error: {name} not set")
    return v


def load_key_info() -> dict:
    raw = env('WORKSPACE_SERVICE_ACCOUNT_JSON')
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        sys.exit(f"error: WORKSPACE_SERVICE_ACCOUNT_JSON is not valid JSON: {e}")


def build_service(key_info: dict, delegate: str):
    creds = service_account.Credentials.from_service_account_info(
        key_info, scopes=SCOPES, subject=delegate)
    return build('admin', 'directory_v1', credentials=creds, cache_discovery=False)


def fetch_groups(service) -> list[dict]:
    """Page through groups.list using customer='my_customer'."""
    out: list[dict] = []
    page_token: str | None = None
    while True:
        kwargs = {
            'customer': 'my_customer',
            'maxResults': PAGE_SIZE,
            'orderBy': 'email',
        }
        if page_token:
            kwargs['pageToken'] = page_token
        resp = service.groups().list(**kwargs).execute()
        out.extend(resp.get('groups', []))
        page_token = resp.get('nextPageToken')
        if not page_token:
            break
    return out


def fetch_members(service, group_email: str) -> list[dict]:
    out: list[dict] = []
    page_token: str | None = None
    while True:
        kwargs = {
            'groupKey': group_email,
            'maxResults': PAGE_SIZE,
            'includeDerivedMembership': False,
        }
        if page_token:
            kwargs['pageToken'] = page_token
        resp = service.members().list(**kwargs).execute()
        out.extend(resp.get('members', []))
        page_token = resp.get('nextPageToken')
        if not page_token:
            break
    return out


def normalize_group(g: dict, members: list[dict], tenant: str) -> dict:
    norm_members = []
    for m in members:
        email = (m.get('email') or '').strip().lower()
        if not email:
            continue
        norm_members.append({
            'email':  email,
            'role':   m.get('role') or 'MEMBER',
            'type':   m.get('type') or 'USER',
            'status': m.get('status') or '',
        })
    aliases = sorted({
        a for a in (g.get('aliases') or []) + (g.get('nonEditableAliases') or [])
        if a and not a.endswith('.test-google-a.com')
    })
    return {
        'email':        (g.get('email') or '').lower(),
        'name':         (g.get('name') or '').strip(),
        'description':  (g.get('description') or '').strip(),
        'tenant':       tenant,
        'aliases':      aliases,
        'member_count': len(norm_members),
        'members':      norm_members,
    }


def load_tenants() -> list[dict]:
    raw = os.environ.get('WORKSPACE_TENANTS')
    if raw:
        try:
            tenants = json.loads(raw)
        except json.JSONDecodeError as e:
            sys.exit(f"error: WORKSPACE_TENANTS is not valid JSON: {e}")
        if not isinstance(tenants, list) or not tenants:
            sys.exit("error: WORKSPACE_TENANTS must be a non-empty JSON array")
        for t in tenants:
            for k in ('name', 'delegate'):
                if k not in t:
                    sys.exit(f"error: WORKSPACE_TENANTS entry missing '{k}'")
        return tenants
    delegate = os.environ.get('WORKSPACE_DELEGATE_USER')
    if not delegate:
        sys.exit("error: neither WORKSPACE_TENANTS nor WORKSPACE_DELEGATE_USER is set")
    name = os.environ.get('WORKSPACE_DOMAIN') or 'letme.co.uk'
    return [{'name': name, 'delegate': delegate}]


def main() -> None:
    key_info = load_key_info()
    tenants = load_tenants()
    all_groups: list[dict] = []
    fetch_errors: list[str] = []
    for t in tenants:
        try:
            svc = build_service(key_info, t['delegate'])
            raw_groups = fetch_groups(svc)
            for g in raw_groups:
                group_email = g.get('email')
                if not group_email:
                    continue
                try:
                    members = fetch_members(svc, group_email)
                except Exception as e:
                    fetch_errors.append(f"members of {group_email}: {e}")
                    members = []
                all_groups.append(normalize_group(g, members, t['name']))
        except Exception as e:
            fetch_errors.append(f"tenant {t['name']}: {e}")

    out_path = Path(os.environ.get('OUT') or 'groups.json')
    payload = {
        'schema_version': 1,
        'updated_at': datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
        'totals': {
            'groups':  len(all_groups),
            'members': sum(g['member_count'] for g in all_groups),
        },
        'tenants':       [t['name'] for t in tenants],
        'fetch_errors':  fetch_errors,
        'groups':        all_groups,
    }
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print(f"wrote {len(all_groups)} groups ({payload['totals']['members']} members) to {out_path}", file=sys.stderr)


if __name__ == '__main__':
    main()
