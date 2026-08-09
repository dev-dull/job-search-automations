# Example corpus (synthetic)

Copy this directory into your PRIVATE repo, replace every file's content
with your own, and point `CORPUS_PATH` at it. All content here is invented
("Jordan Sample") and exists to show the layout and front-matter.

Rules that keep you safe:
- Redact at the source: company names you're targeting, street addresses,
  phone numbers, anything you wouldn't hand a stranger. Placeholders like
  `[COMPANY]` are fine — the agent handles them gracefully.
- Put every string that must NEVER appear into your denylist file
  (`REDACTION_DENYLIST_PATH`); the service refuses to serve a corpus that
  contains one.
- Do not copy your scoring resume or preferences file in here. This corpus
  is employer-facing by definition; those documents are not.
- `profile.md` is the spine — identity, headline, links, contact. Keep it
  current; `get_profile_summary()` and the fetch landing page render it.

Front-matter fields: `title` (required in spirit), `type`, `date`, `tags`,
`summary` (one line — shown in corpus indexes). The file itself is the
record; there is no central manifest.
