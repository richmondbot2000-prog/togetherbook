"""
Generate brandwatch.json — recent public mentions of Together Loans and
TransformCredit across Trustpilot, Reddit, and Google News.

No paid APIs and no admin involvement: every source is publicly readable
without authentication.

Output shape (kept stable so the page can rely on it):

{
  "schema_version": 1,
  "snapshot_date": "YYYY-MM-DD",   # UTC date
  "updated_at": "<ISO8601 UTC>",
  "brands": ["Together Loans", "TransformCredit"],
  "totals": {
    "all": int,
    "by_source": {"trustpilot": int, "reddit": int, "google_news": int},
    "by_brand":  {"together_loans": int, "transform_credit": int}
  },
  "source_status": {                # so the page can show "Reddit: ok"
    "trustpilot":   {"ok": bool, "fetched": int, "error": str|null},
    "reddit":       {"ok": bool, "fetched": int, "error": str|null},
    "google_news":  {"ok": bool, "fetched": int, "error": str|null}
  },
  "mentions": [                     # already sorted desc by date
    {
      "source":         "trustpilot|reddit|google_news",
      "brand":          "together_loans|transform_credit",
      "id":             "stable per-source id",
      "title":          str,        # the headline (post title, review title, article title)
      "snippet":        str,        # ~280-char window around the brand mention
      "url":            str,        # click-out to the ORIGINAL page (not the aggregator)
      "date":           "YYYY-MM-DD",
      "author":         str|null,
      "score":          int|null,   # 1-5 for trustpilot, upvotes for reddit
      "score_max":      int|null,   # 5 for trustpilot, null otherwise
      "site_name":      str,        # display name: "Trustpilot", "r/personalfinance", "Forbes"
      "site_domain":    str         # for favicon: "trustpilot.com", "reddit.com", "forbes.com"
    },
    ...
  ]
}
"""
from __future__ import annotations

import datetime as dt
import html
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote_plus

import requests

# --------------------------------------------------------------------------
# Brand definitions

BRANDS = [
    {
        "key":      "together_loans",
        "label":    "Together Loans",
        "domain":   "togetherloans.com",
        "queries":  ['"Together Loans"', "togetherloans"],
    },
    {
        "key":      "transform_credit",
        "label":    "TransformCredit",
        "domain":   "transformcredit.com",
        "queries":  ['"Transform Credit"', '"TransformCredit"'],
    },
]

USER_AGENT = (
    "APIsForKids-brandwatch/1.0 "
    "(+https://richmondbot2000-prog.github.io/APIsForKids/; contact: "
    "richmondbot2000@gmail.com)"
)

HTTP_TIMEOUT = 20  # seconds per request
PER_BRAND_LIMIT = 25


# --------------------------------------------------------------------------
# Utilities

def _short(text: str, n: int = 280) -> str:
    """Trim a string for snippet display."""
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if len(text) <= n:
        return text
    return text[: n - 1].rstrip() + "…"


def _strip_html(text: str) -> str:
    if not text:
        return ""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


# Brand-mention-aware snippet: show ~n chars centred on the first match of any
# of the brand's queries. If no match found (e.g. brand only in title), trim
# the start of the text. Returns "" for empty input.
def _snippet_around_brand(text: str, brand: dict, n: int = 280) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if not text:
        return ""
    for q in brand["queries"]:
        bare = q.strip('"')
        m = re.search(re.escape(bare), text, re.IGNORECASE)
        if m:
            half = n // 2
            start = max(0, m.start() - half)
            end   = min(len(text), m.end() + half)
            chunk = text[start:end]
            if start > 0:
                chunk = "…" + chunk
            if end < len(text):
                chunk = chunk + "…"
            return chunk[:n + 2]  # +2 for the leading/trailing ellipsis chars
    return _short(text, n)


# Pull the actual publisher domain out of a Google News description string.
# Google News descriptions contain anchor tags like:
#   <a href="https://www.forbes.com/.../article">Title</a>&nbsp;&nbsp;<font color="#6f6f6f">Forbes</font>
# We want forbes.com (for the favicon) and "Forbes" (for the display label).
def _extract_news_publisher(description_html: str, source_url_attr: str | None,
                            source_text: str | None) -> tuple[str, str]:
    domain = ""
    if source_url_attr:
        m = re.search(r"https?://(?:www\.)?([^/]+)", source_url_attr)
        if m:
            domain = m.group(1)
    if not domain and description_html:
        m = re.search(r'<a [^>]*href="https?://(?:www\.)?([^/"]+)', description_html)
        if m:
            domain = m.group(1)
    name = (source_text or "").strip()
    if not name and description_html:
        m = re.search(r'<font[^>]*>([^<]+)</font>', description_html)
        if m:
            name = m.group(1).strip()
    return name or domain or "News", domain or "news.google.com"


def _http_get(url: str, **kwargs) -> requests.Response:
    headers = kwargs.pop("headers", {}) or {}
    headers.setdefault("User-Agent", USER_AGENT)
    headers.setdefault("Accept", "*/*")
    return requests.get(url, headers=headers, timeout=HTTP_TIMEOUT, **kwargs)


# --------------------------------------------------------------------------
# Sources

def fetch_trustpilot(brand: dict) -> list[dict]:
    """
    Trustpilot embeds a JSON blob in a <script id="__NEXT_DATA__"> tag with
    every review on the page. Pull that blob, walk its 'reviews' list.

    If the brand has no Trustpilot profile (404) we return [] and let the
    caller record source_status accordingly.
    """
    url = f"https://www.trustpilot.com/review/{brand['domain']}"
    headers = {
        # Trustpilot ships Cloudflare. A real browser UA gets through reliably.
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.0 Safari/605.1.15"
        ),
        "Accept": "text/html,application/xhtml+xml",
    }
    r = _http_get(url, headers=headers)
    if r.status_code == 404:
        return []
    r.raise_for_status()

    m = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>',
        r.text, re.DOTALL,
    )
    if not m:
        return []
    blob = json.loads(m.group(1))

    reviews = (
        blob.get("props", {})
            .get("pageProps", {})
            .get("reviews", [])
    )

    out = []
    for rv in reviews[:PER_BRAND_LIMIT]:
        try:
            review_id = rv.get("id") or ""
            title = rv.get("title") or ""
            body  = rv.get("text")  or ""
            rating = int(rv.get("rating") or 0) or None
            created = rv.get("dates", {}).get("publishedDate") or rv.get("createdAt")
            consumer = rv.get("consumer", {}).get("displayName")
            slug = rv.get("id", "")
            link = f"{url}#{slug}" if slug else url
            iso_date = (created or "")[:10]
            out.append({
                "source":      "trustpilot",
                "brand":       brand["key"],
                "id":          f"tp-{review_id}",
                "title":       _short(title, 200) or "(no title)",
                "snippet":     _snippet_around_brand(body, brand, 280),
                "url":         link,
                "date":        iso_date,
                "author":      consumer,
                "score":       rating,
                "score_max":   5,
                "site_name":   "Trustpilot",
                "site_domain": "trustpilot.com",
            })
        except Exception as e:
            print(f"  trustpilot: skipping review ({e})", flush=True)
    return out


def fetch_reddit(brand: dict) -> list[dict]:
    """
    Reddit's public search.json endpoint. Returns recent posts that match
    any of the brand's quoted queries.
    """
    out: list[dict] = []
    seen_ids: set[str] = set()

    for query in brand["queries"]:
        url = (
            "https://www.reddit.com/search.json"
            f"?q={quote_plus(query)}&sort=new&limit={PER_BRAND_LIMIT}&t=year"
        )
        r = _http_get(url)
        if r.status_code in (403, 429):
            # rate-limited or blocked — surface to caller via raise
            raise RuntimeError(
                f"reddit returned {r.status_code} for query {query!r}"
            )
        r.raise_for_status()
        data = r.json().get("data", {}).get("children", [])
        for child in data:
            d = child.get("data", {})
            rid = d.get("id")
            if not rid or rid in seen_ids:
                continue
            seen_ids.add(rid)

            title = d.get("title") or ""
            body  = d.get("selftext") or ""
            snippet = _short(body, 280) if body.strip() else "(link post)"
            permalink = d.get("permalink") or ""
            created = d.get("created_utc")
            iso_date = (
                dt.datetime.utcfromtimestamp(int(created)).date().isoformat()
                if created else ""
            )
            sub = d.get("subreddit") or ""
            # Snippet centred on the brand mention if present, else trimmed selftext.
            if body.strip():
                body_snippet = _snippet_around_brand(body, brand, 280)
            else:
                body_snippet = "(link post — no text body)"
            out.append({
                "source":      "reddit",
                "brand":       brand["key"],
                "id":          f"rd-{rid}",
                "title":       _short(title, 200) or "(no title)",
                "snippet":     body_snippet,
                "url":         f"https://www.reddit.com{permalink}",
                "date":        iso_date,
                "author":      f"u/{d.get('author')}" if d.get("author") else None,
                "score":       d.get("score"),
                "score_max":   None,
                "site_name":   f"r/{sub}" if sub else "Reddit",
                "site_domain": "reddit.com",
            })
    return out


def fetch_google_news(brand: dict) -> list[dict]:
    """Google News RSS. Free, no auth, returns recent press / blog mentions."""
    out: list[dict] = []
    for query in brand["queries"]:
        url = (
            "https://news.google.com/rss/search"
            f"?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
        )
        r = _http_get(url)
        r.raise_for_status()
        try:
            root = ET.fromstring(r.text)
        except ET.ParseError as e:
            print(f"  google_news: parse error for {query!r}: {e}", flush=True)
            continue

        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link  = (item.findtext("link")  or "").strip()
            pub   = (item.findtext("pubDate") or "").strip()
            descr_raw = item.findtext("description") or ""
            source_el = item.find("{http://www.w3.org/2005/Atom}source") or item.find("source")
            source_text = source_el.text if source_el is not None and source_el.text else ""
            source_url_attr = source_el.attrib.get("url") if source_el is not None else ""

            try:
                pub_dt = dt.datetime.strptime(pub[:25], "%a, %d %b %Y %H:%M:%S")
                iso_date = pub_dt.date().isoformat()
            except ValueError:
                iso_date = ""

            # Resolve publication name + domain so the page can show its favicon.
            site_name, site_domain = _extract_news_publisher(
                descr_raw, source_url_attr, source_text
            )

            # Description is HTML — strip tags then centre on the brand mention.
            snippet = _snippet_around_brand(_strip_html(descr_raw), brand, 280)

            # Build a stable id from the GUID or fall back to the link.
            guid = (item.findtext("guid") or link)
            stable_id = re.sub(r"[^a-zA-Z0-9]+", "-", guid)[-40:]

            out.append({
                "source":      "google_news",
                "brand":       brand["key"],
                "id":          f"gn-{stable_id}",
                "title":       _short(title, 200) or "(no title)",
                "snippet":     snippet,
                "url":         link,
                "date":        iso_date,
                "author":      None,
                "score":       None,
                "score_max":   None,
                "site_name":   site_name,
                "site_domain": site_domain,
            })
    return out


# --------------------------------------------------------------------------
# Driver

def run() -> dict:
    today_utc = dt.datetime.utcnow().date().isoformat()
    now_utc   = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

    source_status: dict[str, dict] = {
        "trustpilot":   {"ok": True, "fetched": 0, "error": None},
        "reddit":       {"ok": True, "fetched": 0, "error": None},
        "google_news":  {"ok": True, "fetched": 0, "error": None},
    }
    all_mentions: list[dict] = []

    for brand in BRANDS:
        print(f"== {brand['label']} ==", flush=True)
        for source_key, fn in (
            ("trustpilot",  fetch_trustpilot),
            ("reddit",      fetch_reddit),
            ("google_news", fetch_google_news),
        ):
            try:
                rows = fn(brand)
                print(f"  {source_key}: {len(rows)} mentions", flush=True)
                source_status[source_key]["fetched"] += len(rows)
                all_mentions.extend(rows)
            except Exception as e:
                # Don't fail the whole run for one bad source.
                print(f"  {source_key}: FAILED — {e}", flush=True)
                source_status[source_key]["ok"] = False
                source_status[source_key]["error"] = f"{type(e).__name__}: {e}"

    # De-dupe by (source, id)
    seen = set()
    deduped = []
    for m in all_mentions:
        k = (m["source"], m["id"])
        if k in seen:
            continue
        seen.add(k)
        deduped.append(m)

    # Sort by date desc; missing dates sink to the bottom.
    deduped.sort(key=lambda m: (m.get("date") or ""), reverse=True)

    by_source = {"trustpilot": 0, "reddit": 0, "google_news": 0}
    by_brand  = {b["key"]: 0 for b in BRANDS}
    for m in deduped:
        by_source[m["source"]] = by_source.get(m["source"], 0) + 1
        by_brand[m["brand"]]   = by_brand.get(m["brand"], 0) + 1

    return {
        "schema_version": 1,
        "snapshot_date":  today_utc,
        "updated_at":     now_utc,
        "brands":         [b["label"] for b in BRANDS],
        "totals": {
            "all":       len(deduped),
            "by_source": by_source,
            "by_brand":  by_brand,
        },
        "source_status": source_status,
        "mentions":      deduped,
    }


def main():
    out_path = Path(__file__).resolve().parent.parent / "brandwatch.json"
    payload = run()
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(
        f"\nwrote {out_path.relative_to(out_path.parent.parent)}: "
        f"{payload['totals']['all']} mentions "
        f"(trustpilot {payload['totals']['by_source']['trustpilot']}, "
        f"reddit {payload['totals']['by_source']['reddit']}, "
        f"news {payload['totals']['by_source']['google_news']})",
        flush=True,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
