# Free Daily B.E./B.Tech Fresher Job Alert

This repository sends a daily fresher-engineering job report to:

`placement@ksrct.ac.in`

## Coverage

Coimbatore, Bengaluru, Chennai, Hyderabad, Kochi and Thiruvananthapuram.

The workflow runs every day at **9:00 AM IST** using GitHub Actions' timezone-aware scheduled workflows. GitHub documents `on.schedule` with an IANA timezone field. 

## What it does

1. Queries Google News RSS for fresher/entry-level engineering listings for each target city.
2. Resolves aggregator links where possible.
3. Fetches the linked page and extracts available publication/update metadata.
4. Filters for B.E./B.Tech + fresher/entry-level signals.
5. Excludes obvious closed/expired roles and substantial-experience roles.
6. Deduplicates roles.
7. Prioritizes roles that are new/updated since the previous successful run.
8. Emails the report through SMTP.
9. Commits the small `state/job_state.json` file so later runs can compare against the previous alert.

## Email setup — recommended free option

The workflow uses the **Brevo Transactional Email API**. Brevo's current Free plan allows up to **300 email sends per day** and includes transactional email.

Create a free Brevo account:

https://www.brevo.com/

Then verify the sender email address you want to use (recommended: `tpoffice@ksrct.ac.in`) and create an API key.

In your GitHub repository:

**Settings → Secrets and variables → Actions → New repository secret**

Create only these two secrets:

- `BREVO_API_KEY` = your Brevo API key
- `MAIL_FROM` = the verified sender email, for example `tpoffice@ksrct.ac.in`

The recipient is already fixed to `placement@ksrct.ac.in`.

Do **not** put the API key into any file. GitHub Actions secrets are encrypted and only exposed to workflows that explicitly reference them.

Brevo limits and current free-plan details:
https://help.brevo.com/hc/en-us/articles/208580669-FAQs-What-are-the-limits-of-the-Free-plan

## SMTP alternative

The script also supports SMTP using `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, and `SMTP_USE_SSL`. This is optional; Brevo is recommended because it avoids putting a mailbox password into the workflow.

## Install into a GitHub repository

Upload these files/folders:

```text
.github/workflows/daily-job-alert.yml
job_alert.py
requirements.txt
config.json
state/job_state.json
README.md
```

Commit them to the repository's default branch.

Then open:

**Actions → Daily Engineering Fresher Job Alert → Run workflow**

Use a manual run first. When SMTP credentials are correct, the report will arrive at `placement@ksrct.ac.in`.

## GitHub permissions

The workflow uses:

```yaml
permissions:
  contents: write
```

This is only needed because the workflow commits the small state file after each successful email.

GitHub's documentation recommends keeping credentials in encrypted Actions secrets and granting minimum permissions.

## Important limitation

This is a **free job-alert collector**, not a guaranteed full-text crawler for every job board. Some boards block automated access or require login. The script therefore uses public RSS/search results and public linked pages, and it never attempts to bypass robots, CAPTCHAs, paywalls, or authentication.

Because job listings can change status quickly, candidates should confirm the employer/application page before applying.
