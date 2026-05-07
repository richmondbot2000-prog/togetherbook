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
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote_plus

import requests

# --------------------------------------------------------------------------
# Brand definitions

# Each brand has:
#   - queries: passed to each external source. Tight, exact-phrase forms only,
#              to keep recall reasonable while avoiding the noise we get from
#              generic phrasings like "transform credit agreement onboarding".
#   - precision_terms: the post-fetch filter. A mention from any source is
#              kept ONLY if its title or snippet contains at least one of
#              these strings (case-insensitive). Strict so we drop the
#              "transform" + "credit" coincidences.
BRANDS = [
    {
        "key":             "together_loans",
        "label":           "Together Loans",
        "domain":          "togetherloans.com",
        "queries":         ['"Together Loans"', "togetherloans.com"],
        # `precision_terms` is matched case-insensitive — only contains the
        # unambiguous one-word form. The spaced "together loans" was a
        # case-insensitive precision term but matched generic phrases like
        # "show bringing together loans from museums" (museum-loan noise).
        "precision_terms":            ["togetherloans"],
        # `precision_terms_strict` is matched case-SENSITIVE. People naming
        # the brand write it "Together Loans" (both words capitalized);
        # verb-phrase usage is lowercase or sentence-start ("Together
        # loans..."). Strict capitalization is a strong brand-form signal.
        "precision_terms_strict":     ["Together Loans"],
        "contextual_pairs":           [],
    },
    {
        "key":             "transform_credit",
        "label":           "TransformCredit",
        "domain":          "transformcredit.com",
        "queries":         ['"TransformCredit"', '"Transform Credit"', "transformcredit.com"],
        # Same two-tier filter as Together Loans:
        #   case-insensitive  : the unambiguous one-word brand spelling.
        #   case-SENSITIVE    : the spaced proper-noun "Transform Credit".
        # Drops the lowercase verb-phrase usage ("transform credit agreement
        # onboarding") that strict-CI was designed to drop, while still
        # admitting articles where journalists write "Transform Credit".
        "precision_terms":            ["transformcredit", "transformcredit.com"],
        "precision_terms_strict":     ["Transform Credit"],
        "contextual_pairs":           [],
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
    kwargs.setdefault("timeout", HTTP_TIMEOUT)
    return requests.get(url, headers=headers, **kwargs)


def _http_get_proxied(url: str, *, tier: str = "premium", **kwargs) -> requests.Response:
    """
    Like _http_get, but routes through ScraperAPI when SCRAPERAPI_KEY is set.

    `tier` selects the ScraperAPI parameter set (and thus credit cost). The
    right tier is highly target-specific — what works for one Cloudflare-
    fronted site fails on another. Tiers determined by direct probing
    2026-05-07:

      "render"    (10 credits) : &render=true alone.
                                 Required for Trustpilot — its Cloudflare
                                 challenge needs JS execution. premium=true
                                 alone returns 500.
      "premium"   (25 credits) : &premium=true alone.
                                 Required for BBB — adding render=true
                                 *breaks* BBB and returns 500.
      "ultra"     (75 credits) : &ultra_premium=true.
                                 Free trial accounts get 403 here; this is
                                 effectively paid-only. Kept for future
                                 reference.

    Includes one retry on 5xx (transient ScraperAPI failures).

    If SCRAPERAPI_KEY isn't set, falls back to a direct fetch.
    """
    key = os.environ.get("SCRAPERAPI_KEY")
    if not key:
        return _http_get(url, **kwargs)

    params = [
        f"api_key={quote_plus(key)}",
        f"url={quote_plus(url)}",
        "country_code=us",
    ]
    if tier == "render":
        params.append("render=true")
    elif tier == "ultra":
        params.append("ultra_premium=true")
    else:  # "premium"
        params.append("premium=true")
    base = "https://api.scraperapi.com?" + "&".join(params)

    # ScraperAPI takes 20–60 s for these depending on tier and target.
    kwargs.setdefault("timeout", 90)

    last: requests.Response | None = None
    for attempt in (1, 2):
        last = _http_get(base, **kwargs)
        if last.status_code < 500:
            return last
        if attempt == 1:
            import time as _t
            _t.sleep(2)
    return last  # type: ignore[return-value]


# --------------------------------------------------------------------------
# Sources

TRUSTPILOT_MAX_PAGES = 5  # 5 × 20 = 100 most-recent reviews per brand per scan


def fetch_trustpilot(brand: dict) -> list[dict]:
    """
    Trustpilot embeds a JSON blob in a <script id="__NEXT_DATA__"> tag with
    every review on the page. Pull that blob, walk its 'reviews' list.

    Paginates through TRUSTPILOT_MAX_PAGES pages of reviews (default 5) for
    historical depth. Each page costs 10 ScraperAPI credits (render=true).

    If the brand has no Trustpilot profile (404 on page 1) we return [] and
    let the caller record source_status accordingly. If a later page 404s,
    we silently stop (probably ran past the end of available reviews).
    """
    out: list[dict] = []
    seen_ids: set[str] = set()
    headers = {
        # Trustpilot ships Cloudflare. Direct fetch from cloud IPs always 403s,
        # but the request is routed via ScraperAPI when SCRAPERAPI_KEY is set —
        # see _http_get_proxied. Browser UA still helps when called directly.
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.0 Safari/605.1.15"
        ),
        "Accept": "text/html,application/xhtml+xml",
    }
    # Trustpilot's Cloudflare challenge needs JS execution to clear. Tested
    # 2026-05-07: render=true (10 credits) works; premium=true (25) doesn't;
    # ultra_premium=true (75) is paid-only. Render is both cheaper and more
    # reliable here.
    base_url = f"https://www.trustpilot.com/review/{brand['domain']}"

    for page in range(1, TRUSTPILOT_MAX_PAGES + 1):
        page_url = base_url if page == 1 else f"{base_url}?page={page}"
        r = _http_get_proxied(page_url, headers=headers, tier="render")
        if r.status_code == 404:
            # Page 1 missing → no profile. Later page missing → past the end.
            if page == 1:
                return []
            break
        r.raise_for_status()

        m = re.search(
            r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>',
            r.text, re.DOTALL,
        )
        if not m:
            if page == 1:
                return []
            break
        blob = json.loads(m.group(1))

        reviews = (
            blob.get("props", {})
                .get("pageProps", {})
                .get("reviews", [])
        )
        if not reviews:
            break  # past the end of available pages

        for rv in reviews:
            try:
                review_id = rv.get("id") or ""
                if not review_id or review_id in seen_ids:
                    continue
                seen_ids.add(review_id)

                title = rv.get("title") or ""
                body  = rv.get("text")  or ""
                rating = int(rv.get("rating") or 0) or None
                created = rv.get("dates", {}).get("publishedDate") or rv.get("createdAt")
                consumer = rv.get("consumer", {}).get("displayName")
                slug = rv.get("id", "")
                link = f"{base_url}#{slug}" if slug else base_url
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


_REDDIT_OAUTH_TOKEN_CACHE: dict | None = None


def _reddit_oauth_token() -> str | None:
    """
    If REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET are set, fetch a bearer token
    via the client_credentials grant. Cached for the run.
    Returns None if no creds (caller falls back to unauth).
    """
    global _REDDIT_OAUTH_TOKEN_CACHE
    if _REDDIT_OAUTH_TOKEN_CACHE is not None:
        return _REDDIT_OAUTH_TOKEN_CACHE.get("access_token")

    cid = os.environ.get("REDDIT_CLIENT_ID")
    sec = os.environ.get("REDDIT_CLIENT_SECRET")
    if not cid or not sec:
        _REDDIT_OAUTH_TOKEN_CACHE = {}
        return None

    r = requests.post(
        "https://www.reddit.com/api/v1/access_token",
        auth=(cid, sec),
        data={"grant_type": "client_credentials"},
        headers={"User-Agent": USER_AGENT},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    payload = r.json()
    _REDDIT_OAUTH_TOKEN_CACHE = payload
    return payload.get("access_token")


def fetch_reddit(brand: dict) -> list[dict]:
    """
    Reddit search. If REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET are present
    (recommended), use OAuth via oauth.reddit.com — reliable from cloud IPs.
    Otherwise fall back to the unauth endpoint, which Reddit increasingly
    blocks from cloud-IP runners (we try www → old → api in turn).
    """
    out: list[dict] = []
    seen_ids: set[str] = set()

    token = _reddit_oauth_token()

    for query in brand["queries"]:
        data = None
        last_err: Exception | None = None

        if token:
            # Authenticated path — single, reliable host.
            try:
                r = _http_get(
                    f"https://oauth.reddit.com/search.json"
                    f"?q={quote_plus(query)}&sort=new&limit={PER_BRAND_LIMIT}&t=year",
                    headers={"Authorization": f"bearer {token}"},
                )
                r.raise_for_status()
                data = r.json().get("data", {}).get("children", [])
            except Exception as e:
                last_err = e
        else:
            # Unauth fallback — try a few hosts in turn.
            for host in ("www.reddit.com", "old.reddit.com", "api.reddit.com"):
                try:
                    r = _http_get(
                        f"https://{host}/search.json"
                        f"?q={quote_plus(query)}&sort=new&limit={PER_BRAND_LIMIT}&t=year"
                    )
                    if r.status_code in (403, 429):
                        last_err = RuntimeError(
                            f"reddit {host} returned {r.status_code} for {query!r} "
                            f"(set REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET to use OAuth)"
                        )
                        continue
                    r.raise_for_status()
                    data = r.json().get("data", {}).get("children", [])
                    break
                except Exception as e:
                    last_err = e
                    continue

        if data is None:
            raise last_err or RuntimeError(f"reddit: every path failed for {query!r}")
        for child in data:
            d = child.get("data", {})
            rid = d.get("id")
            if not rid or rid in seen_ids:
                continue
            seen_ids.add(rid)

            title = d.get("title") or ""
            body  = d.get("selftext") or ""
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


def fetch_bbb(brand: dict) -> list[dict]:
    """
    BBB (Better Business Bureau) — search the public business-search page
    for the brand and return any business profiles + reviews found.

    BBB is Cloudflare-protected like Trustpilot, so this often 403s from
    cloud-IP runners. The page surfaces source-status warnings when it does.
    """
    out: list[dict] = []
    url = (
        "https://www.bbb.org/search?find_country=USA"
        f"&find_text={quote_plus(brand['domain'])}&page=1"
    )
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.0 Safari/605.1.15"
        ),
        "Accept": "text/html,application/xhtml+xml",
    }
    r = _http_get_proxied(url, headers=headers)
    r.raise_for_status()

    # BBB's profile URLs follow:
    #   /us/{state}/{city}/profile/{category-slug}/{business-slug}-{org}-{id}
    # Their search results wrap the link in rich HTML (<h3>, etc.) — text
    # capture between tags doesn't work. Extract the href only, derive the
    # business name from the slug.
    href_re = re.compile(
        r'href="(/us/[a-z]{2}/[^/"]+/profile/[^/"]+/[^"]+)"',
        re.IGNORECASE,
    )
    raw_hrefs = href_re.findall(r.text)
    # Dedupe while preserving order
    seen = set()
    hrefs = []
    for h in raw_hrefs:
        if h in seen:
            continue
        # Skip non-profile sub-paths like .../leave-a-review or .../complaints
        if "/leave-a-review" in h or "/complaints" in h or "/customer-reviews" in h:
            continue
        seen.add(h)
        hrefs.append(h)
        if len(hrefs) >= 5:
            break

    if not hrefs:
        snippet = re.sub(r"\s+", " ", r.text[:600])
        print(f"  bbb: no profile hrefs matched. response_size={len(r.text)} preview: {snippet}", flush=True)

    today = dt.datetime.utcnow().date().isoformat()

    # For each profile we found in search, follow through to its
    # /customer-reviews page and pull the actual review items. BBB embeds
    # the review array as JSON in window.__PRELOADED_STATE__, at path
    #   businessProfile.customerReviews.items[]
    # with keys: reviewStarRating, displayName, text, date, id, etc.
    # If the reviews page can't be fetched or has no items, fall back to
    # surfacing the profile listing as a single mention.
    for href in hrefs:
        full_url = "https://www.bbb.org" + href
        reviews_url = full_url.rstrip("/") + "/customer-reviews"

        # Derive friendly name + category from the slug in case we need a
        # fallback "profile listing" mention.
        slug = href.rstrip("/").rsplit("/", 1)[-1]
        parts = [p for p in slug.split("-") if not p.isdigit() and not (len(p) == 4 and p.isdigit())]
        biz_name = " ".join(p.capitalize() for p in parts) or "BBB Profile"
        path_pieces = href.strip("/").split("/")
        category_slug = path_pieces[-2] if len(path_pieces) >= 2 else ""
        category = category_slug.replace("-", " ").title() if category_slug else ""

        review_items: list[dict] = []
        try:
            rr = _http_get_proxied(reviews_url, headers=headers)
            rr.raise_for_status()
            m = re.search(
                r'window\.__PRELOADED_STATE__\s*=\s*({.+?});</script>',
                rr.text, re.DOTALL,
            )
            if m:
                state = json.loads(m.group(1))
                review_items = (
                    (state.get("businessProfile", {}) or {})
                    .get("customerReviews", {}) or {}
                ).get("items") or []
        except Exception as e:
            print(f"  bbb: reviews-page fetch/parse failed for {full_url}: {e}", flush=True)

        if review_items:
            for rv in review_items[:10]:
                try:
                    review_id = rv.get("id") or rv.get("customerReviewGuid") or ""
                    rating = rv.get("reviewStarRating")
                    try:
                        rating = int(rating) if rating is not None else None
                    except (TypeError, ValueError):
                        rating = None
                    author = rv.get("displayName") or None

                    # BBB stores dates as either a string or a nested object —
                    # tolerate both. Wrapper string-ifies anything weird so
                    # downstream slicing can't crash the whole run.
                    raw_date = rv.get("date") or ""
                    if isinstance(raw_date, dict):
                        raw_date = raw_date.get("value") or raw_date.get("iso") or ""
                    if not isinstance(raw_date, str):
                        raw_date = str(raw_date)

                    iso_date = ""
                    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
                        try:
                            iso_date = dt.datetime.strptime(raw_date[:19], fmt).date().isoformat()
                            break
                        except ValueError:
                            continue
                    if not iso_date and len(raw_date) >= 10:
                        iso_date = raw_date[:10]

                    body = rv.get("text") or rv.get("extendedText") or ""
                    if not isinstance(body, str):
                        body = ""
                    title = (
                        f"{biz_name} — {rating}-star review"
                        if rating else f"{biz_name} review"
                    )
                    out.append({
                        "source":      "bbb",
                        "brand":       brand["key"],
                        "id":          f"bbb-{review_id or re.sub(r'[^a-zA-Z0-9]+', '-', href)[-30:]}",
                        "title":       _short(title, 200),
                        "snippet":     _short(body, 280) or f"BBB review of {biz_name}.",
                        "url":         reviews_url,
                        "date":        iso_date or today,
                        "author":      author,
                        "score":       rating,
                        "score_max":   5,
                        "site_name":   "BBB",
                        "site_domain": "bbb.org",
                    })
                except Exception as e:
                    print(f"  bbb: skipping review ({e}) raw={rv!r:.200}", flush=True)
            continue

        # Fallback — surface the profile listing as a single mention.
        out.append({
            "source":      "bbb",
            "brand":       brand["key"],
            "id":          f"bbb-{re.sub(r'[^a-zA-Z0-9]+', '-', href)[-50:]}",
            "title":       _short(biz_name, 200),
            "snippet":     (
                f"Better Business Bureau profile in the {category} category."
                if category else f"Better Business Bureau profile for {brand['label']}."
            ),
            "url":         full_url,
            "date":        today,
            "author":      category or None,
            "score":       None,
            "score_max":   None,
            "site_name":   "BBB",
            "site_domain": "bbb.org",
        })
    return out


def fetch_bluesky(brand: dict) -> list[dict]:
    """
    Bluesky's public XRPC search endpoint. No auth, returns recent posts
    matching the brand's queries.

    NOTE the host is `api.bsky.app` — `public.api.bsky.app` was retired
    behind a WAF and now 403s every caller. The new host returns valid
    JSON (often empty) without authentication.
    """
    out: list[dict] = []
    seen_uris: set[str] = set()

    for query in brand["queries"]:
        url = (
            "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts"
            f"?q={quote_plus(query)}&limit={PER_BRAND_LIMIT}&sort=latest"
        )
        r = _http_get(url)
        r.raise_for_status()
        posts = r.json().get("posts", [])
        for p in posts:
            uri = p.get("uri") or ""
            if not uri or uri in seen_uris:
                continue
            seen_uris.add(uri)

            author = p.get("author", {}) or {}
            handle = author.get("handle") or "unknown"
            record = p.get("record", {}) or {}
            text   = record.get("text") or ""
            indexed_at = p.get("indexedAt") or record.get("createdAt") or ""
            iso_date = indexed_at[:10] if indexed_at else ""

            # Convert at://did:plc:.../app.bsky.feed.post/{rkey} into
            # https://bsky.app/profile/{handle}/post/{rkey}
            post_url = uri
            m = re.search(r"app\.bsky\.feed\.post/([^/]+)$", uri)
            if m:
                post_url = f"https://bsky.app/profile/{handle}/post/{m.group(1)}"

            stable_id = re.sub(r"[^a-zA-Z0-9]+", "-", uri)[-40:]

            out.append({
                "source":      "bluesky",
                "brand":       brand["key"],
                "id":          f"bs-{stable_id}",
                "title":       _short(text, 100) or "(post)",
                "snippet":     _snippet_around_brand(text, brand, 280),
                "url":         post_url,
                "date":        iso_date,
                "author":      f"@{handle}",
                "score":       p.get("likeCount") or None,
                "score_max":   None,
                "site_name":   "Bluesky",
                "site_domain": "bsky.app",
            })
    return out


def fetch_hackernews(brand: dict) -> list[dict]:
    """
    Hacker News via the free Algolia search API. No auth.
    Returns stories matching the brand's queries, most recent first.
    """
    out: list[dict] = []
    seen_ids: set[str] = set()

    for query in brand["queries"]:
        url = (
            "https://hn.algolia.com/api/v1/search_by_date"
            f"?query={quote_plus(query)}&tags=story&hitsPerPage={PER_BRAND_LIMIT}"
        )
        r = _http_get(url)
        r.raise_for_status()
        for hit in r.json().get("hits", []):
            obj_id = hit.get("objectID")
            if not obj_id or obj_id in seen_ids:
                continue
            seen_ids.add(obj_id)

            title = hit.get("title") or ""
            target = hit.get("url") or f"https://news.ycombinator.com/item?id={obj_id}"
            created = hit.get("created_at_i")
            iso_date = (
                dt.datetime.utcfromtimestamp(int(created)).date().isoformat()
                if created else ""
            )
            author = hit.get("author")
            domain_match = re.search(r"https?://(?:www\.)?([^/]+)", target)
            site_domain = domain_match.group(1) if domain_match else "news.ycombinator.com"

            out.append({
                "source":      "hackernews",
                "brand":       brand["key"],
                "id":          f"hn-{obj_id}",
                "title":       _short(title, 200) or "(no title)",
                "snippet":     _snippet_around_brand(title, brand, 280),
                "url":         f"https://news.ycombinator.com/item?id={obj_id}",
                "date":        iso_date,
                "author":      author,
                "score":       hit.get("points") or None,
                "score_max":   None,
                "site_name":   "Hacker News",
                "site_domain": "news.ycombinator.com",
            })
    return out


def fetch_courtlistener(brand: dict) -> list[dict]:
    """
    CourtListener (Free Law Project) — free public API for US court records.
    Surfaces lawsuits, opinions, dockets that mention the brand. No auth.

    For a consumer lender this is genuinely high-signal: regulatory actions,
    class actions, and CFPB-style cases land here long before they show up
    on the news side.
    """
    out: list[dict] = []
    seen_ids: set[str] = set()

    for query in brand["queries"]:
        url = (
            "https://www.courtlistener.com/api/rest/v4/search/"
            f"?q={quote_plus(query)}&order_by=dateFiled+desc"
        )
        r = _http_get(url, headers={"Accept": "application/json"})
        r.raise_for_status()
        results = r.json().get("results") or []
        for res in results[:PER_BRAND_LIMIT]:
            cluster_id = res.get("cluster_id") or res.get("id")
            if not cluster_id or cluster_id in seen_ids:
                continue
            seen_ids.add(cluster_id)

            case_name = res.get("caseName") or res.get("caseNameFull") or "(unnamed case)"
            court     = res.get("court") or ""
            date_filed = (res.get("dateFiled") or "")[:10]
            snippet_raw = res.get("snippet") or res.get("text") or ""
            absolute_url = res.get("absolute_url") or ""
            full_url = (
                f"https://www.courtlistener.com{absolute_url}"
                if absolute_url.startswith("/")
                else absolute_url
                or f"https://www.courtlistener.com/opinion/{cluster_id}/"
            )

            out.append({
                "source":      "courtlistener",
                "brand":       brand["key"],
                "id":          f"cl-{cluster_id}",
                "title":       _short(case_name, 200),
                "snippet":     _snippet_around_brand(_strip_html(snippet_raw), brand, 280)
                                  or f"Court case in {court}." if court else "Court case.",
                "url":         full_url,
                "date":        date_filed,
                "author":      _short(court, 60) or None,
                "score":       None,
                "score_max":   None,
                "site_name":   "CourtListener",
                "site_domain": "courtlistener.com",
            })
    return out


def fetch_lemmy(brand: dict) -> list[dict]:
    """
    Lemmy (federated Reddit-alternative) — search the lemmy.world instance
    via its public v3 API. No auth, no rate limit issues at this volume.

    Lemmy is small today; this often returns zero. Worth plumbing anyway:
    it's where some of the privacy-conscious / anti-Reddit crowd has gone,
    and the cost of an empty-list call is negligible.
    """
    out: list[dict] = []
    seen_ids: set[str] = set()

    for query in brand["queries"]:
        url = (
            "https://lemmy.world/api/v3/search"
            f"?q={quote_plus(query)}&type_=Posts&sort=New&limit={PER_BRAND_LIMIT}"
        )
        r = _http_get(url, headers={"Accept": "application/json"})
        r.raise_for_status()
        posts = r.json().get("posts") or []
        for entry in posts:
            post = entry.get("post") or {}
            pid = post.get("id")
            if not pid or pid in seen_ids:
                continue
            seen_ids.add(pid)

            title  = post.get("name") or ""
            body   = post.get("body") or ""
            published = (post.get("published") or "")[:10]
            creator   = (entry.get("creator") or {}).get("name") or ""
            community = (entry.get("community") or {}).get("name") or ""

            post_url = f"https://lemmy.world/post/{pid}"
            site_name = f"c/{community}" if community else "Lemmy"

            snippet = _snippet_around_brand(body, brand, 280) if body.strip() else "(link post — no text body)"

            out.append({
                "source":      "lemmy",
                "brand":       brand["key"],
                "id":          f"lm-{pid}",
                "title":       _short(title, 200) or "(no title)",
                "snippet":     snippet,
                "url":         post_url,
                "date":        published,
                "author":      f"@{creator}" if creator else None,
                "score":       (entry.get("counts") or {}).get("score"),
                "score_max":   None,
                "site_name":   site_name,
                "site_domain": "lemmy.world",
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
        "trustpilot":     {"ok": True, "fetched": 0, "error": None},
        "bbb":            {"ok": True, "fetched": 0, "error": None},
        "reddit":         {"ok": True, "fetched": 0, "error": None},
        "bluesky":        {"ok": True, "fetched": 0, "error": None},
        "lemmy":          {"ok": True, "fetched": 0, "error": None},
        "hackernews":     {"ok": True, "fetched": 0, "error": None},
        "courtlistener":  {"ok": True, "fetched": 0, "error": None},
        "google_news":    {"ok": True, "fetched": 0, "error": None},
    }
    all_mentions: list[dict] = []

    for brand in BRANDS:
        print(f"== {brand['label']} ==", flush=True)
        for source_key, fn in (
            ("trustpilot",     fetch_trustpilot),
            ("bbb",            fetch_bbb),
            ("reddit",         fetch_reddit),
            ("bluesky",        fetch_bluesky),
            ("lemmy",          fetch_lemmy),
            ("hackernews",     fetch_hackernews),
            ("courtlistener",  fetch_courtlistener),
            ("google_news",    fetch_google_news),
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

    # Precision filter — two-tier per brand. A mention is kept if EITHER:
    #   (a) any of the brand's `precision_terms` appears in title+snippet, OR
    #   (b) one of the brand's `contextual_pairs` matches — i.e. the trigger
    #       phrase appears AND at least one context word also appears in the
    #       same title+snippet. This is how we keep "Transform Credit" (the
    #       lender) but drop "transform credit agreement onboarding" (the
    #       verb phrase).
    #
    # Per-source filter strictness:
    #   UNFILTERED — URL is already brand-scoped, no filter.
    #     trustpilot.com/review/X.com only returns reviews of X.com; bbb.org
    #     profile pages are brand-specific. Every result is relevant.
    #   NEWS_LIKE  — strict only: case-INSENSITIVE precision_terms.
    #     Headlines and legal case captions title-case phrases as a matter
    #     of style, so the "proper-noun" capitalization signal is meaningless
    #     here. "Transform Credit Agreement Onboarding" is a headline, not a
    #     brand mention. Drop the strict / contextual tiers for these.
    #   Else (social) — full filter: case-insensitive + case-sensitive +
    #     contextual. Casual users on Reddit / Bluesky / Lemmy / HN write
    #     consistently when naming a brand; lowercase usually means the verb
    #     phrase. Strict capitalization "Transform Credit" is a real signal.
    UNFILTERED_SOURCES = {"trustpilot", "bbb"}
    NEWS_LIKE_SOURCES  = {"google_news", "courtlistener"}

    by_brand_cfg = {b["key"]: b for b in BRANDS}
    filtered = []
    dropped = 0
    for m in deduped:
        src = m["source"]
        if src in UNFILTERED_SOURCES:
            filtered.append(m)
            continue
        cfg = by_brand_cfg.get(m["brand"], {})
        title_snippet = (m.get("title") or "") + " " + (m.get("snippet") or "")
        haystack_ci   = title_snippet.lower()
        haystack_cs   = title_snippet  # case-sensitive

        kept = False
        # Tier 1: case-insensitive precision terms (one-word brand, URL).
        # Always applied — these are unambiguous.
        for t in cfg.get("precision_terms", []):
            if t.lower() in haystack_ci:
                kept = True
                break

        # Tiers 2+3 only for non-news sources (social, etc.). News headlines
        # capitalize phrases regardless of brand-vs-verb, so the strict /
        # contextual signals don't disambiguate there.
        if not kept and src not in NEWS_LIKE_SOURCES:
            for t in cfg.get("precision_terms_strict", []):
                if t in haystack_cs:
                    kept = True
                    break
            if not kept:
                for trigger, ctx_words in cfg.get("contextual_pairs", []):
                    if trigger.lower() in haystack_ci and any(w.lower() in haystack_ci for w in ctx_words):
                        kept = True
                        break

        if kept:
            filtered.append(m)
        else:
            dropped += 1
    if dropped:
        print(f"\nprecision filter: dropped {dropped} mentions with no brand signal in title/snippet", flush=True)
    deduped = filtered

    # Sort by date desc; missing dates sink to the bottom.
    deduped.sort(key=lambda m: (m.get("date") or ""), reverse=True)

    by_source = {k: 0 for k in source_status.keys()}
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
    bs = payload["totals"]["by_source"]
    breakdown = ", ".join(f"{k} {v}" for k, v in bs.items())
    print(
        f"\nwrote {out_path.relative_to(out_path.parent.parent)}: "
        f"{payload['totals']['all']} mentions ({breakdown})",
        flush=True,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
