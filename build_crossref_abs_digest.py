#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import html
import json
import os
import smtplib
import ssl
import sys
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from coding.crossref_abs_journal_monitor import (
    DEFAULT_APP_NAME,
    DEFAULT_MAILTO,
    Journal,
    dedupe_by_doi,
    fetch_journal_works,
    normalize_ajg_rating,
    parse_paper,
    write_outputs,
)


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config" / "crossref_abs_journals.json"
DEFAULT_STATE = ROOT / "state" / "crossref_abs_seen_papers.json"
DEFAULT_DIGEST_DIR = ROOT / "digests" / "crossref_abs"
DEFAULT_OUTPUT_DIR = ROOT / "outputs" / "crossref_abs_monitor"


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def config_journal_to_dataclass(item: dict[str, Any]) -> Journal:
    return Journal(
        issn=str(item.get("issn", "")).strip(),
        field=str(item.get("field", "")).strip(),
        title=str(item.get("name", "")).strip(),
        publisher=str(item.get("publisher", "")).strip(),
        ajg2024=normalize_ajg_rating(item.get("ajg_2024", "")),
        ajg2021=normalize_ajg_rating(item.get("ajg_2021", "")),
        ajg2018=normalize_ajg_rating(item.get("ajg_2018", "")),
        tier=normalize_ajg_rating(item.get("tier", "")),
        alt_issns=tuple(str(issn).strip() for issn in item.get("alt_issns", []) if str(issn).strip()),
    )


def collect_records(
    journals: list[Journal],
    start_date: str,
    end_date: str,
    mode: str,
    mailto: str,
    app_name: str,
    rows: int,
    min_interval: float,
) -> tuple[list[dict[str, str]], list[str]]:
    records: list[dict[str, str]] = []
    errors: list[str] = []

    for index, journal in enumerate(journals, start=1):
        print(f"[{index}/{len(journals)}] {journal.issn} {journal.title}")
        try:
            works = fetch_journal_works(
                journal=journal,
                start_date=start_date,
                end_date=end_date,
                mode=mode,
                mailto=mailto,
                app_name=app_name,
                rows=rows,
                min_interval=min_interval,
            )
            journal_records = [parse_paper(item, journal) for item in works]
            records.extend(journal_records)
            print(f"  found {len(journal_records)} records")
        except Exception as exc:
            message = f"{journal.title} ({journal.issn}): {type(exc).__name__}: {exc}"
            errors.append(message)
            print(f"  error: {message}", file=sys.stderr)

    return dedupe_by_doi(records), errors


def filter_unseen(records: list[dict[str, str]], seen: dict[str, Any], include_seen: bool) -> list[dict[str, str]]:
    if include_seen:
        return records
    filtered: list[dict[str, str]] = []
    for record in records:
        doi = record.get("doi", "").lower().strip()
        if doi and doi in seen:
            continue
        filtered.append(record)
    return filtered


def sort_records(records: list[dict[str, str]]) -> list[dict[str, str]]:
    return sorted(
        records,
        key=lambda row: (
            row.get("published_date", ""),
            row.get("abs_ajg2024", ""),
            row.get("journal", ""),
            row.get("title", ""),
        ),
        reverse=True,
    )


FIELD_NAMES = {
    "ACCOUNT": "Accounting",
    "BUS HIST & ECON HIST": "Business & Economic History",
    "ECON": "Economics",
    "ENT-SBM": "Entrepreneurship & Small Business",
    "ETHICS-CSR-MAN": "General Management, Ethics & CSR",
    "FINANCE": "Finance",
    "HRM&EMP": "HRM & Employment Studies",
    "IB&AREA": "International Business & Area Studies",
    "INFO MAN": "Information Management",
    "INNOV": "Innovation",
    "MDEV&EDU": "Management Development & Education",
    "OPS&TECH": "Operations & Technology Management",
    "OR&MANSCI": "Operations Research & Management Science",
    "ORG STUD": "Organisation Studies",
    "POL SCI": "Political Science",
    "PSYCH (WOP-OB)": "Psychology (Work, Org & OB)",
    "PUB SEC": "Public Sector & Health Care",
    "REGIONAL STUDIES, PLANNING AND ENVIRONMENT": "Regional Studies, Planning & Environment",
    "SOC SCI": "Social Sciences & Sociology",
    "STRAT": "Strategy",
}

Grouped = dict[str, dict[str, dict[str, list[dict[str, str]]]]]


def md_escape(value: str) -> str:
    return value.replace("\n", " ").strip()


def record_tier(record: dict[str, str]) -> str:
    return record.get("digest_tier") or record.get("abs_ajg2024") or "Unrated"


def tier_sort_key(tier: str) -> tuple[int, str]:
    return ({"4*": 0, "4": 1}.get(tier, 2), tier)


def tier_label(tier: str) -> str:
    return f"ABS {tier}" if tier in {"4*", "4"} else tier


def field_label(field: str) -> str:
    return FIELD_NAMES.get(field, field or "Other")


def rating_text(record: dict[str, str]) -> str:
    if record.get("abs_ajg2024"):
        return f"ABS {record['abs_ajg2024']}"
    return f"Not ABS-rated; grouped with ABS {record_tier(record)}"


def group_records(records: list[dict[str, str]]) -> Grouped:
    """Group records as tier -> field -> journal, each level sorted for display."""
    grouped: Grouped = {}
    for record in sort_records(records):
        journal = record.get("abs_journal_title") or record.get("journal") or "Unknown journal"
        field = field_label(record.get("abs_field", ""))
        grouped.setdefault(record_tier(record), {}).setdefault(field, {}).setdefault(journal, []).append(record)
    return {
        tier: {
            field: dict(sorted(grouped[tier][field].items()))
            for field in sorted(grouped[tier])
        }
        for tier in sorted(grouped, key=tier_sort_key)
    }


def build_markdown(
    records: list[dict[str, str]],
    errors: list[str],
    generated_at: datetime,
    start_date: str,
    end_date: str,
    mode: str,
) -> str:
    title_date = generated_at.strftime("%Y-%m-%d")
    lines = [
        f"# Daily ABS 4*/4 Crossref Digest - {title_date}",
        "",
        f"Generated at: {generated_at.strftime('%Y-%m-%d %H:%M UTC')}",
        f"Crossref date mode: `{mode}`",
        f"Window: `{start_date}` to `{end_date}`",
        f"New papers: {len(records)}",
        "",
    ]

    if not records:
        lines.extend(["No new papers found for this run.", ""])

    for tier, fields in group_records(records).items():
        lines.extend([f"## {tier_label(tier)}", ""])
        for field, journals in fields.items():
            lines.extend([f"### {field}", ""])
            for journal, items in journals.items():
                lines.extend(
                    [
                        f"#### {journal}",
                        f"- Rating: {rating_text(items[0])}",
                        f"- Articles: {len(items)}",
                        "",
                    ]
                )
                for item in items:
                    title = md_escape(item.get("title", "") or "(untitled)")
                    url = item.get("url", "").strip()
                    if url:
                        lines.append(f"- [{title}]({url})")
                    else:
                        lines.append(f"- {title}")
                    if item.get("doi"):
                        lines.append(f"  - DOI: {item['doi']}")
                    if item.get("authors"):
                        lines.append(f"  - Authors: {item['authors']}")
                    if item.get("affiliations"):
                        lines.append(f"  - Affiliations: {item['affiliations']}")
                    if item.get("published_date"):
                        lines.append(f"  - Published: {item['published_date']}")
                    if item.get("journal"):
                        lines.append(f"  - Crossref journal: {item['journal']}")
                    if item.get("abstract"):
                        lines.append("  - Abstract:")
                        lines.append(f"    {md_escape(item['abstract'])}")
                    lines.append("")

    if errors:
        lines.extend(["## Crossref Errors", ""])
        for error in errors:
            lines.append(f"- {error}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def h(value: str) -> str:
    return html.escape(value or "", quote=True)


# Palette and type modelled on the NBER "New This Week" working-paper email.
BRAND_BLUE = "#0057b8"
LINK_BLUE = "#0563c1"
PANEL_GREY = "#f1f1f1"
TEXT = "#333333"
MUTED = "#666666"
SANS = "Arial,Helvetica,sans-serif"
SERIF = "Georgia,'Times New Roman',Times,serif"


def long_date(value: date) -> str:
    return f"{value:%B} {value.day}, {value.year}"


def numbered_row(number: int, body: str, padding: str) -> str:
    return f"""
    <tr>
      <td valign="top" width="26" style="width:26px;padding:{padding};font:700 14px {SANS};line-height:1.4;color:{TEXT};">{number}.</td>
      <td valign="top" style="padding:{padding};font:14px {SANS};line-height:1.4;color:{TEXT};">{body}</td>
    </tr>"""


def paper_heading_html(item: dict[str, str]) -> str:
    title = h(item.get("title") or "(untitled)")
    url = item.get("url", "").strip()
    linked = (
        f'<a href="{h(url)}" style="color:{LINK_BLUE};text-decoration:underline;">{title}</a>'
        if url
        else title
    )
    journal = item.get("abs_journal_title") or item.get("journal") or ""
    authors = item.get("authors", "")
    lines = [linked]
    if authors:
        lines.append(h(authors))
    if journal:
        lines.append(f'<span style="color:{MUTED};font-style:italic;">{h(journal)}</span>')
    return "<br>".join(lines)


def paper_detail_html(item: dict[str, str]) -> str:
    parts = [paper_heading_html(item)]
    meta = " | ".join(
        part
        for part in (
            rating_text(item),
            item.get("published_date", ""),
            f"DOI {item['doi']}" if item.get("doi") else "",
        )
        if part
    )
    parts.append(f'<div style="margin-top:4px;font:12px {SANS};line-height:1.4;color:{MUTED};">{h(meta)}</div>')
    if item.get("affiliations"):
        parts.append(
            f'<div style="margin-top:4px;font:12px {SANS};line-height:1.4;color:{MUTED};">{h(item["affiliations"])}</div>'
        )
    abstract = " ".join((item.get("abstract") or "").split())
    parts.append(f'<div style="margin-top:18px;font:700 14px {SANS};color:{TEXT};">Abstract:</div>')
    if abstract:
        parts.append(f'<div style="margin-top:12px;font:14px {SANS};line-height:1.4;color:{MUTED};">{h(abstract)}</div>')
    else:
        parts.append(
            f'<div style="margin-top:12px;font:italic 14px {SANS};line-height:1.4;color:{MUTED};">Crossref did not supply an abstract.</div>'
        )
    return "".join(parts)


def group_label_row(text: str) -> str:
    return f"""
    <tr><td colspan="2" style="padding:22px 0 2px;font:700 12px {SANS};letter-spacing:.06em;text-transform:uppercase;color:{BRAND_BLUE};">{h(text)}</td></tr>"""


def build_html_email(
    records: list[dict[str, str]],
    errors: list[str],
    generated_at: datetime,
    start_date: str,
    end_date: str,
    mode: str,
) -> str:
    total = len(records)
    grouped = group_records(records)

    # Flat, continuously numbered list as in the NBER email, in tier -> field -> journal order,
    # with a light label wherever the tier or field changes.
    ordered: list[tuple[str, dict[str, str]]] = []
    for tier, fields in grouped.items():
        for field, journals in fields.items():
            label = f"{tier_label(tier)} · {field}"
            for items in journals.values():
                ordered.extend((label, item) for item in items)

    index_rows: list[str] = []
    detail_rows: list[str] = []
    previous_label = ""
    for number, (label, item) in enumerate(ordered, start=1):
        if label != previous_label:
            index_rows.append(group_label_row(label))
            previous_label = label
        index_rows.append(numbered_row(number, paper_heading_html(item), "14px 0 0"))
        detail_rows.append(numbered_row(number, paper_detail_html(item), "36px 0 0"))

    if records:
        content_html = f"""
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{''.join(index_rows)}</table>
        <div style="margin:36px 0 0;border-top:2px solid {BRAND_BLUE};font-size:0;line-height:0;">&nbsp;</div>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{''.join(detail_rows)}</table>
        """
    else:
        content_html = f'<p style="margin:24px 0 0;font:14px {SANS};color:{TEXT};">No new papers were found for this run.</p>'

    error_html = ""
    if errors:
        items = "".join(f"<li>{h(error)}</li>" for error in errors)
        error_html = f"""
        <div style="margin-top:36px;padding-top:14px;border-top:1px solid #cccccc;font:12px {SANS};line-height:1.5;color:{MUTED};">
          <strong style="color:{TEXT};">Crossref errors</strong>
          <ul style="margin:6px 0 0 18px;padding:0;">{items}</ul>
        </div>
        """

    window = long_date(date.fromisoformat(end_date))
    if start_date != end_date:
        window = f"{long_date(date.fromisoformat(start_date))} to {window}"
    heading = "New Today" if (date.fromisoformat(end_date) - date.fromisoformat(start_date)).days <= 1 else "New This Period"
    paper_word = "paper" if total == 1 else "papers"

    return f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
      @media (max-width: 620px) {{
        .panel {{ padding-left: 16px !important; padding-right: 16px !important; }}
      }}
    </style>
  </head>
  <body style="margin:0;padding:0;background:#ffffff;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#ffffff;">
      <tr>
        <td align="center" style="padding:16px 0;">
          <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:100%;max-width:600px;">
            <tr>
              <td align="center" style="padding:16px 20px;background:{BRAND_BLUE};font:700 30px {SERIF};letter-spacing:-.5px;color:#ffffff;">
                ABS 4*/4 Digest
              </td>
            </tr>
            <tr>
              <td class="panel" style="padding:20px 40px 40px;background:{PANEL_GREY};">
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                  <tr>
                    <td style="font:14px {SANS};color:{TEXT};">{h(long_date(generated_at.date()))}</td>
                    <td align="right" style="font:14px {SANS};color:{MUTED};">{total} new {paper_word}</td>
                  </tr>
                </table>
                <div style="margin-top:28px;font:700 22px {SERIF};line-height:1.3;color:{TEXT};">{heading}: The Latest ABS 4*/4 Journal Articles</div>
                <div style="margin-top:12px;font:14px {SANS};line-height:1.4;color:{MUTED};">
                  New papers in ABS/AJG 2024 4* and 4 journals registered with Crossref ({h(mode)} date) for {h(window)}.
                  Abstracts and affiliations appear only when Crossref supplies them.
                </div>
                {content_html}
                {error_html}
              </td>
            </tr>
            <tr>
              <td style="padding:16px 40px;font:12px {SANS};line-height:1.5;color:{MUTED};">
                The Markdown and CSV versions are attached. Journals without an ABS rating (political science, some sociology)
                are grouped with the ABS tier matching their standing in their field.
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>
"""


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Missing required environment variable {name}. "
            f"Add it as a GitHub Actions repository secret or variable."
        )
    return value


def send_email(
    markdown_path: Path,
    markdown_text: str,
    csv_path: Path | None = None,
    html_text: str | None = None,
) -> None:
    host = required_env("SMTP_HOST")
    username = required_env("SMTP_USER")
    password = required_env("SMTP_PASSWORD")
    use_ssl = os.environ.get("SMTP_SSL", "false").lower() in {"1", "true", "yes"}
    port = int(os.environ.get("SMTP_PORT") or ("465" if use_ssl else "587"))
    sender = os.environ.get("EMAIL_FROM") or username
    recipients = [x.strip() for x in required_env("EMAIL_TO").split(",") if x.strip()]
    if not recipients:
        raise RuntimeError("EMAIL_TO is set, but no recipient address was found.")
    subject_prefix = os.environ.get("EMAIL_SUBJECT_PREFIX") or "Daily ABS Crossref Digest"

    msg = EmailMessage()
    msg["Subject"] = f"{subject_prefix} - {datetime.now().strftime('%Y-%m-%d')}"
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(markdown_text)
    if html_text:
        msg.add_alternative(html_text, subtype="html")
    msg.add_attachment(
        markdown_text.encode("utf-8"),
        maintype="text",
        subtype="markdown",
        filename=markdown_path.name,
    )
    if csv_path and csv_path.exists():
        msg.add_attachment(
            csv_path.read_bytes(),
            maintype="text",
            subtype="csv",
            filename=csv_path.name,
        )

    if use_ssl:
        with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context()) as smtp:
            smtp.login(username, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(username, password)
            smtp.send_message(msg)


def write_csv(records: list[dict[str, str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "doi",
        "title",
        "authors",
        "affiliations",
        "abstract",
        "journal",
        "published_date",
        "crossref_type",
        "url",
        "source_issn",
        "abs_journal_title",
        "abs_field",
        "abs_publisher",
        "abs_ajg2024",
        "abs_ajg2021",
        "abs_ajg2018",
        "digest_tier",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build and optionally email a daily ABS Crossref digest.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--digest-dir", type=Path, default=DEFAULT_DIGEST_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--send-email", action="store_true", help="Email the digest using SMTP env vars.")
    parser.add_argument("--skip-empty-email", action="store_true", help="Do not send email when no new records remain after the state filter.")
    parser.add_argument("--include-seen", action="store_true", help="Include DOI records already in state.")
    parser.add_argument("--dry-run", action="store_true", help="Do not update state or send email.")
    parser.add_argument("--days", type=int, default=1, help="Days back from today, inclusive.")
    parser.add_argument("--start-date", help="Override start date, YYYY-MM-DD.")
    parser.add_argument("--end-date", help="Override end date, YYYY-MM-DD.")
    parser.add_argument(
        "--mode",
        choices=["published", "online", "created", "updated"],
        default="created",
        help="Crossref date filter. 'created' is recommended for daily monitoring.",
    )
    parser.add_argument("--rows", type=int, default=1000)
    parser.add_argument("--min-interval", type=float, default=0.2)
    parser.add_argument("--limit-journals", type=int, help="Optional cap for testing.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    today = date.today()
    start_date = args.start_date or (today - timedelta(days=args.days)).isoformat()
    end_date = args.end_date or today.isoformat()

    config = read_json(args.config, {})
    config_journals = config.get("journals") or []
    if not config_journals:
        raise SystemExit(f"No journals found in {args.config}")

    journals = [config_journal_to_dataclass(item) for item in config_journals]
    journals = [journal for journal in journals if journal.issn]
    if args.limit_journals:
        journals = journals[: args.limit_journals]

    mailto = os.environ.get("CROSSREF_MAILTO") or config.get("crossref_mailto") or DEFAULT_MAILTO
    app_name = os.environ.get("CROSSREF_APP_NAME") or config.get("app_name") or DEFAULT_APP_NAME

    print(f"Loaded {len(journals)} journals from {args.config}")
    print(f"Crossref polite identity: {app_name} (mailto:{mailto})")
    print(f"Searching mode={args.mode}, window={start_date} to {end_date}")

    records, errors = collect_records(
        journals=journals,
        start_date=start_date,
        end_date=end_date,
        mode=args.mode,
        mailto=mailto,
        app_name=app_name,
        rows=min(max(args.rows, 1), 1000),
        min_interval=args.min_interval,
    )

    state = read_json(args.state, {"seen": {}})
    seen: dict[str, Any] = state.setdefault("seen", {})
    new_records = filter_unseen(records, seen, args.include_seen)
    new_records = sort_records(new_records)

    generated_at = datetime.now(timezone.utc)
    args.digest_dir.mkdir(parents=True, exist_ok=True)
    digest_path = args.digest_dir / f"{generated_at.strftime('%Y-%m-%d')}_crossref_abs_digest.md"
    html_path = args.digest_dir / f"{generated_at.strftime('%Y-%m-%d')}_crossref_abs_digest.html"
    csv_path = args.digest_dir / f"{generated_at.strftime('%Y-%m-%d')}_crossref_abs_digest.csv"

    markdown = build_markdown(new_records, errors, generated_at, start_date, end_date, args.mode)
    html_text = build_html_email(new_records, errors, generated_at, start_date, end_date, args.mode)
    digest_path.write_text(markdown, encoding="utf-8")
    html_path.write_text(html_text, encoding="utf-8")
    write_csv(new_records, csv_path)
    write_outputs(records, args.output_dir, start_date, end_date, args.mode)

    if not args.dry_run:
        for record in new_records:
            doi = record.get("doi", "").lower().strip()
            if not doi:
                continue
            seen[doi] = {
                "title": record.get("title", ""),
                "journal": record.get("journal", ""),
                "abs_journal_title": record.get("abs_journal_title", ""),
                "published_date": record.get("published_date", ""),
                "first_seen": generated_at.isoformat(),
            }
        state["last_run"] = generated_at.isoformat()
        state["last_mode"] = args.mode
        state["last_window"] = {"start_date": start_date, "end_date": end_date}
        write_json(args.state, state)

    if args.send_email and not args.dry_run and args.skip_empty_email and not new_records:
        print("Skipping email because no new records remain after the state filter.")
    elif args.send_email and not args.dry_run:
        send_email(digest_path, markdown, csv_path, html_text)

    print(f"Wrote {digest_path}")
    print(f"Wrote {html_path}")
    print(f"Wrote {csv_path}")
    print(f"Fetched records: {len(records)}")
    print(f"New records after state filter: {len(new_records)}")
    if errors:
        print("Crossref errors:")
        for error in errors:
            print(f"- {error}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
