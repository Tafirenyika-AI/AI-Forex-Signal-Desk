"""Federal Reserve statement archival (Autonomous Upgrade Spec sec. 9).

Uses the Fed's own RSS feed — a structured, explicitly machine-readable
source meant for programmatic consumption (unlike scraping a page not
designed for it), verified live 2026-08-13 with real, current FOMC
statement/minutes entries.

Statement text extraction below is a real HTML content-container parse
(stdlib html.parser, no new dependency), not a guess — verified live
against a real FOMC statement page, whose actual body text is inside the
page's `id="article"` container.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any

import httpx

FED_MONETARY_RSS = "https://www.federalreserve.gov/feeds/press_monetary.xml"


class _ArticleTextExtractor(HTMLParser):
    """Strips tags from the page's #article container, keeping text order."""

    def __init__(self):
        super().__init__()
        self.in_article = False
        self.depth = 0
        self.chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if attrs_dict.get("id") == "article":
            self.in_article = True
            self.depth = 1
        elif self.in_article:
            self.depth += 1
        if self.in_article and tag in ("script", "style"):
            self._skip = True

    def handle_endtag(self, tag):
        if self.in_article:
            self.depth -= 1
            if self.depth <= 0:
                self.in_article = False

    def handle_data(self, data):
        if self.in_article:
            self.chunks.append(data)

    def get_text(self) -> str:
        text = "".join(self.chunks)
        return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()


def extract_article_text(html: str) -> str:
    parser = _ArticleTextExtractor()
    parser.feed(html)
    return parser.get_text()


async def fetch_recent_statements(max_items: int = 10) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=20.0, headers={"User-Agent": "Mozilla/5.0"}) as client:
        rss_response = await client.get(FED_MONETARY_RSS)
        rss_response.raise_for_status()
        items = _parse_rss_items(rss_response.text)[:max_items]

        results = []
        for item in items:
            try:
                page_response = await client.get(item["url"])
                page_response.raise_for_status()
                text = extract_article_text(page_response.text)
            except (httpx.HTTPError, httpx.TransportError):
                text = ""
            results.append({**item, "full_text": text})
        return results


def _parse_rss_items(xml_text: str) -> list[dict[str, Any]]:
    items = []
    for block in re.findall(r"<item>(.*?)</item>", xml_text, flags=re.DOTALL):
        title_match = re.search(r"<title>(.*?)</title>", block, flags=re.DOTALL)
        link_match = re.search(r"<link>\s*<!\[CDATA\[(.*?)\]\]>\s*</link>", block, flags=re.DOTALL)
        date_match = re.search(r"<pubDate>\s*<!\[CDATA\[(.*?)\]\]>\s*</pubDate>", block, flags=re.DOTALL)
        if not (title_match and link_match and date_match):
            continue
        try:
            published_at = parsedate_to_datetime(date_match.group(1).strip())
            if published_at.tzinfo is None:
                published_at = published_at.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        items.append(
            {
                "title": title_match.group(1).strip(),
                "url": link_match.group(1).strip(),
                "published_at": published_at,
            }
        )
    return items
