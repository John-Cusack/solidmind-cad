"""MCP tool implementations for simulation engine lifecycle (sim.* tools)."""

from __future__ import annotations

from typing import Any

from server.sim_engine_manager import engine_status, start_engine, stop_engine


def sim_start_engine(
    *,
    backend: str,
    port: int | None = None,
    headless: bool = True,
    timeout_s: float = 30.0,
    runtime: str = "stub",
) -> dict[str, Any]:
    """Start a simulation backend (chrono, gazebo, or isaac).

    Returns engine_status field in addition to ok/status for state visibility.
    """
    return start_engine(
        backend,
        port=port,
        headless=headless,
        timeout_s=timeout_s,
        runtime=runtime,
    )


def sim_stop_engine(*, backend: str, drain_timeout_s: float = 5.0) -> dict[str, Any]:
    """Stop a simulation backend with graceful draining."""
    return stop_engine(backend, drain_timeout_s=drain_timeout_s)


def sim_engine_status() -> dict[str, Any]:
    """Check status of all simulation backends including health info."""
    return engine_status()
