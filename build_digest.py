from __future__ import annotations

import argparse
import html
import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config" / "journals.json"
DEFAULT_STATE = ROOT / "state" / "seen_articles.json"
DEFAULT_DIGEST_DIR = ROOT / "digests"

SAGE_BASE = "https://journals.sagepub.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0 Safari/537.36"
)


@dataclass(frozen=True)
class Article:
    article_id: str
    journal: str
    title: str
    link: str
    published: str
    authors: list[str]
    summary: str
    ajg_2024: str
    field: str
    feed_url: str


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def fetch_text(url: str, timeout: int = 30, retries: int = 2) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/xml, text/xml, text/html;q=0.8, */*;q=0.5",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://journals.sagepub.com/",
        },
    )
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace")
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in {403, 429, 500, 502, 503, 504} or attempt == retries:
                raise
            time.sleep(2**attempt)
        except urllib.error.URLError as exc:
            last_error = exc
            if attempt == retries:
                raise
            time.sleep(2**attempt)
    assert last_error is not None
    raise last_error


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def md_escape(value: str) -> str:
    return value.replace("\n", " ").strip()


def first_text(element: ET.Element, names: list[str]) -> str:
    for name in names:
        if ":" in name:
            continue
        found = element.find(name)
        if found is not None and found.text:
            return clean_text(found.text)
    return ""


def all_text(element: ET.Element, names: list[str]) -> list[str]:
    values: list[str] = []
    for name in names:
        if ":" in name:
            continue
        for found in element.findall(name):
            if found.text:
                text = clean_text(found.text)
                if text and text not in values:
                    values.append(text)
    return values


def parse_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def sort_date_key(article: Article) -> datetime:
    return parse_date(article.published) or datetime.min.replace(tzinfo=timezone.utc)


def rss_namespaces(root: ET.Element) -> dict[str, str]:
    ns: dict[str, str] = {}
    for elem in root.iter():
        if elem.tag.startswith("{"):
            uri, _, local = elem.tag[1:].partition("}")
            if uri and uri not in ns.values():
                prefix = {
                    "http://purl.org/dc/elements/1.1/": "dc",
                    "http://purl.org/rss/1.0/modules/content/": "content",
                    "http://prismstandard.org/namespaces/basic/2.0/": "prism",
                    "http://www.w3.org/2005/Atom": "atom",
                }.get(uri, f"ns{len(ns)}")
                ns[prefix] = uri
            if local:
                continue
    return ns


def child_text_by_local_name(element: ET.Element, local_names: list[str]) -> str:
    wanted = set(local_names)
    for child in element:
        local = child.tag.rsplit("}", 1)[-1]
        if local in wanted and child.text:
            return clean_text(child.text)
    return ""


def children_text_by_local_name(element: ET.Element, local_names: list[str]) -> list[str]:
    wanted = set(local_names)
    values: list[str] = []
    for child in element:
        local = child.tag.rsplit("}", 1)[-1]
        if local in wanted and child.text:
            text = clean_text(child.text)
            if text and text not in values:
                values.append(text)
    return values


def discover_sage_feed_url(journal: dict[str, Any], preference: str) -> str:
    if journal.get("feed_url"):
        return str(journal["feed_url"])

    code = journal["sage_code"]
    feed_jc = journal.get("sage_feed_jc") or f"{code}a"
    if journal.get("sage_feed_jc"):
        return sage_feed_url(feed_jc, preference)

    connected_url = journal.get("connected_url") or f"{SAGE_BASE}/connected/{code}"

    try:
        html_text = fetch_text(connected_url)
        hrefs = re.findall(r"""href=["']([^"']*showFeed[^"']*feed=rss[^"']*)["']""", html_text)
        urls = [urllib.parse.urljoin(SAGE_BASE, html.unescape(href)) for href in hrefs]
        if urls:
            if preference == "online_first":
                for url in urls:
                    if "type=axatoc" in url:
                        return url
            for url in urls:
                if "type=etoc" in url:
                    return url
            return urls[0]
    except Exception as exc:
        print(f"Warning: could not discover RSS link for {journal['name']}: {exc}", file=sys.stderr)

    return sage_feed_url(feed_jc, preference)


def sage_feed_url(feed_jc: str, preference: str) -> str:
    feed_type = "axatoc" if preference == "online_first" else "etoc"
    query = urllib.parse.urlencode(
        {
            "ui": "0",
            "mi": "ehikzz",
            "ai": "2b4",
            "jc": feed_jc,
            "type": feed_type,
            "feed": "rss",
        }
    )
    return f"{SAGE_BASE}/action/showFeed?{query}"


def parse_feed(feed_xml: str, journal: dict[str, Any], feed_url: str) -> list[Article]:
    root = ET.fromstring(feed_xml)

    if root.tag.endswith("feed"):
        entries = root.findall("{http://www.w3.org/2005/Atom}entry")
    else:
        entries = [elem for elem in root.iter() if elem.tag.rsplit("}", 1)[-1] == "item"]

    articles: list[Article] = []
    for entry in entries:
        title = first_text(entry, ["title"]) or child_text_by_local_name(entry, ["title"])
        link = first_text(entry, ["link"]) or child_text_by_local_name(entry, ["link"])
        if not link:
            for child in entry:
                local = child.tag.rsplit("}", 1)[-1]
                if local == "link" and child.attrib.get("href"):
                    link = child.attrib["href"]
                    break

        guid = first_text(entry, ["guid", "id"]) or child_text_by_local_name(entry, ["guid", "id"])
        doi = child_text_by_local_name(entry, ["doi"])
        article_id = doi or guid or link or title
        if not article_id:
            continue

        published = (
            first_text(entry, ["pubDate", "published", "updated"])
            or child_text_by_local_name(entry, ["pubDate", "publicationDate", "published", "updated"])
        )
        authors = all_text(entry, ["author"]) or children_text_by_local_name(
            entry,
            ["creator", "author"],
        )
        summary = (
            first_text(entry, ["description", "summary", "content:encoded"])
            or child_text_by_local_name(entry, ["description", "summary", "encoded"])
        )

        articles.append(
            Article(
                article_id=article_id,
                journal=journal["name"],
                title=title or "Untitled",
                link=link,
                published=published,
                authors=authors,
                summary=summary,
                ajg_2024=journal.get("ajg_2024", ""),
                field=journal.get("field", ""),
                feed_url=feed_url,
            )
        )

    return articles


def collect_articles(config: dict[str, Any]) -> tuple[list[Article], list[str]]:
    preference = config.get("feed_preference", "online_first")
    request_delay_seconds = float(config.get("request_delay_seconds", 1.0))
    articles: list[Article] = []
    errors: list[str] = []

    for index, journal in enumerate(config["journals"]):
        if index:
            time.sleep(request_delay_seconds)
        try:
            feed_url = discover_sage_feed_url(journal, preference)
            feed_xml = fetch_text(feed_url)
            articles.extend(parse_feed(feed_xml, journal, feed_url))
            print(f"Fetched {journal['name']}: {feed_url}")
        except (urllib.error.URLError, ET.ParseError, KeyError, TimeoutError) as exc:
            errors.append(f"{journal.get('name', 'Unknown journal')}: {exc}")

    return articles, errors


def build_markdown(
    new_articles: list[Article],
    errors: list[str],
    generated_at: datetime,
) -> str:
    title_date = generated_at.strftime("%Y-%m-%d")
    lines = [
        f"# Weekly SAGE AJG 4*/4 Journal Digest - {title_date}",
        "",
        f"Generated at: {generated_at.strftime('%Y-%m-%d %H:%M UTC')}",
        f"New articles: {len(new_articles)}",
        "",
    ]

    if not new_articles:
        lines.extend(["No new articles found since the last run.", ""])

    grouped: dict[str, dict[str, list[Article]]] = {}
    for article in sorted(new_articles, key=sort_date_key, reverse=True):
        rating = article.ajg_2024 or "Unrated"
        grouped.setdefault(rating, {}).setdefault(article.journal, []).append(article)

    for rating in sorted(grouped, key=lambda x: (x != "4*", x)):
        lines.extend([f"## AJG {rating}", ""])
        for journal in sorted(grouped[rating]):
            items = grouped[rating][journal]
            sample = items[0]
            lines.extend([f"### {journal}", f"- Field: {sample.field}", f"- Articles: {len(items)}", ""])
            for item in items:
                title = md_escape(item.title)
                link = item.link.strip()
                if link:
                    lines.append(f"- [{title}]({link})")
                else:
                    lines.append(f"- {title}")
                if item.authors:
                    lines.append(f"  - Authors: {', '.join(item.authors)}")
                if item.published:
                    lines.append(f"  - Published: {item.published}")
                if item.summary:
                    summary = item.summary[:700].rstrip()
                    lines.append(f"  - Summary: {summary}")
                lines.append("")

    if errors:
        lines.extend(["## Feed Errors", ""])
        for error in errors:
            lines.append(f"- {error}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def send_email(markdown_path: Path, markdown_text: str) -> None:
    host = os.environ["SMTP_HOST"]
    username = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    use_ssl = os.environ.get("SMTP_SSL", "false").lower() in {"1", "true", "yes"}
    port = int(os.environ.get("SMTP_PORT") or ("465" if use_ssl else "587"))
    sender = os.environ.get("EMAIL_FROM") or username
    recipients = [x.strip() for x in os.environ["EMAIL_TO"].split(",") if x.strip()]
    subject_prefix = os.environ.get("EMAIL_SUBJECT_PREFIX") or "Weekly Journal RSS Digest"

    msg = EmailMessage()
    msg["Subject"] = f"{subject_prefix} - {datetime.now().strftime('%Y-%m-%d')}"
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(markdown_text)
    msg.add_attachment(
        markdown_text.encode("utf-8"),
        maintype="text",
        subtype="markdown",
        filename=markdown_path.name,
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Build and optionally email a weekly journal RSS digest.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_DIGEST_DIR)
    parser.add_argument("--send-email", action="store_true", help="Email the Markdown digest using SMTP env vars.")
    parser.add_argument("--include-seen", action="store_true", help="Include articles already present in state.")
    parser.add_argument("--dry-run", action="store_true", help="Do not update state or send email.")
    args = parser.parse_args()

    config = read_json(args.config, {})
    if not config.get("journals"):
        raise SystemExit(f"No journals found in {args.config}")

    state = read_json(args.state, {"seen": {}})
    seen: dict[str, Any] = state.setdefault("seen", {})

    articles, errors = collect_articles(config)
    if not articles and len(errors) == len(config["journals"]):
        print("All configured feeds failed; leaving digest, state, and email untouched.")
        for error in errors:
            print(f"- {error}")
        return 2

    unique: dict[str, Article] = {}
    for article in articles:
        unique.setdefault(article.article_id, article)

    if args.include_seen:
        new_articles = list(unique.values())
    else:
        new_articles = [article for article_id, article in unique.items() if article_id not in seen]

    generated_at = datetime.now(timezone.utc)
    markdown_text = build_markdown(new_articles, errors, generated_at)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output_path = args.output_dir / f"{generated_at.strftime('%Y-%m-%d')}_sage_ajg_digest.md"
    output_path.write_text(markdown_text, encoding="utf-8")

    if not args.dry_run:
        for article in new_articles:
            seen[article.article_id] = {
                "title": article.title,
                "journal": article.journal,
                "link": article.link,
                "published": article.published,
                "first_seen": generated_at.isoformat(),
            }
        state["last_run"] = generated_at.isoformat()
        write_json(args.state, state)

    if args.send_email and not args.dry_run:
        send_email(output_path, markdown_text)

    print(f"Wrote {output_path}")
    print(f"New articles: {len(new_articles)}")
    if errors:
        print("Feed errors:")
        for error in errors:
            print(f"- {error}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
