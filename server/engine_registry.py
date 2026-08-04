"""Engine registry — descriptors in, vocabulary out.

Core used to carry a table of the engines it knew: ports, launch commands,
install hints, which backend supported teleop, what to tell the model about
each one.  Every one of those was a reason to edit core to add an engine.

They are now **data**.  A descriptor is a TOML file (``docs/engine-contract.md``
§9); core ships defaults for its own three engines in ``engines.d/`` and users
drop third-party engines into ``~/.solidmind/engines.d/`` — same format, no
registration, no core edit (Principle 9, the N+1 rule).

What an engine *can do* is not in the descriptor at all: capabilities come from
the ``hello`` handshake at runtime, so an engine that grows a feature is
immediately usable without touching this file or core.
"""

from __future__ import annotations

import logging
import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("solidmind.engine_registry")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Descriptors shipped with core.
_BUILTIN_DIR = _PROJECT_ROOT / "engines.d"

#: Where users add their own.  ``SOLIDMIND_ENGINES_D`` overrides it.
_USER_DIR = Path.home() / ".solidmind" / "engines.d"

_DEFAULT_HOST = "127.0.0.1"


class DescriptorError(Exception):
    """Raised when a descriptor file is unusable."""


@dataclass(frozen=True, slots=True)
class EngineDescriptor:
    """One engine's launch and discovery data."""

    name: str
    port: int
    host: str = _DEFAULT_HOST
    launch: tuple[str, ...] = ()
    cwd: str | None = None
    install_hint: str = ""
    when_to_use: str = ""
    default: bool = False
    variants: dict[str, tuple[str, ...]] = field(default_factory=dict)
    source: str = ""

    @property
    def attach_only(self) -> bool:
        """True when core cannot start this engine, only connect to it.

        A descriptor without ``launch`` is how you point core at a
        user-managed daemon or a remote engine.
        """
        return not self.launch

    def launch_command(self, variant: str | None = None) -> tuple[str, ...]:
        """Argv for *variant*, falling back to the default launch command."""
        if variant and variant in self.variants:
            return self.variants[variant]
        return self.launch


def _as_argv(value: Any, *, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise DescriptorError(f"{where}: 'launch' must be a list of strings")
    return tuple(value)


def _parse_descriptor(path: Path) -> EngineDescriptor:
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise DescriptorError(f"Cannot read engine descriptor {path}: {exc}") from exc

    name = data.get("name") or path.stem
    if not isinstance(name, str) or not name.strip():
        raise DescriptorError(f"{path}: 'name' must be a non-empty string")

    port = data.get("port")
    if not isinstance(port, int):
        raise DescriptorError(f"{path}: 'port' must be an integer")

    variants: dict[str, tuple[str, ...]] = {}
    raw_variants = data.get("variants") or {}
    if isinstance(raw_variants, dict):
        for variant_name, variant in raw_variants.items():
            if isinstance(variant, dict):
                variants[str(variant_name)] = _as_argv(
                    variant.get("launch"), where=f"{path}[variants.{variant_name}]"
                )

    return EngineDescriptor(
        name=name.strip().lower(),
        port=port,
        host=str(data.get("host", _DEFAULT_HOST)),
        launch=_as_argv(data.get("launch"), where=str(path)),
        cwd=str(data["cwd"]) if data.get("cwd") else None,
        install_hint=str(data.get("install_hint", "")),
        when_to_use=str(data.get("when_to_use", "")),
        default=bool(data.get("default", False)),
        variants=variants,
        source=str(path),
    )


def _descriptor_dirs() -> list[Path]:
    dirs = [_BUILTIN_DIR]
    override = os.environ.get("SOLIDMIND_ENGINES_D")
    dirs.append(Path(override).expanduser() if override else _USER_DIR)
    return dirs


def load_descriptors(*, refresh: bool = False) -> dict[str, EngineDescriptor]:
    """Load every descriptor, user files overriding core's by name.

    A broken descriptor is logged and skipped — one bad third-party file must
    not take the whole tool surface down with it.
    """
    global _cache
    if _cache is not None and not refresh:
        return _cache

    descriptors: dict[str, EngineDescriptor] = {}
    for directory in _descriptor_dirs():
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.toml")):
            try:
                descriptor = _parse_descriptor(path)
            except DescriptorError as exc:
                logger.error("Ignoring engine descriptor: %s", exc)
                continue
            descriptors[descriptor.name] = descriptor

    _cache = descriptors
    return descriptors


_cache: dict[str, EngineDescriptor] | None = None


def reset_cache() -> None:
    """Drop the descriptor cache (tests, and after a user edits engines.d)."""
    global _cache
    _cache = None


def engine_names() -> list[str]:
    """Every registered engine, sorted — the backend vocabulary."""
    return sorted(load_descriptors())


def default_engine() -> str:
    """The engine used when a caller names none.

    ``SOLIDMIND_DEFAULT_ENGINE`` wins, then the descriptor flagged
    ``default = true``, then the first registered name — so even this is data.
    """
    descriptors = load_descriptors()
    override = os.environ.get("SOLIDMIND_DEFAULT_ENGINE", "").strip().lower()
    if override in descriptors:
        return override
    for name in sorted(descriptors):
        if descriptors[name].default:
            return name
    return sorted(descriptors)[0] if descriptors else ""


def get_descriptor(name: str) -> EngineDescriptor | None:
    return load_descriptors().get(str(name).strip().lower())


def resolve_host(name: str) -> str:
    """Host for *name*: ``SOLIDMIND_SIM_HOST`` wins, then the descriptor."""
    override = os.environ.get("SOLIDMIND_SIM_HOST")
    if override:
        return override
    descriptor = get_descriptor(name)
    return descriptor.host if descriptor else _DEFAULT_HOST


def resolve_port(name: str) -> int:
    """Port for *name*: ``SOLIDMIND_<NAME>_PORT`` wins, then the descriptor."""
    env_key = f"SOLIDMIND_{str(name).strip().upper()}_PORT"
    raw = os.environ.get(env_key)
    if raw is not None:
        try:
            return int(raw)
        except ValueError:
            logger.warning("Invalid %s=%r, using the descriptor's port", env_key, raw)
    descriptor = get_descriptor(name)
    if descriptor is None:
        raise DescriptorError(f"No engine descriptor named {name!r}")
    return descriptor.port


def substitute(argv: tuple[str, ...], values: dict[str, str]) -> list[str]:
    """Expand ``${VAR}`` and ``${VAR:-default}`` in a launch command.

    *values* takes precedence over the environment, so ``${PORT}`` resolves to
    the port core actually allocated.  Unresolvable variables expand to an
    empty string, matching shell behaviour.
    """
    out: list[str] = []
    for token in argv:
        result = token
        while "${" in result:
            start = result.index("${")
            end = result.find("}", start)
            if end == -1:
                break
            expr = result[start + 2 : end]
            if ":-" in expr:
                var, _, default = expr.partition(":-")
            else:
                var, default = expr, ""
            value = values.get(var) or os.environ.get(var) or default
            result = result[:start] + value + result[end + 1 :]
        out.append(result)
    return out


def launch_argv(
    name: str,
    *,
    port: int,
    variant: str | None = None,
    extra: dict[str, str] | None = None,
) -> list[str] | None:
    """Fully-substituted argv for starting *name*, or None if attach-only."""
    descriptor = get_descriptor(name)
    if descriptor is None or descriptor.attach_only:
        return None
    values = {"PORT": str(port), "PYTHON": sys.executable, **(extra or {})}
    return substitute(descriptor.launch_command(variant), values)


def resolve_cwd(name: str) -> str:
    """Working directory for the launch command."""
    descriptor = get_descriptor(name)
    if descriptor is None or not descriptor.cwd:
        return str(_PROJECT_ROOT)
    return str(Path(descriptor.cwd).expanduser())


def install_hint(name: str) -> str:
    descriptor = get_descriptor(name)
    return descriptor.install_hint if descriptor else ""


def when_to_use(name: str) -> str:
    descriptor = get_descriptor(name)
    return descriptor.when_to_use if descriptor else ""
