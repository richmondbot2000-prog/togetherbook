"""Re-deploy apifk-workspace-worker2 with the full bindings list:

  - 4 secret_text bindings restored via {"type":"inherit", "name":...}
  - 2 plain_text bindings preserved
  - 1 kv_namespace binding preserved
  - 1 D1 binding ACTIVITY_DB added

Ships the local worker/workspace-worker.js as the new script.

Pre-deploy guard: refuses to deploy if the local worker file is behind
`origin/main` — protects against the two-Claude-Code-sessions race where
session A pushes a worker change and session B immediately re-deploys an
older local copy. Override with `--force` (and only when you're sure).
"""
from __future__ import annotations
import json
import os
import pathlib
import subprocess
import sys
import uuid
import urllib.error
import urllib.request

REPO_ROOT = pathlib.Path("/Users/richmondrobot/Desktop/togetherbook")
SOURCE = REPO_ROOT / "worker" / "workspace-worker.js"

FORCE = "--force" in sys.argv or os.environ.get("CI") == "true"

# --- Pre-deploy guard: local worker must not be behind origin/main ---

def _sh(cmd: list[str]) -> tuple[int, str]:
    res = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    return res.returncode, (res.stdout + res.stderr).strip()

def _hash(spec: str) -> str | None:
    code, out = _sh(["git", "rev-parse", spec])
    return out if code == 0 else None

# Sync remote refs without modifying the working tree.
_sh(["git", "fetch", "--quiet", "origin", "main"])

local_blob  = _hash(f"HEAD:worker/workspace-worker.js")
remote_blob = _hash(f"origin/main:worker/workspace-worker.js")
disk_bytes  = SOURCE.read_bytes()

# Hash the on-disk file the same way git does, so we can tell whether the
# tree has uncommitted edits vs is purely behind origin.
import hashlib
def _git_blob_sha(data: bytes) -> str:
    header = f"blob {len(data)}\0".encode()
    return hashlib.sha1(header + data).hexdigest()

disk_sha = _git_blob_sha(disk_bytes)

issues = []
# Case A: HEAD's worker differs from origin/main's worker (we're behind).
if local_blob and remote_blob and local_blob != remote_blob:
    # Are we BEHIND (origin has commits HEAD doesn't), or AHEAD?
    code, ahead = _sh(["git", "rev-list", "--count", "origin/main..HEAD", "--", "worker/workspace-worker.js"])
    code, behind = _sh(["git", "rev-list", "--count", "HEAD..origin/main", "--", "worker/workspace-worker.js"])
    if behind.isdigit() and int(behind) > 0 and (not ahead.isdigit() or int(ahead) == 0):
        issues.append(f"worker/workspace-worker.js is {behind} commit(s) behind origin/main. "
                      f"Run `git pull --rebase` first — another session has pushed worker changes you don't have.")

# Case B: working-tree file disagrees with HEAD (uncommitted edits) AND it's not what's on origin either.
if local_blob and disk_sha != local_blob and disk_sha != remote_blob:
    # Allow if user intends to deploy uncommitted local changes — but be loud about it.
    print(f"# note: local worker has uncommitted edits (disk SHA {disk_sha[:8]}, HEAD blob {local_blob[:8]}).")

if issues and not FORCE:
    print("# pre-deploy guard FAILED:", flush=True)
    for i in issues:
        print(f"#   - {i}")
    print("# pass --force to override (only when you're sure).")
    sys.exit(2)
elif issues and FORCE:
    print("# pre-deploy guard would have blocked, but --force was passed. Continuing.")
    for i in issues:
        print(f"#   ! {i}")

account = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
token   = os.environ.get("CLOUDFLARE_API_TOKEN")
if not account or not token:
    cfg_path = pathlib.Path.home() / ".togetherbook" / "cloudflare.json"
    if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text())
        account = account or cfg["account_id"]
        token   = token   or cfg["api_token"]
if not account or not token:
    raise SystemExit("CLOUDFLARE_ACCOUNT_ID + CLOUDFLARE_API_TOKEN must be set (env vars or ~/.togetherbook/cloudflare.json)")
db_id   = cfg["d1_activity_database_id"]

SCRIPT = "apifk-workspace-worker2"

# Existing values captured a moment ago.
COMPAT_DATE = "2026-05-12"

# Look up the KV namespace ID currently bound as PAYROLL_KV so we can
# re-supply it (kv_namespace binding shape requires the namespace_id).
def cf_get(path):
    url = f"https://api.cloudflare.com/client/v4{path}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

# Optional: rotate/install BookR Firebase admin secret if provided in env.
_bookr_secret = os.environ.get("BOOKR_SERVICE_ACCOUNT_JSON")
if _bookr_secret:
    _u = f"https://api.cloudflare.com/client/v4/accounts/{account}/workers/scripts/{SCRIPT}/secrets"
    _req = urllib.request.Request(
        _u,
        data=json.dumps({"name": "BOOKR_SERVICE_ACCOUNT_JSON", "text": _bookr_secret, "type": "secret_text"}).encode(),
        method="PUT",
    )
    _req.add_header("Authorization", f"Bearer {token}")
    _req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(_req, timeout=30) as _r:
            _out = json.loads(_r.read())
        print("# BOOKR_SERVICE_ACCOUNT_JSON secret upsert:", _out.get("success", False))
    except urllib.error.HTTPError as _e:
        print("# BOOKR_SERVICE_ACCOUNT_JSON secret upsert FAILED:", _e.code, _e.read()[:300].decode("utf-8", "replace"))
        raise

code, settings = cf_get(f"/accounts/{account}/workers/scripts/{SCRIPT}/settings")
if not settings.get("success"):
    print("GET settings FAIL:", settings)
    sys.exit(1)
existing = settings["result"].get("bindings") or []
kv_id_for_payroll = None
impersonate_user = None
impersonate_user_tl = None
impersonate_user_letmecouk = None
for b in existing:
    if b.get("type") == "kv_namespace" and b.get("name") == "PAYROLL_KV":
        kv_id_for_payroll = b.get("namespace_id")
    if b.get("type") == "plain_text" and b.get("name") == "IMPERSONATE_USER":
        impersonate_user = b.get("text")
    if b.get("type") == "plain_text" and b.get("name") == "IMPERSONATE_USER_TOGETHERLOANS":
        impersonate_user_tl = b.get("text")
    if b.get("type") == "plain_text" and b.get("name") == "IMPERSONATE_USER_LETMECOUK":
        impersonate_user_letmecouk = b.get("text")

# First-deploy bootstrap: letme.co.uk impersonator was added 2026-05-18 to
# unblock alias-delete on @letme.co.uk accounts. Default to the Group CEO
# if no current binding exists; later deploys preserve whatever's set.
if not impersonate_user_letmecouk:
    impersonate_user_letmecouk = "james.benamor@letme.co.uk"

print(f"# captured PAYROLL_KV namespace_id={kv_id_for_payroll}")
print(f"# captured IMPERSONATE_USER={impersonate_user!r}")
print(f"# captured IMPERSONATE_USER_TOGETHERLOANS={impersonate_user_tl!r}")
print(f"# captured IMPERSONATE_USER_LETMECOUK={impersonate_user_letmecouk!r}")
if not kv_id_for_payroll:
    print("FATAL: could not find PAYROLL_KV namespace_id in current settings")
    sys.exit(1)

bindings = [
    {"type": "inherit",      "name": "CLOUDFLARE_API_TOKEN"},
    {"type": "inherit",      "name": "GIPHY_API_KEY"},
    {"type": "inherit",      "name": "GITHUB_TOKEN"},
    {"type": "inherit",      "name": "GOOGLE_SERVICE_ACCOUNT_JSON"},
    {"type": "inherit",      "name": "BOOKR_SERVICE_ACCOUNT_JSON"},
    {"type": "plain_text",   "name": "IMPERSONATE_USER",              "text": impersonate_user or ""},
    {"type": "plain_text",   "name": "IMPERSONATE_USER_TOGETHERLOANS", "text": impersonate_user_tl or ""},
    {"type": "plain_text",   "name": "IMPERSONATE_USER_LETMECOUK",     "text": impersonate_user_letmecouk},
    {"type": "kv_namespace", "name": "PAYROLL_KV",                    "namespace_id": kv_id_for_payroll},
    {"type": "d1",           "name": "ACTIVITY_DB",                   "id": db_id},
]

metadata = {
    "main_module": "worker.js",
    "compatibility_date": COMPAT_DATE,
    "bindings": bindings,
}

script_body = SOURCE.read_bytes()
print(f"# uploading {SOURCE.name}: {len(script_body):,} bytes")

boundary = "------ClaudeFormBoundary" + uuid.uuid4().hex
parts = []
parts.append((
    f'--{boundary}\r\n'
    f'Content-Disposition: form-data; name="metadata"\r\n'
    f'Content-Type: application/json\r\n\r\n'
    f'{json.dumps(metadata)}\r\n'
).encode())
parts.append((
    f'--{boundary}\r\n'
    f'Content-Disposition: form-data; name="worker.js"; filename="worker.js"\r\n'
    f'Content-Type: application/javascript+module\r\n\r\n'
).encode())
parts.append(script_body)
parts.append(f'\r\n--{boundary}--\r\n'.encode())
body = b"".join(parts)

url = f"https://api.cloudflare.com/client/v4/accounts/{account}/workers/scripts/{SCRIPT}"
req = urllib.request.Request(url, data=body, method="PUT")
req.add_header("Authorization", f"Bearer {token}")
req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")

try:
    with urllib.request.urlopen(req, timeout=120) as r:
        out = json.loads(r.read())
        status = r.status
except urllib.error.HTTPError as e:
    try:
        out = json.loads(e.read())
    except json.JSONDecodeError:
        out = {"errors": [{"message": str(e)}]}
    status = e.code

if not out.get("success"):
    print(f"PUT script FAIL (status={status}):")
    print(json.dumps(out.get("errors") or out, indent=2)[:3000])
    sys.exit(1)

print("# deploy OK")
res = out.get("result") or {}
print(f"  id={res.get('id')}  etag={res.get('etag','')[:12]}")
print(f"  modified_on={res.get('modified_on')}")
print(f"  startup_time_ms={res.get('startup_time_ms')}")

code, after = cf_get(f"/accounts/{account}/workers/scripts/{SCRIPT}/settings")
print("# bindings after deploy:")
for b in after["result"].get("bindings") or []:
    extra = ""
    if b.get("type") == "d1": extra = f"  id={b.get('id')}"
    if b.get("type") == "kv_namespace": extra = f"  ns={b.get('namespace_id')}"
    print(f"    - {b.get('type'):20s} name={b.get('name')!r}{extra}")
