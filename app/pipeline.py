"""
Orchestrates one end-to-end pipeline run:
  scouts -> dedupe against DB -> supervisor review -> store verdict
"""
from . import db, scouts, supervisor


def run_pipeline() -> dict:
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
    # (e.g. the API hit a rate limit mid-batch) before pulling in new candidates.
    for pending in db.list_articles(status="pending"):
        review_and_store(pending, pending["id"])
        recent_titles.append(pending["title"])

    for candidate in candidates:
        if db.article_exists(candidate["url"]):
            continue  # dedupe: already seen this URL before

        article_id = db.insert_article(candidate)
        if article_id == 0:
            continue  # race with the UNIQUE constraint, skip

        new_count += 1
        review_and_store(candidate, article_id)
        recent_titles.append(candidate["title"])  # so within-batch dupes are also caught, but only *after* this one's own check

    return {
        "candidates_seen": len(candidates),
        "new_articles": new_count,
        "accepted": accepted_count,
        "rejected": rejected_count,
        "scout_errors": scout_errors,
        "review_errors": review_errors,
    }
