from __future__ import annotations

import builtins
import importlib
import sys
import unittest
from unittest.mock import patch


class TestMainEnvLoading(unittest.TestCase):
    def test_main_import_without_python_dotenv(self) -> None:
        previous = sys.modules.pop("server.main", None)
        real_import = builtins.__import__

        def _import_blocker(name: str, *args: object, **kwargs: object):
            if name == "dotenv":
                raise ImportError("Mocked missing python-dotenv")
            return real_import(name, *args, **kwargs)

        try:
            with patch("builtins.__import__", side_effect=_import_blocker):
                module = importlib.import_module("server.main")
            self.assertTrue(callable(module._tool_list))
        finally:
            sys.modules.pop("server.main", None)
            if previous is not None:
                sys.modules["server.main"] = previous


if __name__ == "__main__":
    unittest.main()
