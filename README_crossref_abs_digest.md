# Daily ABS Crossref Digest

This workflow searches Crossref every day for new records in ABS/AJG 2024 4* and 4 journals, writes a Markdown/CSV/JSON digest, and emails the Markdown digest with the CSV attached.

## Files

- `config/crossref_abs_journals.json`: journal list generated from `ABS Journal Ranking 2024 (4_ and 4).xlsx`
- `build_crossref_abs_digest.py`: daily Crossref query, digest builder, state updater, and SMTP email sender
- `coding/crossref_abs_journal_monitor.py`: reusable Crossref client/parser
- `.github/workflows/daily-crossref-abs-digest.yml`: GitHub Actions schedule
- `state/crossref_abs_seen_papers.json`: generated after the first non-dry run
- `digests/crossref_abs/`: generated Markdown and CSV digests

## Local Dry Run

```powershell
& '.venv\Scripts\python.exe' 'build_crossref_abs_digest.py' --dry-run --days 1 --mode created
```

Use `--mode published` if you want the date filter to mean publication date rather than newly created Crossref record date.

## Local Email Run

```powershell
$env:SMTP_HOST = "smtp.gmail.com"
$env:SMTP_PORT = "587"
$env:SMTP_USER = "your_email@gmail.com"
$env:SMTP_PASSWORD = "your_app_password"
$env:SMTP_SSL = "false"
$env:EMAIL_FROM = "your_email@gmail.com"
$env:EMAIL_TO = "recipient@example.com"
$env:EMAIL_SUBJECT_PREFIX = "Daily ABS Crossref Digest"

& '.venv\Scripts\python.exe' 'build_crossref_abs_digest.py' --send-email --days 1 --mode created
```

For Gmail, use an app password rather than the normal account password.

## GitHub Actions Setup

Add these under `Settings > Secrets and variables > Actions`:

- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USER`
- `SMTP_PASSWORD`
- `SMTP_SSL`
- `EMAIL_FROM`
- `EMAIL_TO`
- `EMAIL_SUBJECT_PREFIX`

Optional Crossref overrides:

- `CROSSREF_MAILTO`
- `CROSSREF_APP_NAME`

The workflow schedules two UTC checks and only sends when the current time in `Europe/Berlin` is 08:00, so it remains aligned with Berlin daylight saving time.
