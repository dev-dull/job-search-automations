"""Local prescreen: decide what deserves a paid scoring call.

This is a PRESCREEN, never an authoritative score. It never writes scores into
job-store. Its only output is a keep/drop decision plus the evidence behind it,
so that the paid Anthropic call is spent on plausible roles instead of junk.

Three stages, cheapest first, each able to drop a posting:

  1. rules      — deterministic title/keyword checks. No model, no tokens.
  2. triage     — small all-VRAM model on the title only.
  3. gates+fit  — the MoE scorer on the full JD, mirroring the cloud scorer's
                  shape: evidence first, gates as quoted evidence, score last.

STAGES ARE BATCHED, AND THAT IS NOT AN OPTIMISATION — IT IS REQUIRED.
llama-swap keeps exactly one model resident (8 GB of VRAM cannot hold the 5.2 GB
triage model and the MoE's 4.9 GB of attention+KV at once). Screening postings
one at a time therefore swaps models twice per posting, and a cold MoE load off
a spinning disk costs minutes. Measured: a 28-posting run made no progress in
four minutes of thrashing.

So `screen_batch()` runs every triage call, then every gate call — two model
loads for the whole night instead of two per job. Use it. `screen()` remains for
single-posting testing only.

Concurrency is per-stage because the slot counts differ: `scorer` serves `-np 8`,
while `coder` is `-np 1` (it was dropped to a single slot so Claude Code gets the
full context window). Sending 4 concurrent requests to a 1-slot server just
queues them.

Local absolute scores compress toward the middle, so `fit_sketch` is used only
as a coarse threshold and for ranking within a night's batch — never as a
substitute for the authoritative score.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from llm import GATE_SCHEMA, TRIAGE_SCHEMA, LlamaSwap, LLMError
# The backend's scoring floor lives in one place; sources never
# imports prescreen, so this import creates no cycle.
from sources import MIN_DESCRIPTION_CHARS as MIN_DESCRIPTION

TRIAGE_SYSTEM = """You CLASSIFY job titles. You do not decide whether to keep them.

Assign the single closest `family`:
  platform             - platform engineering, internal platforms
  devops               - devops, cloud/infrastructure automation
  build-release        - build, release, CI/CD engineering
  developer-experience - devex, developer productivity, tooling
  infrastructure       - infrastructure, systems, sysadmin
  sre                  - site reliability, on-call ownership as the core
  management           - manager, director, head, lead-of-people
  frontend             - frontend, UI, mobile
  other                - EVERYTHING ELSE: AI/ML, data, security, sales,
                         marketing, support, product, full-stack, founder,
                         developer relations/advocacy, solutions architect

Assign `level` from the title's own wording: below-senior, senior, staff-plus,
management, or unclear when the title says nothing about level.

Be decisive about `family` — "other" is the correct answer for most titles and
is not a rejection. Set `keep` to your honest read, but the classification is
what matters; the caller applies its own policy.

Judge ONLY the title given. Quote it in title_evidence."""

GATE_SYSTEM_TMPL = """You prescreen job postings against a candidate's hard requirements.

You are the cheap wide funnel. A human and a more expensive model make the real
decision. Your job is to discard only what CLEARLY fails, and to show your work.

RULES:
- A gate may fire ONLY on evidence you can quote from the posting. Quote it.
- Ambiguity is NOT a failure. Silence in a posting is NOT a failure. If the
  posting does not say, the gate does not fire.
- List evidence and gaps BEFORE deciding. Do not reason after the number.
- `worth_paid_scoring` is false only when a gate fired or the role is plainly
  irrelevant. When in doubt, say true — a wasted scoring call is cheaper than a
  missed job.

FIELD RUBRICS. Follow these literally; do not leave a field at its default.

`fit_sketch` is an integer 0-100 for evidence-weighted skill match:
    0-20   no meaningful overlap with the candidate's background
    21-45  adjacent, but the posting's named tools are mostly absent
    46-70  solid partial match; some named tools present
    71-85  strong; most named tools appear in the candidate's background
    86-100 excellent; the posting names tools the candidate has used directly
  Use the FULL range. Never default to 0 — 0 means "no overlap whatsoever" and
  is almost never correct for a role in the candidate's own field.

`pace_signals` describe THE POSTING AND THE COMPANY, not the candidate's resume.
  Every entry MUST begin with exactly "RED: " or "GREEN: ".
    RED:   grind/intensity tells — "fast-paced", "wear many hats", "hyper-growth",
           "24/7 ownership", crunch or always-on language
    GREEN: structural rest — minimum/mandatory PTO, shutdown weeks, profitability,
           long median tenure, no-meeting days
  Structural facts outrank aspirational copy. Empty array if the posting says
  nothing eitherway.

`strong_matches` / `required_misses` compare the posting's stated requirements
  against the candidate's background. Quote the posting's wording.

THE CANDIDATE'S HARD REQUIREMENTS:
{gates}

THE CANDIDATE'S BACKGROUND:
{resume}"""


@dataclass
class Decision:
    url: str
    title: str
    company: str
    keep: bool
    stage: str                      # rules | triage | gates | kept | error
                                    # ("error" = fail-open passthrough: shown
                                    # in the report, NEVER submitted for paid
                                    # scoring — discover.py filters on it)
    reason: str
    detail: dict = field(default_factory=dict)

    def row(self) -> dict:
        return {
            "company": self.company,
            "title": self.title,
            "url": self.url,
            "keep": self.keep,
            "stage": self.stage,
            "reason": self.reason,
            "fit_sketch": self.detail.get("fit_sketch"),
            "gates": "; ".join(
                f"{g.get('gate')}: {g.get('evidence','')[:120]}"
                for g in self.detail.get("gate_failures", [])
            ),
        }


# Stage 1 — deterministic. Cheapest possible rejection.
# Only universally-safe drops by default: clearly-entry-level markers no
# operator of THIS funnel is hunting for. Everything that encodes one
# person's target band or excluded families (staff/principal, manager,
# frontend, ML, ...) belongs in the operator's private config: cheap ones in
# TITLE_DROPS_EXTRA / --title-drops-extra, judgment calls in the gates file.
# An earlier version hardcoded one operator's whole exclusion list here —
# the privacy failure mode, one layer up.
_GENERIC_TITLE_DROPS = re.compile(
    r"\b(intern|internship|apprentice|new[- ]grad)\b", re.I)
_SENIOR_HINT = re.compile(r"\b(senior|sr\.?|lead)\b", re.I)



def build_title_drops(extra: str | None) -> re.Pattern | None:
    """Compile TITLE_DROPS_EXTRA / --title-drops-extra: comma-separated
    words/phrases, matched case-insensitively on word boundaries."""
    terms = [t.strip() for t in (extra or "").split(",") if t.strip()]
    if not terms:
        return None
    return re.compile(
        r"\b(" + "|".join(re.escape(t) for t in terms) + r")\b", re.I)


def rules_stage(title: str, extra_drops: re.Pattern | None = None) -> tuple[bool, str]:
    t = (title or "").strip()
    if not t:
        return False, "no title"
    for pat in (_GENERIC_TITLE_DROPS, extra_drops):
        m = pat.search(t) if pat else None
        if m:
            return False, f"title contains {m.group(0)!r}"
    return True, ""


class Prescreener:
    def __init__(
        self,
        llm: LlamaSwap,
        *,
        gates_text: str,
        resume_text: str,
        triage_model: str = "scorer",
        screen_model: str = "coder",
        title_drops_extra: str | None = None,
        fit_floor: int = 25,
        max_jd_chars: int = 24000,
    ):
        self.llm = llm
        self.triage_model = triage_model
        self.screen_model = screen_model
        # The CLI (discover.py) resolves $TITLE_DROPS_EXTRA into the flag;
        # reading env here too would make direct construction (tests, library
        # use) inherit ambient state from the operator's shell.
        self.extra_drops = build_title_drops(title_drops_extra)
        self.fit_floor = fit_floor
        self.max_jd_chars = max_jd_chars
        self.gate_system = GATE_SYSTEM_TMPL.format(
            gates=gates_text.strip(), resume=resume_text.strip()
        )

    # --- batched: one model load per stage, not per posting -----------------

    def screen_batch(
        self,
        postings: list[dict],
        *,
        triage_workers: int = 8,
        gate_workers: int = 1,
        progress=None,
        problems: list[str] | None = None,
    ) -> list[Decision]:
        """Screen many postings with exactly two model loads.

        triage_workers/gate_workers should match each model's `-np` slot count:
        scorer serves 8, coder serves 1. `problems` (if given) collects
        run-degradation notes for the morning report — currently triage
        failures, which otherwise pass through to the full screen invisibly.
        """
        import concurrent.futures as cf

        decisions: list[Decision] = []
        stage2: list[dict] = []
        # list.append is atomic under the GIL, so triage workers can record
        # here without a lock. Gate failures are already visible (stage=error
        # in the report); triage failures were the last silent degradation.
        self._triage_errors: list[str] = []
        self._stage2_count = 0

        # Stage 1 — free.
        for p in postings:
            title = (p.get("title") or "").strip()
            base = dict(url=p.get("url", ""), title=title, company=p.get("company", ""))
            ok, why = rules_stage(title, self.extra_drops)
            jd = (p.get("description") or "").strip()
            if not ok:
                decisions.append(Decision(**base, keep=False, stage="rules", reason=why))
            elif len(jd) < MIN_DESCRIPTION:
                decisions.append(Decision(**base, keep=False, stage="rules",
                                          reason=f"description too short "
                                                 f"({len(jd)} chars, min {MIN_DESCRIPTION})"))
            else:
                stage2.append(p)

        if progress:
            progress(f"rules: {len(postings) - len(stage2)} dropped, {len(stage2)} to triage")

        # Stage 2 — scorer resident for the whole stage.
        self._stage2_count = len(stage2)
        stage3: list[dict] = []
        if stage2:
            with cf.ThreadPoolExecutor(max_workers=triage_workers) as pool:
                for p, res in zip(stage2, pool.map(self._triage, stage2)):
                    d, passed = res
                    if passed:
                        stage3.append(p)
                    elif d is not None:
                        decisions.append(d)
        if progress:
            progress(f"triage: {len(stage2) - len(stage3)} dropped, {len(stage3)} to full screen")
        if self._triage_errors and problems is not None:
            # Deduped BY MESSAGE but keeping the count: 400 identical
            # "declared dead" failures and one transient reset must not render
            # the same. The magnitude is the actionable half.
            from collections import Counter
            for msg, n in Counter(self._triage_errors).most_common():
                problems.append(f"{n} of {self._stage2_count} to triage "
                                f"UNTRIAGED — {msg}")

        # Stage 3 — coder resident for the whole stage.
        if stage3:
            with cf.ThreadPoolExecutor(max_workers=gate_workers) as pool:
                decisions.extend(pool.map(self._gates, stage3))

        return decisions

    # Families the funnel exists to find. Anything else is a drop unless the
    # model itself is unsure.
    KEEP_FAMILIES = {"platform", "devops", "build-release",
                     "developer-experience", "infrastructure"}

    def _triage(self, p: dict) -> tuple[Decision | None, bool]:
        title = (p.get("title") or "").strip()
        base = dict(url=p.get("url", ""), title=title, company=p.get("company", ""))
        try:
            t = self.llm.json_call(
                self.triage_model, TRIAGE_SYSTEM,
                f"Title: {title}\nCompany: {p.get('company','')}", TRIAGE_SCHEMA,
                max_tokens=400,
            )
        except Exception as err:
            # Fail open — a model problem must not shrink the funnel — but
            # record it so a dead triage model shows up in the report instead
            # of masquerading as "classified everything as keep".
            getattr(self, "_triage_errors", []).append(
                f"triage model {self.triage_model!r} failed: "
                f"{type(err).__name__}: {err}")
            return None, True

        # Decide on the CLASSIFICATION, not on the model's `keep` boolean.
        # Trusting `keep` made triage drop 0 of 33 on a live run — the model
        # rubber-stamped Developer Advocate, Content Strategist and two AI
        # Engineer roles straight into the expensive stage. The family label is
        # a far more reliable signal than a yes/no it has been nudged toward.
        family, level = t.get("family"), t.get("level")

        if family in self.KEEP_FAMILIES:
            # Right kind of work — ALWAYS goes to the full screen, whatever the
            # model says about level. It infers "below-senior" from the mere
            # absence of a seniority word, which would have dropped a plain
            # "DevOps Engineer" that scored 85 on a live run. Explicit level
            # misses (junior/staff/principal/manager) are already caught for
            # free by rules_stage; anything subtler needs the JD, not the title.
            return None, True

        if family == "sre":
            # Excluded family, but "SRE" in a title often means platform work.
            # Let the full screen read the JD and gate on quoted evidence.
            return None, True

        return Decision(**base, keep=False, stage="triage",
                        reason=f"{family}/{level}: {t.get('reason','')}",
                        detail=t), False

    def _gates(self, p: dict) -> Decision:
        title = (p.get("title") or "").strip()
        base = dict(url=p.get("url", ""), title=title, company=p.get("company", ""))
        jd = (p.get("description") or "").strip()
        try:
            g = self.llm.json_call(
                self.screen_model, self.gate_system,
                f"COMPANY: {p.get('company','')}\nTITLE: {title}\n\nPOSTING:\n{jd[:self.max_jd_chars]}",
                GATE_SCHEMA, max_tokens=1600,
            )
        except Exception as err:
            # Catch broadly and on purpose. Anything escaping here kills the
            # ThreadPoolExecutor.map and takes the whole night's work with it —
            # which is how one ConnectionResetError lost a 20-minute run.
            # A failed screen is a kept posting, never a crash.
            return Decision(**base, keep=True, stage="error",
                            reason=f"screen failed, passing through: "
                                   f"{type(err).__name__}: {err}")

        gates = [x for x in g.get("gate_failures", []) if x.get("evidence", "").strip()]
        fit = int(g.get("fit_sketch") or 0)
        if gates:
            return Decision(**base, keep=False, stage="gates",
                            reason=f"gate: {gates[0].get('gate')}", detail=g)
        if not g.get("worth_paid_scoring") and fit < self.fit_floor:
            return Decision(**base, keep=False, stage="gates",
                            reason=f"not worth scoring (fit sketch {fit})", detail=g)
        return Decision(**base, keep=True, stage="kept", reason=f"fit sketch {fit}", detail=g)

    def screen(self, posting: dict) -> Decision:
        url = posting.get("url", "")
        title = (posting.get("title") or "").strip()
        company = posting.get("company", "")
        jd = (posting.get("description") or "").strip()

        base = dict(url=url, title=title, company=company)

        ok, why = rules_stage(title, self.extra_drops)
        if not ok:
            return Decision(**base, keep=False, stage="rules", reason=why)

        if len(jd) < MIN_DESCRIPTION:
            return Decision(**base, keep=False, stage="rules",
                            reason=f"description too short ({len(jd)} chars, "
                                   f"min {MIN_DESCRIPTION})")

        # Stage 2 — title triage on the small GPU-resident model.
        try:
            t = self.llm.json_call(
                self.triage_model, TRIAGE_SYSTEM,
                f"Title: {title}\nCompany: {company}", TRIAGE_SCHEMA,
                max_tokens=400,
            )
        except LLMError as err:
            # Fail open: a model problem must not silently shrink the funnel.
            return Decision(**base, keep=True, stage="error",
                            reason=f"triage failed, passing through: {err}")

        if not t.get("keep"):
            if t.get("level") == "unclear" or _SENIOR_HINT.search(title):
                pass  # ambiguity is not a rejection
            else:
                return Decision(
                    **base, keep=False, stage="triage",
                    reason=f"{t.get('family')}/{t.get('level')}: {t.get('reason','')}",
                    detail=t,
                )

        # Stage 3 — full screen on the MoE scorer.
        try:
            g = self.llm.json_call(
                self.screen_model, self.gate_system,
                f"COMPANY: {company}\nTITLE: {title}\n\nPOSTING:\n{jd[:self.max_jd_chars]}",
                GATE_SCHEMA, max_tokens=1600,
            )
        except Exception as err:
            # Catch broadly and on purpose. Anything escaping here kills the
            # ThreadPoolExecutor.map and takes the whole night's work with it —
            # which is how one ConnectionResetError lost a 20-minute run.
            # A failed screen is a kept posting, never a crash.
            return Decision(**base, keep=True, stage="error",
                            reason=f"screen failed, passing through: "
                                   f"{type(err).__name__}: {err}")

        gates = [x for x in g.get("gate_failures", []) if x.get("evidence", "").strip()]
        fit = int(g.get("fit_sketch") or 0)

        if gates:
            return Decision(**base, keep=False, stage="gates",
                            reason=f"gate: {gates[0].get('gate')}", detail=g)
        if not g.get("worth_paid_scoring") and fit < self.fit_floor:
            return Decision(**base, keep=False, stage="gates",
                            reason=f"not worth scoring (fit sketch {fit})", detail=g)

        return Decision(**base, keep=True, stage="kept",
                        reason=f"fit sketch {fit}", detail=g)
