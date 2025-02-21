# Job Search Automations
A collection of tools to help identify jobs and apply to them.

## GitHub Actions
### dev-dull/job-search-automations/gemini-qualified
Uses the free tier of Google Gemini to compare a resume to a job description and ranks if the position is a good fit with a score between 1 and 100.

#### Inputs
|Input Name|Input Description|Required Y/N|Default Value|
|----------|-----------------|------------|-------------|
|GOOGLE_API_KEY|Google API key for Gemini|Y||
|GEMINI_MODEL|The Gemini model to use (as found in the API URL)|Y|`gemini-2.0-flash`|
|RESUME_TEXT|The body of the resume to evaluate|Y||
|RESUME_MIME_TYPE|The http mime type for the resume format|Y|`text/plain`|
|CAREER_GROWTH_KEYWORDS|A list of keywords used to create a score to evaluate if the position will take your career in the desired direction|Y||
|JOB_DESCRIPTION|The body of the job description to evaluate|Y||
|JOB_DESCRIPTION_MIME_TYPE|The http mime type for the job description format|Y|`text/plain`|
|PERSONA|The role Gemini should play (typically starts, "You are..." or "Act as a...")|Y|`Act as an expert technical recruiter with a previous career in software engineering who can crtically compare resumes to job descriptions to determine if a candidate is a fit for a role.`|

#### Outputs
|Output Name|Output Description|
|-----------|------------------|
|RAW|The raw, JSON _string_ returned by Gemini|
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

### dev-dull/job-search-automations/gemini-rewrite
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
|RAW|The raw, JSON _string_ returned by Gemini|
|RESPONSE_TEXT|Just the generated string from Gemini|

The content of `RESPONSE_TEXT` is expected to the modified resume written in an HTML format.
