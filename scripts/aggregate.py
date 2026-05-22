#!/usr/bin/env python3
"""
Kimnews daily aggregator.

Runs at 09:00 KST (00:00 UTC) via GitHub Actions, pulls AI/tech/dev/IT items
from RSS feeds + YouTube channels, dedupes, filters by keyword for
generalist sources, and writes data/latest.json (+ a dated snapshot).

Sources are intentionally lightweight: standard RSS. No API keys required.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

# (name, url, category, default_relevance, lang)
# default_relevance = True  → all items kept (source is already on-topic)
# default_relevance = False → filter by AI/tech keywords
# lang → ISO 639-1 of the source language; used by frontend translator
RSS_SOURCES = [
    # ── 한국 AI/IT ─────────────────────────────────────────────
    ("AI타임스", "https://www.aitimes.com/rss/allArticle.xml", "AI", True, "ko"),
    ("ZDNet Korea", "https://feeds.feedburner.com/zdkorea", "TECH", False, "ko"),

    # ── 영문 AI/ML/Tech ────────────────────────────────────────
    ("TechCrunch", "https://techcrunch.com/feed/", "TECH", False, "en"),
    ("The Verge", "https://www.theverge.com/rss/index.xml", "TECH", False, "en"),
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index", "TECH", False, "en"),
    ("Wired", "https://www.wired.com/feed/rss", "TECH", False, "en"),
    ("MIT Tech Review", "https://www.technologyreview.com/feed/", "AI", False, "en"),
    ("VentureBeat AI", "https://venturebeat.com/category/ai/feed/", "AI", True, "en"),

    # ── AI 연구소 공식 블로그 ──────────────────────────────────
    ("OpenAI", "https://openai.com/blog/rss.xml", "AI", True, "en"),
    ("Google DeepMind", "https://deepmind.google/blog/rss.xml", "AI", True, "en"),
    ("Anthropic", "https://www.anthropic.com/news/rss.xml", "AI", True, "en"),
    ("Hugging Face", "https://huggingface.co/blog/feed.xml", "AI", True, "en"),

    # ── 개발자 커뮤니티 ─────────────────────────────────────────
    ("Hacker News", "https://hnrss.org/frontpage?points=200", "DEV", True, "en"),
    ("GitHub Blog", "https://github.blog/feed/", "DEV", False, "en"),
]

# (name, channel_id, category, lang)
# Channel IDs are public. youtube.com/feeds/videos.xml?channel_id=… returns RSS.
YOUTUBE_CHANNELS = [
    ("Lex Fridman", "UCSHZKyawb77ixDdsGog4iWA", "AI", "en"),
    ("Two Minute Papers", "UCbfYPyITQ-7l4upoX8nvctg", "AI", "en"),
    ("Yannic Kilcher", "UCZHmQk67mSJgfCCTn7xBfew", "AI", "en"),
    ("AI Explained", "UCNJ1Ymd5yFuUPtn21xtRbbw", "AI", "en"),
    ("Fireship", "UCsBjURrPoezykLs9EqgamOA", "DEV", "en"),
    ("ThePrimeagen", "UC8ENHE5xdFSwx71u3fDH5Xw", "DEV", "en"),
    ("노마드 코더", "UCUpJs89fSBXNolQGOYKn0YQ", "DEV", "ko"),
    ("조코딩", "UCQNE2JmbasNYbjGAcuBiRRg", "DEV", "ko"),
]

# Lowercased keywords used to keep "generalist tech" items relevant.
KEYWORDS = (
    "ai ", " ai", "artificial intelligence", "machine learning", "ml ",
    "gpt", "claude", "gemini", "llama", "mistral", "openai", "anthropic",
    "deepmind", "huggingface", "stable diffusion", "midjourney",
    "neural", "model", "llm", "agent", "transformer", "embedding",
    "chatgpt", "copilot", "rag",
    "python", "javascript", "typescript", "rust", "golang", "kotlin",
    "react", "next.js", "svelte", "node.js",
    "github", "open source", "developer",
    "ios", "android", "apple", "google", "microsoft", "meta",
    "chip", "gpu", "nvidia", "amd", "intel", "arm ",
    "robot", "humanoid", "self-driving", "autonomous",
    "인공지능", "머신러닝", "딥러닝", "오픈ai", "오픈AI", "엔비디아",
    "챗gpt", "챗GPT", "구글", "애플", "테크", "개발자", "개발",
    "스타트업", "VC ", "투자",
)

UA = {"User-Agent": "Mozilla/5.0 (KimnewsBot/1.0; +https://kimnews.kimkim.io)"}
TIMEOUT = 15
MAX_PER_SOURCE = 12  # cap per feed so one chatty source doesn't dominate

MAX_FETCH_LEN = 6000   # chars of raw article body we extract and store
MAX_OUTPUT_LEN = 4000  # chars stored per item (large enough for full body)


# ─── helpers ─────────────────────────────────────────────────────────

def fetch(url: str) -> bytes | None:
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        print(f"  ! fetch failed {url}: {e}", file=sys.stderr)
        return None


_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def clean(text: str | None) -> str:
    if not text:
        return ""
    s = html.unescape(_TAG.sub(" ", text))
    return _WS.sub(" ", s).strip()


_KST = dt.timezone(dt.timedelta(hours=9))


def parse_rfc822_or_iso(s: str | None) -> str:
    """Best-effort normalize to ISO 8601 UTC. Returns '' if unparseable.

    Naive datetimes (no timezone in source) are assumed KST — most Korean
    news sites (AI타임스, ZDNet Korea…) emit `YYYY-MM-DD HH:MM:SS` in
    local time. Assuming UTC there would push timestamps 9 hours into the
    future and break "x분 전" rendering on the client.
    """
    if not s:
        return ""
    s = s.strip()
    fmts = [
        "%a, %d %b %Y %H:%M:%S %Z",
        "%a, %d %b %Y %H:%M:%S %z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
    ]
    for fmt in fmts:
        try:
            d = dt.datetime.strptime(s, fmt)
            if d.tzinfo is None:
                d = d.replace(tzinfo=_KST)
            return d.astimezone(dt.timezone.utc).isoformat()
        except ValueError:
            continue
    return s


def relevant(title: str, summary: str) -> bool:
    hay = f"{title} {summary}".lower()
    return any(k in hay for k in KEYWORDS)


def parse_rss(xml_bytes: bytes) -> list[dict]:
    """Minimal RSS/Atom parser using stdlib ET.

    Returns a list of {title, link, summary, published}.
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        print(f"  ! parse error: {e}", file=sys.stderr)
        return []

    items: list[dict] = []

    # RSS 2.0 — <rss><channel><item>...
    for it in root.iter("item"):
        items.append({
            "title": clean(_text(it, "title")),
            "link": (_text(it, "link") or "").strip(),
            "summary": clean(_text(it, "description")),
            "published": parse_rfc822_or_iso(_text(it, "pubDate")),
        })

    # Atom — <feed><entry>...
    if not items:
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for entry in root.findall("a:entry", ns):
            link = ""
            link_el = entry.find("a:link", ns)
            if link_el is not None:
                link = link_el.get("href") or ""
            items.append({
                "title": clean(_text_ns(entry, "a:title", ns)),
                "link": link.strip(),
                "summary": clean(_text_ns(entry, "a:summary", ns) or _text_ns(entry, "a:content", ns)),
                "published": parse_rfc822_or_iso(_text_ns(entry, "a:published", ns) or _text_ns(entry, "a:updated", ns)),
            })

    return items


def _text(parent, tag: str) -> str | None:
    el = parent.find(tag)
    return el.text if el is not None else None


def _text_ns(parent, tag: str, ns: dict) -> str | None:
    el = parent.find(tag, ns)
    return el.text if el is not None else None


def make_id(link: str, title: str) -> str:
    h = hashlib.sha1(f"{link}|{title}".encode("utf-8")).hexdigest()
    return h[:12]


# ─── article body extraction ─────────────────────────────────────────

# Tags whose entire subtree should be removed before paragraph extraction.
_DROP_BLOCKS = re.compile(
    r"<(script|style|noscript|nav|header|footer|aside|form|iframe|svg)\b[^>]*>.*?</\1>",
    re.I | re.S,
)


_BOILERPLATE_MARKERS = (
    "subscribe", "sign up", "newsletter", "cookies", "privacy policy",
    "all rights reserved", "follow us", "advertisement", "powered by",
    "구독", "저작권", "관련기사",
)

# Any short line that embeds an email is overwhelmingly a contact tail.
_INLINE_EMAIL_RE = re.compile(r"[\w.+\-]+@[\w.\-]+\.[A-Za-z]{2,}")

# Korean byline pattern: "이성환 기자 leesh@aitimes.com" — short trailing
# attribution we want to drop, not a content paragraph.
_BYLINE_RE = re.compile(
    r"^[가-힣A-Za-z\s.·]{2,40}\s*(기자|특파원|에디터|writer|editor)\s*[\w.+\-]+@[\w.\-]+\.[A-Za-z]{2,}\s*$",
    re.I,
)
# Pure email line — usually a contact tail.
_EMAIL_ONLY_RE = re.compile(r"^\s*[\w.+\-]+@[\w.\-]+\.[A-Za-z]{2,}\s*$")
# Short tail like "AI타임스 news@aitimes.com" — publication + contact email
# without the explicit "기자" marker.
_PUB_EMAIL_TAIL_RE = re.compile(
    r"^[가-힣A-Za-z0-9\s.·&'-]{2,40}\s+[\w.+\-]+@[\w.\-]+\.[A-Za-z]{2,}\s*$"
)


def _score_container(inner_html: str) -> tuple[int, list[str]]:
    """Return (text-length-of-paragraphs, list-of-cleaned-paragraphs)."""
    paras_raw = re.findall(r"<p\b[^>]*>(.*?)</p>", inner_html, re.I | re.S)
    paras = [clean(p) for p in paras_raw]
    paras = [p for p in paras if len(p) >= 20]   # very short = list bullets, captions
    return sum(len(p) for p in paras), paras


def extract_article_text(html_bytes: bytes) -> str:
    """Find the article body and return clean paragraph text.

    Many real-world pages (AI타임스 has 13 `<article>` elements) make
    "first article tag" useless. So we collect every <article>/<main>
    candidate plus a class-hinted div fallback, score each by paragraph
    text length, and keep the winner.
    """
    html = html_bytes.decode("utf-8", errors="replace")
    html = _DROP_BLOCKS.sub(" ", html)

    candidates: list[str] = []
    for m in re.finditer(r"<(article|main)\b[^>]*>(.*?)</\1>", html, re.I | re.S):
        candidates.append(m.group(2))

    best_score, best_paras = 0, []
    for c in candidates:
        s, p = _score_container(c)
        if s > best_score:
            best_score, best_paras = s, p

    # Fallback: pull <p> from the entire page (after chrome stripping).
    if best_score < 200:
        _, best_paras = _score_container(html)

    cleaned: list[str] = []
    seen: set[str] = set()
    for s in best_paras:
        low = s.lower()
        if any(k in low for k in _BOILERPLATE_MARKERS):
            continue
        if (_BYLINE_RE.match(s) or _EMAIL_ONLY_RE.match(s)
                or (len(s) < 80 and _PUB_EMAIL_TAIL_RE.match(s))
                or (len(s) < 140 and _INLINE_EMAIL_RE.search(s))):
            continue
        if s in seen:
            continue
        seen.add(s)
        cleaned.append(s)
        if sum(len(c) for c in cleaned) >= MAX_FETCH_LEN:
            break

    body = "\n\n".join(cleaned)
    # Truncate at paragraph boundary so the reader never sees "…fend off" cut.
    if len(body) > MAX_FETCH_LEN:
        keep = []
        running = 0
        for c in cleaned:
            if running + len(c) + 2 > MAX_FETCH_LEN:
                break
            keep.append(c)
            running += len(c) + 2
        body = "\n\n".join(keep)
    return body


def fetch_article_body(url: str) -> str | None:
    body = fetch(url)
    if not body:
        return None
    try:
        return extract_article_text(body)
    except Exception as e:
        print(f"  ! extract failed {url}: {e}", file=sys.stderr)
        return None


# NOTE: Translation is handled lazily by the frontend (MyMemory free API,
# CORS-friendly) when a reader opens an article in a non-source language.
# No paid LLM dependency, no API keys in this repo. See index.html for the
# translateText() helper + sessionStorage cache.


# ─── aggregator core ─────────────────────────────────────────────────

def load_existing() -> dict[str, dict]:
    """Read the previous run's data so we can skip re-fetching unchanged
    articles. Source page fetches + body extraction are the expensive
    part of a run — being able to skip them turns a re-run into seconds.
    """
    p = DATA_DIR / "latest.json"
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return {it.get("id"): it for it in d.get("items", []) if it.get("id")}
    except Exception as e:
        print(f"  ! existing read failed: {e}", file=sys.stderr)
        return {}


def collect() -> list[dict]:
    existing = load_existing()
    reused = 0
    fetched = 0
    all_items: list[dict] = []

    for name, url, category, default_on, lang in RSS_SOURCES:
        print(f"[RSS]  {name}")
        body = fetch(url)
        if not body:
            continue
        for it in parse_rss(body)[:MAX_PER_SOURCE]:
            if not it["title"] or not it["link"]:
                continue
            if not default_on and not relevant(it["title"], it["summary"]):
                continue

            iid = make_id(it["link"], it["title"])
            prior = existing.get(iid)
            if prior and prior.get("content") and len(prior["content"]) >= 100:
                article_body = prior["content"]
                reused += 1
            else:
                article_body = fetch_article_body(it["link"]) or ""
                fetched += 1
                if len(article_body) < 100:
                    # Photo/stub post with no real body — drop entirely.
                    continue
            all_items.append({
                "id": iid,
                "source": name,
                "kind": "article",
                "category": category,
                "lang": lang,
                "title": it["title"],
                "link": it["link"],
                "summary": it["summary"][:280],
                "content": article_body,
                "published": it["published"],
            })

    for name, channel_id, category, lang in YOUTUBE_CHANNELS:
        url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        print(f"[YT]   {name}")
        body = fetch(url)
        if not body:
            continue
        for it in parse_rss(body)[:6]:
            if not it["title"] or not it["link"]:
                continue
            all_items.append({
                "id": make_id(it["link"], it["title"]),
                "source": name,
                "kind": "youtube",
                "category": category,
                "lang": lang,
                "title": it["title"],
                "link": it["link"],
                "summary": it["summary"][:280],
                "published": it["published"],
            })

    # Dedupe by id (link+title hash)
    seen: set[str] = set()
    deduped: list[dict] = []
    for it in all_items:
        if it["id"] in seen:
            continue
        seen.add(it["id"])
        deduped.append(it)

    # Sort: items with a parseable published date go first (newest first),
    # then the rest in source order.
    def sort_key(it: dict):
        p = it["published"]
        if p and p[:4].isdigit():
            return (0, -ord(p[0])) + tuple(-int(x) for x in re.findall(r"\d+", p)[:6])
        return (1, 0)

    deduped.sort(key=sort_key)
    print(f"\n[stats] reused={reused} fetched={fetched} unique={len(deduped)}")
    return deduped


def main() -> int:
    DATA_DIR.mkdir(exist_ok=True)
    items = collect()
    now_utc = dt.datetime.now(dt.timezone.utc)
    # KST = UTC+9. Compute the "issue date" in KST so 9am-run = today's paper.
    issue_kst = (now_utc + dt.timedelta(hours=9)).date().isoformat()

    payload = {
        "issue_date": issue_kst,
        "generated_at": now_utc.isoformat(),
        "count": len(items),
        "items": items,
    }

    (DATA_DIR / "latest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (DATA_DIR / f"{issue_kst}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"\n✓ {len(items)} items → data/latest.json (issue {issue_kst})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
