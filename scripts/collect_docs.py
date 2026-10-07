#!/usr/bin/env python3
"""Collect the SqlSugar 5.x help site into an offline Markdown corpus.

Only the documentation pages linked in the site's left navigation are fetched.
Links in article bodies are reported but never crawled, so this script cannot
silently wander into the forum or an older version of the documentation.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from bs4 import BeautifulSoup, NavigableString, Tag


BASE_URL = "https://www.donet5.com/Home/Doc"
SITE_HOSTS = {"www.donet5.com", "donet5.com"}
USER_AGENT = "SqlSugarOfflineDocsCollector/1.0 (documentation archive)"
MAX_PAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024
IMAGE_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/svg+xml": ".svg",
    "image/avif": ".avif",
}
BLOCK_TAGS = {
    "address", "article", "blockquote", "div", "dl", "h1", "h2", "h3",
    "h4", "h5", "h6", "hr", "ol", "p", "pre", "section", "table", "ul",
}
SKIP_TAGS = {"script", "style", "noscript", "iframe", "form", "button"}


@dataclass(frozen=True)
class Document:
    id: str
    title: str
    url: str
    category: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def document_id(url: str) -> str | None:
    """Accept only this site's /Home/Doc?typeId=<digits> URLs."""
    parsed = urlparse(urljoin(BASE_URL, url))
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in SITE_HOSTS:
        return None
    if parsed.path.lower().rstrip("/") != "/home/doc":
        return None
    values = parse_qs(parsed.query).get("typeId", [])
    if len(values) != 1 or not re.fullmatch(r"[0-9]+", values[0]):
        return None
    return values[0]


def is_site_url(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme in {"http", "https"}
        and parsed.hostname in SITE_HOSTS
        and not parsed.path.lower().startswith("/bbs/")
    )


class SameSiteRedirects(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> Request | None:
        if not is_site_url(newurl):
            raise ValueError(f"Redirect outside allowed site: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Fetcher:
    def __init__(self, delay: float, timeout: float, retries: int) -> None:
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self.last_request = 0.0
        self.opener = build_opener(SameSiteRedirects())

    def fetch(self, url: str, kind: str) -> tuple[bytes, str, str]:
        if not is_site_url(url):
            raise ValueError(f"URL outside allowed site: {url}")
        if kind == "page" and urlparse(url).path.lower().rstrip("/") != "/home/doc":
            raise ValueError(f"Not a documentation page: {url}")
        max_bytes = MAX_PAGE_BYTES if kind == "page" else MAX_IMAGE_BYTES
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            wait = self.delay - (time.monotonic() - self.last_request)
            if wait > 0:
                time.sleep(wait)
            self.last_request = time.monotonic()
            request = Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"})
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    final_url = response.geturl()
                    if not is_site_url(final_url):
                        raise ValueError(f"Redirect outside allowed site: {final_url}")
                    if kind == "page" and urlparse(final_url).path.lower().rstrip("/") != "/home/doc":
                        raise ValueError(f"Redirect outside documentation: {final_url}")
                    content_type = response.headers.get_content_type().lower()
                    if kind == "page" and content_type not in {"text/html", "application/xhtml+xml"}:
                        raise ValueError(f"Unexpected document content type: {content_type}")
                    if kind == "image" and content_type not in IMAGE_EXTENSIONS:
                        raise ValueError(f"Unexpected image content type: {content_type}")
                    length = response.headers.get("Content-Length")
                    if length and length.isdigit() and int(length) > max_bytes:
                        raise ValueError(f"Resource exceeds {max_bytes} bytes")
                    body = response.read(max_bytes + 1)
                    if len(body) > max_bytes:
                        raise ValueError(f"Resource exceeds {max_bytes} bytes")
                    charset = response.headers.get_content_charset() or "utf-8"
                    return body, content_type, charset
            except HTTPError as exc:
                last_error = exc
                if exc.code not in {408, 429, 500, 502, 503, 504} or attempt == self.retries:
                    break
                retry_after = exc.headers.get("Retry-After", "")
                backoff = min(float(retry_after), 60.0) if retry_after.isdigit() else min(2 ** (attempt - 1), 8)
                time.sleep(backoff)
            except (TimeoutError, URLError, OSError) as exc:
                last_error = exc
                if attempt == self.retries:
                    break
                time.sleep(min(2 ** (attempt - 1), 8))
        assert last_error is not None
        raise last_error


def discover_navigation(soup: BeautifulSoup) -> list[Document]:
    tree = soup.select_one(".site-tree ul.layui-tree") or soup.select_one("ul.layui-tree")
    if tree is None:
        raise ValueError("Documentation navigation was not found")
    category = "未分类"
    documents: list[Document] = []
    seen: set[str] = set()
    for item in tree.find_all("li", recursive=False):
        heading = item.find("h2")
        if heading is not None and "clear" in item.get("class", []):
            category = heading.get_text(" ", strip=True) or category
            continue
        anchor = item.find("a", href=True)
        if anchor is None:
            continue
        doc_id = document_id(anchor["href"])
        if doc_id is None or doc_id in seen:
            continue
        seen.add(doc_id)
        documents.append(Document(
            id=doc_id,
            title=anchor.get_text(" ", strip=True) or doc_id,
            url=f"{BASE_URL}?typeId={doc_id}",
            category=category,
        ))
    if not documents:
        raise ValueError("Documentation navigation contained no article links")
    return documents


def normalize_text(text: str) -> str:
    return text.replace("\xa0", " ").replace("\u200b", "")


class AssetManager:
    def __init__(self, output: Path, fetcher: Fetcher, failures: list[dict[str, str]],
                 skip_images: bool = False) -> None:
        self.output = output
        self.fetcher = fetcher
        self.failures = failures
        self.assets = output / "assets"
        self.memo: dict[str, str] = {}
        self.skip_images = skip_images

    def localize(self, src: str, doc_id: str) -> str:
        absolute = urljoin(BASE_URL, src.strip())
        if self.skip_images or not is_site_url(absolute):
            return absolute
        if absolute in self.memo:
            return self.memo[absolute]
        digest = hashlib.sha256(absolute.encode("utf-8")).hexdigest()[:24]
        existing = next(
            (path for path in self.assets.glob(f"{digest}.*") if path.suffix in IMAGE_EXTENSIONS.values()),
            None,
        ) if self.assets.exists() else None
        if existing is not None and existing.is_file():
            local = f"assets/{existing.name}"
            self.memo[absolute] = local
            return local
        try:
            body, content_type, _ = self.fetcher.fetch(absolute, "image")
            extension = IMAGE_EXTENSIONS[content_type]
            self.assets.mkdir(parents=True, exist_ok=True)
            target = self.assets / f"{digest}{extension}"
            atomic_write(target, body)
            local = f"assets/{target.name}"
            self.memo[absolute] = local
            return local
        except Exception as exc:
            self.failures.append({
                "kind": "image", "id": doc_id, "url": absolute,
                "error": str(exc), "at": utc_now(),
            })
            self.memo[absolute] = absolute
            return absolute


class MarkdownRenderer:
    def __init__(self, assets: AssetManager, doc_id: str, offline_ids: set[str]) -> None:
        self.assets = assets
        self.doc_id = doc_id
        self.offline_ids = offline_ids

    def inline(self, node: Any, in_table: bool = False) -> str:
        if isinstance(node, NavigableString):
            return re.sub(r"\s+", " ", normalize_text(str(node)))
        if not isinstance(node, Tag) or node.name in SKIP_TAGS:
            return ""
        name = node.name.lower()
        if name == "br":
            return "<br>" if in_table else "  \n"
        if name == "img":
            src = node.get("src") or node.get("data-src") or node.get("data-original")
            if not src:
                return ""
            alt = normalize_text(node.get("alt") or node.get("title") or "图片").replace("]", "\\]")
            return f"![{alt}](<{self.assets.localize(src, self.doc_id)}>)"
        if name == "pre" and in_table:
            code = normalize_text(node.get_text()).replace("\r\n", "\n").replace("\r", "\n")
            return "<code>" + html.escape(code).replace("\n", "<br>") + "</code>"
        content = "".join(self.inline(child, in_table) for child in node.children)
        if name == "a":
            href = node.get("href", "").strip()
            if not href or href.lower().startswith(("javascript:", "data:")):
                return content
            absolute = urljoin(BASE_URL, href)
            target_id = document_id(absolute)
            if target_id in self.offline_ids:
                absolute = f"{target_id}.md"
                fragment = urlparse(href).fragment
                if fragment:
                    absolute += f"#{fragment}"
            label = content.strip() or href
            return f"[{label}](<{absolute}>)"
        if name in {"strong", "b"} and content.strip():
            return f"**{content.strip()}**"
        if name in {"em", "i"} and content.strip():
            return f"*{content.strip()}*"
        if name == "code" and content.strip():
            ticks = "`" * max(1, max((len(run) for run in re.findall(r"`+", content)), default=0) + 1)
            return f"{ticks}{content.strip()}{ticks}"
        return content

    def code_block(self, node: Tag) -> str:
        for br in node.find_all("br"):
            br.replace_with("\n")
        code = normalize_text(node.get_text()).replace("\r\n", "\n").replace("\r", "\n").strip("\n")
        style = " ".join(node.get("class", []))
        brush = re.search(r"brush\s*:\s*([^;\s]+)", style, re.I)
        language = brush.group(1).lower() if brush else ""
        language = {"c#": "csharp", "js": "javascript", "ts": "typescript"}.get(language, language)
        ticks = "`" * max(3, max((len(run) for run in re.findall(r"`+", code)), default=0) + 1)
        return f"{ticks}{language}\n{code}\n{ticks}"

    def table(self, node: Tag) -> str:
        rows: list[list[str]] = []
        for row in node.find_all("tr"):
            cells = row.find_all(["th", "td"], recursive=False)
            if not cells:
                continue
            rendered = []
            for cell in cells:
                value = self.inline(cell, in_table=True).strip()
                value = re.sub(r"\s*\n\s*", "<br>", value)
                value = value.replace("|", "\\|")
                rendered.append(value)
            rows.append(rendered)
        if not rows:
            return ""
        width = max(map(len, rows))
        rows = [row + [""] * (width - len(row)) for row in rows]
        lines = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
        lines.extend("| " + " | ".join(row) + " |" for row in rows[1:])
        return "\n".join(lines)

    def list_block(self, node: Tag, depth: int = 0) -> str:
        lines: list[str] = []
        ordered = node.name == "ol"
        for index, item in enumerate(node.find_all("li", recursive=False), 1):
            nested = [child for child in item.children if isinstance(child, Tag) and child.name in {"ul", "ol"}]
            content = "".join(self.inline(child) for child in item.children if child not in nested).strip()
            prefix = f"{index}. " if ordered else "- "
            lines.append("  " * depth + prefix + content)
            for sublist in nested:
                lines.append(self.list_block(sublist, depth + 1))
        return "\n".join(lines)

    def blocks(self, node: Tag) -> list[str]:
        name = node.name.lower()
        if name in SKIP_TAGS:
            return []
        if re.fullmatch(r"h[1-6]", name):
            title = self.inline(node).strip()
            return [f"{'#' * int(name[1])} {title}"] if title else []
        if name == "pre":
            return [self.code_block(node)]
        if name == "table":
            table = self.table(node)
            return [table] if table else []
        if name in {"ul", "ol"}:
            listing = self.list_block(node)
            return [listing] if listing else []
        if name == "blockquote":
            content = "\n\n".join(self.container(node)).strip()
            return ["\n".join("> " + line for line in content.splitlines())] if content else []
        if name == "hr":
            return ["---"]
        if name == "p":
            content = self.inline(node).strip()
            return [content] if content else []
        return self.container(node)

    def container(self, node: Tag) -> list[str]:
        blocks: list[str] = []
        inline_parts: list[str] = []

        def flush() -> None:
            content = "".join(inline_parts).strip()
            if content:
                blocks.append(content)
            inline_parts.clear()

        for child in node.children:
            if isinstance(child, Tag) and child.name in BLOCK_TAGS:
                flush()
                blocks.extend(self.blocks(child))
            else:
                inline_parts.append(self.inline(child))
        flush()
        return blocks

    def render(self, content: Tag, title: str) -> str:
        blocks = self.container(content)
        if not blocks or not blocks[0].startswith("# "):
            blocks.insert(0, f"# {title}")
        return "\n\n".join(block for block in blocks if block.strip()).rstrip() + "\n"


def atomic_write(path: Path, body: bytes) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(body)
    temporary.replace(path)


def write_json(path: Path, value: Any) -> None:
    atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def previous_records(output: Path) -> dict[str, dict[str, Any]]:
    path = output / "manifest.json"
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("Existing manifest.json must be an array")
    records: dict[str, dict[str, Any]] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        doc_id = str(item.get("id", ""))
        if not re.fullmatch(r"[0-9]+", doc_id) or item.get("path") != f"{doc_id}.md":
            continue
        file_path = output / f"{doc_id}.md"
        if not file_path.is_file():
            continue
        body = file_path.read_bytes()
        if hashlib.sha256(body).hexdigest() == item.get("sha256") and len(body) == item.get("bytes"):
            records[doc_id] = item
    return records


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("docs"), help="Corpus directory (default: docs)")
    parser.add_argument("--limit", type=int, help="Collect only the first N navigation pages")
    parser.add_argument("--delay", type=float, default=0.75, help="Minimum seconds between requests (default: 0.75)")
    parser.add_argument("--timeout", type=float, default=25.0, help="Per-request timeout in seconds")
    parser.add_argument("--retries", type=int, default=3, help="Attempts per URL (default: 3)")
    parser.add_argument("--force", action="store_true", help="Refresh pages already in the manifest")
    parser.add_argument("--skip-images", action="store_true", help="Leave image URLs online; useful for quick checks")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 0:
        parser.error("--limit must be zero or greater")
    if args.delay < 0 or args.timeout <= 0 or args.retries < 1:
        parser.error("--delay must be nonnegative, --timeout positive, and --retries at least 1")
    return args


def main() -> int:
    args = parse_args()
    output: Path = args.output
    output.mkdir(parents=True, exist_ok=True)
    fetcher = Fetcher(args.delay, args.timeout, args.retries)
    try:
        landing, _, charset = fetcher.fetch(BASE_URL, "page")
        navigation = discover_navigation(BeautifulSoup(landing.decode(charset, errors="replace"), "html.parser"))
    except Exception as exc:
        print(f"Navigation fetch failed: {exc}", file=sys.stderr)
        return 1

    selected = navigation[:args.limit] if args.limit is not None else navigation
    records = previous_records(output)
    prior_failures_path = output / "failures.json"
    prior_failures: list[dict[str, str]] = []
    retry_image_ids: set[str] = set()
    if prior_failures_path.is_file():
        try:
            loaded = json.loads(prior_failures_path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                prior_failures = [item for item in loaded if isinstance(item, dict)]
            retry_image_ids = {str(item.get("id")) for item in prior_failures if item.get("kind") == "image"}
        except (ValueError, TypeError, AttributeError):
            pass
    selected_ids = {doc.id for doc in selected}
    failures: list[dict[str, str]] = [
        item for item in prior_failures if str(item.get("id")) not in selected_ids
    ]
    fetched: dict[str, tuple[Document, Tag]] = {}
    cached = 0
    saved = 0
    extra_links: dict[str, set[str]] = {}
    all_navigation_ids = {doc.id for doc in navigation}

    for index, doc in enumerate(selected, 1):
        if doc.id in records and not args.force and doc.id not in retry_image_ids:
            cached += 1
            continue
        try:
            body, _, charset = fetcher.fetch(doc.url, "page")
            soup = BeautifulSoup(body.decode(charset, errors="replace"), "html.parser")
            content = soup.select_one(".site-content")
            if content is None or len(content.get_text(" ", strip=True)) < 10:
                raise ValueError("Missing or empty .site-content")
            fetched[doc.id] = (doc, content)
            for link in content.select("a[href]"):
                target_id = document_id(link["href"])
                if target_id and target_id not in all_navigation_ids:
                    extra_links.setdefault(target_id, set()).add(doc.id)
            print(f"[{index}/{len(selected)}] fetched {doc.id} {doc.title}", flush=True)
        except Exception as exc:
            failures.append({"kind": "page", "id": doc.id, "url": doc.url, "error": str(exc), "at": utc_now()})
            print(f"[{index}/{len(selected)}] failed {doc.id}: {exc}", file=sys.stderr, flush=True)

    offline_ids = set(records) | set(fetched)
    assets = AssetManager(output, fetcher, failures, skip_images=args.skip_images)
    for doc_id, (doc, content) in fetched.items():
        try:
            h1 = content.find("h1")
            title = h1.get_text(" ", strip=True) if h1 else doc.title
            markdown = MarkdownRenderer(assets, doc.id, offline_ids).render(content, title)
            body = markdown.encode("utf-8")
            atomic_write(output / f"{doc.id}.md", body)
            records[doc.id] = {
                "id": doc.id, "title": title, "url": doc.url,
                "path": f"{doc.id}.md", "category": doc.category,
                "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body),
                "fetched_at": utc_now(),
            }
            saved += 1
        except Exception as exc:
            failures.append({"kind": "render", "id": doc.id, "url": doc.url, "error": str(exc), "at": utc_now()})
            print(f"Render failed {doc.id}: {exc}", file=sys.stderr, flush=True)

    # Keep previously collected documents during --limit runs, but discard stale
    # navigation entries when a complete run is requested.
    if args.limit is None:
        records = {doc_id: item for doc_id, item in records.items() if doc_id in all_navigation_ids}
    order = {doc.id: index for index, doc in enumerate(navigation)}
    manifest = sorted(records.values(), key=lambda item: (order.get(str(item["id"]), 10**9), str(item["id"])))
    write_json(output / "manifest.json", manifest)
    write_json(output / "failures.json", failures)
    print(json.dumps({
        "navigation": len(navigation), "selected": len(selected), "cached": cached,
        "downloaded": len(fetched), "saved": saved, "in_manifest": len(manifest),
        "failures": len(failures), "extra_document_links": {
            doc_id: sorted(sources) for doc_id, sources in sorted(extra_links.items())
        },
    }, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
