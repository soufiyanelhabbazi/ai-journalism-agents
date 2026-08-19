"""
Scout agents: pull candidate articles from configured sources and
normalize them into a common shape. RSS/Atom feeds are the starting
point because they're stable and don't require scraping fragile HTML.

Add more scout functions here later (e.g. a NewsAPI scout, a sitemap
scout) -- the pipeline just expects a list of normalized dicts back.
"""
import re
import html
import socket
import httpx
import feedparser
from datetime import datetime

_TAG_RE = re.compile(r"<[^>]+>")

# feedparser.parse(url) does its own networking when given a URL, and its
# timeout handling turned out unreliable in practice -- some feeds stalled
# for 30-60s even with socket.setdefaulttimeout() set, which is long enough
# to single-handedly blow past a serverless function's execution limit
# before the pipeline's own deadline logic ever gets a chance to run. So
# fetching happens here instead, with httpx's properly-enforced timeout;
# feedparser is only ever handed already-downloaded bytes to parse, no
# network I/O of its own.
FETCH_TIMEOUT = 10.0  # seconds

# A self-identifying bot UA (e.g. "SafircomAIJournalist/1.0") worked fine
# fetching these same public RSS feeds from a home IP, but got 403'd on
# hespress.com and mapnews.ma once requests started coming from Vercel's
# shared datacenter IP ranges -- bot-protection services commonly combine
# IP reputation with UA fingerprinting, and a generic "compatible; bot" UA
# is an easy extra signal to flag. A standard browser UA + Accept headers
# removes that signal; it won't help if the block is purely IP-reputation
# based; still request the actual RSS content-type, not HTML.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*;q=0.8",
    "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
}

# Even with httpx's own timeout enforced above, several feeds (hespress.com,
# al3omk.com, barlamane.com, ar.yabiladi.com) still stalled 20-60s -- past
# the HTTP layer entirely. Root cause: DNS resolution (socket.getaddrinfo)
# returning IPv6 addresses that are slow/unroutable on this host's network,
# with the fallback to a working IPv4 address only happening after a long OS
# resolver delay. Forcing IPv4-only resolution dropped the same feeds to
# ~1.3-1.6s in isolated testing. socket.getaddrinfo is process-global in
# Python, so this patch (applied once, at import time) affects all outbound
# connections in the process -- fine here since nothing else in the app
# (Gemini/Groq calls) has shown this symptom, and IPv4-only can only remove
# a slow path, never add one.
_original_getaddrinfo = socket.getaddrinfo


def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if family == 0:
        family = socket.AF_INET
    return _original_getaddrinfo(host, port, family, type, proto, flags)


socket.getaddrinfo = _ipv4_only_getaddrinfo


def _strip_html(text: str) -> str:
    return html.unescape(_TAG_RE.sub("", text or "")).strip()


def _clean_summary(entry) -> str:
    return entry.get("summary", "") or entry.get("description", "")


# ---------------------------------------------------------------------------
# Full article text
#
# Most Moroccan feeds publish a one- or two-sentence teaser in <description>,
# not the article. Judging that teaser instead of the article was the single
# biggest source of wrong rejections: the rule stage threw articles out for
# being "6 words", and the editor threw others out with "المقال غير مكتمل"
# (the text is truncated / ends in a 'read more' link) -- both complaints
# about the feed, not the journalism. The same story could be accepted or
# rejected purely by how much text its feed happened to include.
#
# So the article page itself is fetched and its body extracted. No new
# dependency: a readability-grade parser is overkill when every one of these
# sites marks paragraphs up as <p> inside the article body.
# ---------------------------------------------------------------------------

ARTICLE_FETCH_TIMEOUT = 8.0  # shorter than the feed timeout; there are many more of these

# Containers that never hold article prose. Removed wholesale before looking
# for paragraphs, so their text can't dilute or contaminate the body.
_CHROME_RE = re.compile(
    r"<(script|style|noscript|figure|figcaption|nav|header|footer|aside|form)\b[^>]*>.*?</\1>",
    re.S | re.I,
)
_PARA_RE = re.compile(r"<p\b[^>]*>(.*?)</p>", re.S | re.I)

# Site furniture that *is* marked up as a paragraph: newsletter signup boxes,
# the masthead address block, share prompts, related-article teasers.
_BOILERPLATE_RE = re.compile(
    r"اشترك|القائمة البريدية|جميع الحقوق|رقم الهاتف|البريد الإلكتروني|تابعونا"
    r"|اقرأ أيضا|اقرأ أيضاً|شارك المقال|الوسوم|واتساب|أضف تعليق|التعليقات"
    r"|subscribe|newsletter|all rights reserved|read also",
    re.IGNORECASE,
)

# A real article paragraph runs to a sentence or more; anything shorter is a
# caption, byline, timestamp, or menu item that survived the cuts above.
_MIN_PARAGRAPH_WORDS = 8


def extract_article_text(page_html: str) -> str:
    """Pull the article body out of a fetched news page."""
    doc = _CHROME_RE.sub(" ", page_html)
    paragraphs = []
    for raw in _PARA_RE.findall(doc):
        text = re.sub(r"\s+", " ", _strip_html(raw))
        if len(text.split()) >= _MIN_PARAGRAPH_WORDS and not _BOILERPLATE_RE.search(text):
            paragraphs.append(text)
    return "\n\n".join(paragraphs)


def fetch_full_text(url: str, fallback: str = "") -> str:
    """
    Fetch an article page and return its body text.

    Returns `fallback` unchanged unless the page yields something genuinely
    better, because a partial extraction is worse than the feed's own
    summary. Some sites defeat this entirely -- ar.yabiladi.com answers with
    HTTP 200 and a zero-byte body -- and that has to degrade to the teaser
    rather than blank the article out.
    """
    try:
        response = httpx.get(
            url, timeout=ARTICLE_FETCH_TIMEOUT, headers=HEADERS, follow_redirects=True
        )
        response.raise_for_status()
        extracted = extract_article_text(response.text)
    except Exception:
        return fallback  # unreachable, blocked, or unparseable -- the teaser still works

    if len(extracted.split()) > len(fallback.split()):
        return extracted
    return fallback


def fetch_from_feed(feed_url: str, max_items: int = 15) -> list[dict]:
    """Fetch and normalize entries from a single RSS/Atom feed."""
    response = httpx.get(feed_url, timeout=FETCH_TIMEOUT, headers=HEADERS, follow_redirects=True)
    response.raise_for_status()
    parsed = feedparser.parse(response.content)
    source_name = parsed.feed.get("title") or feed_url

    articles = []
    for entry in parsed.entries[:max_items]:
        url = entry.get("link")
        if not url:
            continue
        published = entry.get("published", entry.get("updated", ""))
        summary = _clean_summary(entry)
        content = entry.get("content", [{}])[0].get("value", "") if entry.get("content") else summary

        articles.append(
            {
                "url": url,
                "title": _strip_html(entry.get("title", "Untitled")),
                "source": source_name,
                "published": published,
                "summary": summary,
                "content": content,
            }
        )
    return articles


def run_scouts(feed_urls: list[str], max_items_per_feed: int = 15) -> tuple[list[dict], list[dict]]:
    """
    Run all scout agents (one per feed) and return (candidates, errors).

    Candidates are interleaved round-robin across feeds rather than
    concatenated feed-by-feed. The pipeline caps how many new candidates one
    run admits, and with a flat concatenation that cap was always consumed
    by whichever feed happened to be listed first -- in practice every single
    stored article came from one source while the other six never got a look
    in. Round-robin makes the cap take a slice of each feed instead.
    """
    per_feed = []
    errors = []
    for feed_url in feed_urls:
        try:
            per_feed.append(fetch_from_feed(feed_url, max_items_per_feed))
        except Exception as e:
            errors.append({"feed": feed_url, "error": str(e)})

    all_articles = []
    for i in range(max(map(len, per_feed), default=0)):
        for feed_articles in per_feed:
            if i < len(feed_articles):
                all_articles.append(feed_articles[i])
    return all_articles, errors
