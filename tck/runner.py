"""Run the TCK against an engine and report.

``python3 -m tck --host 127.0.0.1 --port 9880``
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tck.client import TckClient, TckConnectionError
from tck.report import TckReport
from tck.tiers import (
    tier_latency,
    tier_package,
    tier_physics,
    tier_protocol,
    tier_results,
    tier_sessions,
)

DEFAULT_TIERS = ("protocol", "package", "results", "sessions", "physics", "latency")


def run_tck(
    host: str = "127.0.0.1",
    port: int = 9880,
    *,
    tiers: tuple[str, ...] = DEFAULT_TIERS,
    package_dir: Path | None = None,
    schema_dir: Path | None = None,
    timeout: float = 30.0,
    latency_samples: int = 200,
) -> TckReport:
    """Run the selected tiers against one engine and return the report.

    Never raises for a non-conformant engine — an unreachable one is the only
    hard error, because then there is nothing to report on.
    """
    report = TckReport(address=f"{host}:{port}")
    client = TckClient(host, port, timeout=timeout)
    client.connect()
    try:
        capabilities: dict = {}
        runtime_mode = "real"
        if "protocol" in tiers:
            _tier, hello = tier_protocol(client, report)
            capabilities = hello.get("capabilities") or {}
            runtime_mode = str(hello.get("runtime_mode", "real"))
        else:
            try:
                hello = client.result("hello")
                capabilities = hello.get("capabilities") or {}
                runtime_mode = str(hello.get("runtime_mode", "real"))
            except TckConnectionError:
                capabilities = {}

        if "package" in tiers:
            tier_package(client, report, capabilities, package_dir)
        if "results" in tiers:
            tier_results(client, report, schema_dir)
        if "sessions" in tiers:
            tier_sessions(client, report, capabilities)
        if "physics" in tiers:
            tier_physics(client, report, runtime_mode, capabilities)
        if "latency" in tiers:
            tier_latency(client, report, samples=latency_samples)
    finally:
        client.close()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tck",
        description=(
            "Engine Integration Contract Test Kit — run against any engine, "
            "in any language. See docs/engine-contract.md §3.6."
        ),
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9880)
    parser.add_argument(
        "--tier",
        action="append",
        choices=DEFAULT_TIERS,
        help="Run only these tiers (repeatable). Default: all.",
    )
    parser.add_argument(
        "--package-dir",
        type=Path,
        default=None,
        help="Sim package for the package tier (default: the bundled golden package)",
    )
    parser.add_argument(
        "--schema-dir",
        type=Path,
        default=None,
        help="Directory holding sim_result.schema.json (default: the repo's schemas/)",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--latency-samples",
        type=int,
        default=200,
        help="Pings for the latency tier (default 200)",
    )
    parser.add_argument("--json", action="store_true", help="Emit the report as JSON")
    args = parser.parse_args(argv)

    try:
        report = run_tck(
            args.host,
            args.port,
            tiers=tuple(args.tier) if args.tier else DEFAULT_TIERS,
            package_dir=args.package_dir,
            schema_dir=args.schema_dir,
            timeout=args.timeout,
            latency_samples=args.latency_samples,
        )
    except TckConnectionError as exc:
        print(f"Cannot reach the engine: {exc}", file=sys.stderr)
        print(
            "Start one first, e.g. python3 -m reference_engine.bridge_server --port 9880",
            file=sys.stderr,
        )
        return 2

    print(report.to_json() if args.json else report.render())
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
