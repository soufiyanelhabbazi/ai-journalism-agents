import os
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import db, pipeline, supervisor

app = FastAPI(title="SAFIRCOM AI Journalist")

STATIC_DIR = Path(__file__).parent.parent / "static"


@app.on_event("startup")
def startup():
    db.init_db()
    if not os.environ.get("GEMINI_API_KEY"):
        print("WARNING: GEMINI_API_KEY is not set. Copy .env.example to .env and add your free key from aistudio.google.com/apikey")
    if not os.environ.get("GROQ_API_KEY"):
        print("NOTE: GROQ_API_KEY is not set -- judgment calls (desk + editor) run Gemini-only, with no fallback once its rate limit is hit. Optional free key from console.groq.com/keys.")


# ---------- Config ----------

class Domain(BaseModel):
    name: str
    rubric: str  # this desk's own fit/quality standard, judged by the specialist agent


class ConfigUpdate(BaseModel):
    feeds: list[str] | None = None
    banned_domains: list[str] | None = None
    min_word_count: int | None = None
    exclude_keywords: list[str] | None = None
    require_attribution: bool | None = None
    rubric: str | None = None  # the editor-in-chief's universal standard
    domains: list[Domain] | None = None


@app.get("/api/config")
def get_config():
    return db.get_config()


@app.post("/api/config")
def update_config(payload: ConfigUpdate):
    data = payload.dict(exclude_unset=True)
    for key, value in data.items():
        db.set_config(key, value)
    return db.get_config()


# ---------- Pipeline ----------

@app.post("/api/run")
def run():
    try:
        result = pipeline.run_pipeline()
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------- Articles ----------

@app.get("/api/articles")
def get_articles(status: str | None = None, domain: str | None = None):
    return db.list_articles(status=status, domain=domain)


class OverridePayload(BaseModel):
    status: str  # accepted | rejected


@app.post("/api/articles/{article_id}/override")
def override(article_id: int, payload: OverridePayload):
    if payload.status not in ("accepted", "rejected"):
        raise HTTPException(status_code=400, detail="status must be accepted or rejected")
    db.manual_override(article_id, payload.status)
    return {"ok": True}


@app.delete("/api/articles")
def clear_articles():
    deleted = db.clear_articles()
    return {"deleted": deleted}


@app.post("/api/articles/{article_id}/draft")
def generate_draft(article_id: int):
    article = db.get_article(article_id)
    if article is None:
        raise HTTPException(status_code=404, detail="Article not found")
    if article["status"] != "accepted":
        raise HTTPException(status_code=400, detail="Only accepted articles can have a draft generated")
    try:
        draft = supervisor.write_article(article, domain=article.get("domain"))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Draft generation failed: {e}")
    db.set_draft(article_id, draft.get("headline"), draft.get("article"), draft.get("provider"))
    return {
        "draft_headline": draft.get("headline"),
        "draft_article": draft.get("article"),
        "draft_provider": draft.get("provider"),
    }


# ---------- Dashboard (static) ----------

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")
