const el = (id) => document.getElementById(id);

let currentFilter = "";
let currentDomainFilter = "";

function setEditionDate() {
  const now = new Date();
  el("edition-date").textContent = now.toLocaleDateString("ar-MA", {
    weekday: "long", year: "numeric", month: "long", day: "numeric",
  });
}

function domainRowTemplate(name = "", rubric = "") {
  return `
    <div class="domain-row">
      <div class="domain-row-top">
        <input type="text" class="domain-name" placeholder="اسم المكتب (مثال: رياضة)" value="${escapeHtml(name)}">
        <button type="button" class="domain-remove" onclick="this.closest('.domain-row').remove()">✕</button>
      </div>
      <textarea class="domain-rubric" rows="2" placeholder="ما الذي يغطيه هذا المكتب...">${escapeHtml(rubric)}</textarea>
    </div>
  `;
}

function renderDomains(domains) {
  el("domains-list").innerHTML = (domains || []).map(d => domainRowTemplate(d.name, d.rubric)).join("");
}

function readDomainsFromForm() {
  return Array.from(document.querySelectorAll(".domain-row"))
    .map(row => ({
      name: row.querySelector(".domain-name").value.trim(),
      rubric: row.querySelector(".domain-rubric").value.trim(),
    }))
    .filter(d => d.name && d.rubric);
}

function populateDomainFilter(domains) {
  const select = el("domain-filter");
  const current = select.value;
  select.innerHTML = `<option value="">كل المكاتب</option>` +
    (domains || []).map(d => `<option value="${escapeHtml(d.name)}">${escapeHtml(d.name)}</option>`).join("");
  select.value = [...select.options].some(o => o.value === current) ? current : "";
}

async function loadConfig() {
  const res = await fetch("/api/config");
  const cfg = await res.json();
  applyConfig(cfg);
}

// ---------- Editions ----------
//
// Two source sets -- Moroccan press and Arabic sources -- each with its own
// feeds AND its own editorial standard. They are separate products: judging
// a pan-Arab wire by "news value for a Moroccan reader" would reject most of
// it, so the rubric travels with the edition rather than sitting above both.
//
// The whole sources object is held here and written back on save, because
// the form only ever shows one edition at a time; sending just the visible
// one would wipe the other.
let sources = {};
let activeSource = null;

function activeSet() {
  return sources[activeSource] || { label: "", feeds: [], rubric: "" };
}

function renderEditionPicker() {
  const ids = Object.keys(sources);
  el("edition-picker").innerHTML = ids.map(id => `
    <label class="edition-option ${id === activeSource ? "active" : ""}">
      <input type="radio" name="edition" value="${escapeHtml(id)}" ${id === activeSource ? "checked" : ""}>
      <span>${escapeHtml(sources[id].label || id)}</span>
      <span class="edition-count">${(sources[id].feeds || []).length} مصدر</span>
    </label>
  `).join("");
  const name = activeSet().label || activeSource || "هذه النسخة";
  el("rubric-edition-name").textContent = name;
  el("feeds-edition-name").textContent = name;

  el("edition-picker").querySelectorAll("input[name=edition]").forEach(input => {
    input.addEventListener("change", () => {
      // Keep whatever is on screen before swapping, so edits to one edition
      // aren't silently lost by clicking across to the other.
      captureEditionFromForm();
      activeSource = input.value;
      showActiveEdition();
      renderEditionPicker();
    });
  });
}

function showActiveEdition() {
  el("feeds").value = (activeSet().feeds || []).join("\n");
  el("rubric").value = activeSet().rubric || "";
}

function captureEditionFromForm() {
  if (!sources[activeSource]) return;
  sources[activeSource].feeds = el("feeds").value.split("\n").map(s => s.trim()).filter(Boolean);
  sources[activeSource].rubric = el("rubric").value;
}

function applyConfig(cfg) {
  sources = cfg.sources || {};
  activeSource = cfg.active_source && sources[cfg.active_source]
    ? cfg.active_source
    : Object.keys(sources)[0] || null;

  el("min-words").value = cfg.min_word_count ?? 150;
  el("banned").value = (cfg.banned_domains || []).join("\n");
  el("exclude-keywords").value = (cfg.exclude_keywords || []).join("\n");
  el("require-attribution").checked = cfg.require_attribution !== false;

  if (activeSource) {
    showActiveEdition();
    renderEditionPicker();
  } else {
    // Config predating editions: fall back to the flat feeds/rubric fields.
    el("feeds").value = (cfg.feeds || []).join("\n");
    el("rubric").value = cfg.rubric || "";
    el("edition-picker").innerHTML = "";
  }

  renderDomains(cfg.domains);
  populateDomainFilter(cfg.domains);
}

async function saveConfig() {
  captureEditionFromForm();
  const payload = {
    min_word_count: parseInt(el("min-words").value || "0", 10),
    banned_domains: el("banned").value.split("\n").map(s => s.trim()).filter(Boolean),
    exclude_keywords: el("exclude-keywords").value.split("\n").map(s => s.trim()).filter(Boolean),
    require_attribution: el("require-attribution").checked,
    domains: readDomainsFromForm(),
  };
  // Send both editions, not just the visible one -- the form shows one at a
  // time, so a partial payload would blank the other's feeds and standard.
  if (activeSource) {
    payload.sources = sources;
    payload.active_source = activeSource;
  } else {
    payload.rubric = el("rubric").value;
    payload.feeds = el("feeds").value.split("\n").map(s => s.trim()).filter(Boolean);
  }
  const status = el("save-status");
  status.textContent = "جارٍ الحفظ...";
  const res = await fetch("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  status.textContent = res.ok ? "تم الحفظ." : "فشل الحفظ.";
  status.style.color = res.ok ? "var(--accept)" : "var(--reject)";
  setTimeout(() => (status.textContent = ""), 2500);
  if (res.ok) {
    const updated = await res.json();
    populateDomainFilter(updated.domains);
    renderEditionPicker();  // the per-edition source counts change as feeds are edited
  }
}

// created_at is written by the database at insert time, which is the moment
// the scouts pulled the article in -- so it is the fetch time, not the
// source's own publication date.
function fetchedDate(iso) {
  if (!iso) return null;
  // Stored as UTC ("YYYY-MM-DD HH:MM:SS") with no zone marker; without the
  // Z, browsers would read it as local time and the age would be wrong.
  return new Date(iso.replace(" ", "T") + (iso.includes("Z") ? "" : "Z"));
}

function timeAgo(iso) {
  const d = fetchedDate(iso);
  if (!d) return "";
  const diffMin = Math.round((Date.now() - d.getTime()) / 60000);
  if (diffMin < 1) return "الآن";
  if (diffMin < 60) return `منذ ${diffMin} دقيقة`;
  const h = Math.round(diffMin / 60);
  if (h < 24) return `منذ ${h} ساعة`;
  return `منذ ${Math.round(h / 24)} يوم`;
}

// "جُلب في 20 غشت 2026، 14:32" -- an exact wall-clock time in Morocco, since
// "3h ago" alone doesn't tell you which run an article came in on. ar-MA
// gives Moroccan month names (غشت, not أغسطس) and keeps Latin digits.
function fetchedStamp(iso) {
  const d = fetchedDate(iso);
  if (!d) return "";
  return "جُلب في " + d.toLocaleString("ar-MA", {
    timeZone: "Africa/Casablanca",
    day: "numeric", month: "short", year: "numeric",
    hour: "2-digit", minute: "2-digit", hour12: false,
  });
}

const STATUS_AR_PLAIN = { accepted: "مقبول", rejected: "مرفوض", pending: "قيد المراجعة" };

function stageLabel(stage) {
  return {
    rule: "فحص القواعد",
    specialist: "مراجعة المكتب",
    editor: "المكتب + رئيس التحرير",
    manual: "تدخل يدوي",
    llm: "مراجعة تحريرية",
  }[stage] || "—";
}

function formatParagraphs(text) {
  // dir="auto" goes on each paragraph individually, not a shared wrapper --
  // a wrapper's own auto-direction is resolved from the first strong-direction
  // character anywhere inside it in DOM order, so an earlier English label
  // (like the "Ready to publish" tag) would force the whole block, including
  // these Arabic paragraphs, into LTR alignment despite their own content.
  return (text || "")
    .split(/\n+/)
    .map(p => p.trim())
    .filter(Boolean)
    .map(p => `<p dir="auto">${escapeHtml(p)}</p>`)
    .join("");
}

function providerTag(provider) {
  return provider ? `<span class="provider-tag">عبر ${escapeHtml(provider)}</span>` : "";
}

function draftedArticleBlock(a) {
  return `
    <div class="drafted-article">
      <span class="drafted-tag">جاهز للنشر</span>${providerTag(a.draft_provider)}
      <h4 class="draft-headline" dir="auto">${escapeHtml(a.draft_headline || "")}</h4>
      <div class="draft-body">${formatParagraphs(a.draft_article)}</div>
      <button type="button" class="copy-draft-btn" onclick="copyDraft(this)">نسخ المقال</button>
    </div>
  `;
}

function cardTemplate(a) {
  const stampClass = a.status === "accepted" ? "accepted" : a.status === "rejected" ? "rejected" : "pending";
  const STATUS_AR = { accepted: "مقبول", rejected: "مرفوض", pending: "قيد المراجعة…" };
  const stampLabel = STATUS_AR[a.status] || a.status;
  const domainBadge = a.domain ? `<span class="domain-badge">${escapeHtml(a.domain)}</span>` : "";

  // Writing is the expensive step, so it's opt-in per article rather than automatic --
  // only offer it on accepted articles that don't already have a draft.
  const generateBtn = (a.status === "accepted" && !a.draft_article)
    ? `<button type="button" class="generate-draft-btn" onclick="generateDraft(${a.id}, this)">تحرير المقال</button>`
    : "";

  let reasonBlock = "";
  if (a.draft_article && a.stage === "editor") {
    // Final accept with a written draft -- show the actual article in place
    // of the desk's short proposal text, then the editor's sign-off below it.
    const verb = a.status === "accepted" ? "صادق على القرار" : "عارض المكتب";
    reasonBlock = `
      <div class="review-trail">
        <div class="trail-step proposal drafted">
          <span class="trail-role">${escapeHtml(a.domain || "المكتب")}${providerTag(a.proposal_provider)}</span>
          ${draftedArticleBlock(a)}
        </div>
        <div class="trail-step final ${stampClass}">
          <span class="trail-role">رئيس التحرير · ${verb}${providerTag(a.provider)}</span>
          <p dir="auto">${escapeHtml(a.reason)}</p>
        </div>
      </div>
    `;
  } else if (a.draft_article) {
    // No-domains fallback: a single editor-only pass accepted it, no desk to attribute the draft to.
    reasonBlock = `<div class="review-trail"><div class="trail-step proposal drafted">${draftedArticleBlock(a)}</div></div>`;
  } else if (a.stage === "editor" && a.proposal_reason) {
    // Editor-in-chief reviewed a desk's proposal -- show both steps as a trail,
    // since the final reason alone hides whether the editor agreed or overruled.
    const verb = a.status === "accepted" ? "صادق على القرار" : "عارض المكتب";
    reasonBlock = `
      <div class="review-trail">
        <div class="trail-step proposal">
          <span class="trail-role">${escapeHtml(a.domain || "المكتب")}${providerTag(a.proposal_provider)}</span>
          <p dir="auto">${escapeHtml(a.proposal_reason)}</p>
          ${generateBtn}
        </div>
        <div class="trail-step final ${stampClass}">
          <span class="trail-role">رئيس التحرير · ${verb}${providerTag(a.provider)}</span>
          <p dir="auto">${escapeHtml(a.reason)}</p>
        </div>
      </div>
    `;
  } else if (a.reason) {
    reasonBlock = `<div class="card-reason" dir="auto">${escapeHtml(a.reason)}</div>${providerTag(a.provider)}${generateBtn}`;
  }

  return `
    <article class="card ${stampClass}">
      <span class="stamp ${stampClass}">${stampLabel}</span>
      <div class="card-top">
        <span class="card-source">${escapeHtml(a.source || "مصدر غير معروف")}</span>
        ${domainBadge}
        <span class="card-time" title="${escapeHtml(fetchedStamp(a.created_at))}">${timeAgo(a.created_at)}</span>
      </div>
      <div class="card-fetched">${escapeHtml(fetchedStamp(a.created_at))}</div>
      <h3 class="card-title" dir="auto"><a href="${a.url}" target="_blank" rel="noopener">${escapeHtml(a.title || "بدون عنوان")}</a></h3>
      ${reasonBlock}
      <div class="card-footer">
        <span class="card-meta">${stageLabel(a.stage)} · ${a.confidence != null ? "نسبة الثقة " + Math.round(a.confidence * 100) + "%" : "—"}</span>
        <span class="override-actions">
          <button onclick="override(${a.id}, 'accepted')">قبول يدوي</button>
          <button onclick="override(${a.id}, 'rejected')">رفض يدوي</button>
        </span>
      </div>
    </article>
  `;
}

function escapeHtml(str) {
  const d = document.createElement("div");
  d.textContent = str;
  return d.innerHTML;
}

function copyDraft(btn) {
  const container = btn.closest(".drafted-article");
  const headline = container.querySelector(".draft-headline").textContent;
  const body = [...container.querySelectorAll(".draft-body p")].map(p => p.textContent).join("\n\n");
  const text = `${headline}\n\n${body}`;
  const original = btn.textContent;
  navigator.clipboard.writeText(text).then(() => {
    btn.textContent = "تم النسخ!";
    setTimeout(() => { btn.textContent = original; }, 1800);
  }).catch(() => {
    btn.textContent = "تعذّر النسخ — حدّد النص يدويا";
    setTimeout(() => { btn.textContent = original; }, 2500);
  });
}

async function generateDraft(id, btn) {
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = "جارٍ التحرير...";
  try {
    const res = await fetch(`/api/articles/${id}/draft`, { method: "POST" });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert("فشل تحرير المقال: " + (err.detail || "خطأ غير معروف"));
      btn.disabled = false;
      btn.textContent = original;
      return;
    }
    loadFeed();
  } catch (e) {
    alert("فشل تحرير المقال: " + e.message);
    btn.disabled = false;
    btn.textContent = original;
  }
}

async function loadFeed() {
  const params = new URLSearchParams();
  if (currentFilter) params.set("status", currentFilter);
  if (currentDomainFilter) params.set("domain", currentDomainFilter);
  const qs = params.toString();
  const res = await fetch(`/api/articles${qs ? "?" + qs : ""}`);
  const articles = await res.json();
  const feed = el("feed");
  if (articles.length === 0) {
    feed.innerHTML = `<div class="empty-state">لا توجد مقالات بعد. اضبط معاييرك ثم اضغط «جلب الأخبار».</div>`;
    return;
  }
  feed.innerHTML = articles.map(cardTemplate).join("");
}

async function override(id, status) {
  await fetch(`/api/articles/${id}/override`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status }),
  });
  loadFeed();
}

// Run problems used to go to console.warn only, which meant a completely
// broken pipeline looked identical to a working one from the dashboard --
// articles just sat at "reviewing..." forever with no visible explanation.
// Anything that stops an article getting a verdict is surfaced here instead.
function renderAlerts(items) {
  const box = el("alerts");
  if (!items.length) {
    box.hidden = true;
    box.innerHTML = "";
    return;
  }
  box.hidden = false;
  box.innerHTML = items.map(a => `
    <div class="alert alert-${a.level}">
      <strong>${escapeHtml(a.title)}</strong>
      <div class="alert-body">${a.lines.map(l => `<div>${escapeHtml(l)}</div>`).join("")}</div>
    </div>
  `).join("");
}

function alertsFromRun(result) {
  const alerts = [];
  if (result.review_errors?.length) {
    // Collapse identical errors -- one dead model produces the same line
    // dozens of times, and a wall of duplicates hides everything else.
    const byError = new Map();
    for (const e of result.review_errors) {
      byError.set(e.error, (byError.get(e.error) || 0) + 1);
    }
    alerts.push({
      level: "error",
      title: `${result.review_errors.length} مقال بقي دون قرار — فشلت مرحلة المراجعة`,
      lines: [...byError.entries()].map(([err, n]) => `${n}× ${err}`),
    });
  }
  if (result.scout_errors?.length) {
    alerts.push({
      level: "warn",
      title: `تعذّر جلب ${result.scout_errors.length} مصدر`,
      lines: result.scout_errors.map(e => `${e.feed} — ${e.error}`),
    });
  }
  if (result.deferred) {
    alerts.push({
      level: "warn",
      title: `${result.deferred} مقال مؤجَّل إلى التشغيل القادم`,
      lines: [result.deadline_reached
        ? "بلغ التشغيل حده الزمني. اضغط «جلب الأخبار» مرة أخرى للمتابعة، أو فعّل الوضع التلقائي."
        : "بلغ التشغيل الحد الأقصى للمقالات الجديدة. اضغط «جلب الأخبار» مرة أخرى، أو فعّل الوضع التلقائي."],
    });
  }
  return alerts;
}

let runInFlight = false;

async function runPipeline() {
  // Auto-run and the button share this, and a run can last minutes -- so
  // overlapping runs are refused outright. Two concurrent runs would review
  // the same pending rows twice and pay for it twice.
  if (runInFlight) return null;
  runInFlight = true;
  const btn = el("run-btn");
  btn.disabled = true;
  btn.textContent = "جارٍ الجلب...";
  let result = null;
  try {
    const res = await fetch("/api/run", { method: "POST" });
    result = await res.json();
    if (res.ok) {
      el("stat-seen").textContent = result.candidates_seen;
      el("stat-new").textContent = result.new_articles;
      el("stat-accepted").textContent = result.accepted;
      el("stat-rejected").textContent = result.rejected;
      el("stat-pending").textContent = result.pending ?? "—";
      renderAlerts(alertsFromRun(result));
    } else {
      renderAlerts([{ level: "error", title: "فشل جلب الأخبار", lines: [result.detail || "خطأ غير معروف"] }]);
      result = null;
    }
  } catch (e) {
    renderAlerts([{ level: "error", title: "فشل جلب الأخبار", lines: [e.message] }]);
    result = null;
  } finally {
    runInFlight = false;
    btn.disabled = false;
    btn.textContent = "جلب الأخبار";
    loadFeed();
  }
  return result;
}


// ---------- Auto-run ----------
//
// One run admits a capped number of new candidates, so clearing a busy
// morning's feeds took several clicks. This does the clicking: it runs on an
// interval, and whenever a run reports work left over (deferred > 0) it goes
// again shortly instead of idling until the next slot, so a backlog drains
// on its own.
//
// Browser-side, so it only runs while this page is open -- that is the
// trade-off for needing no setup at all. For runs that continue with the
// laptop shut, set CRON_SECRET and use the scheduled /api/cron/run endpoint.

const AUTO_KEY = "safircom.autorun";
const BACKLOG_DELAY_MS = 15000;  // gap between back-to-back catch-up runs

let autoTimer = null;
let autoTickTimer = null;
let autoNextAt = null;

function loadAutoSettings() {
  try {
    return JSON.parse(localStorage.getItem(AUTO_KEY)) || { on: false, minutes: 30 };
  } catch {
    return { on: false, minutes: 30 };
  }
}

function saveAutoSettings(s) {
  localStorage.setItem(AUTO_KEY, JSON.stringify(s));
}

function renderAutoStatus() {
  const box = el("auto-status");
  const on = el("auto-toggle").checked;
  if (!on) { box.textContent = ""; return; }
  if (runInFlight) { box.textContent = "قيد التشغيل…"; return; }
  if (!autoNextAt) { box.textContent = "مُجدول"; return; }
  const secs = Math.max(0, Math.round((autoNextAt - Date.now()) / 1000));
  box.textContent = secs >= 60
    ? `التشغيل القادم بعد ${Math.round(secs / 60)} دقيقة`
    : `التشغيل القادم بعد ${secs} ثانية`;
}

function scheduleAutoRun(delayMs) {
  clearTimeout(autoTimer);
  autoNextAt = Date.now() + delayMs;
  autoTimer = setTimeout(autoRunTick, delayMs);
  renderAutoStatus();
}

async function autoRunTick() {
  if (!el("auto-toggle").checked) return;
  const result = await runPipeline();
  if (!el("auto-toggle").checked) return;  // turned off mid-run
  const intervalMs = Number(el("auto-interval").value) * 60000;
  // Still work queued? Come straight back for it rather than waiting out
  // the full interval -- this is what draining a backlog by hand looked like.
  scheduleAutoRun(result && result.deferred > 0 ? BACKLOG_DELAY_MS : intervalMs);
}

function applyAutoRun({ runNow }) {
  const on = el("auto-toggle").checked;
  const minutes = Number(el("auto-interval").value);
  saveAutoSettings({ on, minutes });
  el("auto-interval").disabled = !on;
  clearTimeout(autoTimer);
  clearInterval(autoTickTimer);
  autoNextAt = null;

  if (!on) { renderAutoStatus(); return; }
  autoTickTimer = setInterval(renderAutoStatus, 5000);  // keep the countdown honest
  if (runNow) autoRunTick();
  else scheduleAutoRun(minutes * 60000);
}

function initAutoRun() {
  const s = loadAutoSettings();
  el("auto-toggle").checked = !!s.on;
  el("auto-interval").value = String(s.minutes || 30);
  el("auto-interval").disabled = !s.on;
  // Resuming a saved setting shouldn't fire a run the instant the page
  // loads -- a refresh would then always cost a full pipeline run.
  applyAutoRun({ runNow: false });
}

async function clearArticles() {
  if (!confirm("حذف جميع المقالات؟ لا يمكن التراجع عن هذه الخطوة.")) return;
  const btn = el("clear-btn");
  btn.disabled = true;
  btn.textContent = "جارٍ الحذف...";
  try {
    await fetch("/api/articles", { method: "DELETE" });
    el("stat-seen").textContent = "—";
    el("stat-new").textContent = "—";
    el("stat-accepted").textContent = "—";
    el("stat-rejected").textContent = "—";
  } finally {
    btn.disabled = false;
    btn.textContent = "حذف المقالات";
    loadFeed();
  }
}

async function runHealthCheck() {
  const btn = el("health-btn");
  btn.disabled = true;
  btn.textContent = "جارٍ الفحص...";
  try {
    const res = await fetch("/api/health");
    const h = await res.json();
    const alerts = [];

    const badEnv = Object.entries(h.env || {})
      .filter(([, v]) => !v.set || v.had_surrounding_whitespace || v.has_internal_whitespace)
      .map(([k, v]) => {
        if (!v.set) return `${k} — غير مضبوط`;
        if (v.problem) return v.problem;
        return `${k} — يحتوي على فراغات في أوله أو آخره؛ أعد لصقه دون سطر فارغ في النهاية`;
      });
    if (badEnv.length) {
      alerts.push({
        level: Object.values(h.env || {}).some(v => v.has_internal_whitespace) ? "error" : "warn",
        title: "متغيرات البيئة",
        lines: badEnv,
      });
    }

    for (const [name, key] of [["Gemini", "gemini"], ["Groq (احتياطي اختياري)", "groq"], ["قاعدة البيانات", "database"]]) {
      const r = h[key];
      if (r && !r.ok) {
        const lines = [r.error || "خطأ غير معروف"];
        if (r.available_models) lines.push("النماذج المتاحة لهذا المفتاح: " + r.available_models.join(", "));
        if (r.available_models_error) lines.push(r.available_models_error);
        alerts.push({ level: key === "groq" ? "warn" : "error", title: `${name} لا يعمل`, lines });
      }
    }

    if (!alerts.length) {
      alerts.push({ level: "ok", title: "كل الأنظمة تعمل", lines: [
        `Gemini: ${h.gemini?.model}`,
        `Groq: ${h.groq?.model}`,
        `قاعدة البيانات: ${h.database?.articles} مقال — ` +
          Object.entries(h.database?.by_status || {}).map(([k, v]) => `${v} ${STATUS_AR_PLAIN[k] || k}`).join("، "),
      ]});
    }
    renderAlerts(alerts);
  } catch (e) {
    renderAlerts([{ level: "error", title: "فشل فحص النظام", lines: [e.message] }]);
  } finally {
    btn.disabled = false;
    btn.textContent = "فحص النظام";
  }
}

document.querySelectorAll(".tab").forEach(tab => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
    tab.classList.add("active");
    currentFilter = tab.dataset.status;
    loadFeed();
  });
});

el("save-btn").addEventListener("click", saveConfig);
el("run-btn").addEventListener("click", runPipeline);
el("clear-btn").addEventListener("click", clearArticles);
el("health-btn").addEventListener("click", runHealthCheck);
el("auto-toggle").addEventListener("change", () => applyAutoRun({ runNow: el("auto-toggle").checked }));
el("auto-interval").addEventListener("change", () => applyAutoRun({ runNow: false }));
el("add-domain-btn").addEventListener("click", () => {
  el("domains-list").insertAdjacentHTML("beforeend", domainRowTemplate());
});
el("domain-filter").addEventListener("change", () => {
  currentDomainFilter = el("domain-filter").value;
  loadFeed();
});

setEditionDate();
initAutoRun();
loadConfig();
loadFeed();
