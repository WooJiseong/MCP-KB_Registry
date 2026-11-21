"""프로세스 실행부터 MCP 초기화·도구 호출까지 실제 stdio 검증."""

import asyncio
import json
import os
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import yaml


def payload(result):
    assert not result.isError, result.content
    if result.structuredContent:
        return result.structuredContent
    return json.loads(result.content[0].text)


def test_actual_stdio_transport(tmp_path):
    root = tmp_path / "Documents"
    root.mkdir()
    repo = Path(__file__).resolve().parents[1]
    env = dict(os.environ, KB_REGISTRY_ROOT=str(root),
               KB_REGISTRY_DB=str(tmp_path / "registry.sqlite3"))

    async def exercise():
        params = StdioServerParameters(command="bash",
                                       args=[str(repo / "scripts/run.sh")], env=env, cwd=str(tmp_path))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                initialized = await client.initialize()
                assert initialized.serverInfo.name
                tools = {t.name: t for t in (await client.list_tools()).tools}
                names = set(tools)
                assert {"allocate_document", "confirm_document", "get_document", "assign_category",
                        "remove_category", "link_youtrack", "list_categories", "validate_registry",
                        "reconcile_document", "update_document_path", "rename_category", "delete_category"} <= names
                assert "category" in tools["assign_category"].inputSchema["required"]
                assert "path" in tools["allocate_document"].inputSchema["required"]
                document = payload(await client.call_tool("allocate_document", {
                    "path": "stdio.qmd", "category": "RL", "idempotency_key": "stdio-request"
                }))
                kb_id = document["kb_id"]
                assert document["status"] == "pending"
                failed = await client.call_tool("confirm_document", {"kb_id": kb_id})
                assert failed.isError
                assert any("문서" in block.text or "파일" in block.text
                           for block in failed.content if hasattr(block, "text"))
                (root / "stdio.qmd").write_text("---\n" + yaml.safe_dump(
                    document["expected_frontmatter"], allow_unicode=True) + "---\n# stdio\n")
                committed = payload(await client.call_tool("confirm_document", {"kb_id": kb_id}))
                assert committed["status"] == "committed"
                assert payload(await client.call_tool("get_document", {"kb_id": kb_id}))["kb_id"] == kb_id
                assert payload(await client.call_tool("validate_registry", {}))["ok"]
                invalid = await client.call_tool("allocate_document", {"path": "../outside.md"})
                assert invalid.isError
                invalid_type = await client.call_tool("allocate_document", {"path": 123})
                assert invalid_type.isError
                assert any("입력" in block.text or "문자열" in block.text
                           for block in invalid_type.content if hasattr(block, "text"))
                omitted = await client.call_tool("assign_category", {"kb_id": kb_id})
                assert omitted.isError
                assert payload(await client.call_tool("get_document", {"kb_id": kb_id}))["category"] == "RL"
                removed = payload(await client.call_tool("assign_category", {"kb_id": kb_id, "category": None}))
                assert removed["expected_frontmatter"]["category"] is None
                assigned = payload(await client.call_tool("assign_category", {"kb_id": kb_id, "category": "RL"}))
                assert assigned["expected_frontmatter"]["category_number"] == "RL-002"
                payload(await client.call_tool("rename_category", {"code": "RL", "new_code": "MARL"}))
                renamed = payload(await client.call_tool("get_document", {"kb_id": kb_id}))
                assert renamed["expected_frontmatter"]["category_number"] == "MARL-002"
                (root / "stdio.qmd").write_text("---\n" + yaml.safe_dump(renamed["expected_frontmatter"]) + "---\n")
                (root / "stdio.qmd").rename(root / "moved.md")
                moved = payload(await client.call_tool("update_document_path", {"kb_id": kb_id, "path": "moved.md"}))
                assert moved["status"] == "pending"
                payload(await client.call_tool("confirm_document", {"kb_id": kb_id}))
                payload(await client.call_tool("delete_category", {"code": "MARL"}))
                deleted = payload(await client.call_tool("get_document", {"kb_id": kb_id}))
                assert deleted["expected_frontmatter"]["category"] is None
                assert deleted["status"] == "pending"

    asyncio.run(asyncio.wait_for(exercise(), timeout=60))
