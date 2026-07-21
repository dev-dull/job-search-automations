"""Server-side Anthropic scoring for job-store.

The single scoring path for every surface (plugin, poller, rescore): callers
POST a JD to /jobs/score and `app.py` calls `score_job()` here. Scoring is one
Anthropic call per posting with a structured-output schema; the resume,
preferences, and prompt live only on this side.

Configuration (env vars):
- ANTHROPIC_API_KEY (required)
- ANTHROPIC_MODEL (optional; defaults to claude-haiku-4-5)
- RESUME_PATH (required; path to your resume file in any text format —
  YAML, JSON, Markdown, LaTeX, HTML — read verbatim into the prompt)
- GROWTH_KEYWORDS (optional; comma-separated career-growth keywords)
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from anthropic import Anthropic
except ImportError as e:
    raise ImportError(
        "The `anthropic` package is required for server-side scoring. "
        "Run `pip install -r requirements.txt` to install it."
    ) from e


MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")
MAX_DESCRIPTION_CHARS = 12000

# Server-side scoring schema. Keep the field list in lockstep with
# _format_user_message's prompt lines (tests/test_anthropic_schema.py guards
# the desirability half of that).
FIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "candidate_score": {
            "type": "integer",
            "description": "A score between 1 and 100 inclusive for how well the resume matches the job description. Weight specific evidence over keyword density: an exact match on a tool the posting names (especially one marked preferred or required) that the resume demonstrates counts far more than generic keyword overlap, and a probable miss on a REQUIRED qualification lowers the score rather than being masked by surrounding keywords.",
        },
        "candidate_strong_matches": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Named tools, technologies, or title families the posting explicitly asks for (preferred or required) that the resume concretely demonstrates — the strongest screen signals. Empty if none.",
        },
        "required_qualification_misses": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Requirements the posting marks as required that the resume likely does NOT demonstrate. Empty if none.",
        },
        "career_growth_score": {
            "type": "integer",
            "description": "A score between 1 and 100 inclusive for words in the job description that are similar to the career-growth keywords provided.",
        },
        "candidate_explanation": {
            "type": "string",
            "description": "An explanation of candidate_score, no longer than 250 words.",
        },
        "candidate_strengths": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Strengths in the resume that the candidate possesses.",
        },
        "candidate_deficiencies": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Deficiencies in the resume that the candidate likely possesses, but could be better highlighted.",
        },
        "job_description_score": {
            "type": "integer",
            "description": "A score between 1 and 100 inclusive for how well the job description is written.",
        },
        "job_company_name": {
            "type": "string",
            "description": "From the job description, identify the company name.",
        },
    },
    "required": [
        "candidate_score", "career_growth_score", "candidate_explanation",
        "candidate_strengths", "candidate_deficiencies",
        "candidate_strong_matches", "required_qualification_misses",
        "job_description_score", "job_company_name",
    ],
    "additionalProperties": False,
}

SYSTEM_INSTRUCTIONS = (
    "Act as an expert technical recruiter with a previous career in software engineering "
    "who can critically compare resumes to job descriptions to determine if a candidate is a fit for a role."
)

# Added to the schema/prompt only when the candidate has stated preferences
# (PREFERENCES_PATH). desirability_score is the "do I WANT this" axis, distinct
# from candidate_score's "am I a MATCH". See ranking.DESIRABILITY_WEIGHT.
_DESIRABILITY_PROPERTIES: dict[str, Any] = {
    "desirability_score": {
        "type": "integer",
        "description": "A score between 1 and 100 for how well this role matches the kind of work the candidate says they want (see their stated preferences), independent of whether they are qualified for it. Weigh the candidate's sustainable-pace preferences heavily: lower the score for red-flag intensity signals and raise it for green-flag structural-rest signals. Trust STRUCTURAL signals (mandatory or minimum PTO, shutdown weeks, profitability, long median tenure, no-meeting days) far more than aspirational copy ('we value work-life balance') — job-description text is marketing. Credit desirability when the posting names something as preferred or nice-to-have that the resume clearly demonstrates — a rare differentiator that moves the candidate up the shortlist, beyond its fit value. If the posting lists a compensation range, weigh it against any compensation expectations in the candidate's preferences; a range below their stated floor sharply lowers desirability.",
    },
    "gate_failures": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "gate": {
                    "type": "string",
                    "description": "Short gate name: level, location, onsite, timezone, role-family, required-qualification, or similar.",
                },
                "evidence": {
                    "type": "string",
                    "description": "A short quote or near-quote from the posting proving the gate fails.",
                },
            },
            "required": ["gate", "evidence"],
            "additionalProperties": False,
        },
        "description": "HARD deal-breakers from the candidate's stated preferences (Deal-breakers and Must-haves) that this posting clearly fails: wrong level band, ineligible location/timezone, onsite requirement, excluded role family, or a required qualification the candidate lacks. Fail a gate ONLY on clear evidence in the posting — quote it. Empty array when nothing clearly fails; ambiguity is not a failure.",
    },
    "desirability_explanation": {
        "type": "string",
        "description": "A brief explanation of desirability_score, no longer than 150 words. Note any pace/sustainability factors that moved the score.",
    },
    "pace_signals": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Specific pace/sustainability tells found in the job description or company, each prefixed with 'RED: ' (intensity/grind signal) or 'GREEN: ' (sustainable-pace signal), per the candidate's sustainable-pace preferences. Empty array if none are present.",
    },
}


def _schema_with_desirability() -> dict[str, Any]:
    """FIT_SCHEMA plus the desirability fields (used when preferences exist)."""
    s = json.loads(json.dumps(FIT_SCHEMA))      # deep copy
    s["properties"].update(_DESIRABILITY_PROPERTIES)
    s["required"] = s["required"] + ["desirability_score", "desirability_explanation", "pace_signals", "gate_failures"]
    return s


def _resume_path() -> Path:
    override = os.environ.get("RESUME_PATH")
    if not override:
        raise RuntimeError(
            "RESUME_PATH is not set. Point it at your resume file (any text "
            "format — YAML, JSON, Markdown, LaTeX, HTML), "
            "e.g. RESUME_PATH=~/wip/resume/resume_details.yaml"
        )
    return Path(override).expanduser()


def read_resume() -> dict[str, Any]:
    """Read the resume file from disk as text (any format — it's passed to the
    model verbatim, never parsed). Re-read per call by design: the file is small
    and reads are local, so simplicity wins over caching."""
    path = _resume_path()
    if not path.exists():
        raise FileNotFoundError(
            f"resume file not found: {path}. Set RESUME_PATH if it lives elsewhere."
        )
    content = path.read_text(encoding="utf-8")
    stat = path.stat()
    return {
        "content": content,
        "path": str(path),
        "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }


def read_preferences() -> str | None:
    """The candidate's stated job preferences (free text), or None if unset.

    PREFERENCES_PATH is optional: without it, scoring behaves exactly as before
    (no desirability axis). Read verbatim, any text format."""
    path = os.environ.get("PREFERENCES_PATH")
    if not path:
        return None
    p = Path(path).expanduser()
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8").strip()
    return text or None


def _client() -> Anthropic:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Server-side scoring requires it."
        )
    return Anthropic(api_key=key)


def _format_user_message(*, description: str, url: str | None,
                         title: str | None, ats_platform: str | None,
                         growth_keywords: str, has_preferences: bool = False) -> str:
    description = (description or "")[:MAX_DESCRIPTION_CHARS]
    keywords_line = (
        f"- career_growth_score: a score between 1 and 100 for words in the "
        f"job description that are similar to the following: {growth_keywords}"
        if growth_keywords
        else "- career_growth_score: a score between 1 and 100 for words in "
             "the job description that suggest meaningful career growth for "
             "a senior infrastructure / build-and-release engineer."
    )
    fields = [
        "- candidate_score: a score between 1 and 100 for how well the resume matches the job description. Weight specific evidence over keyword density: an exact match on a named tool the posting asks for (especially marked preferred/required) that the resume demonstrates counts far more than generic keyword overlap; a probable miss on a REQUIRED qualification lowers the score rather than being masked by surrounding keywords.",
        "- candidate_strong_matches: a list of named tools/technologies/title families the posting explicitly asks for that the resume concretely demonstrates — the strongest screen signals. Empty list if none.",
        "- required_qualification_misses: a list of requirements the posting marks as required that the resume likely does not demonstrate. Empty list if none.",
        keywords_line,
        "- candidate_explanation: an explanation of the score no longer than 250 words.",
        "- candidate_deficiencies: a list of deficiencies in the resume that the candidate likely possesses, but could be better highlighted in the resume.",
        "- candidate_strengths: a list of strengths in the resume that the candidate possesses.",
        "- job_description_score: a score between 1 and 100 for how well the job description is written.",
        "- job_company_name: from the job description, identify the company name.",
    ]
    if has_preferences:
        fields += [
            "- desirability_score: a score between 1 and 100 for how well this role matches the kind of work the candidate says they want (see <preferences> in the system context), independent of whether they are qualified for it. Weigh the candidate's sustainable-pace preferences heavily: reduce the score for red-flag intensity signals (e.g. 'fast-paced', 'ship big things every week', on-call as a core duty, hypergrowth burn) and raise it for green-flag structural-rest signals (e.g. minimum/mandatory PTO, profitability, genuinely async/remote-first). Trust STRUCTURAL signals (mandatory PTO, shutdown weeks, profitability, long tenure) far more than aspirational copy — job-description text is marketing. Credit desirability for posting-named preferred/nice-to-have items the resume clearly demonstrates, and weigh any listed compensation range against the candidate's stated compensation expectations.",
            "- desirability_explanation: a brief explanation of desirability_score, no longer than 150 words; note any pace/sustainability factors that moved the score.",
            "- pace_signals: a list of the specific pace/sustainability tells from the posting, each prefixed with 'RED: ' or 'GREEN: ' per the candidate's sustainable-pace preferences; an empty list if none.",
            "- gate_failures: a list of {gate, evidence} objects for HARD deal-breakers from the candidate's Deal-breakers/Must-haves that this posting clearly fails (wrong level band, ineligible location/timezone, onsite requirement, excluded role family, a required qualification the candidate lacks). Fail a gate ONLY on clear evidence in the posting and quote that evidence; ambiguity is NOT a failure. Empty list when nothing clearly fails.",
        ]
    instruction = "\n".join([
        "Compare the resume and job description with the goal of helping the candidate tailor their resume for the position.",
        "Provide a JSON formatted response containing the following key/value pairs:",
        *fields,
        "",
        "The JSON should be valid, parsable, and contain no additional formatting or comments.",
    ])
    return (
        f"{instruction}\n\n"
        f"<job_posting>\n"
        f"URL: {url or 'n/a'}\n"
        f"Title: {title or 'n/a'}\n"
        f"ATS Platform: {ats_platform or 'unknown'}\n"
        f"Description:\n{description}\n"
        f"</job_posting>"
    )


def score_job(*, description: str, url: str | None = None,
              title: str | None = None,
              ats_platform: str | None = None) -> dict[str, Any]:
    """Score a JD against the on-disk resume. Returns {fit, usage, model}.

    Raises ValueError if the description is too short, RuntimeError if the
    response can't be parsed, FileNotFoundError if the resume is missing.
    """
    if not description or len(description) < 100:
        raise ValueError("description is empty or too short to score (< 100 chars).")

    resume = read_resume()["content"]
    preferences = read_preferences()
    growth_keywords = os.environ.get("GROWTH_KEYWORDS", "").strip()

    # Resume (and preferences, if any) go in cached system blocks — stable
    # across calls, so prompt caching keeps per-scoring cost low.
    system = [
        {"type": "text", "text": SYSTEM_INSTRUCTIONS},
        {
            "type": "text",
            "text": f"The candidate's resume is below.\n\n<resume>\n{resume}\n</resume>",
            "cache_control": {"type": "ephemeral"},
        },
    ]
    schema = FIT_SCHEMA
    if preferences:
        system.append({
            "type": "text",
            "text": f"The candidate's stated job preferences are below.\n\n<preferences>\n{preferences}\n</preferences>",
            "cache_control": {"type": "ephemeral"},
        })
        schema = _schema_with_desirability()

    # output_config is the structured-outputs parameter; it's not in every
    # version of the SDK as a named kwarg, so route it via extra_body for
    # forward-compat.
    # 4096: the #72 fields (gate evidence quotes, strong matches, required-miss
    # lists) pushed long-JD responses past the old 2048 cap, truncating the JSON
    # mid-string.
    response = _client().messages.create(
        model=MODEL,
        max_tokens=4096,
        temperature=0,
        system=system,
        messages=[
            {
                "role": "user",
                "content": _format_user_message(
                    description=description, url=url, title=title,
                    ats_platform=ats_platform, growth_keywords=growth_keywords,
                    has_preferences=bool(preferences),
                ),
            }
        ],
        extra_body={
            "output_config": {
                "format": {"type": "json_schema", "schema": schema}
            }
        },
    )

    text_block = next(
        (b.text for b in response.content if getattr(b, "type", None) == "text"),
        None,
    )
    if not text_block:
        raise RuntimeError("Anthropic response contained no text block.")
    if response.stop_reason == "max_tokens":
        # Truncated JSON would otherwise surface as a confusing decode error.
        raise RuntimeError(
            "Anthropic response hit max_tokens and was truncated; "
            "the schema output needs a higher cap.")

    try:
        fit = json.loads(text_block)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Anthropic response was not valid JSON: {e}") from e

    usage = getattr(response, "usage", None)
    return {
        "fit": fit,
        "usage": {
            "input_tokens": getattr(usage, "input_tokens", 0),
            "output_tokens": getattr(usage, "output_tokens", 0),
            "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0),
            "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0),
        },
        "model": MODEL,
    }
