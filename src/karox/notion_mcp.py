"""First-party configuration for Notion's official hosted MCP server."""

from __future__ import annotations

from .mcp_client import McpRegistry, McpServerRecord

NOTION_MCP_SERVER_ID = "notion"
NOTION_MCP_NAMESPACE = "notion"
NOTION_MCP_URL = "https://mcp.notion.com/mcp"

# These tools do not change Notion state.  Everything else stays mutating so
# KaroX keeps its normal idempotency/risk handling for writes.
NOTION_READ_ONLY_TOOLS: tuple[str, ...] = (
    "notion-search",
    "notion-fetch",
    "notion-download-attachment",
    "notion-get-comments",
    "notion-get-async-task",
    "notion-get-teams",
    "notion-get-users",
    "notion-query-data-sources",
    "notion-query-database-view",
    "notion-query-meeting-notes",
    "notion-list-private-pages",
    "notion-list-shared-pages",
    "notion-list-favorite-pages",
    "notion-list-recent-pages",
    "notion-search-agents",
)


def notion_mcp_record() -> McpServerRecord:
    return McpServerRecord(
        server_id=NOTION_MCP_SERVER_ID,
        namespace=NOTION_MCP_NAMESPACE,
        transport="streamable_http",
        url=NOTION_MCP_URL,
        oauth=True,
        read_only_tools=NOTION_READ_ONLY_TOOLS,
        timeout_seconds=60.0,
        max_transport_retries=1,
    )


def ensure_notion_mcp_record(registry: McpRegistry) -> McpServerRecord:
    """Create/update the canonical secret-free Notion MCP registry record."""

    record = notion_mcp_record()
    registry.put(record)
    return record


__all__ = [
    "NOTION_MCP_NAMESPACE",
    "NOTION_MCP_SERVER_ID",
    "NOTION_MCP_URL",
    "NOTION_READ_ONLY_TOOLS",
    "ensure_notion_mcp_record",
    "notion_mcp_record",
]
