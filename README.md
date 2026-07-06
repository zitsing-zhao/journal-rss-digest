# Daily AJG 4*/4 Journal Digest

This project collects new articles from AJG 2024 journals rated 4* or 4, writes a weekly Markdown digest with full abstracts where available, and can email the digest through SMTP.

The configured list excludes `BUS HIST & ECON HIST` and `SECTOR`.

## Files

- `config/journals.json`: AJG 4*/4 journal list, ISSNs, fields, publishers, and source hints
- `build_digest.py`: fetch, deduplicate, enrich abstracts, write Markdown, and optionally email
- `tools/update_journal_config.py`: regenerate the AJG 4*/4 list from the public AJG table
- `digests/`: generated weekly Markdown files
- `state/seen_articles.json`: article IDs already included in previous runs
- `.github/workflows/weekly-rss-digest.yml`: weekly GitHub Actions schedule

## Run Locally

Create a digest without sending email:

```powershell
python build_digest.py
```

Preview all current feed items, even if already seen:

```powershell
python build_digest.py --include-seen --dry-run
```

## Email Setup

Set these environment variables before using `--send-email`:

```powershell
$env:SMTP_HOST = "smtp.gmail.com"
$env:SMTP_PORT = "587"
$env:SMTP_USER = "your_email@gmail.com"
$env:SMTP_PASSWORD = "your_app_password"
$env:EMAIL_FROM = "your_email@gmail.com"
$env:EMAIL_TO = "recipient@example.com"
$env:EMAIL_SUBJECT_PREFIX = "Weekly Journal RSS Digest"
```

Then run:

```powershell
python build_digest.py --send-email
```

For Gmail, use an app password rather than your normal account password.

## Weekly Scheduling

The included GitHub Actions workflow runs every Monday at 08:00 UTC. Add the email settings above as repository secrets:

- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USER`
- `SMTP_PASSWORD`
- `SMTP_SSL` set to `false` for STARTTLS on port 587, or `true` for SSL on port 465
- `EMAIL_FROM`
- `EMAIL_TO`
- `EMAIL_SUBJECT_PREFIX`

The action commits the generated digest and updated `state/seen_articles.json` back to the repository.

## Sources

SAGE journals use SAGE RSS feeds where configured. Other publishers use Crossref by ISSN as a fallback because publishers do not share one consistent RSS URL pattern.

The script also enriches shortened RSS abstracts by looking up the article DOI in Crossref before trying the article page.

## Notes

The first successful run after expanding the journal list may be large because `state/seen_articles.json` is empty. Later weekly runs only include articles that have not been seen before.
