"""
Generate staff.json — the Google Workspace user directory.

Authentication: a Google Cloud service account with domain-wide delegation
authorised in each Workspace tenant we want to read. The service account
impersonates a super-admin per tenant to call the Admin SDK Directory API
users.list endpoint.

This scanner can fetch from one OR many tenants and merges everything into
a single staff.json with a `tenant` tag on every row.

Required env vars (always):
  WORKSPACE_SERVICE_ACCOUNT_JSON  — full service-account key JSON (string)

Tenant config — choose ONE of these forms:

  A) Multi-tenant (preferred):
     WORKSPACE_TENANTS = JSON array of {name, delegate, domain} dicts, e.g.
       [
         {"name":"letme.co.uk","delegate":"admin@letme.co.uk","domain":"letme.co.uk"},
         {"name":"rgroup.co.uk","delegate":"admin@rgroup.co.uk","domain":"rgroup.co.uk"}
       ]

  B) Legacy single-tenant fallback (kept for backward compat):
     WORKSPACE_DELEGATE_USER  — super-admin email to impersonate
     WORKSPACE_DOMAIN         — primary domain of that tenant (default letme.co.uk)

If both are set, WORKSPACE_TENANTS wins.

Optional:
  OUT — output path (default: staff.json)
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


def _fetch_users_one(service, show_deleted: bool) -> list[dict]:
    """One paged users.list call. Pass show_deleted=True to fetch the 20-day
    deleted window; False to fetch active+suspended."""
    out: list[dict] = []
    page_token: str | None = None
    while True:
        kwargs = {
            'customer': 'my_customer',
            'maxResults': PAGE_SIZE,
            'orderBy': 'email',
            'projection': 'full',
            'viewType': 'admin_view',
            'showDeleted': 'true' if show_deleted else 'false',
        }
        if page_token:
            kwargs['pageToken'] = page_token
        resp = service.users().list(**kwargs).execute()
        out.extend(resp.get('users', []))
        page_token = resp.get('nextPageToken')
        if not page_token:
            break
    return out


def fetch_users(service) -> list[dict]:
    """Returns active + suspended + recently-deleted Workspace users.

    Google's API needs two calls: showDeleted=false returns active+suspended,
    showDeleted=true returns ONLY the 20-day deleted window. We union them
    so the Directory page can render every state with one staff.json.
    """
    live = _fetch_users_one(service, show_deleted=False)
    try:
        deleted = _fetch_users_one(service, show_deleted=True)
    except Exception as e:
        print(f"# WARNING: deleted-users fetch failed (non-fatal): {e}", flush=True)
        deleted = []
    return live + deleted


def normalize(u: dict, tenant: str) -> dict:
    """Pull just the fields we'll display; tag with tenant name."""
    name = u.get('name') or {}
    orgs = (u.get('organizations') or [{}])
    primary_org = next((o for o in orgs if o.get('primary')), orgs[0]) if orgs else {}
    # User-editable aliases + non-editable ones (auto-generated when a domain is
    # configured as a Workspace alias-domain — these are the rgroup.co.uk /
    # mail.letme.co.uk / etc. ones the user expects to see). We strip the
    # *.test-google-a.com autoprovisioned ones which never appear on real mail.
    # We also expose the editable subset separately so the Directory page can
    # offer alias→group conversion only on aliases that can actually be deleted
    # via the API (nonEditableAliases can't be removed and linger ~21 days).
    editable_raw = u.get('aliases') or []
    raw_aliases = editable_raw + (u.get('nonEditableAliases') or [])
    aliases = sorted({a for a in raw_aliases if a and not a.endswith('.test-google-a.com')})
    editable_aliases = sorted({a for a in editable_raw if a and not a.endswith('.test-google-a.com')})
    return {
        'id':              u.get('id') or '',         # immutable Workspace user id — needed for undelete
        'email':           u.get('primaryEmail') or '',
        'aliases':         aliases,
        'aliases_editable': editable_aliases,
        'name':          (name.get('fullName') or '').strip(),
        'given':         (name.get('givenName') or '').strip(),
        'family':        (name.get('familyName') or '').strip(),
        'title':         (primary_org.get('title') or '').strip(),
        'department':    (primary_org.get('department') or '').strip(),
        'photo_url':     u.get('thumbnailPhotoUrl') or '',
        'suspended':     bool(u.get('suspended')),
        'admin':         bool(u.get('isAdmin')),
        'deletion_time': u.get('deletionTime') or '',  # ISO timestamp if the account is in the 20-day deleted window
        'tenant':        tenant,
    }


def load_tenants() -> list[dict]:
    """Return a list of {name, delegate, domain} dicts. Supports both the new
    WORKSPACE_TENANTS env var and the legacy single-tenant fallback."""
    tenants_raw = os.environ.get('WORKSPACE_TENANTS')
    if tenants_raw:
        try:
            tenants = json.loads(tenants_raw)
        except json.JSONDecodeError as e:
            sys.exit(f"error: WORKSPACE_TENANTS is not valid JSON: {e}")
        if not isinstance(tenants, list) or not tenants:
            sys.exit("error: WORKSPACE_TENANTS must be a non-empty JSON array")
        for t in tenants:
            if not (t.get('delegate') and t.get('domain')):
                sys.exit(f"error: tenant {t} missing 'delegate' or 'domain'")
            t['name'] = t.get('name') or t['domain']
        return tenants

    # Legacy fallback
    domain = env('WORKSPACE_DOMAIN', DOMAIN_DEFAULT)
    delegate = env('WORKSPACE_DELEGATE_USER')
    return [{'name': domain, 'delegate': delegate, 'domain': domain}]


def main() -> None:
    started = datetime.datetime.utcnow()
    print(f"# scan_directory start ({started.isoformat()}Z)", flush=True)

    key_info = load_key_info()
    tenants = load_tenants()
    print(f"# tenants: {[t['name'] for t in tenants]}", flush=True)

    all_users: list[dict] = []
    suspended_total = 0
    per_tenant_counts: dict[str, dict] = {}
    fetch_errors: list[dict] = []

    for t in tenants:
        name, delegate, domain = t['name'], t['delegate'], t['domain']
        try:
            service = build_service(key_info, delegate)
            raw = fetch_users(service)
        except Exception as e:
            print(f"# WARNING: {name} fetch failed: {e}", flush=True)
            fetch_errors.append({
                'tenant': name,
                'domain': domain,
                'error':  str(e)[:300],
            })
            per_tenant_counts[name] = {'users': 0, 'suspended_hidden': 0, 'error': str(e)[:200]}
            continue

        users = [normalize(u, tenant=name) for u in raw]
        # Keep everyone — active, suspended AND recently-deleted. The Directory
        # page sorts suspended to the bottom (greyed) and deleted to the very
        # bottom (red, recoverable for 20 days). Filtering them out here hid
        # leavers entirely, which broke the per-seat-billing-visibility goal.
        suspended_count = sum(1 for u in users if u['suspended'])
        deleted_count = sum(1 for u in users if u.get('deletion_time'))
        suspended_total += suspended_count

        per_tenant_counts[name] = {
            'users': len(users),
            'suspended_hidden': suspended_count,   # name kept for output back-compat; not actually hidden any more
            'deleted': deleted_count,
        }
        all_users.extend(users)
        print(f"#   {name}: {len(users)} total ({suspended_count} suspended, {deleted_count} deleted)", flush=True)

    # Dedupe by email — if a person has accounts in two tenants we keep one,
    # but record both tenants on the kept row so the UI can show that.
    by_email: dict[str, dict] = {}
    for u in all_users:
        key = u['email'].lower()
        if not key:
            continue
        if key in by_email:
            other = by_email[key]
            tenants_set = set(other.get('tenants') or [other['tenant']])
            tenants_set.add(u['tenant'])
            other['tenants'] = sorted(tenants_set)
        else:
            by_email[key] = dict(u)
            by_email[key]['tenants'] = [u['tenant']]
    deduped = list(by_email.values())
    deduped.sort(key=lambda u: (u['family'].lower(), u['given'].lower(), u['email'].lower()))

    # Department breakdown (across all tenants).
    by_department: dict[str, int] = dict(Counter(
        u['department'] or '(unspecified)' for u in deduped
    ))
    # Per-tenant user counts (post-dedupe) — useful for the UI's tenant filter.
    by_tenant: dict[str, int] = dict(Counter(
        t for u in deduped for t in u['tenants']
    ))

    primary_domain = tenants[0]['domain']
    payload = {
        'schema_version': 2,
        'updated_at':     started.isoformat() + 'Z',
        'snapshot_date':  started.date().isoformat(),
        'domain':         primary_domain,             # legacy field — kept for compatibility
        'tenants': [
            {
                'name':              t['name'],
                'domain':            t['domain'],
                'delegate':          t['delegate'],
                'users':             per_tenant_counts.get(t['name'], {}).get('users', 0),
                'suspended_hidden':  per_tenant_counts.get(t['name'], {}).get('suspended_hidden', 0),
                'error':             per_tenant_counts.get(t['name'], {}).get('error'),
            } for t in tenants
        ],
        'totals': {
            'users':            len(deduped),
            'suspended_hidden': suspended_total,
            'by_department':    dict(sorted(by_department.items(), key=lambda kv: -kv[1])),
            'by_tenant':        dict(sorted(by_tenant.items(),     key=lambda kv: -kv[1])),
        },
        'fetch_errors': fetch_errors,
        'users': deduped,
    }

    out_path = Path(os.environ.get('OUT', 'staff.json')).resolve()
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(
        f"# wrote {out_path} — {len(deduped)} active users across "
        f"{len(tenants)} tenant(s); {suspended_total} suspended hidden; "
        f"{len(by_department)} departments",
        flush=True,
    )


if __name__ == '__main__':
    main()
