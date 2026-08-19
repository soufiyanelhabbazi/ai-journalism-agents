"""
Turso (remote, SQLite-compatible) persistence layer.
No ORM on purpose -- keeps the prototype easy to read.

Was local SQLite; moved to Turso because a serverless host (e.g. Vercel)
gives each request an isolated, ephemeral filesystem -- nothing written to
local disk survives between invocations, so a local .db file silently loses
every write. Turso is the same SQL dialect over the network, which is why
almost nothing below changed except how a connection is obtained.
"""
import os
import json
import hashlib
from contextlib import contextmanager

import turso_serverless

from .env import env

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


class _Cursor:
    """
    Wraps a turso_serverless cursor so rows come back as dicts, mirroring
    what sqlite3.Row + row_factory used to give every function in this file
    for free (row["key"] access, dict(row) copies). turso_serverless returns
    plain tuples plus a DB-API-style cursor.description; this just zips the
    two back together rather than reworking every call site in this module.
    """
    def __init__(self, cursor):
        self._cursor = cursor

    def _to_dict(self, row):
        if row is None:
            return None
        columns = [d[0] for d in self._cursor.description]
        return dict(zip(columns, row))

    def fetchone(self):
        return self._to_dict(self._cursor.fetchone())

    def fetchall(self):
        return [self._to_dict(r) for r in self._cursor.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())

    @property
    def lastrowid(self):
        return self._cursor.lastrowid

    @property
    def rowcount(self):
        return self._cursor.rowcount


class _Connection:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        cur = self._conn.execute(sql, params) if params else self._conn.execute(sql)
        return _Cursor(cur)

    def executescript(self, script):
        self._conn.executescript(script)

    def commit(self):
        self._conn.commit()


@contextmanager
def get_conn():
    # env() (not os.environ[...]) so a token pasted with a trailing newline
    # into a hosting dashboard doesn't produce a baffling auth failure, and
    # so a missing one raises a sentence a human can act on rather than a
    # bare KeyError from deep inside a request.
    url = env("TURSO_DATABASE_URL")
    token = env("TURSO_AUTH_TOKEN")
    if not url or not token:
        raise RuntimeError(
            "TURSO_DATABASE_URL and TURSO_AUTH_TOKEN must both be set -- see "
            ".env.example. On Vercel these live in Settings -> Environment Variables."
        )
    raw = turso_serverless.connect(url, auth_token=token)
    conn = _Connection(raw)
    yield conn
    conn.commit()


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


def existing_url_hashes() -> set[str]:
    """
    All known url_hashes in one round trip.

    Every get_conn() here opens a fresh HTTP connection to Turso, so the old
    per-candidate article_exists() call cost one network round trip *per
    candidate* -- ~60 of them per run, which was a large slice of the
    function's execution budget spent purely on dedupe. The whole hash set
    is small (64 chars a row) and the pipeline checks every candidate
    against it anyway, so fetching it once is strictly cheaper.
    """
    with get_conn() as conn:
        rows = conn.execute("SELECT url_hash FROM articles").fetchall()
        return {r["url_hash"] for r in rows}


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


def status_counts() -> dict:
    """{status: count} for the whole table -- one small query rather than
    pulling every row's full body just to count them."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM articles GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}


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
