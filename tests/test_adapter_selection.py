"""DRY_RUN=true forces every adapter to its dry-run implementation."""

from __future__ import annotations

from orchestrator.adapters.factory import build_adapters
from orchestrator.adapters.notifier.null import NullNotifier
from orchestrator.adapters.pbs.dry_run import DryRunPbsAdapter
from orchestrator.adapters.power.dry_run import DryRunPowerAdapter
from orchestrator.adapters.proxmox.dry_run import DryRunProxmoxAdapter


def test_dry_run_selects_all_dry_run_impls() -> None:
    bundle = build_adapters()
    assert isinstance(bundle.power, DryRunPowerAdapter)
    assert isinstance(bundle.proxmox, DryRunProxmoxAdapter)
    assert isinstance(bundle.pbs, DryRunPbsAdapter)
    assert isinstance(bundle.notifier, NullNotifier)
