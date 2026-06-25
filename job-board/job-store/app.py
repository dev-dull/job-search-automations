"""
Job-store backend.

Run for development:

    pip install -r requirements.txt
    flask --app app run --port 5000

The Firefox plugin posts scored jobs to /jobs/score; humans triage them via
the ranked list at /.
"""

import json
import re
from datetime import datetime, date, timedelta, timezone

import glob
import hashlib
import os
import zipfile

from flask import (Flask, abort, jsonify, redirect, render_template, request,
                   send_file, url_for)

import db
import ranking
from urls import canonicalize_url


app = Flask(__name__)
db.init_db()


# ---------------------------------------------------------------------------
# ATS auto-detection — maps a careers URL to (platform, identifier dict).
# Identifier shape is platform-specific; the bot will use these fields to
# build the correct list-jobs API call.
# ---------------------------------------------------------------------------

def _workday_extract(m):
    """Workday tenants come in two URL shapes:
       /<lang>/<site>  (NVIDIA: /en-US/NVIDIAExternalCareerSite)
       /<site>         (Red Hat: /jobs, Autodesk: /Ext, Intel: /External)
    Treat the trailing segment as the site in both cases."""
    first = m.group("first")
    second = m.group("second")
    if second:
        return {"host": m.group("host"), "lang": first, "site": second}
    return {"host": m.group("host"), "site": first}


ATS_DETECTORS = [
    (
        re.compile(r"(?:job-)?boards\.greenhouse\.io/(?:embed/job_board/[^/?#]+/jobs\?for=)?([^/?#]+)"),
        "greenhouse",
        lambda m: {"board": m.group(1)},
    ),
    (re.compile(r"jobs\.lever\.co/([^/?#]+)"), "lever",
     lambda m: {"company": m.group(1)}),
    (re.compile(r"jobs\.ashbyhq\.com/([^/?#]+)"), "ashby",
     lambda m: {"org": m.group(1)}),
    (re.compile(
        r"(?P<host>[a-z0-9-]+\.[a-z0-9-]+\.myworkdayjobs\.com)"
        r"/(?P<first>[^/?#]+)(?:/(?P<second>[^/?#]+))?"
     ),
     "workday",
     _workday_extract),
]


def detect_ats(careers_url):
    """Return (ats_platform, identifier_dict). Falls back to ('other', {})."""
    for pattern, platform, extractor in ATS_DETECTORS:
        m = pattern.search(careers_url)
        if m:
            return platform, extractor(m)
    return "other", {}


# Regex patterns used to find an embedded ATS on a non-ATS careers page.
# Order matters: greenhouse-embed marker first because it's the unambiguous
# signal (board token in querystring).
EMBED_PROBES = [
    (re.compile(r"boards\.greenhouse\.io/embed/job_board/js\?for=([^\"'&]+)"),
     lambda m: ("greenhouse", f"https://boards.greenhouse.io/{m.group(1)}")),
    (re.compile(r"(?:job-)?boards\.greenhouse\.io/([a-z0-9_-]+)/jobs"),
     lambda m: ("greenhouse", f"https://boards.greenhouse.io/{m.group(1)}")),
    (re.compile(r"jobs\.ashbyhq\.com/([a-z0-9_-]+)"),
     lambda m: ("ashby", f"https://jobs.ashbyhq.com/{m.group(1)}")),
    (re.compile(r"jobs\.lever\.co/([a-z0-9_-]+)"),
     lambda m: ("lever", f"https://jobs.lever.co/{m.group(1)}")),
    (re.compile(r"([a-z0-9-]+\.[a-z0-9-]+\.myworkdayjobs\.com)/([^/\"']+)/([^/?#\"']+)"),
     lambda m: ("workday", f"https://{m.group(1)}/{m.group(2)}/{m.group(3)}")),
]


def probe_embedded_ats(careers_url, timeout=8):
    """Fetch the careers URL and scan for an embedded supported ATS.

    Returns (resolved_url, ats_platform) if found, else None. Useful when a
    company's careers page is a wrapper (e.g. company.com/careers) that
    embeds a Greenhouse board via the standard <script> tag — we'd rather
    extract the canonical URL than reject the input outright.
    """
    import urllib.request
    req = urllib.request.Request(
        careers_url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; job-store/0.1)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(500_000).decode("utf-8", errors="replace")
    except Exception:
        return None
    for pattern, resolver in EMBED_PROBES:
        m = pattern.search(body)
        if m:
            platform, resolved = resolver(m)
            return resolved, platform
    return None


# ---------------------------------------------------------------------------
# Page rendering helpers
# ---------------------------------------------------------------------------

def _age_label(posted_at, discovered_at):
    """Human-readable age string for the row."""
    target = posted_at or discovered_at
    if not target:
        return "—"
    if isinstance(target, str):
        try:
            target = datetime.fromisoformat(target[:19].replace("T", " "))
        except ValueError:
            try:
                target = datetime.combine(date.fromisoformat(target[:10]),
                                          datetime.min.time())
            except ValueError:
                return "—"
    if isinstance(target, date) and not isinstance(target, datetime):
        target = datetime.combine(target, datetime.min.time())
    days = (datetime.now() - target).days
    prefix = "discovered" if posted_at is None else "posted"
    if days <= 0:
        return f"{prefix} today"
    if days == 1:
        return f"{prefix} 1 day ago"
    if days < 30:
        return f"{prefix} {days} days ago"
    if days < 90:
        return f"{prefix} {days // 7}w ago"
    return f"{prefix} {days // 30}mo ago"


def _derive_recommendation(fit_score):
    """Map a 1-100 fit_score into the recommendation bucket the UI styles by.
    Matches the client-side derivation in firefox-plugin/popup.js."""
    if fit_score is None:
        return None
    if fit_score >= 80:
        return "strong_match"
    if fit_score >= 60:
        return "consider"
    if fit_score >= 40:
        return "weak_match"
    return "skip"


def _normalize_analysis(analysis):
    """Tolerant adapter: accepts either the old plugin shape (summary,
    strengths, gaps, recommendation) or the new Gemini-aligned shape
    (candidate_explanation, candidate_strengths, candidate_deficiencies)
    and returns the keys the templates expect."""
    return {
        "summary": analysis.get("candidate_explanation") or analysis.get("summary") or "",
        "strengths": analysis.get("candidate_strengths") or analysis.get("strengths") or [],
        "gaps": analysis.get("candidate_deficiencies") or analysis.get("gaps") or [],
        "recommendation": analysis.get("recommendation"),
    }


def _decorate(job):
    """Add derived fields the template needs without round-tripping JSON in Jinja."""
    analysis = db.parse_analysis(job.get("analysis_json"))
    normalized = _normalize_analysis(analysis)
    job["summary"] = normalized["summary"]
    job["recommendation"] = (
        normalized["recommendation"] or _derive_recommendation(job.get("fit_score"))
    )
    job["strengths"] = normalized["strengths"]
    job["gaps"] = normalized["gaps"]
    job["age_label"] = _age_label(job.get("posted_at"), job.get("discovered_at"))
    return job


def _with_live_rank(jobs):
    """Overwrite each job's `rank_score` with a freshly-computed value so
    older listings drift down naturally without needing a /admin/rerank pass.
    Platform stats are computed once per ATS per request so a 500-row inbox
    is N+1 queries (one per ATS), not 2N.
    """
    platform_cache = {}
    for job in jobs:
        ats = job.get("ats_platform")
        if ats not in platform_cache:
            platform_cache[ats] = db.get_platform_stats(ats)
        job["rank_score"] = ranking.compute_rank_score(
            job.get("fit_score"),
            job.get("posted_at"),
            platform_cache[ats],
            discovered_at=job.get("discovered_at"),
            desirability_score=job.get("desirability_score"),
        )
    return jobs


def _slug(value):
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower()) or "unknown"


def _generate_branch_name(job):
    """Match the existing convention: `companyName-YYYYMMDD`."""
    company = _slug(job.get("company"))
    return f"{company}-{datetime.utcnow().strftime('%Y%m%d')}"


# ---------------------------------------------------------------------------
# Routes — UI
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    status = request.args.get("status", "open")
    # `rank_score` is now computed at read time (see `_with_live_rank`) so
    # there's no point sorting by the stored column in SQL — pull rows in an
    # arbitrary stable order, then sort in Python after recomputing.
    if status == "open":
        jobs = db.list_jobs(statuses=["discovered", "ranked"], order="id DESC")
    elif status == "applied":
        jobs = db.list_jobs(statuses=["applied"], order="applied_at DESC")
    elif status == "closed":
        jobs = db.list_jobs(statuses=["closed"], order="discovered_at DESC")
    else:
        jobs = db.list_jobs(order="id DESC")

    if status in ("open", "all", "applied"):
        # Applied jobs we also live-recompute, so the user sees how stale a
        # waiting application is. Closed rows keep their stored rank — they
        # aren't sort keys for that view anyway.
        jobs = _with_live_rank(jobs)
        if status == "open":
            jobs.sort(
                key=lambda j: (
                    -(j.get("rank_score") if j.get("rank_score") is not None else -1),
                    -(j.get("fit_score") or 0),
                ),
            )

    cleanup_days = int(db.get_setting(CLEANUP_DAYS_KEY) or DEFAULT_CLEANUP_DAYS)
    # Encoded by /admin/cleanup as "age,dead,total" so we can render a
    # one-line summary without needing a session/flash mechanism.
    cleanup_summary = None
    raw = request.args.get("cleanup")
    if raw:
        try:
            age, dead, total = (int(x) for x in raw.split(","))
            cleanup_summary = {"age": age, "dead": dead, "total": total}
        except (ValueError, TypeError):
            pass

    return render_template(
        "index.html",
        jobs=[_decorate(j) for j in jobs],
        counts=db.status_counts(),
        stats=db.platform_stats_summary(),
        filter_status=status,
        cleanup_days=cleanup_days,
        cleanup_summary=cleanup_summary,
        extension_available=_extension_xpi() is not None,
    )


# ---------------------------------------------------------------------------
# Routes — JSON ingest
# ---------------------------------------------------------------------------

@app.route("/jobs/score", methods=["GET", "POST", "OPTIONS"])
def score_job():
    if request.method == "OPTIONS":
        return ("", 204)

    # GET path: read-only lookup used by the toolbar badge to surface the
    # existing score for a URL without provoking another Anthropic call. The
    # plugin's local cache is the fast path; this is the fallback when the
    # cache is cold (different browser, after a clear, post-canonicalization).
    if request.method == "GET":
        raw = (request.args.get("url") or "").strip()
        if not raw:
            return jsonify({"error": "url is required"}), 400
        lookup_url = canonicalize_url(raw)
        existing = db.get_job_by_url(lookup_url)
        if not existing or existing.get("fit_score") is None:
            return jsonify({"error": "not scored"}), 404
        stats = db.get_platform_stats(existing.get("ats_platform"))
        live_rank = ranking.compute_rank_score(
            existing["fit_score"], existing.get("posted_at"), stats,
            discovered_at=existing.get("discovered_at"),
            desirability_score=existing.get("desirability_score"),
        )
        return jsonify({
            "id": existing["id"],
            "rank_score": live_rank,
            "status": existing.get("status"),
            "fit_score": existing["fit_score"],
            "company": existing.get("company"),
            "analysis": db.parse_analysis(existing.get("analysis_json")),
        }), 200

    payload = request.get_json(silent=True) or {}
    url = (payload.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url is required"}), 400

    # Manual pastes from the plugin all use a constant URL — make each one
    # unique server-side so we don't overwrite each other.
    if url.startswith("manual-paste"):
        url = f"manual-paste-{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}"
    else:
        # Normalize so the plugin's browser-tab URL and the poller's API
        # URL converge to one row (no trailing-slash / tracking-param drift).
        url = canonicalize_url(url)

    fit_score = payload.get("fit_score")
    analysis = payload.get("analysis")
    posted_at = payload.get("posted_at")
    description = payload.get("description")
    company = payload.get("company")
    force = bool(payload.get("force"))

    # Dedupe: if we've already scored this canonical URL, return the existing
    # analysis without spending another Anthropic call. The plugin's local
    # cache used to handle this client-side, but URL canonicalization
    # invalidated old cache entries; this guard catches any pre-existing row
    # for the canonical URL regardless of which surface discovered it first.
    # The Re-score button passes `force: true` to override.
    if fit_score is None and not force:
        existing = db.get_job_by_url(url)
        if existing and existing.get("fit_score") is not None:
            cached_analysis = db.parse_analysis(existing.get("analysis_json"))
            stats = db.get_platform_stats(existing.get("ats_platform"))
            live_rank = ranking.compute_rank_score(
                existing["fit_score"], existing.get("posted_at"), stats,
                discovered_at=existing.get("discovered_at"),
                desirability_score=existing.get("desirability_score"),
            )
            return jsonify({
                "id": existing["id"],
                "rank_score": live_rank,
                "status": existing.get("status"),
                "fit_score": existing["fit_score"],
                "company": existing.get("company"),
                "analysis": cached_analysis,
                "cached": True,
            }), 200

    # Server-side scoring path: caller (e.g. the poller) didn't compute a
    # score, but did send a JD. Run it through Anthropic here. The plugin
    # currently still scores client-side and sends fit_score+analysis, so it
    # bypasses this branch — letting the migration to server-side scoring
    # happen incrementally per SERVER_SIDE_SCORING.md.
    usage_meta = None
    if fit_score is None and description and len(description) >= 100:
        try:
            from anthropic_client import score_job as _score_job
            result = _score_job(
                description=description,
                url=url,
                title=payload.get("title"),
                ats_platform=payload.get("ats_platform"),
            )
            analysis = result["fit"]
            fit_score = analysis.get("candidate_score")
            company = company or analysis.get("job_company_name")
            usage_meta = result["usage"]
        except Exception as err:
            return jsonify({"error": f"scoring failed: {err}"}), 502

    # Desirability ("do I want it") — present only when preferences are
    # configured and the row was scored server-side; None for legacy plugin
    # analyses, which keeps ranking on fit alone.
    desirability_score = analysis.get("desirability_score") if analysis else None

    job_id = db.upsert_job(
        url=url,
        company=company,
        title=payload.get("title"),
        description=description,
        ats_platform=payload.get("ats_platform"),
        posted_at=posted_at,
        discovered_by=payload.get("discovered_by", "plugin"),
        fit_score=fit_score,
        analysis_json=json.dumps(analysis) if analysis else None,
        desirability_score=desirability_score,
    )

    rank = ranking.compute_rank_score(
        fit_score, posted_at,
        db.get_platform_stats(payload.get("ats_platform")),
        discovered_at=datetime.utcnow().isoformat(),
        desirability_score=desirability_score,
    )
    new_status = "ranked" if fit_score is not None else "discovered"
    db.update_rank_score(job_id, rank, status=new_status)

    response_body = {
        "id": job_id,
        "rank_score": rank,
        "status": new_status,
        "fit_score": fit_score,
        "company": company,
        "analysis": analysis,
    }
    if usage_meta is not None:
        response_body["usage"] = usage_meta
    return jsonify(response_body), 200


@app.route("/resume", methods=["GET"])
def get_resume():
    """Return the on-disk resume contents. Used by the plugin (eventually) to
    bootstrap or refresh its cached copy, and useful for sanity-checking which
    resume file the backend will feed to Anthropic during scoring."""
    try:
        from anthropic_client import read_resume
        return jsonify(read_resume())
    except FileNotFoundError as err:
        return jsonify({"error": str(err)}), 404
    except Exception as err:
        return jsonify({"error": str(err)}), 500


@app.route("/jobs", methods=["GET"])
def list_jobs_json():
    # Mirror the UI's status filter: "open" is a logical state, not a row
    # value, so expand it to ['discovered', 'ranked'] like the index route
    # does. "all" / unset → no filter.
    status = request.args.get("status")
    if status in (None, "", "all"):
        statuses = None
    elif status == "open":
        statuses = ["discovered", "ranked"]
    else:
        statuses = [status]
    # Pull in stable order then re-rank live so consumers see fresh scores.
    jobs = db.list_jobs(statuses=statuses, order="id DESC")
    jobs = _with_live_rank(jobs)
    jobs.sort(
        key=lambda j: -(j.get("rank_score") if j.get("rank_score") is not None else -1),
    )
    return jsonify([_decorate(j) for j in jobs])


# ---------------------------------------------------------------------------
# Routes — actions (form-encoded, redirect back to /)
# ---------------------------------------------------------------------------

@app.route("/jobs/<int:job_id>/apply", methods=["POST"])
def apply_job(job_id):
    row = db.get_job(job_id)
    if not row:
        abort(404)
    branch = (request.form.get("branch") or "").strip() or _generate_branch_name(dict(row))
    db.mark_applied(job_id, branch)
    db.upsert_outcome(job_id)  # ensure outcomes row exists
    return redirect(url_for("index", status="applied"))


@app.route("/jobs/<int:job_id>/dismiss", methods=["POST"])
def dismiss_job(job_id):
    db.update_status(job_id, "closed")
    return redirect(url_for("index", status=request.args.get("status", "open")))


@app.route("/jobs/<int:job_id>/outcome", methods=["POST"])
def record_outcome(job_id):
    rejected = bool(request.form.get("rejected"))
    db.upsert_outcome(
        job_id,
        referral=bool(request.form.get("referral")),
        callback=bool(request.form.get("callback")),
        callback_at=request.form.get("callback_at") or None,
        ghosted=bool(request.form.get("ghosted")),
        rejected=rejected,
        offer=bool(request.form.get("offer")),
        notes=(request.form.get("notes") or "").strip(),
    )
    # A rejection is a terminal state; auto-close so it leaves the applied
    # list without the user having to click Dismiss separately. The outcome
    # row stays intact (cascade-protected), so historical stats are kept.
    if rejected:
        db.update_status(job_id, "closed")
    return redirect(url_for("index", status="applied"))


CLEANUP_DAYS_KEY = "cleanup_days_threshold"
DEFAULT_CLEANUP_DAYS = 35

# Reasonable parallelism for the URL-liveness sweep. Most ATSes respond within
# a second; 10 in flight keeps the cleanup snappy without hammering any one
# host (each posting is on a distinct domain/board in practice).
LIVENESS_WORKERS = 10
LIVENESS_TIMEOUT = 8


def is_url_dead(url):
    """Return True only when the URL clearly resolves to 404 / 410. Network
    errors, timeouts, and other non-success statuses return False — we'd
    rather keep a live job around as 'maybe alive' than delete on noise."""
    import urllib.error
    import urllib.request
    if not url or url.startswith("manual-paste"):
        return False
    headers = {"User-Agent": "Mozilla/5.0 (compatible; job-store-cleanup/0.1)"}
    # Try HEAD first; many servers reject HEAD (405) so fall back to GET.
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, method=method, headers=headers)
            with urllib.request.urlopen(req, timeout=LIVENESS_TIMEOUT):
                return False
        except urllib.error.HTTPError as err:
            if err.code in (404, 410):
                return True
            if err.code == 405 and method == "HEAD":
                continue
            return False
        except Exception:
            return False
    return False


def _parse_discovered_at(value):
    """Best-effort parse of `jobs.discovered_at` (SQLite stores as TEXT)."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace(" ", "T"))
    except ValueError:
        return None


@app.route("/admin/cleanup", methods=["POST"])
def cleanup_stale():
    # Days threshold — form override if provided, otherwise the saved setting,
    # otherwise the default. Persist whatever was used so the input keeps the
    # last value next time the user opens the inbox.
    raw_days = (request.form.get("days") or "").strip()
    if raw_days:
        try:
            days = max(1, min(365, int(raw_days)))
        except ValueError:
            days = int(db.get_setting(CLEANUP_DAYS_KEY) or DEFAULT_CLEANUP_DAYS)
    else:
        days = int(db.get_setting(CLEANUP_DAYS_KEY) or DEFAULT_CLEANUP_DAYS)
    db.set_setting(CLEANUP_DAYS_KEY, days)

    cutoff = datetime.utcnow() - timedelta(days=days)
    candidates = db.list_cleanup_candidates()

    age_stale_ids = []
    liveness_targets = []
    for row in candidates:
        discovered = _parse_discovered_at(row.get("discovered_at"))
        if discovered and discovered < cutoff:
            age_stale_ids.append(row["id"])
        else:
            liveness_targets.append(row)

    # Liveness sweep in parallel — bounded by LIVENESS_WORKERS so we don't
    # spawn a thread per candidate on a 500-row DB.
    dead_ids = []
    if liveness_targets:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=LIVENESS_WORKERS) as pool:
            futures = {pool.submit(is_url_dead, r["url"]): r["id"] for r in liveness_targets}
            for fut, jid in futures.items():
                try:
                    if fut.result():
                        dead_ids.append(jid)
                except Exception:
                    pass

    to_delete = set(age_stale_ids) | set(dead_ids)
    deleted = db.delete_jobs(list(to_delete))

    return redirect(url_for(
        "index",
        status=request.form.get("status", "open"),
        cleanup=f"{len(age_stale_ids)},{len(dead_ids)},{deleted}",
    ))


@app.route("/admin/rerank", methods=["POST"])
def rerank_all():
    """Recompute rank_score for every job. Cheap; safe to run any time.
    Mostly cosmetic now that the inbox reads rank live, but useful for any
    external SQL consumer that queries `rank_score` directly."""
    for job in db.all_jobs_for_rerank():
        rank = ranking.compute_rank_score(
            job["fit_score"], job["posted_at"],
            db.get_platform_stats(job["ats_platform"]),
            discovered_at=job.get("discovered_at"),
            desirability_score=job.get("desirability_score"),
        )
        db.update_rank_score(job["id"], rank)
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Routes — targeted companies (config for the discovery bot)
# ---------------------------------------------------------------------------

def _decorate_target(target):
    """Add derived fields the template needs."""
    target = db.parse_company_target(target)
    target["last_polled_label"] = _age_label(None, target.get("last_polled_at"))
    return target


def _infer_name_from_identifier(careers_url, ats_platform, identifier):
    """Best-effort default name when the user doesn't provide one."""
    if not identifier:
        # Fall back to the URL's hostname
        m = re.search(r"https?://([^/]+)", careers_url)
        return (m.group(1) if m else "Unnamed").split(".")[0].capitalize()
    for key in ("board", "company", "org"):
        if identifier.get(key):
            return identifier[key].replace("-", " ").replace("_", " ").title()
    if ats_platform == "workday" and identifier.get("host"):
        return identifier["host"].split(".")[0].capitalize()
    return "Unnamed"


def _render_companies(add_error=None, add_url="", add_name="", status=200):
    targets = [_decorate_target(t) for t in db.list_company_targets()]
    return (
        render_template(
            "companies.html",
            targets=targets,
            add_error=add_error,
            add_url=add_url,
            add_name=add_name,
        ),
        status,
    )


SUPPORTED_ATSES = ("Greenhouse", "Ashby", "Lever", "Workday")


@app.route("/companies", methods=["GET"])
def companies_index():
    return _render_companies()


@app.route("/companies", methods=["POST"])
def create_company():
    careers_url = (request.form.get("careers_url") or "").strip()
    name = (request.form.get("name") or "").strip()
    if not careers_url:
        return _render_companies(
            add_error="Careers URL is required.",
            add_url=careers_url, add_name=name, status=400,
        )

    ats_platform, identifier = detect_ats(careers_url)

    # If the URL itself doesn't match a known ATS, fetch it and see if it
    # *embeds* one. This catches the common case where a user pastes a
    # wrapper page like https://www.company.com/careers/ that pulls in a
    # Greenhouse board via the standard embed script.
    if ats_platform == "other":
        probe = probe_embedded_ats(careers_url)
        if probe is not None:
            resolved_url, _ = probe
            ats_platform, identifier = detect_ats(resolved_url)
            careers_url = resolved_url  # store the canonical URL
        else:
            supported = ", ".join(SUPPORTED_ATSES)
            return _render_companies(
                add_error=(
                    f"{careers_url} isn't on a supported ATS and doesn't embed one. "
                    f"The poller only knows how to talk to {supported}. "
                    f"Try a direct job-board URL — e.g., "
                    f"https://boards.greenhouse.io/<board>, "
                    f"https://jobs.ashbyhq.com/<org>, "
                    f"https://jobs.lever.co/<company>, or "
                    f"https://<tenant>.<region>.myworkdayjobs.com/<lang>/<site>."
                ),
                add_url=careers_url, add_name=name, status=400,
            )

    if not name:
        name = _infer_name_from_identifier(careers_url, ats_platform, identifier)
    try:
        db.create_company_target(
            name=name,
            careers_url=careers_url,
            ats_platform=ats_platform,
            ats_identifier=identifier,
        )
    except Exception as err:
        # Most likely a UNIQUE constraint violation on careers_url
        return _render_companies(
            add_error=f"Could not add company: {err}",
            add_url=careers_url, add_name=name, status=400,
        )
    return redirect(url_for("companies_index"))


@app.route("/companies/<int:target_id>", methods=["POST"])
def update_company(target_id):
    if not db.get_company_target(target_id):
        abort(404)
    name = (request.form.get("name") or "").strip() or None
    deny_text = request.form.get("deny_list", "")
    deny_list = [line.strip() for line in deny_text.splitlines() if line.strip()]
    db.update_company_target(target_id, name=name, deny_list=deny_list)
    return redirect(url_for("companies_index"))


@app.route("/companies/<int:target_id>/delete", methods=["POST"])
def delete_company(target_id):
    db.delete_company_target(target_id)
    return redirect(url_for("companies_index"))


@app.route("/companies.json", methods=["GET"])
def companies_json():
    """Bot reads this to know which companies to poll and how to filter."""
    return jsonify([_decorate_target(t) for t in db.list_company_targets()])


@app.route("/companies/<int:target_id>/polled", methods=["POST"])
def mark_company_polled(target_id):
    """Poller stamps last_polled after a run (replaces its old direct DB write).
    The server owns the timestamp so there's a single clock."""
    if not db.get_company_target(target_id):
        abort(404)
    data = request.get_json(silent=True) or {}
    db.update_company_target(
        target_id,
        last_polled_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        last_polled_count=data.get("last_polled_count"),
    )
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Poller-facing read/config endpoints. These let the poller run as a pure HTTP
# client (no jobs.db access), so it can run as an out-of-cluster CronJob.
# ---------------------------------------------------------------------------

# Location allow/deny lists persisted in the settings table. The poller holds
# the defaults applied when these are unset.
LOCATION_ALLOWLIST_KEY = "location_allowlist"
LOCATION_DENYLIST_KEY = "location_denylist"


@app.route("/jobs/urls", methods=["GET"])
def jobs_urls():
    """All stored job URLs, for the poller's dedupe / stop-when-seen."""
    return jsonify({"urls": db.all_job_urls()})


@app.route("/settings/locations", methods=["GET"])
def get_location_settings():
    """Raw stored allow/deny CSV strings (null when unset; poller applies its
    own defaults)."""
    return jsonify({
        "allowlist": db.get_setting(LOCATION_ALLOWLIST_KEY),
        "denylist": db.get_setting(LOCATION_DENYLIST_KEY),
    })


@app.route("/settings/locations", methods=["POST"])
def set_location_settings():
    """Persist allow/deny CSV strings. Sets only the keys present in the body."""
    data = request.get_json(silent=True) or {}
    if "allowlist" in data:
        db.set_setting(LOCATION_ALLOWLIST_KEY, data["allowlist"])
    if "denylist" in data:
        db.set_setting(LOCATION_DENYLIST_KEY, data["denylist"])
    return jsonify({
        "allowlist": db.get_setting(LOCATION_ALLOWLIST_KEY),
        "denylist": db.get_setting(LOCATION_DENYLIST_KEY),
    })


# ---------------------------------------------------------------------------
# Firefox extension distribution — install from the inbox instead of
# about:debugging. The signed (unlisted) .xpi is a CI build artifact
# (web-ext sign); drop it in EXTENSION_DIST_DIR and the inbox shows an install
# link. Defaults to the plugin's dist/ for local dev; in the container image
# (which doesn't ship firefox-plugin/) set EXTENSION_DIST_DIR to a mounted path.
# ---------------------------------------------------------------------------

_PLUGIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "firefox-plugin")
EXTENSION_DIST_DIR = os.environ.get("EXTENSION_DIST_DIR") or os.path.join(_PLUGIN_DIR, "dist")


def _extension_xpi():
    """Newest .xpi in the dist dir, or None when none is present."""
    xpis = sorted(glob.glob(os.path.join(EXTENSION_DIST_DIR, "*.xpi")),
                  key=os.path.getmtime)
    return xpis[-1] if xpis else None


def _xpi_manifest(xpi_path):
    """version + gecko id read from inside the served .xpi. Works in the
    container, which doesn't ship firefox-plugin/manifest.json, and is always
    consistent with whatever .xpi is actually served."""
    try:
        with zipfile.ZipFile(xpi_path) as z:
            return json.loads(z.read("manifest.json"))
    except Exception:
        return {}


@app.route("/extension")
def extension_download():
    """Serve the signed .xpi. A plain link to this triggers Firefox's install
    prompt (the served content type is what matters; no JS install API)."""
    xpi = _extension_xpi()
    if not xpi:
        abort(404, "Firefox extension not published yet — see firefox-plugin/README.md.")
    return send_file(xpi, mimetype="application/x-xpinstall",
                     as_attachment=False, download_name=os.path.basename(xpi))


@app.route("/extension/updates.json")
def extension_updates():
    """Firefox self-hosted update manifest, built from the served .xpi. Firefox
    consults it only when the installed xpi's manifest sets gecko.update_url to
    this endpoint (injected at sign time via EXTENSION_UPDATE_URL — see
    firefox-plugin/README.md). update_hash lets Firefox verify the download."""
    xpi = _extension_xpi()
    if not xpi:
        abort(404)
    manifest = _xpi_manifest(xpi)
    version = manifest.get("version")
    gecko_id = (manifest.get("browser_specific_settings", {})
                .get("gecko", {}).get("id"))
    if not (version and gecko_id):
        abort(404)
    with open(xpi, "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()
    base = request.host_url.rstrip("/")
    return jsonify({"addons": {gecko_id: {"updates": [
        {"version": version,
         "update_link": f"{base}/extension",
         "update_hash": f"sha256:{sha}"},
    ]}}})


# ---------------------------------------------------------------------------
# CORS — allow the Firefox plugin (moz-extension://...) to POST.
# ---------------------------------------------------------------------------

@app.after_request
def cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
