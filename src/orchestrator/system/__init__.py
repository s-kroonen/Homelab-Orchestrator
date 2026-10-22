"""The orchestrator managing itself: backups, restore, schema, update, restart.

See docs/system.md. Everything here is plain functions over ``Settings`` so the
web page, the API and ``orchestrator-cli system`` share one implementation.
"""
