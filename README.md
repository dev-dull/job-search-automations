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
|GEMINI_MODEL|The Gemini model to use (as found in the API URL)|Y|`gemini-2.0-flash`|
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
|GEMINI_MODEL|The Gemini model to use (as found in the API URL)|Y|`gemini-2.0-flash`|
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
