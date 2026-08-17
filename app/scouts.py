"""
Scout agents: pull candidate articles from configured sources and
normalize them into a common shape. RSS/Atom feeds are the starting
point because they're stable and don't require scraping fragile HTML.

Add more scout functions here later (e.g. a NewsAPI scout, a sitemap
scout) -- the pipeline just expects a list of normalized dicts back.
"""
import re
import html
import feedparser
from datetime import datetime

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return html.unescape(_TAG_RE.sub("", text or "")).strip()


def _clean_summary(entry) -> str:
    return entry.get("summary", "") or entry.get("description", "")


def fetch_from_feed(feed_url: str, max_items: int = 15) -> list[dict]:
    """Fetch and normalize entries from a single RSS/Atom feed."""
    parsed = feedparser.parse(feed_url)
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


def run_scouts(feed_urls: list[str], max_items_per_feed: int = 15) -> list[dict]:
    """Run all scout agents (one per feed) and return the combined candidate list."""
    all_articles = []
    errors = []
    for feed_url in feed_urls:
        try:
            all_articles.extend(fetch_from_feed(feed_url, max_items_per_feed))
        except Exception as e:
            errors.append({"feed": feed_url, "error": str(e)})
    return all_articles, errors
