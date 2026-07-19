"use strict";

// The plugin is a thin client: extracts the JD from the current tab and POSTs
// it to the job-store backend, which holds the resume, the API key, the
// prompt, and the schema. All scoring is server-side — the poller and the
// plugin share one codepath. See SERVER_SIDE_SCORING.md.

const ATS_HOSTS = [
  ["greenhouse.io", "greenhouse"],
  ["ashbyhq.com", "ashby"],
  ["lever.co", "lever"],
  ["myworkdayjobs.com", "workday"],
  ["linkedin.com", "linkedin"],
  ["icims.com", "icims"],
  ["bamboohr.com", "bamboo"],
  ["workable.com", "workable"],
  ["gem.com", "gem"],
  ["rippling.com", "rippling"],
  ["careers.ibm.com", "ibm"],
  ["taleo.net", "taleo"],
];

// ---------------------------------------------------------------------------
// DOM helpers
// ---------------------------------------------------------------------------

const $ = (id) => document.getElementById(id);
const showOnly = (id) => {
  for (const el of document.querySelectorAll("main")) el.hidden = true;
  $(id).hidden = false;
};

document.getElementById("open-options").addEventListener("click", (e) => {
  e.preventDefault();
  browser.runtime.openOptionsPage();
});

// ---------------------------------------------------------------------------
// Page extraction (runs in the page context)
// ---------------------------------------------------------------------------

function extractInPage() {
  const url = location.href;
  const hostname = location.hostname;

  // schema.org JobPosting JSON-LD is the most reliable source when a page embeds
  // it (Taleo and many careers sites do, for Google for Jobs). Prefer it for
  // title + description over fragile DOM scraping, and grab company + date too.
  const ld = (() => {
    const clean = (s) => {
      const d = document.createElement("div");
      d.innerHTML = s || "";                       // decode entities + drop tags
      return (d.textContent || "").replace(/\s+/g, " ").trim();
    };
    for (const el of document.querySelectorAll('script[type="application/ld+json"]')) {
      let data;
      try { data = JSON.parse(el.textContent); } catch { continue; }
      const nodes = [];
      const visit = (x) => {
        if (!x || typeof x !== "object") return;
        nodes.push(x);
        if (Array.isArray(x["@graph"])) x["@graph"].forEach(visit);
      };
      (Array.isArray(data) ? data : [data]).forEach(visit);
      const jp = nodes.find((n) => {
        const t = n["@type"];
        return Array.isArray(t) ? t.includes("JobPosting") : String(t) === "JobPosting";
      });
      if (jp) {
        const org = jp.hiringOrganization;
        return {
          title: (jp.title || "").trim() || null,
          description: clean(jp.description),
          postedAt: (jp.datePosted || "").slice(0, 10) || null,
          company: (org && (org.name || (typeof org === "string" ? org : null))) || null,
        };
      }
    }
    return null;
  })();

  // Title extraction is ATS-specific-first, then generic fallback. Two known
  // traps the specific selectors avoid:
  //   - LinkedIn split-pane (`/jobs/collections/.../?currentJobId=N`) renders a
  //     page-level h1 like "Recommended for you"; the real title lives in the
  //     detail pane's unified-top-card.
  //   - Workday's CXS SPA renders the position in `[data-automation-id=
  //     'jobPostingHeader']` (an h2, not an h1). With no Workday-specific
  //     selector this fell through to document.title — the static site banner
  //     ("CAREERS AT NVIDIA") — so every Workday posting was mis-titled (#42).
  const titleEl =
    document.querySelector("[class*='jobs-unified-top-card__job-title']") ||  // LinkedIn
    document.querySelector("[data-automation-id='jobPostingHeader']") ||      // Workday
    document.querySelector("h1");
  const title = (ld?.title || titleEl?.innerText || document.title || "").trim();

  // Two-tier extraction. Tier 1 is ATS-specific selectors known to wrap just
  // the description — if any match with substantial text we prefer them, since
  // they beat generic containers that often include navigation chrome or, on
  // LinkedIn split-pane views, the entire left-rail job list.
  const HIGH_CONFIDENCE = [
    // LinkedIn — class names rotate, so prefer wildcard plus known variants
    ".jobs-description__content",
    ".jobs-description-content__text",
    ".jobs-box__html-content",
    ".jobs-description",
    "[class*='jobs-description']",
    // IBM careers.ibm.com — article__content__view is just the role
    // description; section__content also includes "ABOUT BUSINESS UNIT".
    ".article__content__view",
    ".section__content",
    // Workday
    "[data-automation-id='jobPostingDescription']",
  ].join(", ");

  // Tier 2: generic SPA / careers-site patterns. Longest-wins among these.
  const GENERIC = [
    "main",
    "article",
    "[role='main']",
    ".job",
    ".posting",
    ".opening",
    "#content",
    "#job-description",
    ".description",
    "[data-testid*='job']",
    "[data-testid*='description']",
    "[class*='job-description']",
    "[class*='JobDescription']",
    "[class*='job-detail']",
    "[class*='JobDetail']",
    "[id*='job-description']",
    "[id*='JobDescription']",
    ".phs-jobs-content",  // Phenom
  ].join(", ");

  const pickLongest = (selectorString) => {
    let bestEl = null;
    let bestLen = 0;
    for (const el of document.querySelectorAll(selectorString)) {
      const len = (el.innerText || "").length;
      if (len > bestLen && len < 50000) {
        bestLen = len;
        bestEl = el;
      }
    }
    return { el: bestEl, len: bestLen };
  };

  let main = null;
  let bestLen = 0;

  // LinkedIn anchor extraction — class names rotate frequently, but the
  // section header text "About the job" is stable English copy. Find that
  // header and walk up to the smallest ancestor with substantial text. This
  // beats class-based extraction in robustness and bypasses the issue where
  // the longest-text container on a split-pane recommended-jobs page is the
  // "Top job picks" feed sidebar rather than the description itself.
  if (hostname.includes("linkedin.com")) {
    for (const h of document.querySelectorAll("h1, h2, h3, h4")) {
      if (!/^about the job\b/i.test((h.innerText || "").trim())) continue;
      let walker = h;
      while (walker.parentElement && (walker.innerText || "").length < 1500) {
        walker = walker.parentElement;
      }
      const len = (walker.innerText || "").length;
      if (len >= 1500 && len < 50000) {
        main = walker;
        bestLen = len;
      }
      break;
    }
  }

  if (!main) {
    ({ el: main, len: bestLen } = pickLongest(HIGH_CONFIDENCE));
    if (bestLen < 200) {
      main = null;
      bestLen = 0;
    }
  }
  if (!main) ({ el: main, len: bestLen } = pickLongest(GENERIC));
  if (!main) ({ el: main } = pickLongest("div, section"));
  // Prefer the JSON-LD description when present and substantial: it's the clean
  // full JD, free of page chrome (e.g. Taleo, whose DOM is a legacy frame mess).
  const domDescription = (main?.innerText || document.body.innerText || "").trim();
  const description =
    (ld?.description && ld.description.length >= 200) ? ld.description : domDescription;

  // If this page embeds Greenhouse via their JS embed (`<script src=
  // "boards.greenhouse.io/embed/job_board/js?for=<token>">`), surface the
  // board token so the popup can fetch the canonical job from the API
  // — generic page-scrape can't see across the iframe Greenhouse injects.
  let greenhouseBoardToken = null;
  for (const s of document.querySelectorAll("script[src*='greenhouse.io/embed']")) {
    const m = (s.src || "").match(/[?&]for=([^&]+)/);
    if (m) { greenhouseBoardToken = m[1]; break; }
  }

  // Ashby embeds JS-render their JD post-load, so DOM extraction often
  // misses it. Capture the org slug from any reference to jobs.ashbyhq.com
  // on the page; the popup then fetches the canonical JD via Ashby's API.
  let ashbyOrgSlug = null;
  for (const el of document.querySelectorAll(
        "script[src*='ashbyhq.com'], a[href*='jobs.ashbyhq.com'], iframe[src*='ashbyhq.com']")) {
    const src = el.src || el.href || "";
    const m = src.match(/jobs\.ashbyhq\.com\/([a-z0-9_-]+)/i);
    if (m) { ashbyOrgSlug = m[1]; break; }
  }
  // ashbyhq.com's own marketing-site careers page hosts their internal
  // postings under slug "ashby".
  if (!ashbyOrgSlug && (hostname === "www.ashbyhq.com" || hostname === "ashbyhq.com")) {
    ashbyOrgSlug = "ashby";
  }

  return { url, hostname, title, description, posted_at: ld?.postedAt || null,
           company: ld?.company || null, greenhouseBoardToken, ashbyOrgSlug };
}

function detectAts(hostname) {
  for (const [needle, name] of ATS_HOSTS) {
    if (hostname.includes(needle)) return name;
  }
  return "unknown";
}

async function extractCurrentTab() {
  const [tab] = await browser.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id) throw new Error("No active tab.");
  const results = await browser.scripting.executeScript({
    target: { tabId: tab.id },
    func: extractInPage,
  });
  const extracted = results?.[0]?.result;
  if (!extracted) throw new Error("Failed to extract page content.");
  let job = { ...extracted, ats: detectAts(extracted.hostname) };

  // Canonicalize LinkedIn URLs so the same posting reached via the
  // recommended-list (`?currentJobId=<id>`) and via direct view
  // (`/jobs/view/<id>/`) dedupe to the same job-store row.
  if (job.hostname.includes("linkedin.com")) {
    try {
      const u = new URL(job.url);
      let jobId = u.searchParams.get("currentJobId");
      if (!jobId) {
        const m = u.pathname.match(/\/jobs\/view\/(\d+)/);
        if (m) jobId = m[1];
      }
      if (jobId) job.url = `https://www.linkedin.com/jobs/view/${jobId}/`;
    } catch { /* leave URL alone if parsing fails */ }
  }

  // If we're on a Greenhouse-embedded page (any company hosting Greenhouse
  // via their own domain — Outside Inc, etc.), the page-scraped description
  // is just the company landing page. Fetch the canonical job content
  // from Greenhouse's public job-board API instead. Works because the
  // plugin has host_permissions for *.greenhouse.io, which lets the popup
  // fetch despite the API not returning open CORS headers.
  const ghJid = new URL(job.url).searchParams.get("gh_jid");
  if (ghJid && job.greenhouseBoardToken) {
    try {
      const r = await fetch(
        `https://boards-api.greenhouse.io/v1/boards/${encodeURIComponent(job.greenhouseBoardToken)}/jobs/${encodeURIComponent(ghJid)}`
      );
      if (r.ok) {
        const data = await r.json();
        const tmp = document.createElement("div");
        tmp.innerHTML = data.content || "";
        const plain = (tmp.innerText || tmp.textContent || "").trim();
        if (plain.length > 200) {
          job.title = data.title || job.title;
          job.description = plain;
          job.ats = "greenhouse";
        }
      }
    } catch (err) {
      console.warn("Greenhouse API fetch failed; falling back to page scrape:", err);
    }
  }

  // Ashby same idea: the embed renders the JD client-side, so DOM extraction
  // typically misses the actual posting text. Pull from the public posting
  // API using the ashby_jid (query param on the embed wrapper, or UUID in
  // the path on jobs.ashbyhq.com) plus the org slug surfaced by extractInPage.
  const jobUrl = new URL(job.url);
  let ashbyJid = jobUrl.searchParams.get("ashby_jid");
  if (!ashbyJid && jobUrl.hostname === "jobs.ashbyhq.com") {
    const m = jobUrl.pathname.match(/^\/[^/]+\/([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\/?$/i);
    if (m) ashbyJid = m[1];
  }
  if (ashbyJid && job.ashbyOrgSlug) {
    try {
      const r = await fetch(
        `https://api.ashbyhq.com/posting-api/job-board/${encodeURIComponent(job.ashbyOrgSlug)}?includeCompensation=true`
      );
      if (r.ok) {
        const data = await r.json();
        const match = (data.jobs || []).find((j) => j.id === ashbyJid);
        if (match) {
          let desc = match.descriptionPlain || "";
          if (!desc && match.descriptionHtml) {
            const tmp = document.createElement("div");
            tmp.innerHTML = match.descriptionHtml;
            desc = (tmp.innerText || tmp.textContent || "").trim();
          }
          if (desc && desc.length > 200) {
            job.title = match.title || job.title;
            job.description = desc;
            job.ats = "ashby";
          }
        }
      }
    } catch (err) {
      console.warn("Ashby API fetch failed; falling back to page scrape:", err);
    }
  }

  // iCIMS: the plain job page is a JS shell (no server-side JSON-LD, generic
  // DOM), but the same URL with ?in_iframe=1 is fully server-rendered and
  // embeds a complete schema.org JobPosting. Re-fetch that variant and parse
  // its ld+json — host permission for *.icims.com lets the popup fetch it.
  if (job.ats === "icims" && /\/jobs\/\d+\//.test(jobUrl.pathname)) {
    try {
      const sep = jobUrl.search ? "&" : "?";
      const r = await fetch(`${jobUrl.origin}${jobUrl.pathname}${jobUrl.search}${sep}in_iframe=1`);
      if (r.ok) {
        const html = await r.text();
        const doc = new DOMParser().parseFromString(html, "text/html");
        for (const s of doc.querySelectorAll('script[type="application/ld+json"]')) {
          let data;
          try { data = JSON.parse(s.textContent); } catch { continue; }
          for (const item of Array.isArray(data) ? data : [data]) {
            const t = item["@type"];
            if ((Array.isArray(t) ? t.includes("JobPosting") : t === "JobPosting")) {
              const tmp = document.createElement("div");
              tmp.innerHTML = item.description || "";
              const plain = (tmp.innerText || tmp.textContent || "").replace(/\s+/g, " ").trim();
              if (plain.length > 200) {
                job.title = (item.title || "").trim() || job.title;
                job.description = plain;
                job.posted_at = (item.datePosted || "").slice(0, 10) || job.posted_at;
              }
            }
          }
        }
      }
    } catch (err) {
      console.warn("iCIMS in_iframe fetch failed; falling back to page scrape:", err);
    }
  }

  // Greenhouse behind a custom domain with NO embed script and NO gh_jid param
  // (HubSpot: www.hubspot.com/careers/jobs/7988809 serves a JS shell; the JD
  // loads client-side, so page scraping gets directory chrome). Guess board
  // tokens from the domain (hubspot -> hubspot, hubspotjobs, ...) and verify
  // the specific posting id against the public board API; on a hit, use the
  // canonical content. Mirrors the backend resolver in adapters/greenhouse.py.
  const pathJid = (jobUrl.pathname.match(/\/(\d{5,})\/?$/) || [])[1];
  if (job.ats === "unknown" && pathJid) {
    const parts = jobUrl.hostname.toLowerCase().split(".");
    const twoPart = new Set(["co.uk", "org.uk", "co.jp", "com.au", "com.br"]);
    const suffixLen = twoPart.has(parts.slice(-2).join(".")) ? 2 : 1;
    const label = parts[parts.length - suffixLen - 1];
    const generic = new Set(["www", "jobs", "careers", "apply", "boards", "talent"]);
    if (label && !generic.has(label)) {
      const dehyph = label.replace(/-/g, "");
      const candidates = [...new Set([label, `${label}jobs`, dehyph, `${dehyph}jobs`])];
      for (const board of candidates) {
        try {
          const r = await fetch(
            `https://boards-api.greenhouse.io/v1/boards/${encodeURIComponent(board)}/jobs/${encodeURIComponent(pathJid)}`
          );
          if (!r.ok) continue;
          const data = await r.json();
          const tmp = document.createElement("div");
          tmp.innerHTML = data.content || "";
          const plain = (tmp.innerText || tmp.textContent || "").trim();
          if (plain.length > 200) {
            job.title = data.title || job.title;
            job.description = plain;
            job.ats = "greenhouse";
          }
          break;
        } catch (err) {
          console.warn("Greenhouse board-guess fetch failed:", err);
          break;
        }
      }
    }
  }

  return job;
}

// ---------------------------------------------------------------------------
// Backend score call — POSTs the raw JD; backend reads the resume from disk,
// holds the API key, calls Anthropic, returns the analysis blob. One scoring
// codepath shared with the targeted-company poller. See SERVER_SIDE_SCORING.md.
// ---------------------------------------------------------------------------

async function scoreViaBackend({ backendUrl, job, force = false }) {
  const body = {
    url: job.url,
    title: job.title,
    description: job.description,
    ats_platform: job.ats,
    posted_at: job.posted_at || null,   // from JSON-LD datePosted when available
    company: job.company || null,       // backend falls back to job_company_name
    discovered_by: "plugin",
    // No fit_score / analysis — sending those would tell the backend to
    // persist as-is and skip Anthropic; we want it to score.
    // `force` bypasses the backend's "already scored" dedupe guard.
    force,
  };

  const r = await fetch(`${backendUrl}/jobs/score`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });

  if (!r.ok) {
    const text = await r.text().catch(() => "");
    throw new Error(`backend scoring failed: HTTP ${r.status} ${text.slice(0, 200)}`);
  }

  const data = await r.json();
  // Backend returns the full analysis blob under `analysis` and surfaces
  // top-level helpers (`fit_score`, `company`, `rank_score`) plus per-call
  // token usage. Plugin only needs the analysis + usage for rendering.
  return {
    fit: data.analysis || {},
    usage: data.usage || {},
    rankScore: data.rank_score,
    jobId: data.id,
  };
}

// ---------------------------------------------------------------------------
// Render
// ---------------------------------------------------------------------------

const REC_LABELS = {
  strong_match: { label: "Strong match", cls: "s-strong" },
  consider: { label: "Consider", cls: "s-consider" },
  weak_match: { label: "Weak match", cls: "s-weak" },
  skip: { label: "Skip", cls: "s-skip" },
};

// Map a 1-100 candidate_score to a recommendation bucket. Derived client-side
// from the score itself so the UI never disagrees with the number. Cutoffs
// match the popup's previous prompt-baked calibration.
function deriveRecommendation(score) {
  if (score >= 80) return "strong_match";
  if (score >= 60) return "consider";
  if (score >= 40) return "weak_match";
  return "skip";
}

// Tolerant adapter: maps either the old plugin schema (fit_score, company,
// summary, strengths, gaps, recommendation) or the new Gemini-aligned schema
// (candidate_score, job_company_name, candidate_explanation, ...) into the
// canonical shape the UI renders. Lets cached entries from before the schema
// migration keep displaying without a forced re-score.
function normalizeFit(fit) {
  return {
    candidate_score: fit.candidate_score ?? fit.fit_score ?? 0,
    career_growth_score: fit.career_growth_score ?? null,
    candidate_explanation: fit.candidate_explanation ?? fit.summary ?? "",
    candidate_strengths: fit.candidate_strengths ?? fit.strengths ?? [],
    candidate_deficiencies: fit.candidate_deficiencies ?? fit.gaps ?? [],
    job_description_score: fit.job_description_score ?? null,
    job_company_name: fit.job_company_name ?? fit.company ?? null,
  };
}

function firstSentence(text) {
  if (!text) return "";
  const m = text.match(/^[^.!?]*[.!?]/);
  return (m ? m[0] : text).trim();
}

function renderResult(job, rawFit, usage) {
  const fit = normalizeFit(rawFit);
  const rec = REC_LABELS[deriveRecommendation(fit.candidate_score)] || REC_LABELS.consider;

  const circle = $("score-circle");
  circle.className = `score-circle ${rec.cls}`;
  $("score-number").textContent = fit.candidate_score;
  $("recommendation-label").textContent = rec.label;
  $("ats-platform").textContent = job.ats !== "unknown" ? job.ats : "";
  $("summary").textContent = firstSentence(fit.candidate_explanation);
  const companyTitle = fit.job_company_name
    ? `${fit.job_company_name} — ${job.title}`
    : job.title;
  $("job-title").textContent = companyTitle.slice(0, 70);

  // Subscore badges — only rendered if the field is present (old cached
  // entries from before the schema change leave them null).
  const renderSubscore = (id, label, score) => {
    const el = $(id);
    if (score == null) {
      el.hidden = true;
      return;
    }
    el.hidden = false;
    el.textContent = `${label} ${score}`;
  };
  renderSubscore("growth-subscore", "Growth", fit.career_growth_score);

  const renderList = (id, items) => {
    const ul = $(id);
    ul.innerHTML = "";
    for (const item of items) {
      const li = document.createElement("li");
      li.textContent = item;
      ul.appendChild(li);
    }
  };
  renderList("strengths", fit.candidate_strengths);
  renderList("gaps", fit.candidate_deficiencies);

  // Full explanation (up to 250 words) lives behind a details disclosure;
  // the summary line already shows the first sentence.
  const explanationSection = $("explanation-section");
  if (fit.candidate_explanation && fit.candidate_explanation !== firstSentence(fit.candidate_explanation)) {
    $("explanation").textContent = fit.candidate_explanation;
    explanationSection.hidden = false;
  } else {
    explanationSection.hidden = true;
  }

  const jdScoreEl = $("jd-score");
  if (fit.job_description_score != null) {
    jdScoreEl.hidden = false;
    jdScoreEl.textContent = `JD ${fit.job_description_score}/100`;
  } else {
    jdScoreEl.hidden = true;
  }

  const cacheRead = usage.cache_read_input_tokens || 0;
  const cacheWrite = usage.cache_creation_input_tokens || 0;
  const fresh = usage.input_tokens || 0;
  const out = usage.output_tokens || 0;
  $("cache-info").textContent =
    cacheRead > 0
      ? `${cacheRead} cached / ${fresh} fresh / ${out} out`
      : `${fresh + cacheWrite} in / ${out} out`;

  showOnly("status-result");
}

function renderSaveStatus(state) {
  const el = $("save-status");
  if (!state) {
    el.hidden = true;
    return;
  }
  el.hidden = false;
  el.className = `save-status ${state.cls}`;
  el.textContent = state.text;
}

function formatAge(timestamp) {
  const seconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000));
  if (seconds < 60) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

function renderCacheNotice(state) {
  const el = $("cache-notice");
  if (!state) {
    el.hidden = true;
    el.textContent = "";
    return;
  }
  el.hidden = false;
  el.textContent = `Cached • scored ${formatAge(state.timestamp)}`;
}

function showError(message) {
  $("error-message").textContent = message;
  showOnly("status-error");
}

// ---------------------------------------------------------------------------
// Orchestration
// ---------------------------------------------------------------------------

async function loadConfig() {
  const { backendUrl } = await browser.storage.local.get(["backendUrl"]);
  const trimmed = (backendUrl || "").trim().replace(/\/$/, "");
  if (!trimmed) {
    throw new Error("No backend URL configured. Open settings to set the job-store URL.");
  }
  return { backendUrl: trimmed };
}

async function getActiveTabUrl() {
  const [tab] = await browser.tabs.query({ active: true, currentWindow: true });
  return tab?.url || null;
}

// Defensive: paint the toolbar badge directly from the popup after a
// successful render. background.js also paints on storage.onChanged, but
// this guarantees the user sees the badge the moment the popup shows the
// score — independent of background-event timing.
async function paintBadgeFromFit(fit) {
  try {
    const score = (typeof fit?.candidate_score === "number" ? fit.candidate_score
                 : typeof fit?.fit_score === "number" ? fit.fit_score
                 : null);
    if (score == null) return;
    const [tab] = await browser.tabs.query({ active: true, currentWindow: true });
    if (tab?.id != null) await paintBadgeForTab(tab.id, score);
  } catch (err) {
    console.warn("paintBadgeFromFit failed:", err);
  }
}

async function run({ manualJD = null, force = false } = {}) {
  renderSaveStatus(null);
  renderCacheNotice(null);

  if (!manualJD) {
    // Always-quiet states first — no scoring, no prompt.
    try {
      const tabUrl = await getActiveTabUrl();
      const { backendUrl } = await browser.storage.local.get(["backendUrl"]);
      if (backendUrl && isOwnBackendUrl(tabUrl, backendUrl)) {
        showOnly("status-self");
        return;
      }
      if (isCareersIndexUrl(tabUrl)) {
        showOnly("status-careers-index");
        return;
      }

      // Cache-first: previously-scored URLs render instantly. Re-score
      // button passes `force: true` to bypass.
      if (!force && isScoreableUrl(tabUrl)) {
        let cached = await getCachedScore(tabUrl);
        // Local cache miss — ask the backend before falling through to the
        // Evaluate button. The backend's `dedupe_key` lookup catches URL
        // forms the plugin's local cache may have lost (canonicalization
        // changes, cross-browser, eviction).
        if (!cached && backendUrl) {
          cached = await lookupBackendScore(tabUrl, backendUrl);
          if (cached) {
            try { await setCachedScore(tabUrl, cached); } catch { /* ignore */ }
          }
        }
        if (cached?.fit) {
          renderResult(cached.job, cached.fit, cached.usage || {});
          renderCacheNotice({ timestamp: cached.timestamp });
          await paintBadgeFromFit(cached.fit);
          return;
        }
      }
    } catch (err) {
      console.warn("Pre-score checks failed:", err);
    }

    // No cached score and the user hasn't asked for one yet — wait for
    // an explicit click on the "Evaluate this job listing" button. This
    // prevents the plugin from scoring careers-search pages, marketing
    // pages, etc. just because they happen to be open when the popup
    // is invoked. The Re-score button passes `force: true` and skips
    // this gate.
    if (!force) {
      showOnly("status-actions");
      return;
    }
  }

  showOnly("status-loading");
  try {
    const { backendUrl } = await loadConfig();
    let job;
    if (manualJD) {
      job = {
        url: "manual-paste",
        title: "Manual paste",
        ats: "unknown",
        description: manualJD,
      };
    } else {
      const tabUrl = await getActiveTabUrl();
      if (!isScoreableUrl(tabUrl)) {
        showOnly("status-empty");
        return;
      }
      job = await extractCurrentTab();
      // Short-circuit before bothering the backend if the page didn't yield
      // a plausible JD.
      if (!job.description || job.description.trim().length < 200) {
        showOnly("status-empty");
        return;
      }
    }

    const { fit, usage, rankScore } = await scoreViaBackend({ backendUrl, job, force });
    renderResult(job, fit, usage);
    renderSaveStatus({
      cls: "ok",
      text: `Saved to inbox · rank ${rankScore != null ? rankScore.toFixed(0) : "—"}`,
    });
    await paintBadgeFromFit(fit);

    // Local-only cache: avoids a second backend round-trip (and a second
    // Anthropic call) if the user re-opens the popup on the same URL.
    // Manual-paste has no stable URL key so setCachedScore no-ops on it.
    if (!manualJD) {
      try {
        await setCachedScore(job.url, { job, fit, usage });
      } catch (err) {
        console.warn("Cache write failed:", err);
      }
    }
  } catch (err) {
    showError(err.message || String(err));
  }
}

document.getElementById("error-options").addEventListener("click", () =>
  browser.runtime.openOptionsPage()
);
document.getElementById("error-retry").addEventListener("click", () => run({ force: true }));
document.getElementById("rescore").addEventListener("click", () => run({ force: true }));
document.getElementById("score-action").addEventListener("click", () => run({ force: true }));

// ---------------------------------------------------------------------------
// Watch-this-company bar — shows when the current tab is on a supported ATS
// and the backend is reachable. Posts to /companies on click, or shows
// "already watching" if the careers URL is already in /companies.json.
// ---------------------------------------------------------------------------

async function setupWatchBar() {
  const bar = $("watch-bar");
  const btn = $("watch-btn");
  const status = $("watch-status");
  bar.hidden = true;

  const [tab] = await browser.tabs.query({ active: true, currentWindow: true });
  if (!tab?.url) return;

  const { backendUrl } = await browser.storage.local.get(["backendUrl"]);
  const backend = (backendUrl || "").replace(/\/$/, "");
  if (!backend) return;
  // Suppress the watch bar on the user's own job-store — there's no company
  // to watch on the inbox itself.
  if (isOwnBackendUrl(tab.url, backend)) return;

  const careersUrl = deriveCareersUrl(tab.url);
  if (!careersUrl) return;

  // Best-effort lookup. If the backend's offline we still show the button —
  // the click handler will surface any failure.
  let alreadyWatched = false;
  try {
    const r = await fetch(`${backend}/companies.json`);
    if (r.ok) {
      const list = await r.json();
      alreadyWatched = list.some((t) => (t.careers_url || "") === careersUrl);
    }
  } catch { /* keep button enabled */ }

  bar.hidden = false;
  status.className = "watch-status";
  if (alreadyWatched) {
    btn.textContent = "✓ Watching";
    btn.classList.add("is-watched");
    btn.disabled = true;
    status.textContent = careersUrl.replace(/^https?:\/\//, "");
    return;
  }

  btn.textContent = "+ Watch this company";
  btn.classList.remove("is-watched");
  btn.disabled = false;
  status.textContent = careersUrl.replace(/^https?:\/\//, "");
  btn.onclick = async () => {
    btn.disabled = true;
    btn.textContent = "Adding…";
    try {
      const body = new URLSearchParams({ careers_url: careersUrl });
      const r = await fetch(`${backend}/companies`, {
        method: "POST",
        headers: { "content-type": "application/x-www-form-urlencoded" },
        body,
        redirect: "manual",
      });
      // Flask redirects to /companies on success (302). fetch with redirect:manual
      // returns response.type === "opaqueredirect" with status 0 — treat as success.
      const ok = r.ok || r.type === "opaqueredirect" || r.status === 0 || r.status === 302;
      if (!ok) {
        const text = await r.text().catch(() => "");
        throw new Error(text.slice(0, 200) || `HTTP ${r.status}`);
      }
      btn.textContent = "✓ Watching";
      btn.classList.add("is-watched");
    } catch (err) {
      btn.disabled = false;
      btn.textContent = "+ Watch this company";
      status.className = "watch-status err";
      status.textContent = `Failed: ${err.message}`;
    }
  };
}

setupWatchBar();
document.getElementById("manual-score").addEventListener("click", () => {
  const text = $("manual-jd").value.trim();
  if (text.length < 100) {
    showError("Paste at least 100 characters of job description.");
    return;
  }
  run({ manualJD: text });
});

run();
