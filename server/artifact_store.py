"""Content-addressed artifact store (CAS).

Every artifact is immutable and addressed by the sha256 of its stored bytes.
JSON artifacts are stored as their RFC 8785 (JCS) canonical bytes, so
``hash == sha256(file contents)`` always holds and the store is self-verifying
on read.

Layout under the root (default ``<repo>/artifacts``, override with the
``SOLIDMIND_ARTIFACTS_ROOT`` env var; every function also accepts ``root=``):

    store.json                   {"version": 1, "algo": "sha256_jcs_rfc8785"}
    objects/<2-char>/<sha256>    artifact bytes, sharded by first two hex chars
    refs/<namespace>/<name>.json {"hash": ..., "created_at": ..., "meta": {...}}
    lineage/<hash>.json          {"op", "inputs": {label: hash}, "env_identity_hash", "created_at"}
    tmp/                         staging area for atomic writes (same filesystem)

Mutable state (refs, lineage) lives beside the objects, never inside hashed
payloads — timestamps and free-text stay out of anything content-addressed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from server import jcs
from server.paths import data_path

STORE_VERSION = 1
STORE_ALGO = "sha256_jcs_rfc8785"

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


class ArtifactError(ValueError):
    pass


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def store_root(root: Path | None = None) -> Path:
    if root is not None:
        return root
    env = os.environ.get("SOLIDMIND_ARTIFACTS_ROOT", "")
    if env:
        return Path(env)
    return data_path("artifacts")


def _ensure_root(root: Path) -> None:
    (root / "objects").mkdir(parents=True, exist_ok=True)
    (root / "refs").mkdir(exist_ok=True)
    (root / "lineage").mkdir(exist_ok=True)
    (root / "tmp").mkdir(exist_ok=True)
    marker = root / "store.json"
    if not marker.exists():
        _atomic_write_bytes(
            marker,
            json.dumps({"version": STORE_VERSION, "algo": STORE_ALGO}).encode("utf-8"),
            root,
        )


def _atomic_write_bytes(dest: Path, data: bytes, root: Path) -> None:
    tmp_dir = root / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / f"{uuid.uuid4().hex}.tmp"
    tmp.write_bytes(data)
    os.replace(tmp, dest)


def _object_path(root: Path, hash_: str) -> Path:
    return root / "objects" / hash_[:2] / hash_


def _check_hash(hash_: str) -> str:
    if not _HEX64.match(hash_):
        raise ArtifactError(f"Not a sha256 hex digest: {hash_!r}")
    return hash_


def _check_name(name: str, what: str) -> str:
    if not _SAFE_NAME.match(name):
        raise ArtifactError(f"Unsafe {what}: {name!r}")
    return name


def put_bytes(data: bytes, *, root: Path | None = None) -> str:
    """Store raw bytes; return their sha256. Existing objects are a no-op."""
    r = store_root(root)
    _ensure_root(r)
    hash_ = hashlib.sha256(data).hexdigest()
    dest = _object_path(r, hash_)
    if dest.exists():
        return hash_
    dest.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_bytes(dest, data, r)
    return hash_


def put_json(obj: Any, *, root: Path | None = None) -> str:
    """Store a JSON-serializable object as its JCS canonical bytes."""
    canonical = jcs.canonicalize(obj)
    return put_bytes(canonical.encode("utf-8"), root=root)


def exists(hash_: str, *, root: Path | None = None) -> bool:
    return _object_path(store_root(root), _check_hash(hash_)).exists()


def get_bytes(hash_: str, *, root: Path | None = None) -> bytes:
    r = store_root(root)
    path = _object_path(r, _check_hash(hash_))
    if not path.exists():
        raise ArtifactError(f"Object not found: {hash_}")
    data = path.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if actual != hash_:
        raise ArtifactError(f"Object {hash_} is corrupt (content hashes to {actual})")
    return data


def get_json(hash_: str, *, root: Path | None = None) -> Any:
    return json.loads(get_bytes(hash_, root=root).decode("utf-8"))


def set_ref(
    namespace: str,
    name: str,
    hash_: str,
    *,
    meta: dict[str, Any] | None = None,
    root: Path | None = None,
) -> None:
    """Point a named ref at an object (last-writer-wins, atomic)."""
    r = store_root(root)
    _ensure_root(r)
    _check_hash(hash_)
    ns_dir = r / "refs" / _check_name(namespace, "ref namespace")
    ns_dir.mkdir(parents=True, exist_ok=True)
    record = {"hash": hash_, "created_at": _now_iso(), "meta": meta or {}}
    _atomic_write_bytes(
        ns_dir / f"{_check_name(name, 'ref name')}.json",
        json.dumps(record, indent=2).encode("utf-8"),
        r,
    )


def get_ref(namespace: str, name: str, *, root: Path | None = None) -> dict[str, Any] | None:
    path = (
        store_root(root)
        / "refs"
        / _check_name(namespace, "ref namespace")
        / f"{_check_name(name, 'ref name')}.json"
    )
    if not path.exists():
        return None
    return json.loads(path.read_text())


def list_refs(namespace: str, *, root: Path | None = None) -> list[dict[str, Any]]:
    """All refs in a namespace as [{name, hash, created_at, meta}], name-sorted."""
    ns_dir = store_root(root) / "refs" / _check_name(namespace, "ref namespace")
    if not ns_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(ns_dir.glob("*.json")):
        record = json.loads(path.read_text())
        record["name"] = path.stem
        out.append(record)
    return out


def record_lineage(
    out_hash: str,
    *,
    op: str,
    inputs: dict[str, str],
    env_identity_hash: str,
    root: Path | None = None,
) -> None:
    """Record how an object was produced: operation + labeled input hashes."""
    r = store_root(root)
    _ensure_root(r)
    _check_hash(out_hash)
    for input_hash in inputs.values():
        _check_hash(input_hash)
    record = {
        "op": op,
        "inputs": inputs,
        "env_identity_hash": env_identity_hash,
        "created_at": _now_iso(),
    }
    _atomic_write_bytes(
        r / "lineage" / f"{out_hash}.json",
        json.dumps(record, indent=2).encode("utf-8"),
        r,
    )


def get_lineage(hash_: str, *, root: Path | None = None) -> dict[str, Any] | None:
    path = store_root(root) / "lineage" / f"{_check_hash(hash_)}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def resolve(ref_or_hash: str, *, namespace: str = "dg_revisions", root: Path | None = None) -> str:
    """Resolve a name in ``refs/<namespace>/`` or pass a hex digest through."""
    if _HEX64.match(ref_or_hash):
        return ref_or_hash
    record = get_ref(namespace, ref_or_hash, root=root)
    if record is None:
        raise ArtifactError(f"Unknown ref {ref_or_hash!r} in namespace {namespace!r}")
    return record["hash"]
