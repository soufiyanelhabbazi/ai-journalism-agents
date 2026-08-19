"""
Supervisor agent.

Review pipeline, cheapest checks first:
  1. rule_check       -- fast, free, deterministic. Handles as much as
                          possible so the LLM stages only see genuinely
                          ambiguous cases. This is where a lot of your real
                          precision comes from -- a good rubric matters, but
                          a tight rule stage matters just as much and costs
                          nothing.
  2. specialist_review -- one domain-desk agent (Politics/Sports/Art & Culture/
                          ...you define) picks the single best-fit desk for
                          the article and judges it against that desk's own
                          standard. A proposed rejection is final -- it never
                          reaches stage 3.
  3. editor_review     -- only for proposed *accepts*: an editor-in-chief
                          agent independently checks the article against your
                          universal rubric before ratifying it. This is a
                          real check, not a rubber stamp -- it's told what
                          the desk proposed but instructed to verify rather
                          than defer.

Articles that fail stage 1 never reach stage 2. Gating the editor pass on
proposed-accepts only (rather than reviewing everything twice) keeps LLM call
volume close to one call per article on average instead of doubling it --
worth protecting deliberately, since the free tier's requests-per-minute cap
is the tightest constraint this app runs into in practice. If no domains are
configured, stages 2/3 collapse into a single editor-only pass against the
universal rubric (the original single-agent behavior).

Why Gemini: the free tier needs no credit card (unlike some other free/fast
providers, e.g. Cerebras, which gate API access behind payment verification),
and it's an OpenAI-compatible API. We use the "-latest" alias rather than a
dated model name (e.g. gemini-2.5-flash) because Google retires dated models
for new API keys with no notice -- the alias is Google's own mechanism for
avoiding that breakage, and it consistently allowed a far higher free-tier
requests-per-minute rate in testing than the dated flash models did.

Fallback: judgment calls (specialist + editor) fall through to Groq -- a
genuinely separate quota pool -- once Gemini's own retries are exhausted.
The writer stage deliberately does NOT get this fallback: free-form prose
from two different models would read as two different voices across your
published articles, which matters far more for writing than for a
structured accept/reject verdict. If Gemini can't currently serve a draft,
the dashboard just shows a retry, rather than silently substituting a
different writer. Every verdict/draft records which provider actually
produced it (see the "provider" fields) so this is auditable, not hidden.
GROQ_API_KEY is optional -- with none set, that fallback entry just fails
fast and falls through, leaving Gemini-only behavior for judgment too.
"""
import os
import re
import json
import time
import difflib
from openai import OpenAI, RateLimitError

from .env import env

_gemini_client = None
_groq_client = None

LLM_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
LLM_MODEL = env("GEMINI_MODEL", "gemini-flash-lite-latest")

# Groq retires model names on a published schedule and starts 404ing them
# ("model_not_found") with no grace period -- llama-3.3-70b-versatile, the
# original pick here, was decommissioned and silently took the entire
# fallback path down with it. Overridable by env so a future retirement is
# a dashboard edit rather than a redeploy; /api/health lists what the key
# can currently reach.
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_MODEL = env("GROQ_MODEL", "openai/gpt-oss-120b")


class AllProvidersFailedError(RuntimeError):
    """Every configured provider failed; the message names each one's error."""


def _check_api_key_shape(name: str, key: str) -> None:
    """
    Reject a key that can't possibly work *before* it becomes an HTTP header.

    An API key is sent as `Authorization: Bearer <key>`, and header values
    can't contain newlines. If one does, h11 refuses to serialize the
    request and the OpenAI SDK reports the result as a bare
    "Connection error." -- which reads as a network outage and sends you
    looking in entirely the wrong place. That is exactly what happened in
    production: GEMINI_API_KEY had been set to the contents of a Google
    service-account JSON file (~1500 chars, multi-line) instead of an AI
    Studio API key, and every judgment call failed for days while the feed
    fetching beside it worked perfectly.

    A wrong-but-well-formed key is left alone -- the provider's own 400
    ("Please pass a valid API key") is already a clear answer.
    """
    if len(key.split()) != 1:
        raise RuntimeError(
            f"{name} contains whitespace or line breaks, so it cannot be sent as an "
            f"HTTP header ({len(key)} chars). This usually means a JSON key file or a "
            f"multi-line block was pasted in instead of the API key itself. Set it to "
            f"the single-line key string and redeploy."
        )


def get_client() -> OpenAI:
    global _gemini_client
    if _gemini_client is None:
        key = env("GEMINI_API_KEY")
        if not key:
            raise RuntimeError(
                "GEMINI_API_KEY is not set. Add it in your host's environment "
                "variables (Vercel: Settings -> Environment Variables) and redeploy. "
                "Free key, no credit card: https://aistudio.google.com/apikey"
            )
        _check_api_key_shape("GEMINI_API_KEY", key)
        _gemini_client = OpenAI(api_key=key, base_url=LLM_BASE_URL)
    return _gemini_client


def get_groq_client() -> OpenAI:
    global _groq_client
    if _groq_client is None:
        key = env("GROQ_API_KEY")
        # Groq is the optional fallback -- an unset key is a normal
        # configuration, so this fails fast and lets the caller move on
        # rather than being dressed up as an outage.
        if not key or key == "your-key-here":
            raise RuntimeError("GROQ_API_KEY is not set (optional fallback provider)")
        _check_api_key_shape("GROQ_API_KEY", key)
        _groq_client = OpenAI(api_key=key, base_url=GROQ_BASE_URL)
    return _groq_client


# ---------------------------------------------------------------------------
# Stage 1: free, deterministic rule checks
# ---------------------------------------------------------------------------

# A crude but useful attribution heuristic: does the text contain language
# that signals a named source, quote, or official statement? Articles with
# zero attribution signals are disproportionately likely to be opinion,
# rumor, or unsourced aggregation -- exactly what most rubrics want rejected.
#
# Covers English signals plus Arabic equivalents (قال/أفاد/صرح-family verbs
# with the common و/ف conjunction prefix, حسب/بحسب "according to", بيان/تصريح
# "statement", متحدث/الناطق "spokesperson") and quote pairs using ASCII,
# curly, or Arabic guillemet quotation marks.
ATTRIBUTION_PATTERNS = re.compile(
    r'\bsaid\b|\baccording to\b|\btold\b|\bspokesperson\b|\bstatement\b'
    r'|(?:^|\s)و?(?:قال|قالت|قالوا|يقول|تقول|أضاف|أضافت|صرح|صرّح|صرحت|صرّحت'
    r'|أفاد|أفادت|أكد|أكدت|ذكر|ذكرت|أعلن|أعلنت|كشف|كشفت|أخبر|أخبرت|أبلغ|أبلغت'
    r'|أوضح|أوضحت)\b'
    r'|(?:^|\s)و?(?:حسب|بحسب|وفق|وفقا|وفقاً|حسبما|بيان|تصريح|تصريحات|متحدث|متحدثة|الناطق|الناطقة)\b'
    r'|["«“][^"»”]{15,}["»”]',
    re.IGNORECASE,
)


def _title_is_near_duplicate(title: str, recent_titles: list[str], threshold: float = 0.82) -> str | None:
    for existing in recent_titles:
        ratio = difflib.SequenceMatcher(None, title.lower(), existing.lower()).ratio()
        if ratio >= threshold:
            return existing
    return None


def rule_check(article: dict, config: dict, recent_titles: list[str]) -> dict | None:
    """
    Returns a verdict dict if the rule stage can decide on its own, or None
    if the article should be passed on to the LLM stage.
    """
    url = article["url"].lower()
    title = article.get("title", "")
    body = article.get("content") or article.get("summary") or ""

    for domain in config.get("banned_domains", []):
        if domain.lower() in url:
            return {"decision": "rejected", "reason": f"Source domain '{domain}' is banned", "confidence": 1.0}

    word_count = len(body.split())
    min_words = config.get("min_word_count", 150)
    if word_count < min_words:
        return {
            "decision": "rejected",
            "reason": f"Only {word_count} words, below the {min_words} minimum",
            "confidence": 1.0,
        }

    exclude_keywords = [k.lower() for k in config.get("exclude_keywords", []) if k.strip()]
    if exclude_keywords:
        haystack = (title + " " + body).lower()
        hit = next((k for k in exclude_keywords if k in haystack), None)
        if hit:
            return {"decision": "rejected", "reason": f"Matches excluded keyword '{hit}'", "confidence": 1.0}

    if config.get("require_attribution", True) and not ATTRIBUTION_PATTERNS.search(body):
        return {
            "decision": "rejected",
            "reason": "No attribution signal found (no quotes, 'said', 'according to', etc.) — likely unsourced",
            "confidence": 0.85,
        }

    dup = _title_is_near_duplicate(title, recent_titles)
    if dup:
        return {
            "decision": "rejected",
            "reason": f"Near-duplicate of a recently seen title: \"{dup[:80]}\"",
            "confidence": 0.9,
        }

    return None  # ambiguous -- send to the LLM


# ---------------------------------------------------------------------------
# Stage 2: domain specialist + editor-in-chief
#
# Mirrors a real newsroom: a domain desk (Politics/Sports/Art & Culture/...)
# proposes whether an article fits its beat and meets that beat's standard;
# only proposed *accepts* go on to the editor-in-chief, who independently
# checks the article against the universal rubric before it's final. A
# specialist's rejection is final on its own -- a second opinion adds nothing
# for an article that's already excluded, and skipping it keeps LLM call
# volume close to a single call per article on average rather than doubling
# it, which matters a lot on a rate-limited free tier.
#
# If no domains are configured, this degrades to a single editor-only pass
# against the universal rubric (the original single-agent behavior).
# ---------------------------------------------------------------------------

EDITOR_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_verdict",
        "description": "Submit the final editorial verdict for this article.",
        "parameters": {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "enum": ["accepted", "rejected"]},
                "reason": {"type": "string", "description": "One to two sentence justification, citing the standards. Written in the same language as the article."},
                "confidence": {"type": "number", "description": "0 to 1 confidence in this decision."},
            },
            "required": ["decision", "reason", "confidence"],
        },
    },
}


def _article_block(article: dict) -> str:
    body = (article.get("content") or article.get("summary") or "")[:4000]
    return f"""Title: {article.get('title')}
Source: {article.get('source')}
URL: {article.get('url')}
---
{body}
---"""


# Judgment calls (specialist + editor) get a Groq fallback -- structured,
# schema-bounded output, so cross-model variance is low-risk. The writer
# stays Gemini-only on purpose (see module docstring): free-form prose is
# exactly where a second voice would actually be noticeable to a reader.
JUDGMENT_PROVIDERS = [
    (get_client, LLM_MODEL, "Gemini"),
    (get_groq_client, GROQ_MODEL, "Groq"),
]
WRITER_PROVIDERS = [
    (get_client, LLM_MODEL, "Gemini"),
]


def _create_with_fallback(providers, **kwargs):
    """
    Try each (get_client_fn, model_name, label) in order. Within a provider,
    a rate-limited call gets retried a couple of times with backoff first (a
    batch run can burst through the free tier's requests-per-minute cap even
    with a generous model choice) -- only once that's exhausted does this
    fall through to the next provider. A non-rate-limit failure (e.g. a
    missing API key for an unconfigured fallback provider) skips straight to
    the next provider instead of wasting retries on something backoff can't
    fix. Returns (response, label) so callers can record which provider
    actually served the request. The caller leaves the article 'pending' on
    total failure, so nothing is lost even if every provider is out of quota.
    """
    failures = []  # one (label, error) per provider, in the order tried
    for get_client_fn, model_name, label in providers:
        max_attempts = 3
        provider_error = None
        for attempt in range(max_attempts):
            try:
                client = get_client_fn()
                response = client.chat.completions.create(model=model_name, **kwargs)
                return response, label
            except RateLimitError as e:
                provider_error = e
                if attempt < max_attempts - 1:
                    time.sleep(2 ** attempt * 5)  # 5s, 10s
            except Exception as e:
                provider_error = e
                if _is_transient(e) and attempt < max_attempts - 1:
                    time.sleep(1)
                    continue
                break  # retrying this provider won't help
        failures.append(f"{label} ({model_name}): {provider_error}")

    # Report *every* provider's failure, not just the last one. Raising only
    # the final error meant the fallback provider's message overwrote the
    # primary's -- production spent a long time looking like a dead Groq
    # model when the actual fault was Gemini failing first, invisibly.
    raise AllProvidersFailedError("All providers failed -- " + " | ".join(failures))


# Every judgment call here is made with tool_choice forcing a specific
# function, but a model can still return an empty completion -- Groq answers
# that with a 400 "tool_use_failed: model did not call a tool". It's a
# sampling fluke, not a configuration problem, and a plain retry clears it,
# so it shouldn't be lumped in with the permanent 400s that make retrying
# pointless. Also covers server-side blips worth one more attempt.
_TRANSIENT_MARKERS = ("tool_use_failed", "internal_server_error", "service_unavailable")


def _is_transient(error: Exception) -> bool:
    message = str(error).lower()
    return any(marker in message for marker in _TRANSIENT_MARKERS)


def _parse_tool_call(response, fallback_reason: str) -> dict:
    message = response.choices[0].message
    if message.tool_calls:
        try:
            return json.loads(message.tool_calls[0].function.arguments)
        except (json.JSONDecodeError, IndexError):
            pass
    return {"decision": "rejected", "reason": fallback_reason, "confidence": 0.0}


def specialist_review(article: dict, domains: list[dict]) -> dict:
    """Ask a domain-desk agent which desk this article fits and whether it meets that desk's standard."""
    desk_listing = "\n".join(f"- {d['name']}: {d['rubric']}" for d in domains)
    prompt = f"""You are triaging incoming articles for a newsroom with the following desks. Read the article, decide which single desk it best fits by actual subject matter -- not by whether a desk's name literally appears in the text -- and judge whether it meets that desk's specific standard.

DESKS:
{desk_listing}

ARTICLE TO REVIEW:
{_article_block(article)}

Pick exactly one desk from the list above and call propose_domain_verdict with your pick and verdict. Write the "reason" in the same language as the article itself (e.g. Arabic if the article is in Arabic) -- never translate it to English."""

    tool = {
        "type": "function",
        "function": {
            "name": "propose_domain_verdict",
            "description": "Propose which domain desk this article belongs to and whether it meets that desk's standard.",
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {"type": "string", "enum": [d["name"] for d in domains]},
                    "decision": {"type": "string", "enum": ["accepted", "rejected"]},
                    "reason": {"type": "string", "description": "One to two sentence justification citing the chosen desk's standard. Written in the same language as the article."},
                    "confidence": {"type": "number", "description": "0 to 1 confidence in this proposal."},
                },
                "required": ["domain", "decision", "reason", "confidence"],
            },
        },
    }

    response, provider = _create_with_fallback(
        JUDGMENT_PROVIDERS,
        max_tokens=500,
        temperature=0.1,
        tools=[tool],
        tool_choice={"type": "function", "function": {"name": "propose_domain_verdict"}},
        messages=[{"role": "user", "content": prompt}],
    )
    result = _parse_tool_call(response, "Specialist failed to produce a parseable proposal")
    result["provider"] = provider
    return result


def editor_review(article: dict, rubric: str, domain: str | None = None, specialist_reason: str | None = None) -> dict:
    """
    Ask the editor-in-chief agent for the final verdict against the universal
    rubric. If a domain specialist already proposed an accept, the editor is
    told that proposal but explicitly instructed to verify rather than defer
    to it -- this is the actual quality gate, not a rubber stamp.
    """
    if domain and specialist_reason:
        context = f"""The "{domain}" desk has proposed ACCEPTING this article, with this justification:
"{specialist_reason}"

Do not simply defer to the desk's judgment -- independently verify the article meets the standards below before ratifying. You may overrule the desk if it doesn't."""
    else:
        context = "Review this article independently against the standards below."

    prompt = f"""You are the editor-in-chief giving final sign-off before publication. Be precise: when in doubt, favor rejecting borderline material over accepting it, and always justify your decision against the standards.

{context}

UNIVERSAL EDITORIAL STANDARDS:
{rubric}

ARTICLE TO REVIEW:
{_article_block(article)}

Call submit_verdict with your final decision. Write the "reason" in the same language as the article itself (e.g. Arabic if the article is in Arabic) -- never translate it to English."""

    response, provider = _create_with_fallback(
        JUDGMENT_PROVIDERS,
        max_tokens=500,
        temperature=0.1,
        tools=[EDITOR_TOOL],
        tool_choice={"type": "function", "function": {"name": "submit_verdict"}},
        messages=[{"role": "user", "content": prompt}],
    )
    result = _parse_tool_call(response, "Editor failed to produce a parseable verdict")
    result["provider"] = provider
    return result


# ---------------------------------------------------------------------------
# Stage 4: staff writer -- only for articles that make it all the way to a
# final accept. Writing a full article costs far more output tokens than a
# short judgment, so this deliberately runs on the smallest possible subset
# (confirmed accepts), the same cost-gating principle behind skipping the
# editor pass on desk rejections.
# ---------------------------------------------------------------------------

WRITER_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_draft",
        "description": "Submit a publish-ready, originally-written article.",
        "parameters": {
            "type": "object",
            "properties": {
                "headline": {"type": "string", "description": "An original headline in the article's own language -- not copied from the source title."},
                "article": {"type": "string", "description": "The full original article body, ready to publish, in the article's own language."},
            },
            "required": ["headline", "article"],
        },
    },
}

# A fabricated (not real) example anchoring the target register and structure
# -- inverted pyramid, formal attribution phrasing ("وأوضح المصدر ذاته أن...")
# typical of professional Moroccan press. This narrows stylistic drift across
# runs/temperature, and is what would let a future second writer provider
# converge toward the same house voice rather than its own default style.
STYLE_EXAMPLE = (
    'أعلنت وزارة التربية الوطنية، في بلاغ رسمي، عن تأجيل الموسم الدراسي بأسبوع '
    'كامل بسبب الأحوال الجوية. وأوضح المصدر ذاته أن القرار جاء بعد تشاور مع '
    'النقابات التعليمية، مشيرا إلى أن الإدارات المحلية ستتولى تنظيم الحصص '
    'الاستدراكية لاحقا.'
)


def write_article(article: dict, domain: str | None = None) -> dict:
    """
    Ask a staff-writer agent to produce an original, publish-ready article
    covering the same story -- written in the voice of a professional
    Moroccan journalist, not a copy or light paraphrase of the source.
    """
    desk_context = f" for the {domain} desk" if domain else ""
    prompt = f"""You are a professional Moroccan journalist writing{desk_context} for a Moroccan digital news outlet. Below is a source article -- your job is to write an ORIGINAL news article covering the same story, ready to publish.

Hard rules:
- Write in the same language as the source article (Modern Standard Arabic / الفصحى for Arabic sources), in the register and structure of professional Moroccan press (e.g. Hespress, MAP, Al Ahdath) -- lead with the most newsworthy fact, then context, then supporting detail and any quotes.
- Do NOT copy sentences or phrasing from the source. Express the same facts in your own original wording and structure, as if you independently reported this story -- not as a paraphrase or summary of the original text.
- Do NOT invent facts, quotes, or details that are not present in the source. Stay strictly factually faithful -- only the expression is original, not the substance.
- Preserve attribution accurately -- if someone is quoted or a claim is attributed to a source in the original, keep that attribution correct in your version.
- Write an original headline, not the source's headline verbatim.
- No HTML, no "read more" links, no meta-commentary about the source -- just the headline and clean article text.

STYLE REFERENCE (tone and structure only -- this is a fabricated example, not a real event, and none of its facts belong in your article):
{STYLE_EXAMPLE}

SOURCE ARTICLE:
{_article_block(article)}

Call submit_draft with your original headline and article."""

    response, provider = _create_with_fallback(
        WRITER_PROVIDERS,
        max_tokens=2000,  # a real article needs far more room than a one-line verdict
        temperature=0.4,  # a bit more room than judgment calls for natural, varied prose
        tools=[WRITER_TOOL],
        tool_choice={"type": "function", "function": {"name": "submit_draft"}},
        messages=[{"role": "user", "content": prompt}],
    )
    result = _parse_tool_call(response, "Writer failed to produce a parseable draft")
    result["provider"] = provider
    return result


def review_article(article: dict, config: dict, recent_titles: list[str]) -> dict:
    """
    Run the full review and return a unified verdict dict with 'stage' and
    'domain' fields. Deliberately does NOT call write_article() -- writing is
    the most expensive step (long output, meaningfully more tokens than a
    judgment call), so it's triggered on demand per-article from the
    dashboard (POST /api/articles/{id}/draft) rather than automatically for
    every accept. A pipeline run is judgment-only.
    """
    rule_verdict = rule_check(article, config, recent_titles)
    if rule_verdict is not None:
        rule_verdict["stage"] = "rule"
        return rule_verdict

    universal_rubric = config.get("rubric", "")
    domains = [d for d in config.get("domains", []) if d.get("name") and d.get("rubric")]

    if not domains:
        verdict = editor_review(article, universal_rubric)
        verdict["stage"] = "llm"
        verdict["domain"] = None
        return verdict

    proposal = specialist_review(article, domains)
    if proposal.get("decision") != "accepted":
        proposal["stage"] = "specialist"
        proposal["proposal_reason"] = proposal.get("reason")
        proposal["proposal_confidence"] = proposal.get("confidence", 0.0)
        proposal["proposal_provider"] = proposal.get("provider")
        return proposal

    final = editor_review(
        article, universal_rubric,
        domain=proposal.get("domain"),
        specialist_reason=proposal.get("reason"),
    )
    final["stage"] = "editor"
    final["domain"] = proposal.get("domain")
    final["proposal_reason"] = proposal.get("reason")
    final["proposal_confidence"] = proposal.get("confidence", 0.0)
    final["proposal_provider"] = proposal.get("provider")
    return final
