"""Protocol and search checks with a tiny local-only documentation snapshot."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from server import DocStore, call_tool


ROOT = Path(__file__).resolve().parents[1]


class ServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.docs_dir = Path(self.temp.name)
        entries = [
            {
                "id": "1187",
                "title": "简单的查询",
                "url": "https://www.donet5.com/Home/Doc?typeId=1187",
                "path": "1187.md",
                "category": "查询",
                "sha256": "",
                "bytes": 0,
            },
            {
                "id": "1191",
                "title": "更新数据",
                "url": "https://www.donet5.com/Home/Doc?typeId=1191",
                "path": "1191.md",
                "category": "写入",
                "sha256": "",
                "bytes": 0,
            },
        ]
        (self.docs_dir / "1187.md").write_text(
            "# 简单的查询\n\n## 查询所有\n\n```csharp\n"
            "db.Queryable<Student>().ToList();\n```\n",
            encoding="utf-8",
        )
        (self.docs_dir / "1191.md").write_text(
            "# 更新数据\n\n使用 Updateable 更新实体。\n", encoding="utf-8"
        )
        (self.docs_dir / "manifest.json").write_text(
            json.dumps(entries, ensure_ascii=False), encoding="utf-8"
        )
        assets_dir = self.docs_dir / "assets"
        assets_dir.mkdir()
        self.image_name = "a" * 24 + ".png"
        (assets_dir / self.image_name).write_bytes(
            base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")
        )
        self.store = DocStore(self.docs_dir)

    def test_chinese_and_api_search(self) -> None:
        for query in ("查询", "Queryable<Student>"):
            result = self.store.search(query, 5)
            self.assertEqual(result["results"][0]["id"], "1187")
            self.assertEqual(result["results"][0]["url"], self.store.items[0]["url"])
        self.assertEqual(self.store.search("更新", 5, "写入")["total"], 1)
        self.assertEqual(self.store.search("查询 Queryable", 5)["results"][0]["id"], "1187")

    def test_manifest_cannot_escape_docs_dir(self) -> None:
        manifest_path = self.docs_dir / "manifest.json"
        entries = json.loads(manifest_path.read_text(encoding="utf-8"))
        entries[0]["path"] = "../secrets.md"
        manifest_path.write_text(json.dumps(entries), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Invalid path"):
            DocStore(self.docs_dir)

    def test_lookup_and_pagination(self) -> None:
        first = self.store.get("https://www.donet5.com/home/doc?typeId=1187", 0, 12)
        self.assertEqual(first["id"], "1187")
        self.assertEqual(first["next_start"], 12)
        second = self.store.get("sqlsugar://doc/1187", first["next_start"], 400)
        self.assertIn("Queryable<Student>", second["content"])
        self.assertIsNone(second["next_start"])
        self.assertEqual(self.store.get("1187.md#查询所有", 0, 10)["id"], "1187")
        self.assertEqual(self.store.topics(None, 1, 1)["topics"][0]["id"], "1191")

    def test_missing_document_is_tool_error(self) -> None:
        result = call_tool(self.store, "get_doc", {"id": "../private"})
        self.assertTrue(result["isError"])
        self.assertIn("not found", result["content"][0]["text"])

    def test_offline_image_tool(self) -> None:
        result = call_tool(self.store, "get_image", {"path": f"assets/{self.image_name}"})
        self.assertEqual(result["content"][1]["mimeType"], "image/png")
        self.assertTrue(base64.b64decode(result["content"][1]["data"]).startswith(b"\x89PNG"))
        self.assertTrue(call_tool(self.store, "get_image", {"path": "../private.png"})["isError"])

    def test_stdio_protocol(self) -> None:
        requests = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "search_docs", "arguments": {"query": "查询"}},
            },
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "resources/read",
                "params": {"uri": "sqlsugar://doc/1187"},
            },
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "does_not_exist", "arguments": {}},
            },
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "resources/read",
                "params": {"uri": "sqlsugar://doc/9999"},
            },
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "resources/read",
                "params": {"uri": f"sqlsugar://asset/{self.image_name}"},
            },
        ]
        env = {**os.environ, "SQLSUGAR_DOCS_DIR": str(self.docs_dir)}
        process = subprocess.run(
            [sys.executable, str(ROOT / "server.py")],
            input="\n".join(json.dumps(request, ensure_ascii=False) for request in requests) + "\n",
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=10,
            env=env,
            check=True,
        )
        responses = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual([result["id"] for result in responses], [1, 2, 3, 4, 5, 6, 7])
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(
            {tool["name"] for tool in responses[1]["result"]["tools"]},
            {"list_topics", "search_docs", "get_doc", "get_image"},
        )
        self.assertEqual(responses[2]["result"]["structuredContent"]["results"][0]["id"], "1187")
        self.assertIn("Queryable<Student>", responses[3]["result"]["contents"][0]["text"])
        self.assertEqual(responses[4]["error"]["code"], -32602)
        self.assertEqual(responses[5]["error"]["code"], -32002)
        self.assertEqual(responses[6]["result"]["contents"][0]["mimeType"], "image/png")
        self.assertEqual(process.stderr, "")

    def test_older_protocol_uses_text_result_only(self) -> None:
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "search_docs", "arguments": {"query": "查询"}}},
        ]
        process = subprocess.run(
            [sys.executable, str(ROOT / "server.py"), "--docs-dir", str(self.docs_dir)],
            input="\n".join(json.dumps(request, ensure_ascii=False) for request in requests) + "\n",
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=10,
            check=True,
        )
        responses = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2024-11-05")
        self.assertNotIn("structuredContent", responses[1]["result"])
        self.assertIn("1187", responses[1]["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
