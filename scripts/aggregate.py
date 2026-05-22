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

# When ANTHROPIC_API_KEY is set we paraphrase each article so the in-app
# reader shows our own copy with attribution instead of redirecting users.
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")
MAX_FETCH_LEN = 6000   # chars of raw article we keep before paraphrase
MAX_OUTPUT_LEN = 1400  # chars of stored content per item


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


def extract_article_text(html_bytes: bytes) -> str:
    """Cheap readability — drop chrome blocks, grab paragraphs, dedupe junk.

    Targets ~80% of mainstream news layouts. Far from perfect; perfectly fine
    for "give me a few paragraphs to paraphrase."
    """
    html = html_bytes.decode("utf-8", errors="replace")
    html = _DROP_BLOCKS.sub(" ", html)

    # Prefer <article> / <main> if present — those usually wrap the body.
    for tag in ("article", "main"):
        m = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", html, re.I | re.S)
        if m and len(m.group(1)) > 400:
            html = m.group(1)
            break

    paragraphs = re.findall(r"<p\b[^>]*>(.*?)</p>", html, re.I | re.S)
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in paragraphs:
        s = clean(raw)
        if len(s) < 40:
            continue
        # Drop common boilerplate
        lower = s.lower()
        if any(k in lower for k in (
            "subscribe", "sign up", "newsletter", "cookies", "privacy policy",
            "all rights reserved", "follow us", "댓글", "구독", "저작권",
        )):
            continue
        if s in seen:
            continue
        seen.add(s)
        cleaned.append(s)
        if sum(len(c) for c in cleaned) > MAX_FETCH_LEN:
            break

    return "\n\n".join(cleaned)[:MAX_FETCH_LEN]


def fetch_article_body(url: str) -> str | None:
    body = fetch(url)
    if not body:
        return None
    try:
        return extract_article_text(body)
    except Exception as e:
        print(f"  ! extract failed {url}: {e}", file=sys.stderr)
        return None


# ─── paraphrase (Anthropic) ──────────────────────────────────────────

PARAPHRASE_PROMPT = """You are localizing a news article for a multilingual reader. Rewrite it
in three languages (Korean, English, Japanese) and translate the headline too.

Rules:
- Keep all facts, numbers, named entities (people, companies, products) intact.
- Don't copy the original wording verbatim — paraphrase naturally in each language.
- Body: 4–6 short paragraphs per language, ~300–600 chars each.
- Do not append source attribution, URLs, or "more info" lines. Body only.
- If the input is in one language, you still must produce the other two fully translated.

Headline (original): {title}
Source: {source}

Body (raw):
{body}

Respond with ONLY a JSON object, no prose, no markdown fences:
{{
  "title_ko": "...", "title_en": "...", "title_ja": "...",
  "content_ko": "...", "content_en": "...", "content_ja": "..."
}}"""


def _post_anthropic(payload_obj: dict, timeout: int = 90) -> dict | None:
    """Single Anthropic /messages call with prefilled assistant '{' for JSON."""
    msgs = list(payload_obj.get("messages") or [])
    msgs.append({"role": "assistant", "content": "{"})
    body = json.dumps({**payload_obj, "messages": msgs}).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        snippet = ""
        try: snippet = e.read()[:240].decode("utf-8", errors="replace")
        except Exception: pass
        print(f"  ! Anthropic HTTP {e.code}: {snippet}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  ! Anthropic error: {e}", file=sys.stderr)
        return None


def paraphrase(body: str, title: str, source: str) -> dict | None:
    """Returns {'title_ko','title_en','title_ja','content_ko','content_en','content_ja'}
    or None if disabled / failed."""
    if not ANTHROPIC_KEY or not body:
        return None

    resp = _post_anthropic({
        "model": ANTHROPIC_MODEL,
        "max_tokens": 2200,
        "messages": [{
            "role": "user",
            "content": PARAPHRASE_PROMPT.format(title=title, source=source, body=body[:MAX_FETCH_LEN]),
        }],
    })
    if not resp: return None
    parts = resp.get("content") or []
    if not parts: return None
    text = "{" + parts[0].get("text", "").strip()

    # Strip any accidental code fences and parse JSON.
    text = re.sub(r"^```(?:json)?\s*|\s*```\s*$", "", text.strip())
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        # Fall back: try to slice from first { to last }
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            print(f"  ! paraphrase JSON parse error: {e}", file=sys.stderr)
            return None
        try: obj = json.loads(m.group(0))
        except Exception: return None

    keys = ("title_ko","title_en","title_ja","content_ko","content_en","content_ja")
    out = {k: (str(obj.get(k, "")).strip()[:MAX_OUTPUT_LEN]) for k in keys}
    return out if any(out.values()) else None


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

            # Fetch + paraphrase into KO/EN/JA so the reader stays on kimnews
            # AND the chosen UI language drives which text shows up. Without
            # ANTHROPIC_API_KEY we just store the extracted text as-is.
            article_body = fetch_article_body(it["link"])
            tri = paraphrase(article_body or it["summary"], it["title"], name) if article_body else None
            fallback_content = (article_body or "")[:MAX_OUTPUT_LEN]

            entry = {
                "id": make_id(it["link"], it["title"]),
                "source": name,
                "kind": "article",
                "category": category,
                "title": it["title"],
                "link": it["link"],
                "summary": it["summary"][:280],
                "content": (tri and tri.get("content_ko")) or fallback_content,
                "paraphrased": bool(tri),
                "published": it["published"],
            }
            if tri:
                for k in ("title_ko","title_en","title_ja","content_ko","content_en","content_ja"):
                    if tri.get(k): entry[k] = tri[k]
            all_items.append(entry)

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
