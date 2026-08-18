"""
Orchestrates one end-to-end pipeline run:
  scouts -> dedupe against DB -> supervisor review -> store verdict
"""
import time
from . import db, scouts, supervisor

# Comfortably under Vercel Hobby's hard 300s function timeout, leaving
# margin for scout fetching, response serialization, and platform overhead.
# A run that hits this deadline stops itself gracefully and returns a normal
# partial response instead of being killed mid-batch by the platform, which
# used to strand articles as bare 'pending' rows with no verdict and no
# logged error. Override via the deadline_seconds param (e.g. a higher value
# on Vercel Pro's longer timeout, or None for genuinely unbounded local runs).
DEFAULT_DEADLINE_SECONDS = 240

# Caps how many *new* candidates one run pulls in and reviews (the
# pending-retry loop is exempt -- it only ever touches what's already
# stuck, so it's naturally self-limiting). This is a proxy for time, not a
# guarantee: a handful of rate-limited retries can still burn a lot of
# wall-clock, which is what the deadline above actually protects against.
# But a smaller, predictable batch means fewer chances to hit the rate
# limit in the first place, so the two are complementary, not redundant.
DEFAULT_MAX_NEW_CANDIDATES = 15


def run_pipeline(
    deadline_seconds: float | None = DEFAULT_DEADLINE_SECONDS,
    max_new_candidates: int | None = DEFAULT_MAX_NEW_CANDIDATES,
) -> dict:
    start = time.monotonic()

    def out_of_time() -> bool:
        return deadline_seconds is not None and (time.monotonic() - start) >= deadline_seconds

    config = db.get_config()
    feeds = config.get("feeds", [])

    candidates, scout_errors = scouts.run_scouts(feeds)

    # Recent titles feed the near-duplicate check in the rule stage. Pulling
    # this once up front (rather than per-article) keeps the pipeline fast.
    recent_titles = [a["title"] for a in db.list_articles(limit=300) if a.get("title")]

    new_count = 0
    accepted_count = 0
    rejected_count = 0
    review_errors = []
    deferred_count = 0  # candidates/pending left untouched this run because the deadline hit first

    def review_and_store(article, article_id):
        nonlocal accepted_count, rejected_count
        try:
            verdict = supervisor.review_article(article, config, recent_titles)
        except Exception as e:
            # Leave status as 'pending' -- a transient failure (rate limit,
            # network blip) shouldn't be recorded as a verdict. The next run
            # picks this article back up automatically, see below.
            review_errors.append({"url": article["url"], "error": str(e)})
            return
        db.update_verdict(
            article_id,
            status=verdict["decision"],
            reason=verdict["reason"],
            confidence=verdict.get("confidence", 0.0),
            stage=verdict["stage"],
            domain=verdict.get("domain"),
            proposal_reason=verdict.get("proposal_reason"),
            proposal_confidence=verdict.get("proposal_confidence"),
            draft_headline=verdict.get("draft_headline"),
            draft_article=verdict.get("draft_article"),
            provider=verdict.get("provider"),
            proposal_provider=verdict.get("proposal_provider"),
        )
        if verdict["decision"] == "accepted":
            accepted_count += 1
        else:
            rejected_count += 1

    # Retry anything left over from a previous run that never got a verdict
    # (e.g. the API hit a rate limit mid-batch, or -- the case this deadline
    # exists for -- the previous invocation ran out of time) before pulling
    # in new candidates. Deadline is checked *before* starting each review,
    # not mid-review -- an in-flight call still finishes normally, this just
    # stops new ones from starting once time is short.
    still_pending = db.list_articles(status="pending")
    for i, pending in enumerate(still_pending):
        if out_of_time():
            deferred_count += len(still_pending) - i
            break
        review_and_store(pending, pending["id"])
        recent_titles.append(pending["title"])

    if not out_of_time():
        for candidate in candidates:
            if out_of_time():
                deferred_count += 1
                continue
            if db.article_exists(candidate["url"]):
                continue  # dedupe: already seen this URL before -- free to check, doesn't count against the cap

            if max_new_candidates is not None and new_count >= max_new_candidates:
                deferred_count += 1
                continue  # cap reached -- not inserted, so it's picked up fresh (still "new") next run

            article_id = db.insert_article(candidate)
            if article_id == 0:
                continue  # race with the UNIQUE constraint, skip

            new_count += 1
            recent_titles.append(candidate["title"])  # so within-batch dupes are also caught, but only *after* this one's own check
            review_and_store(candidate, article_id)

    return {
        "candidates_seen": len(candidates),
        "new_articles": new_count,
        "accepted": accepted_count,
        "rejected": rejected_count,
        "scout_errors": scout_errors,
        "review_errors": review_errors,
        "deadline_reached": out_of_time(),
        "deferred": deferred_count,  # non-zero means: click Run Scouts again to keep draining the backlog
    }
