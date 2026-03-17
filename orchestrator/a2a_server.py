"""A2A server — worker-side HTTP endpoint implementing the A2A protocol.

Runs inside a worker container. Wraps Claude Code execution: receives
a sub-spec, launches Claude Code, streams progress, returns artifacts.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Background tasks must be kept alive to prevent GC before completion.
_background_tasks: set[asyncio.Task] = set()  # type: ignore[type-arg]

# ---------------------------------------------------------------------------
# Task store (in-memory, single worker per container)
# ---------------------------------------------------------------------------

TASK_STATUS_SUBMITTED = "submitted"
TASK_STATUS_WORKING = "working"
TASK_STATUS_INPUT_REQUIRED = "input-required"
TASK_STATUS_COMPLETED = "completed"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_CANCELED = "canceled"


@dataclass(slots=True)
class WorkerTask:
    """Internal task tracking for a single build job."""

    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = TASK_STATUS_SUBMITTED
    sub_spec: dict[str, Any] = field(default_factory=dict)
    interfaces: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    progress: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


# Global task store — one task per container (MVP)
_tasks: dict[str, WorkerTask] = {}


# ---------------------------------------------------------------------------
# Agent Card generation
# ---------------------------------------------------------------------------


def generate_agent_card(
    *,
    worker_name: str,
    worker_description: str,
    host: str = "0.0.0.0",
    port: int = 8080,
) -> dict[str, Any]:
    """Generate an A2A Agent Card from worker config."""
    return {
        "name": worker_name,
        "description": worker_description,
        "url": f"http://{host}:{port}",
        "version": "1",
        "capabilities": {
            "streaming": True,
            "pushNotifications": False,
        },
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
        "skills": [
            {
                "id": "build_subsystem",
                "name": "Build CAD Subsystem",
                "description": "Build geometry from frozen spec + interfaces",
                "inputModes": ["application/json"],
                "outputModes": ["application/json"],
            },
        ],
    }


# ---------------------------------------------------------------------------
# JSON-RPC dispatch
# ---------------------------------------------------------------------------


def _rpc_success(id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id, "result": result}


def _rpc_error(id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}}


async def handle_rpc(
    body: dict[str, Any],
    *,
    build_fn: Any | None = None,
) -> dict[str, Any]:
    """Dispatch a JSON-RPC 2.0 request to the appropriate handler."""
    rpc_id = body.get("id")
    method = body.get("method", "")
    params = body.get("params", {})

    if method == "message/send":
        return await _handle_message_send(rpc_id, params, build_fn=build_fn)
    elif method == "task/get":
        return _handle_task_get(rpc_id, params)
    elif method == "task/cancel":
        return _handle_task_cancel(rpc_id, params)
    else:
        return _rpc_error(rpc_id, -32601, f"Method not found: {method}")


async def _handle_message_send(
    rpc_id: Any,
    params: dict[str, Any],
    *,
    build_fn: Any | None = None,
) -> dict[str, Any]:
    """Handle message/send — create a task and start building."""
    message = params.get("message", {})
    parts = message.get("parts", [])

    # Extract sub-spec and interfaces from message parts
    sub_spec: dict[str, Any] = {}
    interfaces: list[dict[str, Any]] = []
    for part in parts:
        if part.get("type") == "data" and part.get("mimeType") == "application/json":
            data = part.get("data", {})
            sub_spec = data.get("sub_spec", sub_spec)
            interfaces = data.get("interfaces", interfaces)

    task = WorkerTask(
        sub_spec=sub_spec,
        interfaces=interfaces,
        metadata=params.get("metadata", {}),
    )
    _tasks[task.task_id] = task

    # Launch build in background if build_fn provided
    if build_fn is not None:
        bg = asyncio.create_task(_run_build(task, build_fn))
        _background_tasks.add(bg)
        bg.add_done_callback(_background_tasks.discard)

    return _rpc_success(rpc_id, {
        "taskId": task.task_id,
        "status": task.status,
    })


async def _run_build(task: WorkerTask, build_fn: Any) -> None:
    """Execute the build function and update task status."""
    task.status = TASK_STATUS_WORKING
    try:
        result = await build_fn(task.sub_spec, task.interfaces, task)
        task.artifacts = result.get("artifacts", [])
        task.status = TASK_STATUS_COMPLETED
        log.info("Task %s completed with %d artifacts", task.task_id, len(task.artifacts))
    except asyncio.CancelledError:
        task.status = TASK_STATUS_CANCELED
        log.info("Task %s canceled", task.task_id)
    except Exception as exc:
        task.status = TASK_STATUS_FAILED
        task.error = str(exc)
        log.error("Task %s failed: %s", task.task_id, exc)


def _handle_task_get(rpc_id: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Handle task/get — return current task status."""
    task_id = params.get("taskId", "")
    task = _tasks.get(task_id)
    if not task:
        return _rpc_error(rpc_id, -32602, f"Task not found: {task_id}")
    return _rpc_success(rpc_id, {
        "taskId": task.task_id,
        "status": task.status,
        "artifacts": task.artifacts,
        "error": task.error,
        "progress": task.progress,
    })


def _handle_task_cancel(rpc_id: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Handle task/cancel — mark task as canceled."""
    task_id = params.get("taskId", "")
    task = _tasks.get(task_id)
    if not task:
        return _rpc_error(rpc_id, -32602, f"Task not found: {task_id}")
    if task.status in {TASK_STATUS_COMPLETED, TASK_STATUS_FAILED, TASK_STATUS_CANCELED}:
        return _rpc_error(rpc_id, -32602, f"Task already terminal: {task.status}")
    task.status = TASK_STATUS_CANCELED
    return _rpc_success(rpc_id, {"taskId": task.task_id, "status": task.status})


# ---------------------------------------------------------------------------
# ASGI app (Starlette-based, lazy import)
# ---------------------------------------------------------------------------


def create_app(
    *,
    worker_name: str = "solidmind-worker",
    worker_description: str = "CAD worker",
    build_fn: Any | None = None,
    health_fn: Any | None = None,
) -> Any:
    """Create a Starlette ASGI app implementing the A2A protocol.

    Args:
        worker_name: Name for the Agent Card.
        worker_description: Description for the Agent Card.
        build_fn: Async callable(sub_spec, interfaces, task) -> {"artifacts": [...]}.
                  If None, tasks stay in 'submitted' state (useful for testing).
        health_fn: Optional callable() -> dict for custom health checks.
                   If None, returns {"status": "ok"}.
    """
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    agent_card = generate_agent_card(
        worker_name=worker_name,
        worker_description=worker_description,
    )

    async def agent_card_endpoint(request: Request) -> JSONResponse:
        return JSONResponse(agent_card)

    async def rpc_endpoint(request: Request) -> JSONResponse:
        body = await request.json()
        result = await handle_rpc(body, build_fn=build_fn)
        return JSONResponse(result)

    async def health(request: Request) -> JSONResponse:
        if health_fn is not None:
            data = health_fn()
            status_code = 200 if data.get("status") == "ok" else 503
            return JSONResponse(data, status_code=status_code)
        return JSONResponse({"status": "ok"})

    routes = [
        Route("/.well-known/agent-card.json", agent_card_endpoint, methods=["GET"]),
        Route("/", rpc_endpoint, methods=["POST"]),
        Route("/health", health, methods=["GET"]),
    ]

    return Starlette(routes=routes)


# ---------------------------------------------------------------------------
# Standalone runner (for worker_entry.py)
# ---------------------------------------------------------------------------


async def serve(
    *,
    host: str = "0.0.0.0",
    port: int = 8080,
    worker_name: str = "solidmind-worker",
    worker_description: str = "CAD worker",
    build_fn: Any | None = None,
) -> None:
    """Run the A2A server with uvicorn."""
    import uvicorn

    app = create_app(
        worker_name=worker_name,
        worker_description=worker_description,
        build_fn=build_fn,
    )
    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()
