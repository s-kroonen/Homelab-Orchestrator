"""Dumps: backing up one database or application on its own, without the whole guest.

A guest backup (Proxmox -> PBS) is the unit for a root service. A sub-service that owns data -
a database in a Portainer stack - can also be dumped by itself: the orchestrator runs the
database's own dump tool *inside its container* (through Portainer's exec), streams the output
into a compressed file on the orchestrator host, verifies it and keeps the newest few.
See docs/dumps.md.
"""
