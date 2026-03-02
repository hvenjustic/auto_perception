from __future__ import annotations

import hashlib
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from app.models import SiteConfig, SiteRuntimeState

DEFAULT_HEADERS = {
    "User-Agent": (
        "AutoPerceptionBot/1.0 (+https://example.local/auto-perception; "
        "purpose=change-monitor)"
    )
}


@dataclass
class Layer1Result:
    candidate_urls: set[str] = field(default_factory=set)
    signals: list[str] = field(default_factory=list)
    rss_items: dict[str, str] = field(default_factory=dict)
    sitemap_lastmod: dict[str, str] = field(default_factory=dict)
    index_hash: str | None = None


@dataclass
class PageDelta:
    page_url: str
    old_hash: str | None
    new_hash: str
    change_type: str
    old_text: str
    new_text: str


@dataclass
class Layer2Result:
    page_hashes: dict[str, str] = field(default_factory=dict)
    page_snapshots: dict[str, str] = field(default_factory=dict)
    changes: list[PageDelta] = field(default_factory=list)


class WebsiteMonitor:
    def __init__(self, timeout_seconds: int = 15) -> None:
        self.timeout_seconds = timeout_seconds
        self.firecrawl_api_key = os.getenv("FIRECRAWL_API_KEY", "").strip()
        self.firecrawl_api_base = (
            os.getenv("FIRECRAWL_API_BASE", "https://api.firecrawl.dev")
            .strip()
            .rstrip("/")
        )
        self.firecrawl_timeout_ms = self._read_int_env("FIRECRAWL_TIMEOUT_MS", 30000)
        self.firecrawl_max_age_ms = self._read_int_env("FIRECRAWL_MAX_AGE_MS", 0)

    async def layer1(
        self, client: httpx.AsyncClient, site: SiteConfig, runtime: SiteRuntimeState
    ) -> Layer1Result:
        result = Layer1Result(
            rss_items=dict(runtime.rss_items),
            sitemap_lastmod=dict(runtime.sitemap_lastmod),
            index_hash=runtime.index_hash,
        )

        homepage_html = await self._fetch_page_html(client, site.url)

        feed_urls = {urljoin(site.url + "/", x) for x in ("rss.xml", "atom.xml", "feed")}
        if homepage_html:
            feed_urls.update(self._discover_feed_urls(homepage_html, site.url))
        for feed_url in sorted(feed_urls):
            feed_xml = await self._fetch_raw_text(client, feed_url)
            if not feed_xml:
                continue
            try:
                entries = self._parse_feed(feed_xml, base_url=site.url)
            except ET.ParseError:
                continue
            for item_id, item_url, updated in entries:
                previous = result.rss_items.get(item_id)
                if previous != updated:
                    result.rss_items[item_id] = updated
                    if previous is not None:
                        result.signals.append(f"RSS/Atom 更新: {item_id}")
                    if item_url and self._same_domain(site.url, item_url):
                        result.candidate_urls.add(item_url)
            if entries:
                break
        result.rss_items = self._trim_map(result.rss_items, max_size=3000)

        sitemap_url = urljoin(site.url + "/", "sitemap.xml")
        sitemap_entries = await self._collect_sitemap_entries(client, sitemap_url, depth=0)
        for page_url, lastmod in sitemap_entries.items():
            old_lastmod = result.sitemap_lastmod.get(page_url)
            if old_lastmod != lastmod:
                result.sitemap_lastmod[page_url] = lastmod
                if old_lastmod is not None:
                    result.signals.append(f"Sitemap lastmod 更新: {page_url}")
                result.candidate_urls.add(page_url)
        result.sitemap_lastmod = self._trim_map(result.sitemap_lastmod, max_size=5000)

        if homepage_html:
            index_hash, index_links = self._extract_index_fingerprint(homepage_html, site.url)
            if result.index_hash and result.index_hash != index_hash:
                result.signals.append("首页/列表页结构发生变化")
                result.candidate_urls.update(index_links)
            result.index_hash = index_hash

        return result

    async def layer2(
        self,
        client: httpx.AsyncClient,
        site: SiteConfig,
        runtime: SiteRuntimeState,
        candidate_urls: set[str],
    ) -> Layer2Result:
        result = Layer2Result(
            page_hashes=dict(runtime.page_hashes),
            page_snapshots=dict(runtime.page_snapshots),
        )
        if not candidate_urls:
            return result

        urls = list(sorted(candidate_urls))[: site.max_pages_per_scan]
        for page_url in urls:
            html = await self._fetch_page_html(client, page_url)
            if not html:
                continue
            core_text = self._extract_core_text(html, site.content_selector)
            if len(core_text) < 30:
                continue
            new_hash = hashlib.sha256(core_text.encode("utf-8")).hexdigest()
            old_hash = result.page_hashes.get(page_url)
            old_text = result.page_snapshots.get(page_url, "")
            if old_hash != new_hash:
                change_type = "new_page" if old_hash is None else "content_updated"
                result.changes.append(
                    PageDelta(
                        page_url=page_url,
                        old_hash=old_hash,
                        new_hash=new_hash,
                        change_type=change_type,
                        old_text=old_text,
                        new_text=core_text,
                    )
                )
            result.page_hashes[page_url] = new_hash
            result.page_snapshots[page_url] = core_text[:10000]

        result.page_hashes = self._trim_map(result.page_hashes, max_size=6000)
        result.page_snapshots = self._trim_map(result.page_snapshots, max_size=6000)
        return result

    async def _fetch_text(self, client: httpx.AsyncClient, url: str) -> str | None:
        return await self._fetch_raw_text(client, url)

    async def _fetch_page_html(self, client: httpx.AsyncClient, url: str) -> str | None:
        firecrawl_html = await self._fetch_with_firecrawl(client, url)
        if firecrawl_html:
            return firecrawl_html
        return await self._fetch_raw_text(client, url)

    async def _fetch_with_firecrawl(
        self, client: httpx.AsyncClient, url: str
    ) -> str | None:
        if not self.firecrawl_api_key:
            return None
        endpoint = f"{self.firecrawl_api_base}/v2/scrape"
        payload: dict[str, object] = {
            "url": url,
            "formats": ["html", "markdown"],
            "onlyMainContent": False,
            "timeout": self.firecrawl_timeout_ms,
        }
        if self.firecrawl_max_age_ms >= 0:
            payload["maxAge"] = self.firecrawl_max_age_ms
        headers = {
            "Authorization": f"Bearer {self.firecrawl_api_key}",
            "Content-Type": "application/json",
        }
        try:
            response = await client.post(
                endpoint,
                headers=headers,
                json=payload,
                timeout=max(self.timeout_seconds, 30),
            )
            if response.status_code >= 400:
                return None
            body = response.json()
            if not body.get("success", False):
                return None
            data = body.get("data", {})
            if not isinstance(data, dict):
                return None
            html = data.get("html") or data.get("rawHtml")
            if isinstance(html, str) and html.strip():
                return html
            markdown = data.get("markdown")
            if isinstance(markdown, str) and markdown.strip():
                return markdown
            return None
        except (httpx.HTTPError, ValueError, TypeError):
            return None

    async def _fetch_raw_text(self, client: httpx.AsyncClient, url: str) -> str | None:
        try:
            response = await client.get(
                url, follow_redirects=True, timeout=self.timeout_seconds, headers=DEFAULT_HEADERS
            )
            if response.status_code >= 400:
                return None
            content_type = response.headers.get("content-type", "").lower()
            if content_type and "text" not in content_type and "xml" not in content_type and "json" not in content_type:
                return None
            return response.text
        except (httpx.HTTPError, ValueError):
            return None

    def _discover_feed_urls(self, homepage_html: str, base_url: str) -> set[str]:
        soup = BeautifulSoup(homepage_html, "html.parser")
        output: set[str] = set()
        for link in soup.select("link[rel]"):
            rel = " ".join(link.get("rel", [])).lower()
            type_attr = str(link.get("type", "")).lower()
            href = str(link.get("href", "")).strip()
            if not href:
                continue
            if "alternate" in rel and ("rss" in type_attr or "atom" in type_attr or "xml" in type_attr):
                output.add(urljoin(base_url + "/", href))
        return output

    def _parse_feed(self, xml_text: str, base_url: str) -> list[tuple[str, str, str]]:
        root = ET.fromstring(xml_text)
        tag = self._local_name(root.tag)
        entries: list[tuple[str, str, str]] = []

        if tag in {"rss", "rdf"}:
            items = root.findall(".//item") + root.findall(".//{*}item")
            seen_nodes: set[int] = set()
            for item in items:
                pointer = id(item)
                if pointer in seen_nodes:
                    continue
                seen_nodes.add(pointer)
                guid = self._child_text(item, "guid")
                link = self._child_text(item, "link")
                pub_date = self._child_text(item, "pubDate") or self._child_text(item, "lastBuildDate") or ""
                item_url = urljoin(base_url + "/", link) if link else ""
                item_id = guid or item_url or f"rss-item-{len(entries)+1}"
                entries.append((item_id, item_url, pub_date))
            return entries

        if tag == "feed":
            for entry in root.findall(".//{*}entry"):
                entry_id = self._child_text(entry, "id")
                updated = self._child_text(entry, "updated") or self._child_text(entry, "published") or ""
                link = ""
                for link_node in entry.findall("{*}link"):
                    rel = link_node.attrib.get("rel", "alternate")
                    if rel == "alternate":
                        link = link_node.attrib.get("href", "")
                        break
                if not link:
                    link_node = entry.find("{*}link")
                    if link_node is not None:
                        link = link_node.attrib.get("href", "")
                item_url = urljoin(base_url + "/", link) if link else ""
                item_id = entry_id or item_url or f"atom-entry-{len(entries)+1}"
                entries.append((item_id, item_url, updated))
            return entries

        return entries

    async def _collect_sitemap_entries(
        self, client: httpx.AsyncClient, sitemap_url: str, depth: int
    ) -> dict[str, str]:
        if depth > 2:
            return {}
        xml_text = await self._fetch_text(client, sitemap_url)
        if not xml_text:
            return {}
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return {}

        output: dict[str, str] = {}
        root_tag = self._local_name(root.tag)
        if root_tag == "urlset":
            for url_node in root.findall("{*}url"):
                loc = self._child_text(url_node, "loc")
                if not loc:
                    continue
                lastmod = self._child_text(url_node, "lastmod") or ""
                output[loc.strip()] = lastmod.strip()
            return output

        if root_tag == "sitemapindex":
            nested_urls: list[str] = []
            for sitemap_node in root.findall("{*}sitemap"):
                loc = self._child_text(sitemap_node, "loc")
                if loc:
                    nested_urls.append(loc.strip())
            for nested in nested_urls[:20]:
                nested_entries = await self._collect_sitemap_entries(client, nested, depth + 1)
                output.update(nested_entries)
            return output
        return output

    def _extract_index_fingerprint(self, html: str, base_url: str) -> tuple[str, list[str]]:
        soup = BeautifulSoup(html, "html.parser")
        container = soup.find("main") or soup.find("body") or soup
        links: list[str] = []
        lines: list[str] = []
        seen: set[str] = set()
        for anchor in container.select("a[href]"):
            text = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True))
            href = str(anchor.get("href", "")).strip()
            if not href:
                continue
            abs_url = urljoin(base_url + "/", href).split("#", 1)[0]
            if abs_url in seen:
                continue
            seen.add(abs_url)
            if len(text) < 2:
                continue
            if self._same_domain(base_url, abs_url):
                links.append(abs_url)
            lines.append(f"{text[:120]}|{abs_url}")
            if len(lines) >= 20:
                break

        if not lines:
            body_text = re.sub(r"\s+", " ", container.get_text(" ", strip=True))
            lines.append(body_text[:2000])
        digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
        return digest, links[:20]

    def _extract_core_text(self, html: str, content_selector: str | None) -> str:
        soup = BeautifulSoup(html, "html.parser")

        if content_selector:
            selected = soup.select(content_selector)
            if selected:
                text = "\n".join(node.get_text(" ", strip=True) for node in selected)
                return self._normalize_text(text)

        candidates: list[str] = []
        for selector in ("article", "main", "section"):
            for node in soup.select(selector):
                text = self._normalize_text(node.get_text(" ", strip=True))
                if len(text) > 80:
                    candidates.append(text)
        if not candidates:
            for node in soup.select("div"):
                text = self._normalize_text(node.get_text(" ", strip=True))
                if len(text) > 200:
                    candidates.append(text)
                if len(candidates) >= 20:
                    break
        if not candidates:
            body = soup.find("body")
            return self._normalize_text((body or soup).get_text(" ", strip=True))
        candidates.sort(key=len, reverse=True)
        return candidates[0][:12000]

    def _local_name(self, tag: str) -> str:
        if "}" in tag:
            return tag.rsplit("}", 1)[-1]
        return tag

    def _child_text(self, node: ET.Element, child_name: str) -> str:
        child = node.find(f"{{*}}{child_name}")
        if child is None:
            child = node.find(child_name)
        if child is None or child.text is None:
            return ""
        return child.text.strip()

    def _normalize_text(self, text: str) -> str:
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _trim_map(self, data: dict[str, str], max_size: int) -> dict[str, str]:
        if len(data) <= max_size:
            return data
        items = list(data.items())[-max_size:]
        return dict(items)

    def _same_domain(self, base_url: str, target_url: str) -> bool:
        return urlparse(base_url).netloc == urlparse(target_url).netloc

    def _read_int_env(self, key: str, default: int) -> int:
        value = os.getenv(key, "").strip()
        if not value:
            return default
        try:
            return int(value)
        except ValueError:
            return default
