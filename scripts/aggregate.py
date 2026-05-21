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

# (name, url, category, default_relevance)
# default_relevance = True  → all items kept (source is already on-topic)
# default_relevance = False → filter by AI/tech keywords
RSS_SOURCES = [
    # ── 한국 AI/IT ─────────────────────────────────────────────
    ("AI타임스", "https://www.aitimes.com/rss/allArticle.xml", "AI", True),
    ("ZDNet Korea", "https://feeds.feedburner.com/zdkorea", "TECH", False),

    # ── 영문 AI/ML/Tech ────────────────────────────────────────
    ("TechCrunch", "https://techcrunch.com/feed/", "TECH", False),
    ("The Verge", "https://www.theverge.com/rss/index.xml", "TECH", False),
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index", "TECH", False),
    ("Wired", "https://www.wired.com/feed/rss", "TECH", False),
    ("MIT Tech Review", "https://www.technologyreview.com/feed/", "AI", False),
    ("VentureBeat AI", "https://venturebeat.com/category/ai/feed/", "AI", True),

    # ── AI 연구소 공식 블로그 ──────────────────────────────────
    ("OpenAI", "https://openai.com/blog/rss.xml", "AI", True),
    ("Google DeepMind", "https://deepmind.google/blog/rss.xml", "AI", True),
    ("Anthropic", "https://www.anthropic.com/news/rss.xml", "AI", True),
    ("Hugging Face", "https://huggingface.co/blog/feed.xml", "AI", True),

    # ── 개발자 커뮤니티 ─────────────────────────────────────────
    ("Hacker News", "https://hnrss.org/frontpage?points=200", "DEV", True),
    ("GitHub Blog", "https://github.blog/feed/", "DEV", False),
]

# (name, channel_id, category)
# Channel IDs are public. youtube.com/feeds/videos.xml?channel_id=… returns RSS.
YOUTUBE_CHANNELS = [
    ("Lex Fridman", "UCSHZKyawb77ixDdsGog4iWA", "AI"),
    ("Two Minute Papers", "UCbfYPyITQ-7l4upoX8nvctg", "AI"),
    ("Yannic Kilcher", "UCZHmQk67mSJgfCCTn7xBfew", "AI"),
    ("AI Explained", "UCNJ1Ymd5yFuUPtn21xtRbbw", "AI"),
    ("Fireship", "UCsBjURrPoezykLs9EqgamOA", "DEV"),
    ("ThePrimeagen", "UC8ENHE5xdFSwx71u3fDH5Xw", "DEV"),
    ("노마드 코더", "UCUpJs89fSBXNolQGOYKn0YQ", "DEV"),
    ("조코딩", "UCQNE2JmbasNYbjGAcuBiRRg", "DEV"),
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


def parse_rfc822_or_iso(s: str | None) -> str:
    """Best-effort normalize to ISO 8601. Returns '' if unparseable."""
    if not s:
        return ""
    s = s.strip()
    # Try common formats
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
                d = d.replace(tzinfo=dt.timezone.utc)
            return d.astimezone(dt.timezone.utc).isoformat()
        except ValueError:
            continue
    return s  # give up: pass through


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


# ─── aggregator core ─────────────────────────────────────────────────

def collect() -> list[dict]:
    all_items: list[dict] = []

    for name, url, category, default_on in RSS_SOURCES:
        print(f"[RSS]  {name}")
        body = fetch(url)
        if not body:
            continue
        for it in parse_rss(body)[:MAX_PER_SOURCE]:
            if not it["title"] or not it["link"]:
                continue
            if not default_on and not relevant(it["title"], it["summary"]):
                continue
            all_items.append({
                "id": make_id(it["link"], it["title"]),
                "source": name,
                "kind": "article",
                "category": category,
                "title": it["title"],
                "link": it["link"],
                "summary": it["summary"][:280],
                "published": it["published"],
            })

    for name, channel_id, category in YOUTUBE_CHANNELS:
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
