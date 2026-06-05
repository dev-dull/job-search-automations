"""Server-side Anthropic scoring for job-store.

Mirrors the prompt and schema used by `firefox-plugin/popup.js` so plugin-mode
and poller-mode scoring stay aligned. The plugin keeps calling Anthropic
directly for now (`fit_score` is in the POST payload); when a caller omits
`fit_score`, `app.py` falls through to `score_job()` here.

Configuration (env vars):
- ANTHROPIC_API_KEY (required)
- ANTHROPIC_MODEL (optional; defaults to claude-haiku-4-5)
- RESUME_PATH (optional; defaults to ../resume_details.yaml relative to this file)
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

# Schema kept in lockstep with `firefox-plugin/popup.js`'s FIT_SCHEMA. Edit
# both files together when the prompt or output shape changes.
FIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "candidate_score": {
            "type": "integer",
            "description": "A score between 1 and 100 inclusive for how well the resume matches the job description.",
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
        "job_description_score", "job_company_name",
    ],
    "additionalProperties": False,
}

SYSTEM_INSTRUCTIONS = (
    "Act as an expert technical recruiter with a previous career in software engineering "
    "who can critically compare resumes to job descriptions to determine if a candidate is a fit for a role."
)


def _resume_path() -> Path:
    override = os.environ.get("RESUME_PATH")
    if not override:
        raise RuntimeError(
            "RESUME_PATH is not set. Point it at your resume YAML, "
            "e.g. RESUME_PATH=~/wip/resume/resume_details.yaml"
        )
    return Path(override).expanduser()


def read_resume() -> dict[str, Any]:
    """Read resume_details.yaml from disk. Re-read per call by design: the
    file is small and reads are local, so simplicity wins over caching."""
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


def _client() -> Anthropic:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Server-side scoring requires it."
        )
    return Anthropic(api_key=key)


def _format_user_message(*, description: str, url: str | None,
                         title: str | None, ats_platform: str | None,
                         growth_keywords: str) -> str:
    description = (description or "")[:MAX_DESCRIPTION_CHARS]
    keywords_line = (
        f"- career_growth_score: a score between 1 and 100 for words in the "
        f"job description that are similar to the following: {growth_keywords}"
        if growth_keywords
        else "- career_growth_score: a score between 1 and 100 for words in "
             "the job description that suggest meaningful career growth for "
             "a senior infrastructure / build-and-release engineer."
    )
    instruction = "\n".join([
        "Compare the resume and job description with the goal of helping the candidate tailor their resume for the position.",
        "Provide a JSON formatted response containing the following key/value pairs:",
        "- candidate_score: a score between 1 and 100 for how well the resume matches the job description.",
        keywords_line,
        "- candidate_explanation: an explanation of the score no longer than 250 words.",
        "- candidate_deficiencies: a list of deficiencies in the resume that the candidate likely possesses, but could be better highlighted in the resume.",
        "- candidate_strengths: a list of strengths in the resume that the candidate possesses.",
        "- job_description_score: a score between 1 and 100 for how well the job description is written.",
        "- job_company_name: from the job description, identify the company name.",
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
    growth_keywords = os.environ.get("GROWTH_KEYWORDS", "").strip()

    # output_config is the structured-outputs parameter; it's not in every
    # version of the SDK as a named kwarg, so route it via extra_body for
    # forward-compat.
    response = _client().messages.create(
        model=MODEL,
        max_tokens=2048,
        temperature=0,
        system=[
            {"type": "text", "text": SYSTEM_INSTRUCTIONS},
            {
                "type": "text",
                "text": f"The candidate's resume is below.\n\n<resume>\n{resume}\n</resume>",
                "cache_control": {"type": "ephemeral"},
            },
        ],
        messages=[
            {
                "role": "user",
                "content": _format_user_message(
                    description=description, url=url, title=title,
                    ats_platform=ats_platform, growth_keywords=growth_keywords,
                ),
            }
        ],
        extra_body={
            "output_config": {
                "format": {"type": "json_schema", "schema": FIT_SCHEMA}
            }
        },
    )

    text_block = next(
        (b.text for b in response.content if getattr(b, "type", None) == "text"),
        None,
    )
    if not text_block:
        raise RuntimeError("Anthropic response contained no text block.")

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
