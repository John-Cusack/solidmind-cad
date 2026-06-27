"""Reusable Isaac Sim lifecycle manager.

Wraps ``BridgeServer`` + ``IsaacClient`` into a single entry point for
both tests and scripts.  Handles:

- BridgeServer startup in a background thread
- Ephemeral port binding (port=0)
- IsaacClient connection with retry
- Clean shutdown (disconnect → server shutdown → thread join)
- Context manager support (``with IsaacLifecycle(...) as lc:``)
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from server.isaac_client import IsaacClient

logger = logging.getLogger("solidmind.isaac_lifecycle")


class IsaacLifecycle:
    """Manages the full Isaac bridge lifecycle.

    Usage::

        lc = IsaacLifecycle(headless=True)
        lc.start()
        try:
            result = lc.import_urdf("/path/to.urdf")
            lc.screenshot()
        finally:
            lc.stop()

    Or as a context manager::

        with IsaacLifecycle(headless=True) as lc:
            lc.import_urdf("/path/to.urdf")
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self._headless = headless
        self._host = host
        self._port = port
        self._server: Any = None  # BridgeServer instance
        self._client: IsaacClient | None = None
        self._bridge_thread: threading.Thread | None = None
        self._pump_thread: threading.Thread | None = None
        self._started = False

    @property
    def port(self) -> int:
        """Actual bound port (available after ``start()``)."""
        if self._server is not None:
            return self._server.port
        return self._port

    @property
    def client(self) -> IsaacClient:
        """The connected ``IsaacClient`` (available after ``start()``)."""
        if self._client is None:
            raise RuntimeError("IsaacLifecycle not started — call start() first")
        return self._client

    def start(self, timeout: float = 30.0) -> None:
        """Start the bridge server, pump thread, and connect the client.

        Args:
            timeout: Maximum seconds to wait for the bridge to become ready.

        Raises:
            RuntimeError: If the bridge doesn't become ready within *timeout*.
        """
        if self._started:
            return

        # Import here to avoid requiring Isaac at import time
        from isaac_bridge.bridge_server import BridgeServer, _pump_main_thread
        from isaac_bridge.runtime_isaac import main_thread_dispatcher

        self._server = BridgeServer(
            host=self._host,
            port=self._port,
            headless=self._headless,
        )

        # Start bridge TCP server in a background thread
        self._bridge_thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="isaac-lifecycle-bridge",
        )
        self._bridge_thread.start()

        # Start main-thread pump in another thread (Kit event loop)
        main_thread_dispatcher.enable()
        self._pump_thread = threading.Thread(
            target=_pump_main_thread,
            args=(self._server,),
            daemon=True,
            name="isaac-lifecycle-pump",
        )
        self._pump_thread.start()

        # Wait for the server to bind and become ready
        deadline = time.monotonic() + timeout
        actual_port = self._server.port
        while time.monotonic() < deadline:
            if actual_port != 0:
                break
            time.sleep(0.1)
            actual_port = self._server.port

        if actual_port == 0:
            self.stop()
            raise RuntimeError(f"Bridge server did not bind within {timeout}s")

        # Connect client
        self._client = IsaacClient(host=self._host, port=actual_port)
        client_deadline = deadline  # reuse remaining time
        last_exc: Exception | None = None
        while time.monotonic() < client_deadline:
            try:
                self._client.connect(timeout=2.0)
                break
            except Exception as exc:
                last_exc = exc
                time.sleep(0.5)
        else:
            self.stop()
            raise RuntimeError(f"Client could not connect to bridge within {timeout}s: {last_exc}")

        # Verify the bridge is responsive
        try:
            self._client.send_command("ping", timeout=5.0)
        except Exception as exc:
            self.stop()
            raise RuntimeError(f"Bridge ping failed: {exc}") from exc

        self._started = True
        logger.info("Isaac lifecycle started (port=%d, headless=%s)", actual_port, self._headless)

    def stop(self) -> None:
        """Disconnect client, shutdown server, join threads."""
        if self._client is not None:
            try:
                self._client.disconnect()
            except Exception:
                pass
            self._client = None

        if self._server is not None:
            self._server.shutdown()

        if self._bridge_thread is not None:
            self._bridge_thread.join(timeout=5.0)
            self._bridge_thread = None

        if self._pump_thread is not None:
            self._pump_thread.join(timeout=5.0)
            self._pump_thread = None

        self._server = None
        self._started = False
        logger.info("Isaac lifecycle stopped")

    def import_urdf(
        self,
        path: str,
        **config: Any,
    ) -> dict[str, Any]:
        """Import a URDF file into the Isaac scene.

        Args:
            path: Absolute path to the URDF file.
            **config: Optional import config overrides.

        Returns:
            Import result dict from the bridge.
        """
        return self.client.import_urdf(path, import_config=config or None)

    def screenshot(
        self,
        path: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Capture a screenshot of the Isaac viewport.

        Args:
            path: If given, write the PNG to this path.
            **kwargs: Optional camera_position, camera_target, width, height.

        Returns:
            Screenshot result dict (includes base64 PNG data).
        """
        result = self.client.screenshot(**kwargs)
        if path is not None and "image_base64" in result:
            import base64

            with open(path, "wb") as f:
                f.write(base64.b64decode(result["image_base64"]))
            result["saved_to"] = path
        return result

    def reload(self) -> dict[str, Any]:
        """Reset the Isaac World without restarting SimulationApp.

        Use between test cases to get a clean scene.

        Returns:
            Reload result dict from the bridge.
        """
        return self.client.send_command("reload")

    def simulate(
        self,
        *,
        urdf_path: str | None = None,
        duration_s: float = 1.0,
        dt_s: float = 0.001,
        output_interval: float = 0.01,
        mechanism: dict[str, Any] | None = None,
        import_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run a batch physics simulation.

        Returns:
            Simulation result dict with time series.
        """
        cmd_args: dict[str, Any] = {
            "duration_s": duration_s,
            "dt_s": dt_s,
            "output_interval": output_interval,
        }
        if urdf_path is not None:
            cmd_args["urdf_path"] = urdf_path
        if mechanism is not None:
            cmd_args["mechanism"] = mechanism
        if import_config is not None:
            cmd_args["import_config"] = import_config
        return self.client.send_command("simulate", **cmd_args)

    def diagnose(self, prim_path: str = "/") -> dict[str, Any]:
        """Diagnose the USD prim tree at the given path.

        Returns:
            Diagnosis result dict.
        """
        return self.client.send_command("diagnose", prim_path=prim_path)

    def __enter__(self) -> IsaacLifecycle:
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()
