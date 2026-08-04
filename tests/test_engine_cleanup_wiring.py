"""The MCP server must stop the engines it started.

``sim_engine_manager.shutdown_all`` was written and tested from the
start, and its docstring says "Call at server exit" — but nothing called
it, so every engine outlived the server. Three Isaac bridges were found
orphaned for 18 hours holding 16 GB between them.

These tests guard the *wiring*, which is the part that was missing;
``shutdown_all`` itself is covered by ``test_sim_subprocess_lifecycle``.
"""

from __future__ import annotations

import signal
import unittest
from unittest import mock

from server import main as main_mod


class TestEngineCleanupWiring(unittest.TestCase):
    def test_shutdown_all_is_registered_with_atexit(self) -> None:
        from server import sim_engine_manager

        with (
            mock.patch("atexit.register") as register,
            mock.patch.object(signal, "signal"),
        ):
            main_mod._install_engine_cleanup()

        register.assert_called_once_with(sim_engine_manager.shutdown_all)

    def test_sigterm_and_sigint_are_both_handled(self) -> None:
        with (
            mock.patch("atexit.register"),
            mock.patch.object(signal, "signal") as sigsignal,
        ):
            main_mod._install_engine_cleanup()

        handled = {call.args[0] for call in sigsignal.call_args_list}
        self.assertIn(signal.SIGTERM, handled)
        self.assertIn(signal.SIGINT, handled)

    def test_signal_handler_drains_engines_then_re_raises(self) -> None:
        """Engines must stop, but the exit status must still say "killed"."""
        handlers: dict[int, object] = {}

        def _capture(sig: int, handler: object) -> None:
            handlers[sig] = handler

        with (
            mock.patch("atexit.register"),
            mock.patch.object(signal, "signal", side_effect=_capture),
        ):
            main_mod._install_engine_cleanup()

        handler = handlers[signal.SIGTERM]
        with (
            mock.patch("server.sim_engine_manager.shutdown_all") as shutdown,
            mock.patch.object(signal, "signal") as restore,
            mock.patch.object(signal, "raise_signal") as raise_signal,
        ):
            handler(signal.SIGTERM, None)  # type: ignore[operator]

        shutdown.assert_called_once_with()
        restore.assert_called_once_with(signal.SIGTERM, signal.SIG_DFL)
        raise_signal.assert_called_once_with(signal.SIGTERM)

    def test_survives_being_installed_off_the_main_thread(self) -> None:
        """Embedded use: signal() raises, but atexit must still be wired."""
        with (
            mock.patch("atexit.register") as register,
            mock.patch.object(signal, "signal", side_effect=ValueError("not main thread")),
        ):
            main_mod._install_engine_cleanup()  # must not raise

        register.assert_called_once()

    def test_main_installs_the_cleanup(self) -> None:
        """The wiring is only worth anything if main() actually calls it."""
        with (
            mock.patch.object(main_mod, "_install_engine_cleanup") as install,
            mock.patch.object(main_mod, "serve", return_value=0),
            self.assertRaises(SystemExit),
        ):
            main_mod.main([])

        install.assert_called_once()


if __name__ == "__main__":
    unittest.main()
