"""MCP server for droidrt.

Exposes the Android apps running in the droidrt VM to MCP clients over stdio:
generic UI tools backed by the on-device agent (see docs/agent-protocol.md),
per-app semantic tools from the `semantic` package and recorded flows.

Run it with `python3 -m droidrt_mcp`.
"""

__version__ = "0.1.0"
