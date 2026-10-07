#!/usr/bin/env python3
"""Dependency-free, offline SqlSugar documentation MCP server (stdio)."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import unicodedata
from pathlib import Path
from urllib.parse import parse_qs, urlparse


SERVER_INFO = {"name": "sqlsugar-docs", "version": "1.0.0"}
PROTOCOL_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}
DEFAULT_VERSION = "2025-11-25"
MAX_QUERY_LENGTH = 200
MAX_PAGE_SIZE = 100
MAX_DOC_CHARS = 40_000
TOKEN_RE = re.compile(r"[a-z0-9_.#+-]+|[\u3400-\u9fff]+", re.I)
SOURCE_HOST = "www.donet5.com"
IMAGE_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
    ".avif": "image/avif",
}


def normalized(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def query_terms(query: str) -> list[str]:
    terms: list[str] = []
    for token in TOKEN_RE.findall(normalized(query)):
        if "\u3400" <= token[0] <= "\u9fff" and len(token) > 2:
            terms.extend(token[index : index + 2] for index in range(len(token) - 1))
        else:
            terms.append(token)
    return list(dict.fromkeys(terms))


def _integer(value: object, default: int, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"Expected integer between {minimum} and {maximum}")
    return value


class DocStore:
    def __init__(self, docs_dir: Path):
        self.docs_dir = docs_dir.resolve()
        manifest_file = self.docs_dir / "manifest.json"
        if not manifest_file.is_file():
            raise FileNotFoundError(f"Missing offline documentation: {manifest_file}")
        raw = json.loads(manifest_file.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("manifest.json must contain a JSON array")
        self.items: list[dict] = []
        self.by_id: dict[str, dict] = {}
        self.by_url: dict[str, dict] = {}
        self._contents: dict[str, str] = {}
        self._search_data: dict[str, tuple[str, str, str]] = {}
        self._order: dict[str, int] = {}
        asset_dir = self.docs_dir / "assets"
        if asset_dir.exists() and not asset_dir.resolve().is_relative_to(self.docs_dir):
            raise ValueError("Image directory is outside the documentation directory")
        self.assets = {
            path.name: path
            for path in asset_dir.glob("*")
            if path.is_file()
            and re.fullmatch(r"[0-9a-f]{24}\.[a-z0-9]+", path.name)
            and path.suffix.lower() in IMAGE_MIME_TYPES
            and path.resolve().is_relative_to(asset_dir.resolve())
        }
        for entry in raw:
            if not isinstance(entry, dict):
                raise ValueError("Invalid manifest entry")
            doc_id = str(entry.get("id", ""))
            if not re.fullmatch(r"\d+|home", doc_id) or doc_id in self.by_id:
                raise ValueError(f"Invalid or duplicate document id: {doc_id}")
            relative_path = entry.get("path")
            if not isinstance(relative_path, str) or relative_path != f"{doc_id}.md":
                raise ValueError(f"Invalid path for document {doc_id}")
            path = (self.docs_dir / relative_path).resolve()
            if not path.is_relative_to(self.docs_dir) or not path.is_file():
                raise ValueError(f"Missing or out-of-tree document {doc_id}: {relative_path}")
            item = {
                "id": doc_id,
                "title": str(entry.get("title", "")),
                "url": str(entry.get("url", "")),
                "path": relative_path,
                "category": str(entry.get("category", "")),
                "sha256": str(entry.get("sha256", "")),
                "bytes": entry.get("bytes", path.stat().st_size),
                "fetched_at": str(entry.get("fetched_at", "")),
            }
            content = path.read_text(encoding="utf-8")
            title = normalized(item["title"])
            headings = normalized(" ".join(re.findall(r"^#{1,6}\s+(.+)$", content, re.M)))
            body = normalized(content)
            self.items.append(item)
            self._order[doc_id] = len(self.items) - 1
            self.by_id[doc_id] = item
            self.by_url[item["url"].lower()] = item
            self._contents[doc_id] = content
            self._search_data[doc_id] = (title, headings, body)

    def resolve(self, identifier: str) -> dict:
        if identifier in self.by_id:
            return self.by_id[identifier]
        local_link = re.fullmatch(r"(\d+)\.md(?:#.*)?", identifier)
        if local_link and local_link.group(1) in self.by_id:
            return self.by_id[local_link.group(1)]
        if identifier.startswith("sqlsugar://doc/"):
            doc_id = identifier.removeprefix("sqlsugar://doc/")
            if doc_id in self.by_id:
                return self.by_id[doc_id]
        parsed = urlparse(identifier)
        if parsed.scheme in {"https", "http"} and parsed.hostname in {
            SOURCE_HOST,
            "donet5.com",
        }:
            query = {key.casefold(): value for key, value in parse_qs(parsed.query).items()}
            query_id = query.get("typeid", [None])[0]
            if query_id in self.by_id:
                return self.by_id[query_id]
            doc_route = re.fullmatch(r"/doc/1/(\d+)", parsed.path, re.I)
            if doc_route and doc_route.group(1) in self.by_id:
                return self.by_id[doc_route.group(1)]
            if parsed.path.lower() == "/home/doc" and not query_id and "home" in self.by_id:
                return self.by_id["home"]
        raise KeyError(f"Document not found: {identifier}")

    def topics(self, category: str | None, limit: int, offset: int) -> dict:
        matched = [
            item for item in self.items if not category or item["category"] == category
        ]
        categories = sorted({item["category"] for item in self.items if item["category"]})
        return {
            "total": len(matched),
            "offset": offset,
            "limit": limit,
            "categories": categories,
            "topics": [self._summary(item) for item in matched[offset : offset + limit]],
        }

    @staticmethod
    def _summary(item: dict) -> dict:
        return {
            key: item[key]
            for key in ("id", "title", "category", "url", "fetched_at")
        }

    def search(self, query: str, limit: int, category: str | None = None) -> dict:
        query = query.strip()
        if not query or len(query) > MAX_QUERY_LENGTH:
            raise ValueError(f"query must contain 1 to {MAX_QUERY_LENGTH} characters")
        phrase = normalized(query)
        terms = query_terms(query)
        if not terms:
            raise ValueError("query must contain searchable characters")
        ranked = []
        for item in self.items:
            if category and item["category"] != category:
                continue
            title, headings, body = self._search_data[item["id"]]
            compact_title = re.sub(r"\s+", "", title)
            compact_headings = re.sub(r"\s+", "", headings)
            title_hits = sum(term in title or term in compact_title for term in terms)
            heading_hits = sum(term in headings or term in compact_headings for term in terms)
            body_hits = sum(term in body for term in terms)
            covered = sum(
                term in compact_title or term in compact_headings or term in body
                for term in terms
            )
            if not covered:
                continue
            # Covering every query term is more useful than one generic title
            # hit, particularly for multi-word API + scenario searches.
            score = round(100 * covered / len(terms))
            score += title_hits * 35 + heading_hits * 8 + body_hits * 2
            if phrase in title or re.sub(r"\s+", "", phrase) in compact_title:
                score += 50
            elif phrase in headings or re.sub(r"\s+", "", phrase) in compact_headings:
                score += 35
            elif phrase in body:
                score += 20
            positions = [position for term in terms if (position := body.find(term)) >= 0]
            position = body.find(phrase)
            if position < 0:
                position = min(positions) if positions else 0
            source = self._contents[item["id"]]
            # NFKC can change character lengths. Use the normalized text for an
            # approximate position, then retain the original Markdown in output.
            start = max(0, position - 100)
            excerpt = source[start : start + 360].replace("\n", " ").strip()
            ranked.append((score, item, excerpt))
        ranked.sort(key=lambda result: (-result[0], self._order[result[1]["id"]]))
        return {
            "query": query,
            "total": len(ranked),
            "results": [
                {**self._summary(item), "score": score, "excerpt": excerpt}
                for score, item, excerpt in ranked[:limit]
            ],
        }

    def get(self, identifier: str, start: int, max_chars: int) -> dict:
        item = self.resolve(identifier)
        content = self._contents[item["id"]]
        if start > len(content):
            raise ValueError(f"start exceeds document length ({len(content)})")
        end = min(len(content), start + max_chars)
        return {
            **self._summary(item),
            "content": content[start:end],
            "start": start,
            "next_start": end if end < len(content) else None,
            "total_chars": len(content),
        }

    def get_image(self, identifier: str) -> tuple[str, str, bytes]:
        name = identifier.removeprefix("sqlsugar://asset/").removeprefix("assets/")
        path = self.assets.get(name)
        if path is None:
            raise KeyError(f"Image not found: {identifier}")
        return name, IMAGE_MIME_TYPES[path.suffix.lower()], path.read_bytes()


TOOLS = [
    {
        "name": "list_topics",
        "description": "List offline SqlSugar documentation topics and categories. Use pagination for the complete catalog.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "Optional exact category name from the catalog."},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_PAGE_SIZE, "default": 50},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
            },
        },
    },
    {
        "name": "search_docs",
        "description": "Search the local SqlSugar docs in Chinese or English, including C# API names. Returns excerpts, IDs, and original source URLs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keywords, phrase, or API name."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 30, "default": 8},
                "category": {"type": "string", "description": "Optional exact category name."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_doc",
        "description": "Read a SqlSugar document by ID, relative .md link, official URL, or sqlsugar://doc/{id}. Use next_start to continue long documents.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Document ID, relative .md link, source URL, or resource URI."},
                "start": {"type": "integer", "minimum": 0, "default": 0},
                "max_chars": {"type": "integer", "minimum": 1, "maximum": MAX_DOC_CHARS, "default": 16000},
            },
            "required": ["id"],
        },
    },
    {
        "name": "get_image",
        "description": "Read an offline image referenced by a document as MCP image content. Accepts assets/<filename> or sqlsugar://asset/<filename>.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Local image reference from the Markdown document."},
            },
            "required": ["path"],
        },
    },
]


def tool_result(value: dict, *, is_error: bool = False) -> dict:
    result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}
    if is_error:
        result["isError"] = True
    else:
        result["structuredContent"] = value
    return result


def call_tool(store: DocStore, name: str, arguments: object) -> dict:
    if not isinstance(arguments, dict):
        return tool_result({"error": "arguments must be an object"}, is_error=True)
    try:
        if name == "list_topics":
            category = arguments.get("category")
            if category is not None and not isinstance(category, str):
                raise ValueError("category must be a string")
            value = store.topics(
                category,
                _integer(arguments.get("limit"), 50, 1, MAX_PAGE_SIZE),
                _integer(arguments.get("offset"), 0, 0, 1_000_000),
            )
        elif name == "search_docs":
            query = arguments.get("query")
            category = arguments.get("category")
            if not isinstance(query, str):
                raise ValueError("query must be a string")
            if category is not None and not isinstance(category, str):
                raise ValueError("category must be a string")
            value = store.search(query, _integer(arguments.get("limit"), 8, 1, 30), category)
        elif name == "get_doc":
            identifier = arguments.get("id")
            if not isinstance(identifier, str):
                raise ValueError("id must be a string")
            value = store.get(
                identifier,
                _integer(arguments.get("start"), 0, 0, 10_000_000),
                _integer(arguments.get("max_chars"), 16000, 1, MAX_DOC_CHARS),
            )
        elif name == "get_image":
            identifier = arguments.get("path")
            if not isinstance(identifier, str):
                raise ValueError("path must be a string")
            image_name, mime_type, body = store.get_image(identifier)
            return {
                "content": [
                    {"type": "text", "text": f"Offline SqlSugar image: {image_name}"},
                    {"type": "image", "mimeType": mime_type, "data": base64.b64encode(body).decode("ascii")},
                ]
            }
        else:
            return tool_result({"error": f"Unknown tool: {name}"}, is_error=True)
        return tool_result(value)
    except (ValueError, KeyError) as exc:
        return tool_result({"error": str(exc)}, is_error=True)


def dispatch(store: DocStore, request: dict) -> dict:
    method = request.get("method")
    params = request.get("params", {})
    if not isinstance(params, dict):
        raise ProtocolError(-32602, "params must be an object")
    if method == "initialize":
        requested = params.get("protocolVersion")
        if requested is not None and not isinstance(requested, str):
            raise ProtocolError(-32602, "protocolVersion must be a string")
        version = requested if requested in PROTOCOL_VERSIONS else DEFAULT_VERSION
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {}, "resources": {}},
            "serverInfo": SERVER_INFO,
            "instructions": "Offline snapshot of the official SqlSugar help site. Search first, then read the cited document. Relative links such as 1188.md can be passed to get_doc; image references such as assets/abc.png can be passed to get_image. Each result includes its original URL; content may be older than the live site.",
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str):
            raise ProtocolError(-32602, "Tool name must be a string")
        if name not in {tool["name"] for tool in TOOLS}:
            raise ProtocolError(-32602, f"Unknown tool: {name}")
        return call_tool(store, name, params.get("arguments", {}))
    if method == "resources/list":
        return {
            "resources": [
                {
                    "uri": f"sqlsugar://doc/{item['id']}",
                    "name": item["title"],
                    "description": item["category"],
                    "mimeType": "text/markdown",
                }
                for item in store.items
            ]
            + [
                {
                    "uri": f"sqlsugar://asset/{name}",
                    "name": name,
                    "mimeType": IMAGE_MIME_TYPES[path.suffix.lower()],
                }
                for name, path in sorted(store.assets.items())
            ],
        }
    if method == "resources/templates/list":
        return {
            "resourceTemplates": [
                {
                    "uriTemplate": "sqlsugar://doc/{id}",
                    "name": "SqlSugar documentation by ID",
                    "mimeType": "text/markdown",
                },
                {
                    "uriTemplate": "sqlsugar://asset/{filename}",
                    "name": "SqlSugar documentation image",
                },
            ]
        }
    if method == "resources/read":
        uri = params.get("uri")
        if not isinstance(uri, str) or not uri.startswith(("sqlsugar://doc/", "sqlsugar://asset/")):
            raise ProtocolError(-32602, "Invalid SqlSugar resource URI")
        if uri.startswith("sqlsugar://asset/"):
            try:
                _, mime_type, body = store.get_image(uri)
            except KeyError as exc:
                raise ProtocolError(-32002, str(exc)) from exc
            return {
                "contents": [
                    {
                        "uri": uri,
                        "mimeType": mime_type,
                        "blob": base64.b64encode(body).decode("ascii"),
                    }
                ]
            }
        try:
            item = store.resolve(uri)
        except KeyError as exc:
            raise ProtocolError(-32002, str(exc)) from exc
        return {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": "text/markdown",
                    "text": store._contents[item["id"]],
                }
            ]
        }
    raise ProtocolError(-32601, f"Method not found: {method}")


class ProtocolError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def serve(store: DocStore) -> None:
    negotiated_version: str | None = None
    for line in sys.stdin.buffer:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
            elif "id" not in request:
                # MCP notifications never receive a response.
                continue
            else:
                response = {"jsonrpc": "2.0", "id": request["id"]}
                try:
                    response["result"] = dispatch(store, request)
                    if request.get("method") == "initialize":
                        negotiated_version = response["result"]["protocolVersion"]
                    elif request.get("method") == "tools/call" and negotiated_version in {
                        "2024-11-05",
                        "2025-03-26",
                    }:
                        # structuredContent entered the protocol in 2025-06-18.
                        response["result"].pop("structuredContent", None)
                except ProtocolError as exc:
                    response["error"] = {"code": exc.code, "message": str(exc)}
                except Exception as exc:  # Keep the protocol stream alive for later requests.
                    print(f"Unexpected server error: {exc}", file=sys.stderr)
                    response["error"] = {"code": -32603, "message": "Internal error"}
        sys.stdout.buffer.write((json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
        sys.stdout.buffer.flush()


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline SqlSugar documentation MCP server")
    parser.add_argument("--docs-dir", type=Path, default=Path(os.environ.get("SQLSUGAR_DOCS_DIR", Path(__file__).parent / "docs")))
    args = parser.parse_args()
    try:
        store = DocStore(args.docs_dir)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Cannot load offline SqlSugar docs: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    serve(store)


if __name__ == "__main__":
    main()
