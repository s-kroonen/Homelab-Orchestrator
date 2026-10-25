"""The MCP server: an AI assistant's eye on the server space, and a short, guarded set of hands.

* ``scopes``  what a token may do, and the role presets
* ``store``   OAuth clients, authorisation codes, tokens, the call history
* ``oauth``   the authorisation server (metadata, registration, authorise, token, revoke)
* ``tools``   the tools an assistant can call, each with its scope and risk
* ``server``  the JSON-RPC layer behind ``POST /mcp``
* ``approvals`` the human step for dangerous calls

See docs/mcp.md.
"""
