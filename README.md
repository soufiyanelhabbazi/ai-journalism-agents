# SAFIRCOM AI Journalist (prototype)
Owned by Btissam Machkour

A minimal multi-agent pipeline for journalism curation, built to run entirely on free infrastructure:

- **Scout agents** pull candidate articles from RSS feeds you configure (free — no API needed).
- **Domain desk agents** — you define one or more desks (Politics, Sports, Art & Culture, ...), each with its own rubric. A specialist agent picks the single best-fit desk for an article by actual subject matter (not keyword matching) and judges it against that desk's specific standard.
- **Editor-in-chief agent** — reviews only what a desk *proposes to accept*, independently checking it against a universal rubric before ratifying. It's told what the desk proposed but instructed to verify rather than defer, and can veto (in testing, it overturns roughly 1 in 5 proposed accepts — usually for weak sourcing or truncated content the desk's narrower fit-check didn't catch).
- **Staff-writer agent** — on demand only (a **Generate Article** button on accepted cards, not automatic), writes an original, publish-ready article in the voice of a professional Moroccan journalist: same facts as the source, own wording and structure, not a copy or light paraphrase, anchored to a house style example baked into the prompt. Shown in place of the desk's short proposal note, with a one-click copy button. A vetoed or rejected article never gets this button.
- All of this sits behind a free, deterministic **rule stage** first — banned domains, minimum word count, excluded keywords, an attribution heuristic, near-duplicate detection — so the LLM agents only ever see genuinely ambiguous cases.
- **Dashboard** — configure desks and standards, trigger a run, and review every verdict, including the desk's original proposal (or drafted article) alongside the editor's final call (with the ability to manually override). Each verdict shows which LLM provider produced it (see below).

A desk's rejection is final on its own — the editor only reviews proposed *accepts*, not the (usually larger) rejected pool. That keeps LLM call volume close to one call per article on average for the judgment stages. If no domains are configured, review collapses to a single editor-only pass against the universal rubric.

**Provider fallback, scoped deliberately by agent.** The **desk and editor** calls fall through to Groq (a separate free-tier account/quota pool) once Gemini's own retries are exhausted for that call — real additive capacity, not a workaround of the same limit. The **staff-writer never falls back**: free-form prose from two different models would read as two visibly different voices across your published articles, which matters far more for writing than for a structured accept/reject verdict bounded by a rubric. If Gemini can't currently write a draft, the button just fails with a retry option rather than silently swapping in a different writer. Every verdict and draft records which provider actually produced it — shown as a small "via Gemini"/"via Groq" tag next to each desk/editor step and on the drafted article — so any stylistic drift is auditable, not hidden. `GROQ_API_KEY` is optional; leave it unset to run Gemini-only everywhere, exactly as before.

This is a prototype meant to be extended, not a finished product. Everything is deliberately simple and readable so you can see exactly how each piece works.

## 1. Setup

```bash
cd journalism-agents
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# then edit .env and add your GEMINI_API_KEY (required) and optionally GROQ_API_KEY (fallback)
```

Get a **free** Gemini key at https://aistudio.google.com/apikey — no credit card required. Check https://ai.google.dev/gemini-api/docs/rate-limits for current free-tier limits, since these change over time; the real numbers as observed from an actual account dashboard were 15 requests/minute, 250K tokens/minute, and 500 requests/day for `gemini-3.5-flash-lite` — RPM and RPD are the binding constraints, TPM has substantial headroom.

Optionally, get a **free** Groq key at https://console.groq.com/keys to enable the judgment-stage fallback described above (desk + editor only, never the writer). Leave `GROQ_API_KEY` unset to run Gemini-only. (We also looked at Cerebras as a fallback, but its free tier requires a verified payment method to activate API access at all, which neither Gemini nor Groq do.)

**Want it fully local/offline instead (zero rate limits, needs a decent GPU)?** Install [Ollama](https://ollama.com), pull a model (`ollama pull qwen2.5:14b` is a good precision/speed balance), then in `app/supervisor.py` change `LLM_BASE_URL` to `http://localhost:11434/v1`, `LLM_MODEL` to `"qwen2.5:14b"`, and the API key can be any placeholder string. Everything else works unchanged since Ollama speaks the same OpenAI-compatible API.

## 2. Run it

```bash
uvicorn app.main:app --reload
```

Open **http://localhost:8000** — that's the dashboard.

## 3. First run

1. In **Domain Desks**, review/edit the pre-loaded desks (Politics, Sports, Art & Culture) or add your own — each desk's rubric is what its specialist agent judges fit/quality against. Delete all desks to fall back to a single editor-only pass with no domain routing.
2. In **Editor-in-Chief**, review/edit the **universal standard** (this is what every desk's proposed accepts get checked against before they're final — be as specific as you want: sourcing rigor, tone, completeness, what to reject regardless of topic).
3. Tune the **free rule fields** — excluded keywords, the attribution checkbox, minimum word count. The tighter these are, the fewer articles need any LLM call at all.
4. Add or remove **RSS feed URLs** under Sources — a few defaults are pre-loaded so you can test immediately.
5. Click **Save Standards**.
6. Click **Run Scouts** — this fetches new articles from all feeds, runs them through the desks and the editor, and populates the feed below with stamped verdicts and the reasoning behind each one (including the desk's original proposal when the editor's final call differs from it).
7. Use the **desk filter dropdown** next to the status tabs to see one desk's queue at a time. Use **Force Accept / Force Reject** on any card to override the pipeline — useful for spotting where a desk's rubric or the universal standard needs tightening.

Re-running is safe: already-seen URLs are automatically skipped (deduped by URL hash), so you can run it on a schedule (cron, or a simple `while true; do curl -X POST localhost:8000/api/run; sleep 900; done`) to keep collecting.

## How it's structured

```
app/
  main.py        FastAPI routes (config, run, articles, overrides)
  db.py           SQLite persistence — articles + config, no ORM
  scouts.py       RSS fetching & normalization
  supervisor.py   Rule checks + domain specialist + editor-in-chief + staff-writer agents
  pipeline.py     Orchestrates scouts -> dedupe -> supervisor -> storage
static/
  index.html, style.css, app.js    The dashboard (vanilla JS, no build step)
```

## Where to extend next

- **More scout types**: add a function to `scouts.py` for a news API (e.g. GDELT, NewsAPI) or a specific site's sitemap, and merge results into `run_pipeline()`.
- **Smarter dedupe**: current dedupe is exact URL match. For near-duplicate stories covered by multiple outlets, add an embedding-similarity check (e.g. via the Claude API or a local embedding model) before insert.
- **Per-domain rule tuning**: the rule stage (word count, attribution, etc.) is still global across all desks. If one desk needs different thresholds (e.g. shorter minimum length for breaking sports news than for investigative politics pieces), that needs per-domain rule config, not just per-domain rubric.
- **Editor escalation**: right now a veto is final and silent -- the desk never learns it was overruled. A real feedback loop (editor sends specifics back, desk gets a chance to respond) is where an orchestration framework like LangGraph starts to pay off, since you'd want a real state machine with retries, not a single function call.
- **Scheduling**: wrap `run_pipeline()` in a cron job or a lightweight scheduler (APScheduler) so it runs automatically instead of via the button.
- **Auth & multi-user**: none of that exists yet — this is single-user/local by design.
- **Swap SQLite for Postgres** once you need concurrent writers or want to deploy this somewhere persistent.
