"use strict";

// Open the options page the first time the extension is installed so the user
// is prompted to paste an API key + resume.
browser.runtime.onInstalled.addListener(({ reason }) => {
  if (reason === "install") {
    browser.runtime.openOptionsPage();
  }
});

// Toolbar icon: render the briefcase emoji into an OffscreenCanvas so the
// extension's action button shows 💼 instead of a placeholder puzzle piece.
// Done at runtime (rather than shipping a static PNG) so the emoji matches
// the user's OS font. The score badge from setBadgeText draws on top, so
// users still see the fit score for any job they've previously scored.
async function paintBriefcaseIcon() {
  const sizes = [16, 32];
  const imageData = {};
  for (const size of sizes) {
    const canvas = new OffscreenCanvas(size, size);
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, size, size);
    // Slightly under-fill so the emoji has room to breathe at the edges.
    ctx.font = `${Math.round(size * 0.85)}px "Apple Color Emoji", "Segoe UI Emoji", "Noto Color Emoji", sans-serif`;
    ctx.textBaseline = "middle";
    ctx.textAlign = "center";
    ctx.fillText("💼", size / 2, size / 2 + size * 0.04);
    imageData[size] = ctx.getImageData(0, 0, size, size);
  }
  try {
    await browser.action.setIcon({ imageData });
  } catch (err) {
    console.warn("Failed to paint briefcase icon:", err);
  }
}
paintBriefcaseIcon();

// Badge text reflects the cached fit-score for the active tab's URL. Set
// per-tab so switching tabs surfaces the score for whichever posting is in
// front. Tabs with no cached score show no badge.
// Update the toolbar badge for `tab`.
//
// `allowClear` controls what happens when we don't find a score:
//   true  — actively clear the badge (used when the URL changed to a page
//           that genuinely has no score)
//   false — leave any existing badge alone (used for transient events like
//           focus changes, popup close, status:complete refires)
//
// The asymmetry matters: Firefox fires `tabs.onActivated` when the action
// popup closes, and `tab.url` is sometimes briefly empty / storage reads can
// momentarily miss. Without `allowClear=false`, those transient signals
// would wipe a perfectly-good badge.
async function updateBadgeForTab(tab, { allowClear = true } = {}) {
  if (!tab?.id || !tab.url) return;

  if (!isScoreableUrl(tab.url)) {
    if (allowClear) await paintBadgeForTab(tab.id, null);
    return;
  }

  // Cache first. On miss, ask the backend — covers (a) entries lost to URL
  // canonicalization, (b) jobs scored on another browser, (c) freshly-installed
  // extension instances that haven't seen this URL yet.
  let entry = await getCachedScore(tab.url);
  if (!entry) {
    const { backendUrl } = await browser.storage.local.get(["backendUrl"]);
    if (backendUrl) {
      entry = await lookupBackendScore(tab.url, backendUrl);
      if (entry) {
        try { await setCachedScore(tab.url, entry); } catch { /* ignore */ }
      }
    }
  }

  const score = fitScoreFromCacheEntry(entry);
  if (score != null) {
    await paintBadgeForTab(tab.id, score);
  } else if (allowClear) {
    await paintBadgeForTab(tab.id, null);
  }
  // else: keep the existing badge — a transient event shouldn't wipe it.
}

browser.tabs.onActivated.addListener(async ({ tabId }) => {
  const tab = await browser.tabs.get(tabId).catch(() => null);
  // Tab focus changed; the URL hasn't necessarily changed. Don't clear.
  if (tab) await updateBadgeForTab(tab, { allowClear: false });
});

browser.tabs.onUpdated.addListener(async (tabId, changeInfo, tab) => {
  if (changeInfo.url) {
    // URL navigated to a new posting (or a non-job page). The previous
    // badge is stale; clear if the new URL has no cached score.
    await updateBadgeForTab(tab, { allowClear: true });
  } else if (changeInfo.status === "complete") {
    // Page finished loading at same URL — refresh score, don't clear.
    await updateBadgeForTab(tab, { allowClear: false });
  }
});

// Cache write from the popup — refresh the active tab's badge but never
// clear it from this path (the popup's own painting already cleared it
// if it needed to, and a tab-event will catch any stragglers).
browser.storage.onChanged.addListener(async (changes, area) => {
  if (area !== "local" || !changes.scoreCache) return;
  const [tab] = await browser.tabs.query({ active: true, currentWindow: true });
  if (tab) await updateBadgeForTab(tab, { allowClear: false });
});

// Initial paint on startup. Allow clearing here so a stale badge from a
// previous session doesn't survive into a non-job tab.
(async () => {
  const [tab] = await browser.tabs.query({ active: true, currentWindow: true });
  if (tab) await updateBadgeForTab(tab, { allowClear: true });
})();
