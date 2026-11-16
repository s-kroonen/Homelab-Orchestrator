"""Guests: what the orchestrator knows about each VM (and later each host) from inside it.

``model`` (the shapes), ``providers`` (who reads a guest), ``collector`` (reads them all on
time), ``store`` (keeps the latest reading), ``checks`` (turns readings into alerts),
``setup_guide`` (what is missing and how to fix it). See docs/guest_agent.md.
"""
