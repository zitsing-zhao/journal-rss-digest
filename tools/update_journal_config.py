from __future__ import annotations

import html
import json
import re
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "journals.json"
CACHE_PATH = ROOT / "tools" / "ajg.html"
AJG_URL = "https://journalranking.org/"
EXCLUDED_FIELDS = {"BUS HIST & ECON HIST", "SECTOR", "SECATOR"}


def clean(value: str) -> str:
    value = html.unescape(value)
    value = re.sub(r"<[^>]+>", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def fetch_ajg_rows() -> list[dict[str, str]]:
    if CACHE_PATH.exists():
        html_text = CACHE_PATH.read_text(encoding="utf-8", errors="replace")
    else:
        request = urllib.request.Request(
            AJG_URL,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            html_text = response.read().decode("utf-8", errors="replace")

    rows: list[dict[str, str]] = []
    for row_html in re.findall(r"<tr[^>]*>(.*?)</tr>", html_text, flags=re.IGNORECASE | re.DOTALL):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row_html, flags=re.IGNORECASE | re.DOTALL)
        if len(cells) != 7:
            continue
        issn, field, title, publisher, ajg_2024, ajg_2021, ajg_2018 = [clean(cell) for cell in cells]
        if ajg_2024 not in {"4*", "4"}:
            continue
        if field in EXCLUDED_FIELDS:
            continue
        rows.append(
            {
                "name": title,
                "issn": issn,
                "publisher": publisher,
                "field": field,
                "ajg_2024": ajg_2024,
                "ajg_2021": ajg_2021,
                "ajg_2018": ajg_2018,
            }
        )
    return rows


def main() -> None:
    existing = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    existing_by_name = {journal["name"]: journal for journal in existing.get("journals", [])}

    journals = []
    for row in fetch_ajg_rows():
        prior = existing_by_name.get(row["name"], {})
        merged = {
            **row,
            "source": "crossref",
        }
        for key in ("sage_code", "sage_feed_jc", "feed_url", "connected_url"):
            if prior.get(key):
                merged[key] = prior[key]
                merged["source"] = "sage_rss"
        journals.append(merged)

    config = {
        "feed_preference": existing.get("feed_preference", "online_first"),
        "request_delay_seconds": existing.get("request_delay_seconds", 1.0),
        "article_page_delay_seconds": existing.get("article_page_delay_seconds", 1.0),
        "max_articles_per_journal": existing.get("max_articles_per_journal", 30),
        "enrich_abstracts": existing.get("enrich_abstracts", True),
        "excluded_fields": sorted(EXCLUDED_FIELDS),
        "source_note": "AJG 2024 rows with AJG2024 equal to 4* or 4, excluding BUS HIST & ECON HIST and SECTOR.",
        "journals": journals,
    }
    CONFIG_PATH.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {CONFIG_PATH}")
    print(f"Journal count: {len(journals)}")


if __name__ == "__main__":
    main()
