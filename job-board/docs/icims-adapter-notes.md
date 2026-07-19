# iCIMS adapter: investigation notes

**Date**: 2026-06-04
**Status**: Implemented for classic-iframe tenants (`job-store/adapters/icims.py`, issue #29, 2026-07-19): listing via `jobs/search?ss=1&in_iframe=1&pr=N`, detail via the job page's `?in_iframe=1` ld+json, and per-tenant crawlability verified at create time — which resolves the per-tenant-variance objection below. Bot-guarded tenants (like the Rivian case that prompted this doc) still correctly fail at create. Kept for the recon rationale.

## Context

User flagged that the Rivian careers page (`https://careers.rivian.com/careers-home/jobs`) doesn't register on the plugin for watching via the poller. Investigation traced the page to iCIMS as the backing ATS.

The plugin's `deriveCareersUrl()` in `firefox-plugin/lib.js` only recognizes hostnames for Greenhouse, Ashby, Lever, and Workday (the four ATSes with poller adapters). iCIMS pages render no watch bar, and the `/companies` create endpoint would reject the URL as unsupported.

## What's behind Rivian's careers page

- Customer-facing surface: `careers.rivian.com/careers-home/jobs`
- Underlying iCIMS tenant: `careers-rivian.icims.com` (redirects to `us-careers-rivian.icims.com`)
- iCIMS subdomain pattern observed in many other tenants: `careers-<slug>.icims.com` or `<slug>.icims.com/jobs/search`

Confirmed by:
- iCIMS markers in the main HTML body (`icims.com/jobs/login`, "iCIMS System ID")
- iCIMS-platform JS bundles loaded from `cdn02.icims.com`
- A direct fetch of `https://careers-rivian.icims.com/jobs/search?ss=1` redirects to `us-careers-rivian.icims.com/jobs/search?ss=1`

## Why iCIMS is the hardest of the major ATSes to support

| Aspect | Greenhouse | Ashby | Lever | Workday | iCIMS |
|---|---|---|---|---|---|
| Public JSON API | Yes (`boards-api.greenhouse.io`) | Yes (`api.ashbyhq.com/posting-api`) | Yes (`api.lever.co/v0/postings`) | Yes (CXS POST API) | No |
| Auth required for list | No | No | No | No | Effectively yes (bot-checks) |
| HTML-only fallback works | N/A (API is clean) | N/A | N/A | N/A | No (JS-rendered) |
| Cloudflare-style guard | No | No | No | No | Yes |
| Multi-page pagination | No (single response) | No | No | Yes (per `MAX_PAGES`) | Yes |

Three specific iCIMS blockers we observed:
1. The plain-HTML fetch of `careers-rivian.icims.com/jobs/search?ss=1&in_iframe=1` returns 143 bytes (a redirect / bot-check shell). The actual job listings only materialize after iCIMS's platform JS runs in a browser.
2. No documented public JSON endpoint for the job list. Some tenants leak data via guessable URLs but the pattern varies per customer and breaks when iCIMS updates their platform.
3. Listings include JSON-LD `JobPosting` schema markup, but only after JS render (same blocker as above).

## Options considered

1. **Headless browser harness** (Playwright / Puppeteer). Would JS-render the iCIMS page and scrape the resulting DOM. Heavy dependency. Slow per-poll. Brittle to iCIMS UI changes. Not consistent with the lightweight `urllib`-only adapter pattern used today.
2. **3rd-party aggregator** (Adzuna or similar). Outsources the iCIMS scraping. Adds vendor dependency, often per-region rate limits, costs money beyond a free tier, drift in data freshness.
3. **Tenant-specific scrape.** Probe each iCIMS customer for an undocumented endpoint that returns JSON. May work for some tenants today, may break on the next iCIMS platform release. Best-case: 30 minutes per tenant; worst-case: doesn't work at all.
4. **Skip iCIMS in the poller.** Use the plugin only for individual iCIMS-hosted job pages (the plugin already recognizes `icims.com` in `ATS_HOSTS` for content extraction, so per-page scoring works fine). Forgo the periodic auto-poll behavior for any iCIMS-backed company.

## Current decision

Defer until there's a real driver (e.g., a high-fit iCIMS-hosted role you want to track over time, or several iCIMS customers piling up in the inbox). The 22 iCIMS rows already in the DB from earlier plugin browsing skew low-fit, which weakens the ROI argument.

Manual fallback that works today: bookmark `careers.rivian.com/careers-home/jobs`, visit periodically, score interesting roles via the plugin.

## What would reverse the decision

- Multiple iCIMS-hosted companies become top-of-funnel targets.
- A high-fit iCIMS role surfaces and the user wants ongoing visibility (vs. a one-shot application).
- iCIMS publishes (or someone reverse-engineers cleanly) a stable JSON endpoint pattern.

## If we revisit, start by

1. Trying the tenant-specific scrape (option 3) against `careers-rivian.icims.com` first. Inspect dev-tools network panel for the actual XHR that populates the listing.
2. If that pattern holds across several tenants, write `job-store/adapters/icims.py` that accepts `{tenant: "<slug>"}` in the identifier dict.
3. Update `deriveCareersUrl()` in `firefox-plugin/lib.js` to recognize `*.icims.com` and customer-domain wrappers (`careers.<company>.com/careers-home/jobs` is one pattern; not the only one).
4. Update `detect_ats()` in `app.py` to detect iCIMS host patterns.
5. Update `probe_embedded_ats()` in `app.py` so the auto-resolve path can recover when a user pastes a customer-domain URL that wraps iCIMS.

## Related files (current state, no iCIMS code yet)

- `job-store/adapters/__init__.py`: `ADAPTERS` dispatch dict (Greenhouse, Ashby, Lever, Workday)
- `job-store/adapters/{greenhouse,ashby,lever,workday}.py`: existing reference implementations
- `job-store/app.py`: `ATS_DETECTORS` and `probe_embedded_ats()`
- `firefox-plugin/lib.js`: `deriveCareersUrl()` and `JOB_SPECIFIER_PARAMS`
