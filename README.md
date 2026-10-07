# SqlSugar 离线文档 MCP

本项目把 [SqlSugar 官方帮助站](https://www.donet5.com/Home/Doc)的 v5 文档保存到 `docs/`，通过本地 stdio MCP 提供给 Agent。MCP 运行时只读本地 Markdown 与 `manifest.json`，不访问网络，也不连接数据库。

## 启动

需要 Python 3.10 或更新版本。文档快照已随项目保存在 `docs/`，启动 MCP **不需要安装第三方包**：

```powershell
python -u D:\dev\sqlsugarMcp\server.py
```

在支持 MCP stdio 的 Agent 客户端中添加服务器。下例是通用配置格式，具体配置文件位置由客户端决定：

```json
{
  "mcpServers": {
    "sqlsugar-docs": {
      "command": "python",
      "args": ["-u", "D:\\dev\\sqlsugarMcp\\server.py"]
    }
  }
}
```

如果移动了本项目，请把 `server.py` 改成新的绝对路径。文档放在其他目录时，可使用 `--docs-dir <目录>` 或 `SQLSUGAR_DOCS_DIR` 环境变量。

服务器实现 MCP 的 `initialize` 握手协议（2024-11-05 至 2025-11-25）。使用 2026-07-28 协议的客户端需允许回退到旧握手模式；只接受新协议的客户端无法连接。

## Agent 可用接口

| 工具 | 用途 |
| --- | --- |
| `list_topics(category?, limit?, offset?)` | 按官方目录列出文档及栏目，可分页。 |
| `search_docs(query, limit?, category?)` | 搜索中文说明与 C# API，返回相关片段、原站链接和文档 ID。 |
| `get_doc(id, start?, max_chars?)` | 按 ID、原站 URL 或 `sqlsugar://doc/<id>` 读取 Markdown；长文档通过 `next_start` 继续读取。 |
| `get_image(path)` | 读取文档中 `assets/<文件名>` 引用的本地图片。 |

还提供 `resources/list`、`resources/templates/list` 和 `resources/read`。文档资源 URI 为 `sqlsugar://doc/<id>`，图片资源 URI 为 `sqlsugar://asset/<文件名>`。`get_doc` 也接受正文中的 `<id>.md` 相对链接。

建议 Agent 先搜索，再读取命中的原文；回答时标明原站链接，留意页面中的版本要求和数据库差异。结果中的 `fetched_at` 是快照抓取时间，原站后续更新不会自动同步。

## 更新离线快照

更新操作才会联网。先安装采集依赖，然后重新抓取：

```powershell
python -m pip install -r requirements-collector.txt
python scripts/collect_docs.py --output docs --delay 0.6 --force
```

采集器从官方 v5 目录发现文档，保存正文、代码、表格和来源信息，并记录抓取失败项。`docs/manifest.json` 是 MCP 唯一的文档索引；每篇 Markdown 以文档 ID 命名。外部站点的视频和附件仅保留链接，不纳入离线内容。

## 验证

```powershell
python -m unittest discover -s tests -v
```

文档内容归原站作者所有。本项目保留每篇文档的官方 URL；需要核对最新用法时，以原站为准。
