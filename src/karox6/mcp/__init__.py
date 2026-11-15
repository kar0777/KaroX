"""MCP server: drive KaroX runs from any MCP client (ChatGPT web, IDEs, the inspector).

Same core as the CLI: every run is a ``Session`` and its events. ``runs`` owns the
runs, ``protocol`` answers JSON-RPC, ``transport`` speaks stdio and Streamable HTTP.
No third-party MCP SDK: the protocol surface we need is small, and the core
dependency list stays at httpx.
"""
