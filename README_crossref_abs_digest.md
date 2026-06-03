# Daily ABS Crossref Digest

This workflow searches Crossref every day for new records in ABS/AJG 2024 4* and 4 journals, writes a Markdown/CSV/JSON digest, and emails a formatted HTML digest with Markdown and CSV attachments.

## Files

- `config/crossref_abs_journals.json`: journal list generated from `ABS Journal Ranking 2024 (4_ and 4).xlsx`
- `build_crossref_abs_digest.py`: daily Crossref query, digest builder, state updater, and SMTP email sender
- `coding/crossref_abs_journal_monitor.py`: reusable Crossref client/parser
- `.github/workflows/daily-crossref-abs-digest.yml`: GitHub Actions schedule
- `state/crossref_abs_seen_papers.json`: generated after the first non-dry run
- `digests/crossref_abs/`: generated HTML, Markdown, and CSV digests

## Email Layout

The email is sent as multipart text plus HTML. Mail clients that support HTML show:

- a compact header with the date window and Crossref filter mode
- summary cards for total papers, AJG 4*, AJG 4, abstracts, and affiliations
- grouped sections by AJG rating and journal
- paper cards with title link, DOI, authors, affiliations, publication date, journal, and abstract
- saved HTML, Markdown, and CSV digest files for archiving or further analysis

## Local Dry Run

```powershell
& '.venv\Scripts\python.exe' 'build_crossref_abs_digest.py' --dry-run --days 1 --mode created
```

Use `--mode published` if you want the date filter to mean publication date rather than newly created Crossref record date.

## Adjust The Journal List

Edit `config/crossref_abs_journals.json` directly to add, remove, or change journals. Each journal needs at least:

```json
{
  "name": "Strategic Management Journal",
  "issn": "1097-0266",
  "publisher": "Wiley-Blackwell",
  "field": "STRAT",
  "ajg_2024": "4*",
  "ajg_2021": "4*",
  "ajg_2018": "4*"
}
```

The `issn` field is the key field Crossref uses. Keep it in `NNNN-NNNN` format when possible.

To regenerate the config from a new Excel list:

```powershell
& '.venv\Scripts\python.exe' 'tools\update_crossref_abs_config.py' 'C:\path\to\ABS Journal Ranking.xlsx'
```

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
