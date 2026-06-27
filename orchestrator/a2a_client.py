"""A2A client — orchestrator-side task submission, polling, and artifact collection."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# httpx is a future dependency (Docker/A2A mode). Lazy import to avoid
# breaking the module when only subagent/claude_code modes are used.
httpx: Any = None


def _ensure_httpx() -> None:
    global httpx
    if httpx is None:
        try:
            import httpx as _httpx

            httpx = _httpx
        except ImportError as exc:
            raise ImportError(
                "httpx is required for A2A client mode. Install with: pip install httpx"
            ) from exc


# ---------------------------------------------------------------------------
# Agent Card (parsed from /.well-known/agent-card.json)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AgentSkill:
    """A single skill advertised by a worker agent."""

    id: str = ""
    name: str = ""
    description: str = ""
    input_modes: list[str] = field(default_factory=lambda: ["application/json"])
    output_modes: list[str] = field(default_factory=lambda: ["application/json"])


@dataclass(slots=True)
class AgentCard:
    """Parsed /.well-known/agent-card.json from a worker."""

    name: str = ""
    description: str = ""
    url: str = ""
    version: str = "1"
    streaming: bool = True
    push_notifications: bool = False
    default_input_modes: list[str] = field(
        default_factory=lambda: ["application/json"],
    )
    default_output_modes: list[str] = field(
        default_factory=lambda: ["application/json"],
    )
    skills: list[AgentSkill] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AgentCard:
        caps = d.get("capabilities", {})
        skills = [
            AgentSkill(
                id=s.get("id", ""),
                name=s.get("name", ""),
                description=s.get("description", ""),
                input_modes=s.get("inputModes", ["application/json"]),
                output_modes=s.get("outputModes", ["application/json"]),
            )
            for s in d.get("skills", [])
        ]
        return cls(
            name=d.get("name", ""),
            description=d.get("description", ""),
            url=d.get("url", ""),
            version=d.get("version", "1"),
            streaming=caps.get("streaming", True),
            push_notifications=caps.get("pushNotifications", False),
            default_input_modes=d.get("defaultInputModes", ["application/json"]),
            default_output_modes=d.get("defaultOutputModes", ["application/json"]),
            skills=skills,
        )


# ---------------------------------------------------------------------------
# Task status
# ---------------------------------------------------------------------------

TERMINAL_STATES = frozenset({"completed", "failed", "canceled"})


@dataclass(slots=True)
class A2ATask:
    """Tracks a submitted A2A task."""

    task_id: str = ""
    worker_url: str = ""
    status: str = (
        "submitted"  # submitted | working | input-required | completed | failed | canceled
    )
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# JSON-RPC helpers
# ---------------------------------------------------------------------------

_rpc_id_counter = 0


def _rpc_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
    global _rpc_id_counter
    _rpc_id_counter += 1
    return {
        "jsonrpc": "2.0",
        "id": _rpc_id_counter,
        "method": method,
        "params": params,
    }


# ---------------------------------------------------------------------------
# A2A Client
# ---------------------------------------------------------------------------


class A2AClient:
    """Orchestrator-side A2A client for worker communication.

    Discovers workers via Agent Card, submits tasks via JSON-RPC,
    polls status, and collects artifacts.
    """

    def __init__(
        self,
        *,
        timeout_sec: float = 600,
        poll_interval_sec: float = 5.0,
    ) -> None:
        _ensure_httpx()
        self._timeout = timeout_sec
        self._poll_interval = poll_interval_sec
        self._http: Any = None  # httpx.AsyncClient, lazy

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(self._timeout))
        return self._http

    async def close(self) -> None:
        if self._http and not self._http.is_closed:
            await self._http.aclose()

    # -- Discovery ----------------------------------------------------------

    async def discover(self, base_url: str) -> AgentCard:
        """Fetch and parse the Agent Card from a worker."""
        client = await self._client()
        url = f"{base_url.rstrip('/')}/.well-known/agent-card.json"
        resp = await client.get(url)
        resp.raise_for_status()
        return AgentCard.from_dict(resp.json())

    # -- Task submission ----------------------------------------------------

    async def submit_task(
        self,
        worker_url: str,
        *,
        sub_spec: dict[str, Any],
        interfaces: list[dict[str, Any]],
        task_metadata: dict[str, Any] | None = None,
    ) -> A2ATask:
        """Submit a build task to a worker via message/send."""
        client = await self._client()
        message = {
            "role": "user",
            "parts": [
                {
                    "type": "data",
                    "mimeType": "application/json",
                    "data": {
                        "sub_spec": sub_spec,
                        "interfaces": interfaces,
                    },
                },
            ],
        }
        body = _rpc_request(
            "message/send",
            {
                "message": message,
                **({"metadata": task_metadata} if task_metadata else {}),
            },
        )
        resp = await client.post(worker_url, json=body)
        resp.raise_for_status()
        result = resp.json().get("result", {})
        task_id = result.get("taskId", result.get("task_id", ""))
        status = result.get("status", "submitted")
        return A2ATask(
            task_id=task_id,
            worker_url=worker_url,
            status=status,
            metadata=task_metadata or {},
        )

    # -- Status polling -----------------------------------------------------

    async def get_task_status(self, task: A2ATask) -> A2ATask:
        """Poll current task status."""
        client = await self._client()
        body = _rpc_request("task/get", {"taskId": task.task_id})
        resp = await client.post(task.worker_url, json=body)
        resp.raise_for_status()
        result = resp.json().get("result", {})
        task.status = result.get("status", task.status)
        task.artifacts = result.get("artifacts", task.artifacts)
        if result.get("error"):
            task.error = str(result["error"])
        return task

    async def wait_for_completion(
        self,
        task: A2ATask,
        *,
        timeout_sec: float | None = None,
    ) -> A2ATask:
        """Poll until task reaches a terminal state or timeout."""
        timeout = timeout_sec or self._timeout
        deadline = asyncio.get_running_loop().time() + timeout
        while task.status not in TERMINAL_STATES:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                await self.cancel_task(task)
                task.status = "canceled"
                task.error = f"Timeout after {timeout}s"
                return task
            await asyncio.sleep(min(self._poll_interval, remaining))
            await self.get_task_status(task)
            if task.status == "input-required":
                task.error = "Worker requires clarification (input-required)"
                log.warning("Task %s input-required: %s", task.task_id, task.metadata)
                break
        return task

    # -- Cancellation -------------------------------------------------------

    async def cancel_task(self, task: A2ATask) -> None:
        """Request task cancellation."""
        client = await self._client()
        body = _rpc_request("task/cancel", {"taskId": task.task_id})
        try:
            resp = await client.post(task.worker_url, json=body)
            resp.raise_for_status()
            task.status = "canceled"
        except Exception as exc:
            log.warning("Cancel failed for task %s: %s", task.task_id, exc)

    # -- Artifact collection ------------------------------------------------

    async def collect_artifacts(
        self,
        task: A2ATask,
        output_dir: Path,
    ) -> list[Path]:
        """Download artifacts from a completed task to output_dir.

        Returns list of downloaded file paths.
        """
        if task.status != "completed":
            log.warning(
                "Collecting artifacts from non-completed task %s (status=%s)",
                task.task_id,
                task.status,
            )
        output_dir.mkdir(parents=True, exist_ok=True)
        collected: list[Path] = []
        for artifact in task.artifacts:
            art_type = artifact.get("type", "")
            if art_type == "file":
                name = artifact.get("name", f"artifact_{len(collected)}")
                data = artifact.get("data", b"")
                if isinstance(data, str):
                    import base64

                    data = base64.b64decode(data)
                path = output_dir / name
                path.write_bytes(data)
                collected.append(path)
                log.info("Collected artifact: %s (%d bytes)", path, len(data))
            elif art_type == "data":
                name = artifact.get("name", f"metadata_{len(collected)}.json")
                path = output_dir / name
                path.write_text(json.dumps(artifact.get("data", {}), indent=2))
                collected.append(path)
        return collected

    # -- Convenience: full lifecycle ----------------------------------------

    async def submit_and_wait(
        self,
        worker_url: str,
        *,
        sub_spec: dict[str, Any],
        interfaces: list[dict[str, Any]],
        output_dir: Path,
        timeout_sec: float | None = None,
        task_metadata: dict[str, Any] | None = None,
    ) -> A2ATask:
        """Submit a task, wait for completion, and collect artifacts."""
        task = await self.submit_task(
            worker_url,
            sub_spec=sub_spec,
            interfaces=interfaces,
            task_metadata=task_metadata,
        )
        log.info("Submitted task %s to %s", task.task_id, worker_url)
        task = await self.wait_for_completion(task, timeout_sec=timeout_sec)
        if task.status == "completed":
            await self.collect_artifacts(task, output_dir)
        return task
