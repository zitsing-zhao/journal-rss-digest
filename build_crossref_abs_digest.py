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
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def config_journal_to_dataclass(item: dict[str, Any]) -> Journal:
    return Journal(
        issn=str(item.get("issn", "")).strip(),
        field=str(item.get("field", "")).strip(),
        title=str(item.get("name", "")).strip(),
        publisher=str(item.get("publisher", "")).strip(),
        ajg2024=str(item.get("ajg_2024", "")).strip(),
        ajg2021=str(item.get("ajg_2021", "")).strip(),
        ajg2018=str(item.get("ajg_2018", "")).strip(),
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


def md_escape(value: str) -> str:
    return value.replace("\n", " ").strip()


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

    grouped: dict[str, dict[str, list[dict[str, str]]]] = {}
    for record in sort_records(records):
        rating = record.get("abs_ajg2024") or "Unrated"
        journal = record.get("abs_journal_title") or record.get("journal") or "Unknown journal"
        grouped.setdefault(rating, {}).setdefault(journal, []).append(record)

    for rating in sorted(grouped, key=lambda x: (x != "4*", x)):
        lines.extend([f"## AJG {rating}", ""])
        for journal in sorted(grouped[rating]):
            items = grouped[rating][journal]
            sample = items[0]
            lines.extend(
                [
                    f"### {journal}",
                    f"- Field: {sample.get('abs_field', '')}",
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


def group_records(records: list[dict[str, str]]) -> dict[str, dict[str, list[dict[str, str]]]]:
    grouped: dict[str, dict[str, list[dict[str, str]]]] = {}
    for record in sort_records(records):
        rating = record.get("abs_ajg2024") or "Unrated"
        journal = record.get("abs_journal_title") or record.get("journal") or "Unknown journal"
        grouped.setdefault(rating, {}).setdefault(journal, []).append(record)
    return grouped


def short_text(value: str, limit: int = 900) -> str:
    value = " ".join((value or "").split())
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "..."


def build_html_email(
    records: list[dict[str, str]],
    errors: list[str],
    generated_at: datetime,
    start_date: str,
    end_date: str,
    mode: str,
) -> str:
    total = len(records)
    abstract_count = sum(bool(record.get("abstract")) for record in records)
    affiliation_count = sum(bool(record.get("affiliations")) for record in records)
    rating_counts: dict[str, int] = {}
    for record in records:
        rating = record.get("abs_ajg2024") or "Unrated"
        rating_counts[rating] = rating_counts.get(rating, 0) + 1

    stat_cards = [
        ("New papers", str(total)),
        ("AJG 4*", str(rating_counts.get("4*", 0))),
        ("AJG 4", str(rating_counts.get("4", 0))),
        ("With abstracts", str(abstract_count)),
        ("With affiliations", str(affiliation_count)),
    ]
    stat_html = "".join(
        f"""
        <td style="padding:0 8px 12px 0;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
                 style="background:#ffffff;border:1px solid #d9e2ec;border-radius:8px;">
            <tr><td style="padding:12px 14px 4px;font:12px Arial,sans-serif;color:#5b6b7a;">{h(label)}</td></tr>
            <tr><td style="padding:0 14px 12px;font:700 24px Arial,sans-serif;color:#0f2f4a;">{h(value)}</td></tr>
          </table>
        </td>
        """
        for label, value in stat_cards
    )

    if not records:
        content_html = """
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
               style="background:#ffffff;border:1px solid #d9e2ec;border-radius:8px;">
          <tr><td style="padding:22px;font:15px Arial,sans-serif;color:#314154;">
            No new papers were found for this run.
          </td></tr>
        </table>
        """
    else:
        sections: list[str] = []
        grouped = group_records(records)
        for rating in sorted(grouped, key=lambda x: (x != "4*", x)):
            journal_blocks: list[str] = []
            for journal in sorted(grouped[rating]):
                items = grouped[rating][journal]
                sample = items[0]
                paper_blocks: list[str] = []
                for item in items:
                    title = item.get("title") or "(untitled)"
                    url = item.get("url", "").strip()
                    linked_title = (
                        f'<a href="{h(url)}" style="color:#0b5cab;text-decoration:none;">{h(title)}</a>'
                        if url
                        else h(title)
                    )
                    meta_parts = [
                        item.get("published_date", ""),
                        f"DOI: {item.get('doi', '')}" if item.get("doi") else "",
                        item.get("journal", ""),
                    ]
                    meta = " | ".join(part for part in meta_parts if part)
                    abstract = short_text(item.get("abstract", ""))
                    paper_blocks.append(
                        f"""
                        <tr>
                          <td style="padding:16px 0;border-top:1px solid #edf2f7;">
                            <div style="font:700 16px Arial,sans-serif;line-height:1.35;color:#14324a;">{linked_title}</div>
                            {f'<div style="margin-top:6px;font:12px Arial,sans-serif;color:#637487;">{h(meta)}</div>' if meta else ''}
                            {f'<div style="margin-top:10px;font:13px Arial,sans-serif;line-height:1.45;color:#314154;"><strong>Authors:</strong> {h(item.get("authors", ""))}</div>' if item.get("authors") else ''}
                            {f'<div style="margin-top:6px;font:13px Arial,sans-serif;line-height:1.45;color:#314154;"><strong>Affiliations:</strong> {h(short_text(item.get("affiliations", ""), 450))}</div>' if item.get("affiliations") else ''}
                            {f'<div style="margin-top:10px;padding:10px 12px;background:#f6f8fb;border-left:3px solid #2b7bbb;font:13px Arial,sans-serif;line-height:1.5;color:#314154;">{h(abstract)}</div>' if abstract else ''}
                          </td>
                        </tr>
                        """
                    )
                journal_blocks.append(
                    f"""
                    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
                           style="margin:0 0 14px;background:#ffffff;border:1px solid #d9e2ec;border-radius:8px;">
                      <tr>
                        <td style="padding:15px 18px 4px;">
                          <div style="font:700 17px Arial,sans-serif;color:#0f2f4a;">{h(journal)}</div>
                          <div style="margin-top:5px;font:12px Arial,sans-serif;color:#637487;">
                            Field: {h(sample.get("abs_field", ""))} | Articles: {len(items)}
                          </div>
                        </td>
                      </tr>
                      <tr><td style="padding:0 18px 2px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0">{''.join(paper_blocks)}</table></td></tr>
                    </table>
                    """
                )
            sections.append(
                f"""
                <tr><td style="padding:22px 0 8px;font:700 20px Arial,sans-serif;color:#0f2f4a;">AJG {h(rating)}</td></tr>
                <tr><td>{''.join(journal_blocks)}</td></tr>
                """
            )
        content_html = f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{"".join(sections)}</table>'

    error_html = ""
    if errors:
        items = "".join(f"<li>{h(error)}</li>" for error in errors)
        error_html = f"""
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
               style="margin-top:16px;background:#fff7ed;border:1px solid #fed7aa;border-radius:8px;">
          <tr><td style="padding:16px 18px;font:13px Arial,sans-serif;line-height:1.5;color:#7c2d12;">
            <strong>Crossref errors</strong>
            <ul style="margin:8px 0 0 18px;padding:0;">{items}</ul>
          </td></tr>
        </table>
        """

    return f"""<!doctype html>
<html>
  <body style="margin:0;padding:0;background:#eef3f7;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eef3f7;">
      <tr>
        <td align="center" style="padding:24px 12px;">
          <table role="presentation" width="760" cellpadding="0" cellspacing="0"
                 style="max-width:760px;width:100%;background:#f8fafc;border-radius:12px;overflow:hidden;">
            <tr>
              <td style="padding:28px 30px;background:#0f2f4a;color:#ffffff;">
                <div style="font:700 24px Arial,sans-serif;line-height:1.25;">Daily ABS 4*/4 Crossref Digest</div>
                <div style="margin-top:8px;font:14px Arial,sans-serif;line-height:1.45;color:#dbeafe;">
                  {h(start_date)} to {h(end_date)} | mode: {h(mode)} | generated {h(generated_at.strftime('%Y-%m-%d %H:%M UTC'))}
                </div>
              </td>
            </tr>
            <tr>
              <td style="padding:22px 22px 8px;">
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>{stat_html}</tr></table>
              </td>
            </tr>
            <tr><td style="padding:0 22px 24px;">{content_html}{error_html}</td></tr>
            <tr>
              <td style="padding:16px 24px;background:#e7eef5;font:12px Arial,sans-serif;line-height:1.45;color:#536475;">
                The Markdown and CSV versions are attached. Abstracts and affiliations appear only when Crossref supplies them.
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
    csv_path = args.digest_dir / f"{generated_at.strftime('%Y-%m-%d')}_crossref_abs_digest.csv"

    markdown = build_markdown(new_records, errors, generated_at, start_date, end_date, args.mode)
    html_text = build_html_email(new_records, errors, generated_at, start_date, end_date, args.mode)
    digest_path.write_text(markdown, encoding="utf-8")
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

    if args.send_email and not args.dry_run:
        send_email(digest_path, markdown, csv_path, html_text)

    print(f"Wrote {digest_path}")
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
