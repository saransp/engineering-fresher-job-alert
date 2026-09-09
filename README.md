# Daily B.E./B.Tech Fresher Job Alert

This repository searches public job listings for fresher/entry-level B.E./B.Tech engineering openings and emails a daily report to `placement@ksrct.ac.in`.

## Coverage

Coimbatore, Bengaluru, Chennai, Hyderabad, Kochi and Thiruvananthapuram.

## Schedule

GitHub Actions runs the workflow every day at **9:00 AM Asia/Kolkata (IST)**.

## Email setup — free Brevo option

The workflow is configured to use Brevo's transactional email API. Create a free Brevo account, verify the sender address you want to use, and create an API key.

In GitHub:

**Repository → Settings → Secrets and variables → Actions → New repository secret**

Add:

- `BREVO_API_KEY` — your Brevo API key
- `MAIL_FROM` — the verified sender email, e.g. `tpoffice@ksrct.ac.in`

The recipient is fixed in the workflow as `placement@ksrct.ac.in`.

Never commit API keys or passwords to this repository.

Brevo Free-plan information:
https://help.brevo.com/hc/en-us/articles/208580669-FAQs-What-are-the-limits-of-the-Free-plan

## What the script does

- Searches Google News RSS for fresher/entry-level engineering roles by target city.
- Resolves public links and reads accessible public listing pages.
- Filters out obvious closed/expired roles and substantial-experience roles.
- Extracts posting/update date, company, title, location, branches, experience/batch, salary when available, requirements, deadline and direct application URL.
- Tracks previously seen roles in `state/job_state.json` so newer/updated roles are highlighted first.
- Sends the completed report through Brevo.

## Important limitation

Some job boards block automated access, require login, or expose incomplete information. This workflow only uses public RSS/search results and accessible public pages; it does not bypass CAPTCHAs, authentication, robots restrictions or paywalls. Candidates should confirm the employer's application page before applying.
