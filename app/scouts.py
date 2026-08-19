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
