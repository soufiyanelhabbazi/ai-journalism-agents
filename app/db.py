"""
Simple SQLite persistence layer.
No ORM on purpose -- keeps the prototype easy to read and swap out later
(e.g. for Postgres) once you outgrow it.
"""
import sqlite3
import json
import hashlib
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path(__file__).parent.parent / "data.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT UNIQUE NOT NULL,
    url_hash TEXT UNIQUE NOT NULL,
    title TEXT,
    source TEXT,
    published TEXT,
    summary TEXT,
    content TEXT,
    status TEXT DEFAULT 'pending',   -- pending | accepted | rejected
    reason TEXT,                      -- final reasoning (rule / editor / manual)
    confidence REAL,                  -- final confidence
    stage TEXT,                       -- which check produced the final verdict: rule | specialist | editor | manual
    domain TEXT,                      -- domain desk that claimed this article (e.g. Sports), if any
    proposal_reason TEXT,             -- the domain specialist's original reasoning, kept even if the editor overrides it
    proposal_confidence REAL,         -- the domain specialist's original confidence
    draft_headline TEXT,              -- staff writer's original headline, only set for final accepts
    draft_article TEXT,               -- staff writer's original, publish-ready article body
    provider TEXT,                    -- which LLM provider served the final verdict (e.g. Gemini, Groq)
    proposal_provider TEXT,           -- which provider served the specialist's original proposal
    draft_provider TEXT,              -- which provider wrote the drafted article
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def _migrate(conn):
    """Add columns introduced after the initial release to existing databases.
    CREATE TABLE IF NOT EXISTS doesn't touch a table that already exists, so
    a fresh column needs an explicit ALTER TABLE for anyone upgrading in place.
    """
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(articles)")}
    for col, ddl_type in [
        ("domain", "TEXT"),
        ("proposal_reason", "TEXT"),
        ("proposal_confidence", "REAL"),
        ("draft_headline", "TEXT"),
        ("draft_article", "TEXT"),
        ("provider", "TEXT"),
        ("proposal_provider", "TEXT"),
        ("draft_provider", "TEXT"),
    ]:
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE articles ADD COLUMN {col} {ddl_type}")


DEFAULT_CONFIG = {
    "feeds": [
        "http://feeds.bbci.co.uk/news/world/rss.xml",
        "https://www.reutersagency.com/feed/?best-topics=tech",
        "https://feeds.npr.org/1001/rss.xml",
    ],
    "banned_domains": [],
    "min_word_count": 150,
    "exclude_keywords": [],       # article is rejected if it matches any
    "require_attribution": True,  # reject articles with no quote/'said'/'according to' signal
    "rubric": (
        "Accept articles that are original reporting (not opinion pieces, not "
        "press releases), cover verifiable events with named sources, and are "
        "written in a neutral, non-sensational tone. Reject articles that are "
        "primarily speculation, clickbait, sponsored content, or lack any "
        "attribution for their claims."
    ),  # the editor-in-chief's universal standards -- applied to every domain's proposed accepts
    "domains": [
        {
            "name": "Politics",
            "rubric": "Covers government action, policy, elections, or legislation. Prioritize substance (what changed, who's affected) over pure punditry or campaign rhetoric.",
        },
        {
            "name": "Sports",
            "rubric": "Covers matches, tournaments, transfers, or athletes. Judge by subject matter, not by whether the word 'sports' appears -- team names, match reports, and competition results all count.",
        },
        {
            "name": "Art & Culture",
            "rubric": "Covers cinema, music, literature, exhibitions, festivals, or other cultural output. A listing or program announcement alone doesn't count -- look for actual coverage or critique.",
        },
    ],  # each domain desk's own fit/quality criteria -- edit freely, add or remove desks
}


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        # seed default config only if empty
        cur = conn.execute("SELECT COUNT(*) as c FROM config")
        if cur.fetchone()["c"] == 0:
            for k, v in DEFAULT_CONFIG.items():
                conn.execute(
                    "INSERT INTO config (key, value) VALUES (?, ?)",
                    (k, json.dumps(v)),
                )


def url_hash(url: str) -> str:
    return hashlib.sha256(url.strip().lower().encode()).hexdigest()


def get_config() -> dict:
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM config").fetchall()
        return {r["key"]: json.loads(r["value"]) for r in rows}


def set_config(key: str, value) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )


def article_exists(url: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM articles WHERE url_hash = ?", (url_hash(url),)
        ).fetchone()
        return row is not None


def insert_article(article: dict) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO articles
               (url, url_hash, title, source, published, summary, content)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                article["url"],
                url_hash(article["url"]),
                article.get("title"),
                article.get("source"),
                article.get("published"),
                article.get("summary"),
                article.get("content"),
            ),
        )
        return cur.lastrowid


def update_verdict(
    article_id: int,
    status: str,
    reason: str,
    confidence: float,
    stage: str,
    domain: str | None = None,
    proposal_reason: str | None = None,
    proposal_confidence: float | None = None,
    draft_headline: str | None = None,
    draft_article: str | None = None,
    provider: str | None = None,
    proposal_provider: str | None = None,
):
    with get_conn() as conn:
        conn.execute(
            """UPDATE articles
               SET status=?, reason=?, confidence=?, stage=?, domain=?,
                   proposal_reason=?, proposal_confidence=?, draft_headline=?, draft_article=?,
                   provider=?, proposal_provider=?
               WHERE id=?""",
            (status, reason, confidence, stage, domain, proposal_reason, proposal_confidence,
             draft_headline, draft_article, provider, proposal_provider, article_id),
        )


def list_articles(status: str | None = None, domain: str | None = None, limit: int = 200):
    clauses, params = [], []
    if status:
        clauses.append("status=?")
        params.append(status)
    if domain:
        clauses.append("domain=?")
        params.append(domain)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM articles {where} ORDER BY id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_article(article_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
        return dict(row) if row else None


def set_draft(article_id: int, headline: str | None, article: str | None, provider: str | None = None):
    with get_conn() as conn:
        conn.execute(
            "UPDATE articles SET draft_headline=?, draft_article=?, draft_provider=? WHERE id=?",
            (headline, article, provider, article_id),
        )


def manual_override(article_id: int, status: str, reason: str = "Manual override"):
    with get_conn() as conn:
        row = conn.execute(
            """SELECT domain, proposal_reason, proposal_confidence, draft_headline, draft_article,
                      provider, proposal_provider
               FROM articles WHERE id=?""",
            (article_id,),
        ).fetchone()
    existing = dict(row) if row else {}
    update_verdict(
        article_id, status, reason, confidence=1.0, stage="manual",
        domain=existing.get("domain"),
        proposal_reason=existing.get("proposal_reason"),
        proposal_confidence=existing.get("proposal_confidence"),
        draft_headline=existing.get("draft_headline"),
        draft_article=existing.get("draft_article"),
        provider=existing.get("provider"),
        proposal_provider=existing.get("proposal_provider"),
    )


def clear_articles() -> int:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM articles")
        return cur.rowcount
