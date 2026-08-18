import os
import secrets
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException, Request, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, JSONResponse
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

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
    if not os.environ.get("SECRET_KEY") or not os.environ.get("ADMIN_PASSWORD"):
        print("WARNING: SECRET_KEY and/or ADMIN_PASSWORD not set -- see .env.example. The app will still start, but no one (including you) will be able to log in.")


# ---------- Admin authentication ----------
#
# The whole app (dashboard + every /api/* route) is gated behind a single
# admin login -- this curates and drafts content before anything is
# published elsewhere, so it isn't meant to be publicly reachable at all.
#
# SessionMiddleware signs the session cookie with SECRET_KEY (itsdangerous
# under the hood) so a client can't forge or tamper with it; https_only is
# only enforced in Vercel's production environment so local http:// dev
# still works. AuthMiddleware then just checks one flag in that session.
#
# secrets.compare_digest (not ==) on both username and password so a wrong
# guess can't be timed to leak how many characters matched.
PUBLIC_PATHS = {"/login"}

class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith("/static/"):
            return await call_next(request)
        if not request.session.get("authenticated"):
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Not authenticated"}, status_code=401)
            return RedirectResponse("/login", status_code=303)
        return await call_next(request)


# Middleware order matters in Starlette: the last one added is outermost,
# so it runs first on the way in. SessionMiddleware must run before
# AuthMiddleware touches request.session, so it's added second (outer).
app.add_middleware(AuthMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SECRET_KEY", "insecure-dev-key-set-SECRET_KEY-in-.env"),
    same_site="lax",
    https_only=os.environ.get("VERCEL_ENV") == "production",
)


LOGIN_PAGE = """<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<title>SAFIRCOM AI Journalist -- Login</title>
<style>
  body {{ font-family: system-ui, sans-serif; background: #0f172a; color: #e2e8f0;
         display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }}
  form {{ background: #1e293b; padding: 2.5rem; border-radius: 12px; width: 320px; box-shadow: 0 10px 30px rgba(0,0,0,.3); }}
  h1 {{ font-size: 1.1rem; margin: 0 0 1.5rem; text-align: center; }}
  label {{ display: block; font-size: .85rem; margin-bottom: .3rem; color: #94a3b8; }}
  input {{ width: 100%; padding: .6rem; margin-bottom: 1rem; border-radius: 6px; border: 1px solid #334155;
          background: #0f172a; color: #e2e8f0; box-sizing: border-box; font-size: 1rem; }}
  button {{ width: 100%; padding: .7rem; border-radius: 6px; border: none; background: #6366f1;
           color: white; font-size: 1rem; cursor: pointer; }}
  button:hover {{ background: #4f46e5; }}
  .error {{ color: #f87171; font-size: .85rem; margin-bottom: 1rem; text-align: center; }}
</style>
</head>
<body>
<form method="post" action="/login">
  <h1>SAFIRCOM AI Journalist -- Admin Login</h1>
  {error_html}
  <label for="username">Username</label>
  <input type="text" id="username" name="username" autocomplete="username" required autofocus>
  <label for="password">Password</label>
  <input type="password" id="password" name="password" autocomplete="current-password" required>
  <button type="submit">Log in</button>
</form>
</body>
</html>"""


@app.get("/login", response_class=HTMLResponse)
def login_page(error: str | None = None):
    error_html = '<div class="error">Invalid username or password.</div>' if error else ""
    return LOGIN_PAGE.format(error_html=error_html)


@app.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    admin_user = os.environ.get("ADMIN_USERNAME", "")
    admin_pass = os.environ.get("ADMIN_PASSWORD", "")
    valid = (
        admin_pass != ""
        and secrets.compare_digest(username, admin_user)
        and secrets.compare_digest(password, admin_pass)
    )
    if valid:
        request.session["authenticated"] = True
        return RedirectResponse("/", status_code=303)
    return RedirectResponse("/login?error=1", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


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
def run(
    deadline_seconds: float = pipeline.DEFAULT_DEADLINE_SECONDS,
    max_new_candidates: int = pipeline.DEFAULT_MAX_NEW_CANDIDATES,
):
    """
    Optional ?deadline_seconds= and ?max_new_candidates= query params
    override the defaults -- e.g. a higher deadline if you're on Vercel
    Pro's longer function timeout. Always bounded over HTTP on purpose
    (unlike a direct pipeline.run_pipeline() call, which accepts None for a
    genuinely unbounded local run) -- an internet-exposed endpoint shouldn't
    have a way to disable its own timeout protection.
    """
    try:
        return pipeline.run_pipeline(deadline_seconds=deadline_seconds, max_new_candidates=max_new_candidates)
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
