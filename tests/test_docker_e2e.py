"""Docker E2E test — spin up worker containers, submit tasks via A2A, verify artifacts.

Requires Docker and docker compose. Skip if not available.

Usage:
    python -m pytest tests/test_docker_e2e.py -v
    python -m unittest tests.test_docker_e2e -v
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
import unittest
from pathlib import Path
from typing import Any

# Worker ports exposed on the host
WORKER_PORTS = [8081, 8082, 8083]
COMPOSE_FILE = Path(__file__).parent.parent / "docker" / "docker-compose.yml"
PROJECT_DIR = Path(__file__).parent.parent


def _httpx_available() -> bool:
    """Check if httpx is importable (required by the A2A client)."""
    try:
        import httpx  # noqa: F401

        return True
    except ImportError:
        return False


def _docker_available() -> bool:
    """Check if docker and docker compose are available."""
    try:
        subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            timeout=10,
        )
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _compose(*args: str, timeout: int = 300) -> subprocess.CompletedProcess:
    """Run docker compose with the project compose file."""
    cmd = ["docker", "compose", "-f", str(COMPOSE_FILE), *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(PROJECT_DIR),
    )


def _wait_for_workers(ports: list[int], timeout: float = 180.0) -> dict[int, bool]:
    """Poll worker health endpoints until all are ready or timeout."""
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + timeout
    ready = {p: False for p in ports}

    while time.monotonic() < deadline and not all(ready.values()):
        for port in ports:
            if ready[port]:
                continue
            try:
                req = urllib.request.Request(f"http://localhost:{port}/health")
                resp = urllib.request.urlopen(req, timeout=3)
                data = json.loads(resp.read())
                if data.get("freecad") is True:
                    ready[port] = True
            except Exception:
                pass
        if not all(ready.values()):
            time.sleep(5)

    return ready


async def _submit_task(
    port: int,
    sub_spec: dict[str, Any],
    interfaces: list[dict[str, Any]],
) -> dict[str, Any]:
    """Submit a build task to a worker via A2A JSON-RPC."""
    from orchestrator.a2a_client import A2AClient

    client = A2AClient(timeout_sec=300, poll_interval_sec=3.0)
    try:
        task = await client.submit_and_wait(
            f"http://localhost:{port}",
            sub_spec=sub_spec,
            interfaces=interfaces,
            output_dir=Path(f"/tmp/docker_e2e_worker_{port}"),
            timeout_sec=120,
        )
        return {
            "task_id": task.task_id,
            "status": task.status,
            "artifacts": task.artifacts,
            "error": task.error,
        }
    finally:
        await client.close()


@unittest.skipUnless(_docker_available() and _httpx_available(), "Docker or httpx not available")
class TestDockerE2E(unittest.TestCase):
    """End-to-end test with Docker worker containers."""

    _stack_up = False

    @classmethod
    def setUpClass(cls) -> None:
        """Build and start the Docker compose stack."""
        print("\n=== Building worker images (this may take a few minutes)... ===")
        result = _compose("build", "--quiet", timeout=600)
        if result.returncode != 0:
            print(f"Build failed:\n{result.stderr}")
            raise unittest.SkipTest(f"Docker build failed: {result.stderr[:500]}")

        print("=== Starting worker containers... ===")
        result = _compose("up", "-d")
        if result.returncode != 0:
            print(f"Compose up failed:\n{result.stderr}")
            raise unittest.SkipTest(f"Docker compose up failed: {result.stderr[:500]}")

        cls._stack_up = True

        print("=== Waiting for workers to be ready... ===")
        ready = _wait_for_workers(WORKER_PORTS, timeout=180)
        not_ready = [p for p, ok in ready.items() if not ok]
        if not_ready:
            # Print logs for debugging
            for port in not_ready:
                svc = f"worker-{WORKER_PORTS.index(port) + 1}"
                logs = _compose("logs", svc, timeout=10)
                print(f"\n--- Logs for {svc} (port {port}) ---\n{logs.stdout[-2000:]}")
            raise unittest.SkipTest(f"Workers not ready on ports {not_ready} within timeout")

        print(f"=== All {len(WORKER_PORTS)} workers ready ===")

    @classmethod
    def tearDownClass(cls) -> None:
        """Tear down the Docker compose stack."""
        if cls._stack_up:
            print("\n=== Stopping worker containers... ===")
            _compose("down", "-v", timeout=60)

    # -- Discovery tests --

    def test_agent_card_discovery(self) -> None:
        """Each worker serves a valid Agent Card."""
        import urllib.request

        for port in WORKER_PORTS:
            with urllib.request.urlopen(
                f"http://localhost:{port}/.well-known/agent-card.json",
                timeout=5,
            ) as resp:
                card = json.loads(resp.read())
                self.assertIn("name", card)
                self.assertIn("skills", card)
                self.assertGreaterEqual(len(card["skills"]), 1)
                self.assertEqual(card["skills"][0]["id"], "build_subsystem")

    def test_health_endpoint(self) -> None:
        """Health endpoint reports FreeCAD ready."""
        import urllib.request

        for port in WORKER_PORTS:
            with urllib.request.urlopen(
                f"http://localhost:{port}/health",
                timeout=5,
            ) as resp:
                data = json.loads(resp.read())
                self.assertEqual(data["status"], "ok")
                self.assertTrue(data["freecad"])

    # -- Single worker build --

    def test_single_worker_build(self) -> None:
        """Submit a cube build to one worker and verify completion."""
        sub_spec = {
            "name": "test_cube",
            "envelope_mm": [20, 20, 10],
            "material": "aluminum",
        }
        interfaces: list[dict[str, Any]] = []

        result = asyncio.run(_submit_task(WORKER_PORTS[0], sub_spec, interfaces))

        self.assertEqual(result["status"], "completed", f"Error: {result.get('error')}")
        self.assertGreater(len(result["artifacts"]), 0)

        # Check we got STEP and STL files
        art_names = [a.get("name", "") for a in result["artifacts"]]
        self.assertTrue(
            any(n.endswith(".step") for n in art_names),
            f"No STEP file in artifacts: {art_names}",
        )
        self.assertTrue(
            any(n.endswith(".stl") for n in art_names),
            f"No STL file in artifacts: {art_names}",
        )

    def test_single_worker_build_with_interface(self) -> None:
        """Submit a build with a cylindrical interface feature."""
        sub_spec = {
            "name": "boss_cube",
            "envelope_mm": [20, 20, 10],
            "role": "boss",
        }
        interfaces = [
            {
                "id": "ifc_mate",
                "name": "Boss-hole fit",
                "subsystem_a": "boss_cube",
                "geometry": {"type": "cylinder", "diameter_mm": 10, "depth_mm": 5},
            }
        ]

        result = asyncio.run(_submit_task(WORKER_PORTS[0], sub_spec, interfaces))

        self.assertEqual(result["status"], "completed", f"Error: {result.get('error')}")

        # Check metadata artifact has interface actuals
        metadata_arts = [a for a in result["artifacts"] if a.get("name", "").endswith(".json")]
        self.assertGreater(len(metadata_arts), 0, "No metadata artifact")
        meta_data = metadata_arts[0].get("data", {})
        self.assertIn("ifc_mate", meta_data.get("interface_actuals", {}))

    # -- Parallel workers --

    def test_parallel_builds(self) -> None:
        """Submit tasks to all workers in parallel and verify all complete."""
        specs = [
            {"name": f"parallel_part_{i}", "envelope_mm": [15 + i * 5, 15 + i * 5, 8 + i * 2]}
            for i in range(len(WORKER_PORTS))
        ]

        async def _run_parallel() -> list[dict[str, Any]]:
            tasks = [
                _submit_task(port, spec, [])
                for port, spec in zip(WORKER_PORTS, specs, strict=False)
            ]
            return await asyncio.gather(*tasks)

        results = asyncio.run(_run_parallel())

        for i, result in enumerate(results):
            with self.subTest(worker=i):
                self.assertEqual(
                    result["status"],
                    "completed",
                    f"Worker {i} failed: {result.get('error')}",
                )
                self.assertGreater(len(result["artifacts"]), 0)

    def test_parallel_builds_produce_different_parts(self) -> None:
        """Each parallel worker builds a part with the correct name."""
        specs = [
            {"name": f"unique_part_{i}", "envelope_mm": [20, 20, 10]}
            for i in range(len(WORKER_PORTS))
        ]

        async def _run() -> list[dict[str, Any]]:
            tasks = [
                _submit_task(port, spec, [])
                for port, spec in zip(WORKER_PORTS, specs, strict=False)
            ]
            return await asyncio.gather(*tasks)

        results = asyncio.run(_run())

        for i, result in enumerate(results):
            with self.subTest(worker=i):
                self.assertEqual(result["status"], "completed")
                # Check artifact names match expected part
                art_names = [a.get("name", "") for a in result["artifacts"]]
                self.assertTrue(
                    any(f"unique_part_{i}" in n for n in art_names),
                    f"Worker {i} artifacts don't contain expected part name: {art_names}",
                )


@unittest.skipUnless(_docker_available() and _httpx_available(), "Docker or httpx not available")
class TestDockerTaskProtocol(unittest.TestCase):
    """Test the A2A task protocol specifics (requires running stack)."""

    @classmethod
    def setUpClass(cls) -> None:
        """Verify stack is running (assumes TestDockerE2E.setUpClass ran first)."""
        ready = _wait_for_workers([WORKER_PORTS[0]], timeout=5)
        if not ready[WORKER_PORTS[0]]:
            raise unittest.SkipTest("Worker stack not running")

    def test_task_get_returns_status(self) -> None:
        """Submitting a task and polling returns valid status."""
        import urllib.request

        port = WORKER_PORTS[0]

        # Submit via raw JSON-RPC
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "message/send",
                "params": {
                    "message": {
                        "role": "user",
                        "parts": [
                            {
                                "type": "data",
                                "mimeType": "application/json",
                                "data": {
                                    "sub_spec": {
                                        "name": "protocol_test",
                                        "envelope_mm": [10, 10, 5],
                                    },
                                    "interfaces": [],
                                },
                            }
                        ],
                    },
                },
            }
        ).encode()

        req = urllib.request.Request(
            f"http://localhost:{port}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=10)
        result = json.loads(resp.read())

        self.assertIn("result", result)
        task_id = result["result"]["taskId"]
        self.assertTrue(len(task_id) > 0)

        # Poll for completion
        deadline = time.monotonic() + 60
        status = "submitted"
        while time.monotonic() < deadline and status not in ("completed", "failed"):
            time.sleep(2)
            poll_body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "task/get",
                    "params": {"taskId": task_id},
                }
            ).encode()
            poll_req = urllib.request.Request(
                f"http://localhost:{port}",
                data=poll_body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            poll_resp = urllib.request.urlopen(poll_req, timeout=10)
            poll_result = json.loads(poll_resp.read())
            status = poll_result["result"]["status"]

        self.assertEqual(status, "completed")

    def test_cancel_nonexistent_task(self) -> None:
        """Canceling a nonexistent task returns an error."""
        import urllib.request

        port = WORKER_PORTS[0]
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "task/cancel",
                "params": {"taskId": "nonexistent_task_id"},
            }
        ).encode()

        req = urllib.request.Request(
            f"http://localhost:{port}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=5)
        result = json.loads(resp.read())

        self.assertIn("error", result)

    def test_unknown_method(self) -> None:
        """Unknown JSON-RPC method returns method-not-found error."""
        import urllib.request

        port = WORKER_PORTS[0]
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "task/bogus",
                "params": {},
            }
        ).encode()

        req = urllib.request.Request(
            f"http://localhost:{port}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=5)
        result = json.loads(resp.read())

        self.assertIn("error", result)
        self.assertEqual(result["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()
