# Job Search Automations
A collection of tools to help identify jobs and apply to them.

The repo holds two distinct surfaces:

1. **Job Board** (`job-board/`): a runtime stack you self-host. Flask + SQLite backend, a CLI poller that walks targeted companies' ATS feeds, and a Firefox extension that scores postings as you browse. Server-side scoring against Anthropic. See [`job-board/README.md`](job-board/README.md).
2. **GitHub Actions** (below): composite actions a separate resume-as-code repo invokes at commit time to qualify, tailor, and last-look a resume against a specific job description using Google Gemini.

The two surfaces are independent. You can use either without the other.

## Job Board

Three components living together in [`job-board/`](job-board/):

| Component | Role |
|---|---|
| **job-store** | Flask + SQLite backend. Inbox UI, scoring endpoint, dedupe, ranking math. |
| **poller** | CLI that watches `company_targets` and posts new openings to job-store. |
| **firefox-plugin** | Browser extension that scores postings via job-store and surfaces a "watch this company" button. |

Both the plugin and the poller POST job descriptions to job-store without a fit score. job-store calls Anthropic with the resume and the prompt, persists the analysis, returns the score. Neither the plugin nor the poller ever sees the API key.

See [`job-board/README.md`](job-board/README.md) for the architecture diagram, configuration, and local-run instructions.

## How it all works together

Two independent surfaces. Here's how the pieces of each fit, and how a posting flows from discovery to application.

### The Job Board at runtime

`job-store` is the hub — a Flask + SQLite service that is the single source of truth for the resume, the Anthropic API key, the scoring prompt, and the schema. Two **producers** feed it job descriptions; neither ever holds the key:

- **firefox-plugin** — as you browse, extracts the JD from the page and POSTs it.
- **poller** — on a schedule, walks the `company_targets` table, pulls each company's openings from its ATS (Greenhouse / Ashby / Lever / Workday adapters), filters by title and location, and POSTs the survivors. It's a pure HTTP client of job-store (no DB access).

For each posting that arrives without a `fit_score`, job-store calls Anthropic with the resume + prompt, persists the structured analysis, dedupes by `dedupe_key`, and ranks on read (`fit_score × age_decay × platform_factor`). You triage the ranked inbox at `/` — apply / dismiss / record outcome — and the poller's location and deny filters keep the noise down.

```
    browse a posting           on a schedule
         │                          │
    ┌────▼─────┐              ┌──────▼──────┐
    │ firefox  │  POST        │   poller    │  POST
    │ plugin   │  /jobs/score │  (CronJob)  │  /jobs/score
    └────┬─────┘              └──────┬──────┘
         │   (no fit_score in body)  │
         └────────────┬──────────────┘
                      ▼
            ┌───────────────────────┐  score  ┌──────────────────┐
     you ──►│      job-store        │────────►│  api.anthropic   │
    (inbox  │  Flask + SQLite       │◄────────│  (resume + key   │
     at /)  │  dedupe · rank · UI   │         │   live here)     │
            │  serves signed .xpi   │         └──────────────────┘
            │  at /extension        │
            └───────────────────────┘
```

The plugin is also **distributed by** the job board: job-store serves a Mozilla-signed `.xpi` at `/extension`, so the inbox shows an "Install Firefox extension" link — no `about:debugging` needed.

### Lifecycle: develop → build → deploy → operate

1. **Develop locally.** `flask run` job-store, run `poller.py` against it, load the plugin as a temporary add-on. (See [`job-board/README.md`](job-board/README.md).)
2. **Build the image.** On backend changes, CI builds `ghcr.io/dev-dull/job-store` and bakes in the current signed plugin `.xpi`.
3. **Sign & distribute the plugin.** Push a `plugin-v*` tag → CI signs the extension via Mozilla AMO (unlisted, no review), publishes the `.xpi` to the floating `plugin-latest` release, and rebuilds the image with it baked in. (See [`job-board/firefox-plugin/README.md`](job-board/firefox-plugin/README.md).)
4. **Deploy.** The Helm chart runs job-store on Kubernetes: Deployment + Service (single-writer SQLite → one replica, `Recreate`), a PVC for `jobs.db`, the API-key Secret, the resume (from the Secret or git-cloned via an init-container), optional Ingress + TLS, and the poller as a CronJob. (See [`job-board/job-store/helm/README.md`](job-board/job-store/helm/README.md).)
5. **Operate.** Browse and the plugin scores live; the CronJob polls on its schedule; you triage in the inbox and install the plugin from it.

### How the two surfaces relate

Decoupled on purpose:

- **Job Board** (Anthropic / Claude) does *discovery, scoring, triage* — "which open roles are worth my time?"
- **GitHub Actions** (Google Gemini, below) do *per-application resume tailoring* at commit time, invoked by a separate resume-as-code repo — "make my resume sharp for this one role."

The intended bridge is the inbox's "apply" action: cut a branch in the resume repo from a stored posting, which triggers the Gemini tailoring flow. Until that lands, use either surface on its own.

## GitHub Actions
### dev-dull/job-search-automations/gemini-qualified
Uses the free tier of Google Gemini to compare a resume to a job description and ranks if the position is a good fit with a score between 1 and 100.

#### Inputs
|Input Name|Input Description|Required Y/N|Default Value|
|----------|-----------------|------------|-------------|
|GOOGLE_API_KEY|Google API key for Gemini|Y||
|GEMINI_MODEL|The Gemini model to use (as found in the API URL)|Y|`gemini-flash-latest`|
|RESUME_TEXT|The body of the resume to evaluate|Y||
|RESUME_MIME_TYPE|The http mime type for the resume format|Y|`text/plain`|
|CAREER_GROWTH_KEYWORDS|A list of keywords used to create a score to evaluate if the position will take your career in the desired direction|Y||
|JOB_DESCRIPTION|The body of the job description to evaluate|Y||
|JOB_DESCRIPTION_MIME_TYPE|The http mime type for the job description format|Y|`text/plain`|
|PERSONA|The role Gemini should play (typically starts, "You are..." or "Act as a...")|Y|`Act as an expert technical recruiter with a previous career in software engineering who can critically compare resumes to job descriptions to determine if a candidate is a fit for a role.`|

#### Outputs
|Output Name|Output Description|
|-----------|------------------|
|RAW|The full, raw JSON _string_ returned by Gemini|
|RESPONSE_TEXT|Just the generated string from Gemini|

The content of `RESPONSE_TEXT` is expected to be a JSON _string_ where Gemini has been given the following key/value descriptions:
- `candidate_score`: a score between 1 and 100 for how well the resume matches the job description.
- `career_growth_score`: a score between 1 and 100 for words in the job description that are similar to the following: ${process.env.CAREER_GROWTH_KEYWORDS}
- `candidate_explanation`: an explanation of the score no longer than 250 words.
- `candidate_deficiencies`: a list of deficiencies in the resume that the candidate likely possesses, but could be better highlighted in the resume.
- `candidate_strengths`: a list of strengths in the resume that the candidate possesses.
- `candidate_recommendations`: a list of specific changes that the candidate should make to their resume to improve their chances of getting the job.
- `candidate_errors`: a list of spelling and grammar errors in the resume.
- `job_description_score`: a score between 1 and 100 for how well the job description is written.
- `job_description_explanation`: an explanation of the job_description_score no longer than 250 words.
- `job_description_deficiencies`: a list of deficiencies in the job description that the candidate should be aware of.
- `job_company_name`: from the job description, identify the company name.
- `job_company_description`: describe the company named in job_company_name in no more than 50 words.
- `job_company_sentiment_score`: without referencing the job description or resume, provide a score between 1 and 100 for how well the company is likely to treat its employees.
- `job_company_explanation`: without referencing the job description or resume, write an explanation of job_company_sentiment_score no longer than 250 words.

### dev-dull/job-search-automations/gemini-tailor
Uses the free tier of Google Gemini to compare a resume to a job description to make suggestions on what to focus on to tailor your resume to target the open role.
#### Inputs
|Input Name|Input Description|Required Y/N|Default Value|
|----------|-----------------|------------|-------------|
|GOOGLE_API_KEY|Google API key for Gemini|Y||
|GEMINI_MODEL|The Gemini model to use (as found in the API URL)|Y|`gemini-flash-latest`|
|RESUME_TEXT|The body of the resume to evaluate|Y||
|RESUME_MIME_TYPE|The http mime type for the resume format|Y|`text/plain`|
|JOB_DESCRIPTION|The body of the job description to evaluate|Y||
|JOB_DESCRIPTION_MIME_TYPE|The http mime type for the job description format|Y|`text/plain`|
|PERSONA|The role Gemini should play (typically starts, "You are..." or "Act as a...")|Y|`Act as an expert technical recruiter with a previous career in software engineering who can critically compare resumes to job descriptions to help a candidate tailor their resume to the open position.`|

#### Outputs
|Output Name|Output Description|
|-----------|------------------|
|RAW|The full, raw JSON _string_ returned by Gemini|
|RESPONSE_TEXT|Just the generated string from Gemini|

The content of `RESPONSE_TEXT` is expected to be a JSON _string_ where Gemini has been given the following key/value descriptions:
- `company_name`: The name of the company where the candidate has worked.
- `position_name`: The name of the position held by the candidate.
- `remove`: Make a determination if the position should be removed from the resume entirely based on its relevance to the job description. If it is not relevant, set this to true, otherwise false.
- `add_emphasis`: A brief paragraph of the experience the candidate has from this position and should emphasize when applying to this role.
- `remove_emphasis`: A brief paragraph of the experience the candidate has from this position that may not be relevant to the job they are applying for and should be deemphasized or removed in the resume.
- `errors`: a list of spelling and grammar errors in the resume. If none are found, set the list to a single result that says "No errors found."
- `suggested_wording`: A list of specific wording the candidate should use to better align their resume with the job description. This should include keywords and phrases from the job description that are relevant to the candidate's experience. If you have no suggestions, set the list to a single item that says, "N/A"
- `additional_help`: Any additional suggestions for the candidate to consider when tailoring their resume for this position.


### dev-dull/job-search-automations/gemini-cover-outline
Uses the free tier of Google Gemini to compare a resume to a job description and the company's career information to generate an outline to a cover letter to aid the user in writing an effective letter to use when applying to a role.
#### Inputs
|Input Name|Input Description|Required Y/N|Default Value|
|----------|-----------------|------------|-------------|
|GOOGLE_API_KEY|Google API key for Gemini|Y||
|GEMINI_MODEL|The Gemini model to use (as found in the API URL)|Y|`gemini-flash-latest`|
|RESUME_TEXT|The body of the resume to evaluate|Y||
|RESUME_MIME_TYPE|The http mime type for the resume format|Y|`text/plain`|
|JOB_DESCRIPTION|The body of the job description to evaluate|Y||
|JOB_DESCRIPTION_MIME_TYPE|The http mime type for the job description format|Y|`text/plain`|
|COMPANY_CAREER_INFO|Text found on the company's websites that describes the company culture, values, mission, and any other details provided on the Company's website which they present to prospective candidates|Y||
|COMPANY_CAREER_INFO_MIME_TYPE|The http mime type for the company career information|`text/plain`|
|PERSONA|The role Gemini should play (typically starts, "You are..." or "Act as a...")|Y|`Act as an expert technical recruiter with a previous career in software engineering who can critically compare resumes to job descriptions to help a candidate write a cover letter.`|

#### Outputs
|Output Name|Output Description|
|-----------|------------------|
|RAW|The full, raw JSON _string_ returned by Gemini|
|RESPONSE_TEXT|Just the generated string from Gemini|

The content of `RESPONSE_TEXT` is expected to be a JSON _string_ where Gemini has been given the following key/value descriptions:
- `introduction_help`: a short paragraph to help the candidate write an introduction to the cover letter that includes some suggested phrasing that will help the candidate get started with a friendly and professional tone.
- `outline`: a list of brief descriptions of what to include in the cover letter. Be sure to incorporate the company career information provided in the input.
- `conclusion_help`: a short paragraph to help the candidate write a conclusion to the cover letter that includes some suggested phrasing that will help the candidate finish the letter with a friendly and professional tone.
- `additional_help`: a short paragraph to help the candidate write a cover letter that includes some suggested phrasing that will help the candidate finish the letter with a friendly and professional tone.


### dev-dull/job-search-automations/gemini-last-looks
Uses the free tier of Google Gemini to check a resume for errors and final recommendations before submitting a resume to a job application.
#### Inputs
|Input Name|Input Description|Required Y/N|Default Value|
|----------|-----------------|------------|-------------|
|GOOGLE_API_KEY|Google API key for Gemini|Y||
|GEMINI_MODEL|The Gemini model to use (as found in the API URL)|Y|`gemini-flash-latest`|
|RESUME_FILENAME|The path and filename of the resume|Y||
|RESUME_MIME_TYPE|The http mime type for the resume format|Y|`application/pdf`|
|PERSONA|The role Gemini should play (typically starts, "You are..." or "Act as a...")|Y|`Act as an expert technical recruiter with a previous career in software engineering who can critically review a resume to identify errors and omissions before the candidate applies to an open position.`|

#### Outputs
|Output Name|Output Description|
|-----------|------------------|
|RAW|The full, raw JSON _string_ returned by Gemini|
|RESPONSE_TEXT|Just the generated string from Gemini|

The content of `RESPONSE_TEXT` is expected to be a JSON _string_ where Gemini has been given the following key/value descriptions:
- `errors`: a list of spelling and grammar errors in the resume. If none are found, set the list to a single result that says "No errors found."
- `ats_score`: a score between 1 and 100 on if the formatting appears to be simple enough for an Applicant Tracking System (ATS) to parse.
- `ats_explanation`: an explanation of the score no longer than 250 words.
- `ats_recommendations`: a list of specific changes that the candidate should make to their resume to improve their chances of passing an ATS.
- `conciseness_recommendations`: a list of specific changes that the candidate should make to their resume to improve its conciseness.


## Deprecated Actions
### dev-dull/job-search-automations/gemini-rewrite@v0.3.0
***Deprecated:*** Not included in releases after `@v0.3.0`. The resumes generated by this action did not result in a net-benefit. Instead, It is recommended that you use `gemini-tailor` and manually update your resume based on the generated suggestions.

Uses the free tier of Google Gemini to compare a resume to a job description and tailor the resume to target the specific role.

#### Inputs
|Input Name|Input Description|Required Y/N|Default Value|
|----------|-----------------|------------|-------------|
|GOOGLE_API_KEY|Google API key for Gemini|Y||
|GEMINI_MODEL|The Gemini model to use (as found in the API URL)|Y|`gemini-2.0-flash`|
|RESUME_TEXT|The body of the resume to evaluate|Y||
|RESUME_MIME_TYPE|The http mime type for the resume format|Y|`text/plain`|
|JOB_DESCRIPTION|The body of the job description to evaluate|Y||
|JOB_DESCRIPTION_MIME_TYPE|The http mime type for the job description format|Y|`text/plain`|
|PERSONA|The role Gemini should play (typically starts, "You are..." or "Act as a...")|Y|`Act as an expert technical recruiter and career coach with a previous career in software engineering who assists candidates with tailoring their resume for a specific job application.`|

#### Outputs
|Output Name|Output Description|
|-----------|------------------|
|RAW|The full, raw JSON _string_ returned by Gemini|
|RESPONSE_TEXT|Just the generated string from Gemini|

The content of `RESPONSE_TEXT` is expected to the modified resume written in an HTML format.


## License

Licensed under the [PolyForm Noncommercial License 1.0.0](LICENSE).

This is a **source-available, noncommercial** license — not an OSI-approved "open source" license, because it restricts commercial use (which the Open Source Definition does not permit). In short:

- **Allowed:** any noncommercial use — individuals, personal/hobby projects, nonprofits and charities, schools and universities, public research, and government use.
- **Not allowed:** commercial use, including a company using it internally for commercial advantage.

See [`LICENSE`](LICENSE) for the authoritative terms; the summary above is not a substitute for it. For commercial-use inquiries, contact the author.
