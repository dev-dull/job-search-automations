# firefox-plugin

Firefox MV3 extension. Thin client to the `job-store` backend: extract the job description from whatever page you're on, POST it to the backend, render the score that comes back. Also surfaces a "watch this company" button when the page is on a supported ATS so you can add it to the poller's company_targets in one click.

The plugin holds no API key, no resume, and no scoring prompt. Server-side scoring lives in job-store; see [`../docs/server-side-scoring.md`](../docs/server-side-scoring.md) for the rationale.

## What it does

- Recognizes job postings on Greenhouse, Ashby, Lever, Workday, LinkedIn, iCIMS, BambooHR, Workable, Gem, Rippling, and IBM Careers.
- Extracts the JD via in-page DOM selectors (tier-1 ATS-specific, tier-2 generic), with fallbacks to canonical APIs (Greenhouse `boards-api` and Ashby `posting-api`) when the in-page DOM is JS-rendered.
- POSTs the JD to `job-store/jobs/score` (no `fit_score` in the body so the backend scores it). Renders the structured response.
- Local cache of recent scores so re-opening on the same URL renders instantly without round-tripping the backend.
- Toolbar badge shows the score number for any page you've already scored. On a cache miss, the background script asks the backend whether it has a score for this URL.
- "Watch this company" button POSTs to `/companies` to add the page's company to the poller's targets, when the page is on a supported ATS.
- Recognizes its own backend URL and shows a friendly "you're on the inbox" state instead of trying to score the inbox itself.

## Architecture

- **`manifest.json`** Manifest V3. `host_permissions` for the supported ATS hosts plus localhost (for the default backend URL). `optional_host_permissions: ["*://*/*"]` so the user can grant access to a hosted backend URL at runtime.
- **`background.js`** Sets the briefcase emoji icon via OffscreenCanvas. Listens to `tabs.onActivated`, `tabs.onUpdated`, and `browser.storage.onChanged` for `scoreCache` to keep the toolbar badge in sync per tab.
- **`lib.js`** Loaded into both popup and background. Holds the score cache, the URL canonicalizer (`linkedin:<id>` / `gh:<id>` / `ashby:<id>` collapse forms), the careers-index URL detector, and the backend-lookup fallback.
- **`popup.html` / `popup.js` / `popup.css`** The action popup. Handles the extract → POST → render flow, the cache notice, the watch bar, the Re-score button, the manual-paste fallback, and the "this is your job-store inbox" state.
- **`options.html` / `options.js` / `options.css`** Settings page. Only setting is the backend URL. Localhost works out of the box; any other host triggers a one-time Firefox permission prompt the first time you click Save or Test.

## Install from the job board (recommended)

If the backend is serving a signed `.xpi`, the inbox page shows a **⤓ Install
Firefox extension** link (top-right). Click it and confirm Firefox's prompt —
that's it. The `.xpi` is Mozilla-signed (unlisted), so it installs on stock
release Firefox with **no `about:config` changes** and persists across restarts.
"Unlisted" just means it isn't in AMO's public catalog; only people using your
job board ever see it.

### Publishing a signed version (maintainer)

Signing is automated by [`.github/workflows/sign-extension.yaml`](../../.github/workflows/sign-extension.yaml).

**One-time setup:**
1. Create a free [addons.mozilla.org](https://addons.mozilla.org/) developer
   account and generate API credentials at
   `https://addons.mozilla.org/developers/addon/api/key/`.
2. Add them as repo secrets: `AMO_JWT_ISSUER` and `AMO_JWT_SECRET`.

**Each release:**
1. Bump `manifest.json` `version` (AMO refuses to re-sign an existing version).
2. Publish a GitHub Release. `sign-extension.yaml` runs `web-ext sign
   --channel=unlisted`, attaches the signed `.xpi` (and a stable-named
   `job-fit-scorer.xpi`) to the release, then triggers an image rebuild.
3. The image rebuild bakes the signed `.xpi` into `ghcr.io/.../job-store:latest`
   (the build pulls `releases/latest/download/job-fit-scorer.xpi`). Pull the new
   image and the `/extension` install link works under both `docker run` and
   Helm — nothing else to configure. Backend-only image builds also re-bake the
   current plugin, so it's never dropped.

The `/extension` route serves the `.xpi` from `EXTENSION_DIST_DIR` (the image
sets this to `/app/extension`; for a local `flask run` it defaults to
`firefox-plugin/dist/`). The route 404s and the inbox hides the link when no
`.xpi` is present.

**Local signing** (instead of CI):

```bash
cd job-board/firefox-plugin
npx web-ext sign --channel=unlisted \
  --api-key=$AMO_JWT_ISSUER --api-secret=$AMO_JWT_SECRET \
  --artifacts-dir=dist
```

**Auto-update** is wired but off by default: job-store serves
`/extension/updates.json`, but Firefox only consults it if the *signed* xpi's
`browser_specific_settings.gecko.update_url` points at one canonical HTTPS host.
Since each operator self-hosts a different host, we don't bake a URL in — set it
in your own build if you want background updates; otherwise users re-click
Install to upgrade.

## Install (development)

1. Open `about:debugging#/runtime/this-firefox` in Firefox.
2. Click **Load Temporary Add-on…**.
3. Pick `manifest.json` from this directory.
4. The options page opens on first install. Set the backend URL to `http://127.0.0.1:5000` (the job-store default).
5. Click Test. If the backend's reachable you'll see "Reachable. Currently N open jobs in inbox."
6. Click Save.
7. Visit a job posting on any supported ATS and click the briefcase icon in the toolbar.

Temporary add-ons are unloaded when Firefox restarts; re-pick the manifest each session. For a persistent install, use the signed flow above ([Install from the job board](#install-from-the-job-board-recommended)).

## Pointing at a hosted backend

When you move job-store off `127.0.0.1`, change the backend URL in the options page. The first time you click Save (or Test) on a non-localhost URL, Firefox prompts to grant the extension access to that host. Accept it. The plugin retries the test automatically.

To revoke later: `about:addons` → Job Fit Scorer → Permissions → uncheck the host.

## Local cache

The plugin stores the most recent ~200 scoring results in `browser.storage.local` under `scoreCache`, keyed by the canonical posting id (`gh:N`, `ashby:N`, `linkedin:N`, or a normalized URL for non-canonicalizable pages). On a cache hit, the popup renders instantly and the toolbar badge picks up the score number; on a cache miss, the background script asks the backend via `GET /jobs/score?url=<encoded>` and populates the cache from there.

The Re-score button bypasses both caches and forces a fresh `force: true` POST to the backend.

## Manual paste

If the JD can't be extracted from the page (JS-heavy ATS that doesn't expose a useful API, paywalled content, etc.), the popup shows a "no job posting detected" state with a textarea. Paste the JD and click "Score this." Manual paste skips the local cache (since there's no stable URL key) but still POSTs to the backend so the row lands in the inbox.

## Files

| File | Purpose |
|---|---|
| `manifest.json` | MV3 manifest, permissions, optional host permissions for non-localhost backends |
| `background.js` | Toolbar badge updater, briefcase icon paint, options-page-on-install |
| `lib.js` | Shared helpers: URL canonicalization, cache, careers-index detection, badge paint |
| `popup.html` / `popup.js` / `popup.css` | Action popup: extract, render, watch bar, manual paste |
| `options.html` / `options.js` / `options.css` | Settings: backend URL with permission grant |
