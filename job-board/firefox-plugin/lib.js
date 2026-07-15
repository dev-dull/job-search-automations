"use strict";

// Shared helpers used by both popup.js and background.js. Loaded as the first
// script in popup.html and as the first entry in the background "scripts" array
// in manifest.json, so its top-level symbols are available to both contexts.

const SCORE_CACHE_KEY = "scoreCache";
const SCORE_CACHE_MAX = 200;

// Tracking params that don't identify the posting. Keep in sync with the
// backend's `_TRACKING_PARAMS` (app.py) so plugin cache lookups and backend
// dedupe land on the same string.
const TRACKING_PARAMS = new Set([
  "gh_src", "utm_source", "utm_medium", "utm_campaign", "utm_term",
  "utm_content", "ref", "source", "lever-origin", "lever-source",
]);

// Stable cache key for a job posting. Same posting reached via different
// surfaces — LinkedIn ?currentJobId vs /jobs/view/, Greenhouse embed wrapper
// (voxel51.com/jd?gh_jid=N) vs Greenhouse direct (job-boards.greenhouse.io/
// voxel51/jobs/N) — maps to a single key here. Mirrors the backend's
// `urls.compute_dedupe_key` so plugin cache hits, backend lookups, and
// inbox dedupe all agree. Returns an opaque string; callers treat it only
// as a Map key, never as a clickable URL.
function canonicalizeJobUrl(rawUrl) {
  if (!rawUrl) return rawUrl;
  try {
    const u = new URL(rawUrl);

    // LinkedIn — collapse recommended-list and direct-view to one form.
    if (u.hostname.includes("linkedin.com")) {
      let jobId = u.searchParams.get("currentJobId");
      if (!jobId) {
        const m = u.pathname.match(/\/jobs\/view\/(\d+)/);
        if (m) jobId = m[1];
      }
      if (jobId) return `linkedin:${jobId}`;
    }

    // Greenhouse: any URL form with gh_jid (in query OR path on a *.greenhouse.io
    // host) collapses to `gh:<id>`. Strips the host entirely so wrapper and
    // direct URLs share a key.
    let ghJid = u.searchParams.get("gh_jid");
    if (!ghJid && u.hostname.endsWith("greenhouse.io")) {
      const m = u.pathname.match(/^\/[^/]+\/jobs\/(\d+)\/?$/);
      if (m) ghJid = m[1];
    }
    if (ghJid) return `gh:${ghJid}`;

    // Ashby: ashby_jid query param (embed wrapper on any host, e.g.
    // www.ashbyhq.com/careers?ashby_jid=<uuid>) or UUID at the end of the
    // path on jobs.ashbyhq.com. Use the UUID's first stanza to keep keys
    // tidy.
    let ashbyJid = u.searchParams.get("ashby_jid");
    if (!ashbyJid && u.hostname === "jobs.ashbyhq.com") {
      const m = u.pathname.match(/^\/[^/]+\/([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\/?$/i);
      if (m) ashbyJid = m[1];
    }
    if (ashbyJid) return `ashby:${ashbyJid.split("-")[0]}`;

    // Generic: drop tracking params, strip trailing slash on the path. Stays
    // as a URL because there's no opaque id to extract.
    const kept = new URLSearchParams();
    for (const [k, v] of u.searchParams) {
      if (!TRACKING_PARAMS.has(k)) kept.append(k, v);
    }
    let path = u.pathname;
    if (path.length > 1 && path.endsWith("/")) path = path.replace(/\/+$/, "");
    const q = kept.toString();
    return `${u.protocol}//${u.host}${path}${q ? "?" + q : ""}`;
  } catch {
    return rawUrl;
  }
}

// Only http(s) pages can host a real job posting. file:// and chrome:// and
// about: tabs are skipped — no point spending an API call on them.
function isScoreableUrl(rawUrl) {
  if (!rawUrl) return false;
  try {
    const u = new URL(rawUrl);
    return u.protocol === "http:" || u.protocol === "https:";
  } catch {
    return false;
  }
}

// Path patterns that strongly suggest a careers-index page (list of
// openings) rather than a specific job posting. Used to skip scoring on
// pages like company.com/careers/ where the extracted text is a wall of
// role titles, not a single JD.
const CAREERS_INDEX_PATHS = [
  /\/careers\/?$/i,
  /\/jobs\/?$/i,
  /\/open-positions\/?$/i,
  /\/openings\/?$/i,
  /\/positions\/?$/i,
  /\/join(-us)?\/?$/i,
  /\/work-with-us\/?$/i,
  /\/hiring\/?$/i,
  /\/we-are-hiring\/?$/i,
];

// Job-specifier query params that turn an otherwise careers-index URL into
// a deep-link at a specific posting. When any of these is present, treat
// the URL as a job listing, not an index.
const JOB_SPECIFIER_PARAMS = ["gh_jid", "ashby_jid", "currentJobId"];

function isCareersIndexUrl(rawUrl) {
  if (!rawUrl) return false;
  try {
    const u = new URL(rawUrl);
    for (const p of JOB_SPECIFIER_PARAMS) {
      if (u.searchParams.get(p)) return false;
    }
    return CAREERS_INDEX_PATHS.some((re) => re.test(u.pathname));
  } catch {
    return false;
  }
}

// True if `rawUrl` is hosted by the user's own job-store backend. Used to
// short-circuit scoring when the user opens the popup on their inbox or
// companies page — those pages contain job text that would otherwise get
// scored as if it were a single posting.
function isOwnBackendUrl(rawUrl, backendUrl) {
  if (!rawUrl || !backendUrl) return false;
  try {
    const u = new URL(rawUrl);
    const b = new URL(backendUrl);
    return u.origin === b.origin;
  } catch {
    return false;
  }
}

// Given any URL on a job board, return the canonical "careers URL" the
// backend's `/companies` endpoint will recognize, or null if the page isn't
// on one of the four ATSes the poller supports today (greenhouse / ashby /
// lever / workday). Used by the popup's "watch this company" button.
function deriveCareersUrl(rawUrl) {
  if (!rawUrl) return null;
  let u;
  try { u = new URL(rawUrl); } catch { return null; }
  const host = u.hostname;
  const segs = u.pathname.split("/").filter(Boolean);

  // Greenhouse: boards.greenhouse.io/<board>/... or job-boards.greenhouse.io/<board>/...
  // Embed URLs carry the board in the `for` query param, not the path — the
  // path segment is the literal "embed" and must not be treated as a board
  // token (issue #52). `for`-less embed URLs (job_app iframes) derive nothing.
  if (host.endsWith("greenhouse.io") && segs.length >= 1) {
    if (segs[0] === "embed") {
      const board = u.searchParams.get("for");
      return board ? `https://boards.greenhouse.io/${board}` : null;
    }
    return `https://boards.greenhouse.io/${segs[0]}`;
  }
  // Ashby: jobs.ashbyhq.com/<org>/...
  if (host === "jobs.ashbyhq.com" && segs.length >= 1) {
    return `https://jobs.ashbyhq.com/${segs[0]}`;
  }
  // Lever: jobs.lever.co/<company>/...
  if (host === "jobs.lever.co" && segs.length >= 1) {
    return `https://jobs.lever.co/${segs[0]}`;
  }
  // Workday: <tenant>.<region>.myworkdayjobs.com/<lang>/<site>/...
  // The path is typically /<lang>/<site>/job/... so we need both first two
  // segments to construct the careers URL the backend's regex matches.
  if (host.endsWith(".myworkdayjobs.com") && segs.length >= 2) {
    return `https://${host}/${segs[0]}/${segs[1]}`;
  }
  // Rippling: ats.rippling.com/<slug>/jobs[/<uuid>] — first segment is the
  // board slug; the backend verifies it against the public board API before
  // creating the target (#22).
  if (host === "ats.rippling.com" && segs.length >= 1) {
    return `https://ats.rippling.com/${segs[0]}/jobs`;
  }
  // Greenhouse on a custom domain (jobs.elastic.co): the host is unknown but
  // the gh_jid param is an unambiguous Greenhouse signal. Send the page URL
  // as-is — the backend guesses the board token from the domain and VERIFIES
  // it against the board API before creating the target (issue #43).
  if (u.searchParams.get("gh_jid")) {
    return rawUrl;
  }
  return null;
}

async function getCachedScore(rawUrl) {
  const key = canonicalizeJobUrl(rawUrl);
  if (!key) return null;
  const data = await browser.storage.local.get(SCORE_CACHE_KEY);
  const cache = data[SCORE_CACHE_KEY] || {};
  return cache[key] || null;
}

// Extract the displayable score from a cache entry. Tolerant of both the new
// Gemini-aligned analysis (`candidate_score`) and old plugin-direct entries
// (`fit_score`). Returns null if there's no usable number.
function fitScoreFromCacheEntry(entry) {
  const fit = entry?.fit;
  if (!fit) return null;
  const v = fit.candidate_score ?? fit.fit_score;
  return typeof v === "number" ? v : null;
}

// Ask the backend whether it has a score for this URL. Returns a cache-entry
// shape on hit, null on miss. Used by the toolbar-badge path when the local
// cache doesn't have an answer — covers jobs scored on another machine, after
// a cache eviction, or before URL canonicalization invalidated old entries.
async function lookupBackendScore(rawUrl, backendUrl) {
  if (!rawUrl || !backendUrl) return null;
  try {
    const r = await fetch(
      `${backendUrl.replace(/\/$/, "")}/jobs/score?url=${encodeURIComponent(rawUrl)}`,
      { method: "GET" },
    );
    if (!r.ok) return null;
    const data = await r.json();
    if (data?.analysis == null) return null;
    return {
      job: { url: rawUrl, ats: null, title: null, description: "" },
      fit: data.analysis,
      usage: {},
    };
  } catch {
    return null;
  }
}

async function setCachedScore(rawUrl, entry) {
  const key = canonicalizeJobUrl(rawUrl);
  if (!key || key === "manual-paste") return;
  const data = await browser.storage.local.get(SCORE_CACHE_KEY);
  const cache = data[SCORE_CACHE_KEY] || {};
  cache[key] = { ...entry, timestamp: Date.now() };

  // Evict oldest entries beyond the cap to keep storage bounded.
  const keys = Object.keys(cache);
  if (keys.length > SCORE_CACHE_MAX) {
    keys.sort((a, b) => (cache[a].timestamp || 0) - (cache[b].timestamp || 0));
    for (const k of keys.slice(0, keys.length - SCORE_CACHE_MAX)) {
      delete cache[k];
    }
  }

  await browser.storage.local.set({ [SCORE_CACHE_KEY]: cache });
}

// Map a 0-100 score to a recommendation bucket and matching badge color. Kept
// out of popup.js so the background script can pick a badge background that
// matches the popup's score-circle colors.
function badgeColorForScore(score) {
  if (score >= 80) return "#28a745"; // strong match — green
  if (score >= 60) return "#17a2b8"; // consider — teal
  if (score >= 40) return "#ffc107"; // weak — yellow
  return "#dc3545";                  // skip — red
}

// Single source of truth for painting the toolbar badge. Used from both the
// background script (on tab events / storage changes) and the popup (right
// after a successful render, as a defensive belt-and-suspenders so the user
// sees the score without waiting on the storage.onChanged round-trip).
async function paintBadgeForTab(tabId, score) {
  if (tabId == null) return;
  try {
    if (typeof score !== "number") {
      await browser.action.setBadgeText({ tabId, text: "" });
      return;
    }
    await browser.action.setBadgeText({ tabId, text: String(Math.round(score)) });
    await browser.action.setBadgeBackgroundColor({ tabId, color: badgeColorForScore(score) });
    if (browser.action.setBadgeTextColor) {
      await browser.action.setBadgeTextColor({ tabId, color: "#ffffff" });
    }
  } catch (err) {
    console.warn("paintBadgeForTab failed:", err);
  }
}
