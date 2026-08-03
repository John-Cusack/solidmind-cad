"""The six conformance tiers (``docs/engine-contract.md`` §3.6).

Each tier takes a connected client and a report, and records checks.  Tiers
never raise on a non-conformant engine — a failure is data, not an exception,
because the point is a report the engine author can act on.
"""

from __future__ import annotations

import json
import math
import shutil
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any

from tck.client import TckClient, TckConnectionError
from tck.report import TckReport, TierResult

_FIXTURES = Path(__file__).resolve().parent / "fixtures"

#: Verbs every engine must implement, forever (contract §3.1).
REQUIRED_VERBS = ("hello", "ping", "simulate", "shutdown")

#: Verbs implied by each advertised capability (contract §3.2).
MODE_VERBS = {
    "session": ("simulate_start", "simulate_status", "simulate_stop"),
    "teleop": ("teleop_start", "teleop_command", "teleop_state", "teleop_stop"),
}
FEATURE_VERBS = {
    "diagnose": ("diagnose",),
    "screenshot": ("screenshot",),
    "spawn_model": ("spawn_model",),
    "import_urdf": ("import_urdf",),
    "load_environment": ("load_environment",),
    "reload": ("reload",),
}

_QUANTITY_KEYS = ("modes", "formats", "features")


# ---------------------------------------------------------------------------
# Tier 1 — protocol
# ---------------------------------------------------------------------------


def tier_protocol(client: TckClient, report: TckReport) -> tuple[TierResult, dict[str, Any]]:
    """Handshake, framing, error taxonomy, capability honesty."""
    tier = report.tier("1. protocol")
    hello: dict[str, Any] = {}

    try:
        response = client.request("hello")
    except TckConnectionError as exc:
        tier.fail("hello answers", str(exc))
        return tier, hello

    tier.expect("hello answers", bool(response.get("ok")), json.dumps(response)[:200])
    hello = response.get("result") if isinstance(response.get("result"), dict) else {}

    report.engine = str(hello.get("engine", "unknown"))
    report.engine_version = str(hello.get("engine_version", ""))

    tier.expect(
        "hello reports a protocol_version",
        isinstance(hello.get("protocol_version"), str) and bool(hello.get("protocol_version")),
    )
    supported = hello.get("contract_versions_supported")
    tier.expect(
        "hello lists contract_versions_supported including '1'",
        isinstance(supported, list) and "1" in [str(v) for v in supported],
        f"got {supported!r}",
    )
    tier.expect(
        "hello names the engine",
        isinstance(hello.get("engine"), str) and bool(hello.get("engine")),
    )
    tier.expect(
        "hello declares a runtime_mode of real or stub",
        hello.get("runtime_mode") in ("real", "stub"),
        f"got {hello.get('runtime_mode')!r}",
    )

    capabilities = hello.get("capabilities")
    if not isinstance(capabilities, dict):
        tier.fail("hello carries a capabilities object", f"got {type(capabilities).__name__}")
        capabilities = {}
    else:
        tier.ok("hello carries a capabilities object")
        for key in _QUANTITY_KEYS:
            value = capabilities.get(key)
            tier.expect(
                f"capabilities.{key} is a list of strings",
                isinstance(value, list) and all(isinstance(v, str) for v in value),
                f"got {value!r}",
            )
        fields = capabilities.get("fields")
        tier.expect(
            "capabilities.fields declares emits and accepts",
            isinstance(fields, dict)
            and isinstance(fields.get("emits"), list)
            and isinstance(fields.get("accepts"), list),
            f"got {fields!r}",
        )

    # request_id: echoed verbatim when sent, absent when not.
    echoed = client.request("ping", request_id="tck-request-1")
    tier.expect(
        "request_id is echoed verbatim",
        echoed.get("request_id") == "tck-request-1",
        f"got {echoed.get('request_id')!r}",
    )
    plain = client.request("ping")
    tier.expect(
        "request_id is absent when not sent",
        "request_id" not in plain,
        f"got {plain.get('request_id')!r}",
    )

    # Required floor.
    for verb in REQUIRED_VERBS:
        if verb == "shutdown":
            continue  # exercised last, by the caller — it ends the engine
        response = client.request(verb, _minimal_args(verb))
        tier.expect(
            f"required verb '{verb}' answers",
            "ok" in response,
            f"no ok field in {json.dumps(response)[:160]}",
        )

    # Error taxonomy.
    unknown = client.request("tck_definitely_not_a_verb")
    code = _error_code(unknown)
    tier.expect(
        "unknown verb returns UNSUPPORTED_COMMAND",
        code == "UNSUPPORTED_COMMAND",
        f"got {code!r}",
    )

    malformed = client.send_raw("{not json}\n")
    tier.expect(
        "malformed JSON returns INVALID_JSON",
        _error_code(malformed) == "INVALID_JSON",
        f"got {_error_code(malformed)!r}",
    )

    no_cmd = client.send_raw(json.dumps({"args": {}}) + "\n")
    tier.expect(
        "missing 'cmd' returns INVALID_REQUEST",
        _error_code(no_cmd) == "INVALID_REQUEST",
        f"got {_error_code(no_cmd)!r}",
    )

    bad_args = client.send_raw(json.dumps({"cmd": "ping", "args": "not-an-object"}) + "\n")
    tier.expect(
        "non-object 'args' returns INVALID_REQUEST",
        _error_code(bad_args) == "INVALID_REQUEST",
        f"got {_error_code(bad_args)!r}",
    )

    # Capability honesty, both directions.
    advertised = _advertised_verbs(capabilities)
    for verb in sorted(advertised):
        response = client.request(verb, _minimal_args(verb))
        # A verb may legitimately reject the TCK's placeholder arguments; what
        # it must not do is deny knowing the verb.
        tier.expect(
            f"advertised verb '{verb}' is implemented",
            _error_code(response) != "UNSUPPORTED_COMMAND",
            "advertised in hello but answered UNSUPPORTED_COMMAND",
        )

    for verb, capability in _unadvertised_verbs(capabilities):
        response = client.request(verb, _minimal_args(verb))
        tier.expect(
            f"unadvertised verb '{verb}' is refused",
            _error_code(response) == "UNSUPPORTED_COMMAND",
            f"{capability!r} not advertised, yet '{verb}' answered {_error_code(response)!r}",
        )

    return tier, hello


# ---------------------------------------------------------------------------
# Tier 2 — package
# ---------------------------------------------------------------------------


def tier_package(
    client: TckClient,
    report: TckReport,
    capabilities: dict[str, Any],
    package_dir: Path | None = None,
) -> TierResult:
    """Ingest the golden sim package."""
    tier = report.tier("2. package")
    formats = capabilities.get("formats") or []
    if "package" not in formats:
        tier.skip("ingests a sim package", f"'package' not advertised (formats={formats})")
        return tier

    source = package_dir or (_FIXTURES / "golden_package")
    if not (source / "manifest.json").is_file():
        tier.fail("golden package is present", f"no manifest.json under {source}")
        return tier

    # Engines compile their own artifacts next to the manifest, so hand them a
    # throwaway copy rather than letting them write into the kit.
    with tempfile.TemporaryDirectory(prefix="tck_package_") as tmp:
        package = Path(tmp) / source.name
        shutil.copytree(source, package)
        response = client.request(
            "simulate",
            {
                "package_path": str(package),
                "duration_s": 0.1,
                "dt_s": 0.01,
                "output_interval": 0.05,
            },
        )
        tier.expect(
            "ingests the golden package",
            bool(response.get("ok")),
            json.dumps(response.get("error", {}))[:200],
        )

    missing = client.request(
        "simulate", {"package_path": "/tck/definitely/not/a/package", "duration_s": 0.1}
    )
    tier.expect(
        "missing package returns PACKAGE_INVALID",
        _error_code(missing) == "PACKAGE_INVALID",
        f"got {_error_code(missing)!r}",
    )
    return tier


# ---------------------------------------------------------------------------
# Tier 3 — results
# ---------------------------------------------------------------------------


def tier_results(
    client: TckClient,
    report: TckReport,
    schema_dir: Path | None = None,
) -> TierResult:
    """``simulate`` output validates against the published result schema."""
    tier = report.tier("3. results")
    response = client.request("simulate", _simple_mechanism_args())
    if not response.get("ok"):
        tier.fail("simulate answers", json.dumps(response.get("error", {}))[:200])
        return tier
    result = response.get("result") or {}

    tier.expect("result has a time_series list", isinstance(result.get("time_series"), list))
    summary = result.get("summary")
    tier.expect("result has a summary object", isinstance(summary, dict))
    if isinstance(summary, dict):
        for key in ("simulation_time_s", "dt_s", "engine_mode"):
            tier.expect(
                f"summary.{key} is present", key in summary, f"summary keys: {sorted(summary)}"
            )

    for entry in result.get("time_series") or []:
        if not isinstance(entry, dict) or not isinstance(entry.get("t"), (int, float)):
            tier.fail("every time_series entry has a numeric t", f"bad entry: {entry!r}")
            break
    else:
        tier.ok("every time_series entry has a numeric t")

    schema = _load_result_schema(schema_dir)
    if schema is None:
        tier.skip("validates against sim_result.schema.json", "schema or jsonschema unavailable")
        return tier
    try:
        import jsonschema

        jsonschema.validate(result, schema)
        tier.ok("validates against sim_result.schema.json")
    except Exception as exc:  # noqa: BLE001 — the message is the finding
        tier.fail("validates against sim_result.schema.json", str(exc).split("\n")[0])
    return tier


# ---------------------------------------------------------------------------
# Tier 4 — sessions and teleop
# ---------------------------------------------------------------------------


def tier_sessions(client: TckClient, report: TckReport, capabilities: dict[str, Any]) -> TierResult:
    """Session and teleop lifecycles — only what the engine advertises."""
    tier = report.tier("4. sessions/teleop")
    modes = capabilities.get("modes") or []

    if "session" not in modes:
        tier.skip("simulate session lifecycle", "'session' mode not advertised")
    else:
        _check_session_lifecycle(client, tier)

    if "teleop" not in modes:
        tier.skip("teleop lifecycle", "'teleop' mode not advertised")
    else:
        _check_teleop_lifecycle(client, tier, capabilities)
    return tier


def _check_session_lifecycle(client: TckClient, tier: TierResult) -> None:
    start = client.request("simulate_start", _simple_mechanism_args())
    if not start.get("ok"):
        tier.fail("simulate_start opens a session", json.dumps(start.get("error", {}))[:200])
        return
    session_id = (start.get("result") or {}).get("session_id")
    tier.expect("simulate_start returns a session_id", isinstance(session_id, str) and session_id)
    if not isinstance(session_id, str):
        return

    deadline = time.monotonic() + 30.0
    status: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = client.request("simulate_status", {"session_id": session_id})
        if not response.get("ok"):
            tier.fail("simulate_status answers", json.dumps(response.get("error", {}))[:200])
            return
        status = response.get("result") or {}
        if status.get("status") == "complete":
            break
        time.sleep(0.02)
    tier.expect("simulate_status reaches 'complete'", status.get("status") == "complete")

    stop = client.request("simulate_stop", {"session_id": session_id})
    tier.expect("simulate_stop returns results", bool(stop.get("ok")))

    unknown = client.request("simulate_status", {"session_id": "tck-no-such-session"})
    tier.expect(
        "unknown session returns SESSION_NOT_FOUND",
        _error_code(unknown) == "SESSION_NOT_FOUND",
        f"got {_error_code(unknown)!r}",
    )


def _check_teleop_lifecycle(
    client: TckClient, tier: TierResult, capabilities: dict[str, Any]
) -> None:
    start = client.request("teleop_start", _simple_mechanism_args())
    if not start.get("ok"):
        tier.fail("teleop_start opens a session", json.dumps(start.get("error", {}))[:200])
        return
    session_id = (start.get("result") or {}).get("session_id")
    tier.expect("teleop_start returns a session_id", isinstance(session_id, str) and session_id)
    if not isinstance(session_id, str):
        return

    dofs = capabilities.get("teleop_dofs")
    if isinstance(dofs, list) and dofs:
        tier.ok("teleop_dofs advertised", ", ".join(str(d) for d in dofs))
        command = {"session_id": session_id, str(dofs[0]): 0.1}
    else:
        tier.info("teleop_dofs not advertised", "core cannot pre-check commanded axes")
        command = {"session_id": session_id, "vx_mps": 0.1}

    tier.expect(
        "teleop_command is accepted", bool(client.request("teleop_command", command).get("ok"))
    )
    tier.expect(
        "teleop_state reports the session",
        bool(client.request("teleop_state", {"session_id": session_id}).get("ok")),
    )
    tier.expect(
        "teleop_stop ends the session",
        bool(client.request("teleop_stop", {"session_id": session_id}).get("ok")),
    )
    after = client.request("teleop_state", {"session_id": session_id})
    tier.expect(
        "stopped session is forgotten",
        _error_code(after) == "SESSION_NOT_FOUND",
        f"got {_error_code(after)!r}",
    )


# ---------------------------------------------------------------------------
# Tier 5 — physics sanity
# ---------------------------------------------------------------------------


def tier_physics(client: TckClient, report: TckReport, runtime_mode: str = "real") -> TierResult:
    """Golden scenarios with analytic answers.

    This is the tier that separates "speaks the protocol" from "simulates
    correctly".  Two things legitimately skip it: an engine reporting
    ``runtime_mode: "stub"`` (it never claimed to compute physics) and a
    kinematics-only engine that reports no ``steady_state_speeds``.
    """
    tier = report.tier("5. physics sanity")
    if runtime_mode == "stub":
        tier.skip(
            "physics sanity scenarios",
            "engine reports runtime_mode='stub' — physics applies to real engines",
        )
        return tier
    _check_gear_ratio(client, tier)
    _check_pendulum(client, tier)
    _check_free_fall(client, tier)
    return tier


def _check_gear_ratio(client: TckClient, tier: TierResult) -> None:
    """A 20:40 mesh halves the speed and reverses it."""
    response = client.request("simulate", _gear_pair_args())
    if not response.get("ok"):
        tier.fail("gear ratio propagates", json.dumps(response.get("error", {}))[:200])
        return
    speeds = ((response.get("result") or {}).get("summary") or {}).get("steady_state_speeds")
    if not isinstance(speeds, dict) or "gear_a" not in speeds or "gear_b" not in speeds:
        tier.skip("gear ratio propagates", "engine reports no steady_state_speeds")
        return
    driver, driven = abs(float(speeds["gear_a"])), abs(float(speeds["gear_b"]))
    if driver == 0:
        tier.fail("gear ratio propagates", "driver speed is zero — nothing moved")
        return
    ratio = driven / driver
    tier.expect(
        "gear ratio propagates (20:40 → ½ speed)",
        math.isclose(ratio, 0.5, rel_tol=0.05),
        f"driven/driver = {ratio:.4f}, expected 0.5",
    )


def _check_pendulum(client: TckClient, tier: TierResult) -> None:
    """A 1 m pendulum has a period of 2π√(L/g) ≈ 2.006 s."""
    args = {
        "mechanism": {
            "name": "tck_pendulum",
            "parts": [
                {"id": "anchor", "is_ground": True},
                {"id": "bob", "mass_kg": 1.0},
            ],
            "joints": [
                {
                    "id": "hinge",
                    "joint_type": "revolute",
                    "parent_part": "anchor",
                    "child_part": "bob",
                    "origin": [0.0, 0.0, -1000.0],
                    "axis": [0.0, 1.0, 0.0],
                    "initial_angle_rad": 0.1,
                }
            ],
            "drives": [],
        },
        "duration_s": 2.006,
        "dt_s": 0.001,
        "output_interval": 0.02,
    }
    response = client.request("simulate", args)
    if not response.get("ok"):
        tier.skip("pendulum returns to its start after one period", "engine rejected the model")
        return
    series = (response.get("result") or {}).get("time_series") or []
    angles = [
        float(entry["parts"]["bob"]["angle_rad"])
        for entry in series
        if isinstance(entry.get("parts"), dict)
        and isinstance(entry["parts"].get("bob"), dict)
        and "angle_rad" in entry["parts"]["bob"]
    ]
    if len(angles) < 3:
        tier.skip("pendulum returns to its start after one period", "no bob angle reported")
        return
    tier.expect(
        "pendulum returns to its start after one period",
        math.isclose(angles[-1], angles[0], abs_tol=0.02),
        f"θ(0)={angles[0]:.4f}, θ(T)={angles[-1]:.4f}",
    )


def _check_free_fall(client: TckClient, tier: TierResult) -> None:
    """A body dropped from 1 m reaches the ground at √(2h/g) ≈ 0.4515 s."""
    args = {
        "mechanism": {
            "name": "tck_free_fall",
            "parts": [{"id": "box", "mass_kg": 1.0, "initial_height_m": 1.0}],
            "joints": [],
            "drives": [],
        },
        "duration_s": 1.0,
        "dt_s": 0.001,
        "output_interval": 0.01,
    }
    response = client.request("simulate", args)
    if not response.get("ok"):
        tier.skip("dropped body settles at √(2h/g)", "engine rejected the model")
        return
    series = (response.get("result") or {}).get("time_series") or []
    heights = [
        (float(entry["t"]), float(entry["parts"]["box"]["z_m"]))
        for entry in series
        if isinstance(entry.get("parts"), dict)
        and isinstance(entry["parts"].get("box"), dict)
        and "z_m" in entry["parts"]["box"]
    ]
    if len(heights) < 3:
        tier.skip("dropped body settles at √(2h/g)", "no box height reported")
        return
    settled = next((t for t, z in heights if z <= 1e-6), None)
    expected = math.sqrt(2.0 * 1.0 / 9.81)
    tier.expect(
        "dropped body settles at √(2h/g)",
        settled is not None and abs(settled - expected) < 0.05,
        f"settled at {settled}s, expected ≈{expected:.4f}s",
    )


# ---------------------------------------------------------------------------
# Tier 6 — latency (informational)
# ---------------------------------------------------------------------------


def tier_latency(client: TckClient, report: TckReport, samples: int = 200) -> TierResult:
    """RTT distribution on a persistent connection.  Never fails a run."""
    tier = report.tier("6. latency (informational)")
    timings: list[float] = []
    for _ in range(samples):
        start = time.perf_counter()
        client.request("ping")
        timings.append((time.perf_counter() - start) * 1e6)
    timings.sort()

    def pct(p: float) -> float:
        return timings[min(len(timings) - 1, int(len(timings) * p))]

    tier.info(
        "ping RTT",
        f"p50={statistics.median(timings):.0f}µs  p95={pct(0.95):.0f}µs  "
        f"max={timings[-1]:.0f}µs  (n={len(timings)})",
    )
    tier.info(
        "headroom",
        "teleop runs at ≤10 Hz; core must never sit in a physics-rate loop "
        "(architecture doc, Principle 5)",
    )
    return tier


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _error_code(response: dict[str, Any]) -> str | None:
    error = response.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        return str(code) if isinstance(code, str) else None
    return None


def _simple_mechanism_args() -> dict[str, Any]:
    """A driven revolute joint — the least an engine can be asked to run.

    Deliberately not the gear pair: gear meshes are a legitimate capability
    gap (Isaac supports revolute/prismatic/fixed), and the protocol, result
    and session tiers are not about which joint types an engine models.
    """
    return {
        "mechanism": {
            "name": "tck_simple",
            "parts": [
                {"id": "frame", "is_ground": True},
                {"id": "link_a", "mass_kg": 0.1},
            ],
            "joints": [
                {
                    "id": "rev_a",
                    "joint_type": "revolute",
                    "parent_part": "frame",
                    "child_part": "link_a",
                }
            ],
            "drives": [{"joint_id": "rev_a", "speed_rpm": 600.0, "torque_nm": 1.0}],
        },
        "duration_s": 0.2,
        "dt_s": 0.001,
        "output_interval": 0.05,
    }


def _gear_pair_args() -> dict[str, Any]:
    """The golden gear train: 20:40, used only by the physics tier."""
    return {
        "mechanism": {
            "name": "tck_gear_pair",
            "parts": [
                {"id": "frame", "is_ground": True},
                {"id": "gear_a", "mass_kg": 0.1},
                {"id": "gear_b", "mass_kg": 0.2},
            ],
            "joints": [
                {
                    "id": "rev_a",
                    "joint_type": "revolute",
                    "parent_part": "frame",
                    "child_part": "gear_a",
                },
                {
                    "id": "rev_b",
                    "joint_type": "revolute",
                    "parent_part": "frame",
                    "child_part": "gear_b",
                },
                {
                    "id": "mesh",
                    "joint_type": "gear_mesh",
                    "parent_part": "gear_a",
                    "child_part": "gear_b",
                    "teeth_parent": 20,
                    "teeth_child": 40,
                    "gear_ratio": 0.5,
                },
            ],
            "drives": [{"joint_id": "rev_a", "speed_rpm": 600.0, "torque_nm": 1.0}],
        },
        "duration_s": 0.2,
        "dt_s": 0.001,
        "output_interval": 0.05,
    }


def _minimal_args(verb: str) -> dict[str, Any]:
    """Plausible arguments for a verb, so a refusal is about support."""
    if verb in ("simulate", "simulate_start", "teleop_start"):
        return _simple_mechanism_args()
    if verb in (
        "simulate_status",
        "simulate_stop",
        "teleop_command",
        "teleop_state",
        "teleop_stop",
    ):
        return {"session_id": "tck-probe-session"}
    if verb == "import_urdf":
        return {"urdf_path": "/tck/probe.urdf"}
    if verb == "spawn_model":
        return {"model_name": "tck_probe"}
    if verb == "load_environment":
        return {"usd_url": ""}
    return {}


def _advertised_verbs(capabilities: dict[str, Any]) -> set[str]:
    verbs: set[str] = set()
    for mode in capabilities.get("modes") or []:
        verbs.update(MODE_VERBS.get(str(mode), ()))
    for feature in capabilities.get("features") or []:
        verbs.update(FEATURE_VERBS.get(str(feature), ()))
    return verbs


def _unadvertised_verbs(capabilities: dict[str, Any]) -> list[tuple[str, str]]:
    """Verbs the engine did *not* claim, paired with the capability that gates them."""
    modes = {str(m) for m in capabilities.get("modes") or []}
    features = {str(f) for f in capabilities.get("features") or []}
    out: list[tuple[str, str]] = []
    for mode, verbs in MODE_VERBS.items():
        if mode not in modes:
            out.append((verbs[0], mode))
    for feature, verbs in FEATURE_VERBS.items():
        if feature not in features:
            out.append((verbs[0], feature))
    return out


def _load_result_schema(schema_dir: Path | None) -> dict[str, Any] | None:
    candidates = [schema_dir] if schema_dir else []
    candidates += [
        _FIXTURES / "schemas",
        Path(__file__).resolve().parents[1] / "schemas",
    ]
    for directory in candidates:
        if directory is None:
            continue
        path = directory / "sim_result.schema.json"
        if path.is_file():
            try:
                with open(path, encoding="utf-8") as fh:
                    return json.load(fh)
            except (OSError, json.JSONDecodeError):
                return None
    return None
