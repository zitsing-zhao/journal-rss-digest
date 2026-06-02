#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import html
import json
import re
import time
import zipfile
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET


DEFAULT_INPUT = Path(r"C:\Users\ziqing\Downloads\ABS Journal Ranking 2024 (4_ and 4).xlsx")
DEFAULT_OUTPUT_DIR = Path("outputs/crossref_abs_monitor")
DEFAULT_MAILTO = "ziqing.zhao@dauphine.eu"
DEFAULT_APP_NAME = "ABSJournalMonitor/1.0"
BASE_URL = "https://api.crossref.org/v1"


@dataclass(frozen=True)
class Journal:
    issn: str
    field: str
    title: str
    publisher: str
    ajg2024: str
    ajg2021: str
    ajg2018: str


def normalize_issn(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[^0-9Xx]", "", text)
    if len(text) == 8:
        return f"{text[:4]}-{text[4:].upper()}"
    return str(value or "").strip().upper()


def cell_ref_to_col_index(cell_ref: str) -> int:
    letters = re.match(r"[A-Z]+", cell_ref.upper())
    if not letters:
        return 0
    index = 0
    for char in letters.group(0):
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


def read_xlsx_first_sheet(path: Path) -> list[dict[str, str]]:
    """Read a simple .xlsx table from the first worksheet using only stdlib."""
    ns = {
        "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    }

    with zipfile.ZipFile(path) as zf:
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall("main:si", ns):
                parts = [node.text or "" for node in si.findall(".//main:t", ns)]
                shared_strings.append("".join(parts))

        workbook = ET.fromstring(zf.read("xl/workbook.xml"))
        first_sheet = workbook.find("main:sheets/main:sheet", ns)
        if first_sheet is None:
            return []
        rel_id = first_sheet.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]

        rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        sheet_target = None
        for rel in rels.findall("rel:Relationship", ns):
            if rel.attrib.get("Id") == rel_id:
                sheet_target = rel.attrib["Target"]
                break
        if sheet_target is None:
            raise ValueError("Could not locate first worksheet relationship.")

        sheet_path = "xl/" + sheet_target.lstrip("/")
        sheet = ET.fromstring(zf.read(sheet_path))

    rows: list[list[str]] = []
    for row_node in sheet.findall(".//main:sheetData/main:row", ns):
        values: list[str] = []
        for cell in row_node.findall("main:c", ns):
            col_index = cell_ref_to_col_index(cell.attrib.get("r", "A1"))
            while len(values) <= col_index:
                values.append("")

            cell_type = cell.attrib.get("t")
            value_node = cell.find("main:v", ns)
            inline_node = cell.find("main:is/main:t", ns)

            if cell_type == "s" and value_node is not None:
                raw = value_node.text or ""
                value = shared_strings[int(raw)] if raw.isdigit() and int(raw) < len(shared_strings) else raw
            elif cell_type == "inlineStr" and inline_node is not None:
                value = inline_node.text or ""
            elif value_node is not None:
                value = value_node.text or ""
            else:
                value = ""
            values[col_index] = value.strip()
        rows.append(values)

    if not rows:
        return []

    headers = [h.strip() for h in rows[0]]
    records: list[dict[str, str]] = []
    for row in rows[1:]:
        if not any(row):
            continue
        records.append({headers[i]: row[i].strip() if i < len(row) else "" for i in range(len(headers))})
    return records


def load_journals(path: Path) -> list[Journal]:
    records = read_xlsx_first_sheet(path)
    journals: list[Journal] = []
    for row in records:
        issn = normalize_issn(row.get("ISSN", ""))
        if not issn:
            continue
        journals.append(
            Journal(
                issn=issn,
                field=row.get("FIELD", ""),
                title=row.get("TITLE", ""),
                publisher=row.get("PUBLISHER", ""),
                ajg2024=row.get("AJG2024", ""),
                ajg2021=row.get("AJG2021", ""),
                ajg2018=row.get("AJG2018", ""),
            )
        )
    return journals


def date_parts_to_iso(parts: Any) -> str:
    if not isinstance(parts, list) or not parts:
        return ""
    first = parts[0]
    if not isinstance(first, list) or not first:
        return ""
    year = int(first[0])
    month = int(first[1]) if len(first) > 1 else 1
    day = int(first[2]) if len(first) > 2 else 1
    return f"{year:04d}-{month:02d}-{day:02d}"


def best_published_date(item: dict[str, Any]) -> str:
    for key in ("published-online", "published-print", "published", "issued"):
        value = date_parts_to_iso(item.get(key, {}).get("date-parts"))
        if value:
            return value
    return ""


def strip_markup(value: Any) -> str:
    if not value:
        return ""
    text = str(value)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def join_list(value: Any) -> str:
    if isinstance(value, list):
        return "; ".join(str(v).strip() for v in value if str(v).strip())
    return str(value or "").strip()


def parse_authors(authors: Any) -> tuple[str, str]:
    if not isinstance(authors, list):
        return "", ""

    names: list[str] = []
    affiliations: list[str] = []
    seen_affiliations: set[str] = set()

    for author in authors:
        if not isinstance(author, dict):
            continue
        given = author.get("given", "")
        family = author.get("family", "")
        literal = author.get("name", "")
        name = " ".join(part for part in (given, family) if part).strip() or literal
        if name:
            names.append(name)

        for aff in author.get("affiliation", []) or []:
            if not isinstance(aff, dict):
                continue
            aff_name = re.sub(r"\s+", " ", str(aff.get("name", "")).strip())
            if aff_name and aff_name not in seen_affiliations:
                affiliations.append(aff_name)
                seen_affiliations.add(aff_name)

    return "; ".join(names), "; ".join(affiliations)


def crossref_get(url: str, params: dict[str, str | int], headers: dict[str, str], retries: int = 4) -> dict[str, Any]:
    full_url = f"{url}?{urlencode(params)}"
    for attempt in range(retries + 1):
        request = Request(full_url, headers=headers)
        try:
            with urlopen(request, timeout=45) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw)
        except HTTPError as exc:
            if exc.code in {429, 500, 502, 503, 504} and attempt < retries:
                wait_seconds = min(60, 2 ** attempt + 1)
                time.sleep(wait_seconds)
                continue
            raise
        except URLError:
            if attempt < retries:
                time.sleep(min(60, 2 ** attempt + 1))
                continue
            raise
    raise RuntimeError("Unreachable retry state.")


def date_filter_for_mode(mode: str, start_date: str, end_date: str, issn: str) -> str:
    filter_names = {
        "published": ("from-pub-date", "until-pub-date"),
        "online": ("from-online-pub-date", "until-online-pub-date"),
        "created": ("from-created-date", "until-created-date"),
        "updated": ("from-update-date", "until-update-date"),
    }
    from_name, until_name = filter_names[mode]
    return f"type:journal-article,issn:{issn},{from_name}:{start_date},{until_name}:{end_date}"


def fetch_journal_works(
    journal: Journal,
    start_date: str,
    end_date: str,
    mode: str,
    mailto: str,
    app_name: str,
    rows: int,
    min_interval: float,
) -> Iterable[dict[str, Any]]:
    headers = {"User-Agent": f"{app_name} (mailto:{mailto})"}
    url = f"{BASE_URL}/works"
    cursor = "*"

    while True:
        params: dict[str, str | int] = {
            "filter": date_filter_for_mode(mode, start_date, end_date, journal.issn),
            "rows": rows,
            "cursor": cursor,
            "mailto": mailto,
        }
        data = crossref_get(url, params, headers)
        message = data.get("message", {})
        items = message.get("items", []) or []

        for item in items:
            yield item

        if len(items) < rows:
            break

        next_cursor = message.get("next-cursor")
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
        time.sleep(min_interval)


def parse_paper(item: dict[str, Any], journal: Journal) -> dict[str, str]:
    authors, affiliations = parse_authors(item.get("author"))
    container_title = join_list(item.get("container-title"))
    title = join_list(item.get("title"))
    doi = str(item.get("DOI", "")).strip()
    url = str(item.get("URL", "")).strip() or (f"https://doi.org/{doi}" if doi else "")

    return {
        "doi": doi,
        "title": title,
        "authors": authors,
        "affiliations": affiliations,
        "abstract": strip_markup(item.get("abstract", "")),
        "journal": container_title,
        "published_date": best_published_date(item),
        "crossref_type": str(item.get("type", "")).strip(),
        "url": url,
        "source_issn": journal.issn,
        "abs_journal_title": journal.title,
        "abs_field": journal.field,
        "abs_publisher": journal.publisher,
        "abs_ajg2024": journal.ajg2024,
        "abs_ajg2021": journal.ajg2021,
        "abs_ajg2018": journal.ajg2018,
    }


def dedupe_by_doi(records: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    deduped: list[dict[str, str]] = []
    for record in records:
        key = record.get("doi", "").lower()
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        deduped.append(record)
    return deduped


def write_outputs(records: list[dict[str, str]], output_dir: Path, start_date: str, end_date: str, mode: str) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"crossref_abs_{mode}_{start_date}_to_{end_date}"
    csv_path = output_dir / f"{stem}.csv"
    json_path = output_dir / f"{stem}.json"

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

    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)

    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(records, handle, ensure_ascii=False, indent=2)

    return csv_path, json_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Search Crossref for recent papers in ABS 4/4* journals.",
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="Path to the ABS journal-list .xlsx.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Directory for CSV/JSON outputs.")
    parser.add_argument("--mailto", default=DEFAULT_MAILTO, help="Email sent to Crossref polite pool.")
    parser.add_argument("--app-name", default=DEFAULT_APP_NAME, help="App name used in the User-Agent header.")
    parser.add_argument("--days", type=int, default=3, help="Number of days back from today, inclusive.")
    parser.add_argument("--start-date", help="Override start date, YYYY-MM-DD.")
    parser.add_argument("--end-date", help="Override end date, YYYY-MM-DD.")
    parser.add_argument(
        "--mode",
        choices=["published", "online", "created", "updated"],
        default="published",
        help="Date filter mode. Use created/updated for recurring monitors.",
    )
    parser.add_argument("--rows", type=int, default=1000, help="Crossref rows per request, max 1000.")
    parser.add_argument("--min-interval", type=float, default=0.2, help="Minimum pause between journal requests.")
    parser.add_argument("--limit-journals", type=int, help="Optional cap for testing.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    today = date.today()
    start_date = args.start_date or (today - timedelta(days=args.days)).isoformat()
    end_date = args.end_date or today.isoformat()

    journals = load_journals(args.input)
    if args.limit_journals:
        journals = journals[: args.limit_journals]

    print(f"Loaded {len(journals)} journals from {args.input}")
    print(f"Crossref polite identity: {args.app_name} (mailto:{args.mailto})")
    print(f"Searching mode={args.mode}, window={start_date} to {end_date}")

    records: list[dict[str, str]] = []
    for index, journal in enumerate(journals, start=1):
        print(f"[{index}/{len(journals)}] {journal.issn} {journal.title}")
        try:
            works = fetch_journal_works(
                journal=journal,
                start_date=start_date,
                end_date=end_date,
                mode=args.mode,
                mailto=args.mailto,
                app_name=args.app_name,
                rows=min(max(args.rows, 1), 1000),
                min_interval=args.min_interval,
            )
            journal_records = [parse_paper(item, journal) for item in works]
            records.extend(journal_records)
            print(f"  found {len(journal_records)} records")
        except Exception as exc:
            print(f"  error: {type(exc).__name__}: {exc}")
        time.sleep(args.min_interval)

    records = dedupe_by_doi(records)
    csv_path, json_path = write_outputs(records, args.output_dir, start_date, end_date, args.mode)
    print(f"Saved {len(records)} unique papers")
    print(f"CSV: {csv_path}")
    print(f"JSON: {json_path}")


if __name__ == "__main__":
    main()
