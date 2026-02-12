from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import yaml


DEFAULT_MANIFEST = Path("me_knowledge/corpus/manifest.yml")
DEFAULT_RAW_DIR = Path("me_knowledge/corpus/raw")
DEFAULT_TEXT_DIR = Path("me_knowledge/corpus/text")
DEFAULT_INDEX = Path("me_knowledge/corpus/index.json")


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        piece = data.strip()
        if piece:
            self._chunks.append(piece)

    def get_text(self) -> str:
        return "\n".join(self._chunks)


@dataclass
class SyncResult:
    id: str
    status: str
    reason: str | None = None
    raw_path: str | None = None
    text_path: str | None = None
    sha256: str | None = None
    bytes: int | None = None
    source_url: str | None = None


def _load_manifest(path: Path) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Manifest must be a mapping: {path}")
    resources = data.get("resources")
    if not isinstance(resources, list):
        raise ValueError(f"Manifest missing resources list: {path}")
    out: list[dict[str, Any]] = []
    for idx, item in enumerate(resources):
        if not isinstance(item, dict):
            raise ValueError(f"Manifest resources[{idx}] must be a mapping")
        out.append(item)
    return out


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fetch(url: str, timeout_seconds: int = 90) -> tuple[bytes, str, str]:
    req = Request(url, headers={"User-Agent": "solidmind-cad-me-corpus-sync/1.0"})
    with urlopen(req, timeout=timeout_seconds) as resp:  # noqa: S310
        body = resp.read()
        content_type = resp.headers.get_content_type() or "application/octet-stream"
        final_url = resp.geturl()
    return body, content_type, final_url


def _extract_html_to_text(data: bytes) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(data.decode("utf-8", errors="ignore"))
    return parser.get_text()


def _run_pdftotext(pdf_path: Path, txt_path: Path) -> bool:
    cmd = ["pdftotext", "-layout", "-nopgbrk", str(pdf_path), str(txt_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode == 0


def _extract_zip_to_text(zip_path: Path, text_path: Path) -> bool:
    chunks: list[str] = []
    with tempfile.TemporaryDirectory(prefix="me_zip_extract_") as tmp:
        tmp_dir = Path(tmp)
        with zipfile.ZipFile(zip_path) as zf:
            for member in zf.infolist():
                if member.is_dir():
                    continue
                member_path = Path(member.filename)
                ext = member_path.suffix.lower()
                if ext not in {".txt", ".md", ".rst", ".yaml", ".yml", ".json", ".csv", ".html", ".htm", ".pdf", ".inp", ".py", ".f", ".f90"}:
                    continue
                payload = zf.read(member)
                header = f"\n\n=== {member.filename} ===\n"

                if ext in {".html", ".htm"}:
                    chunks.append(header + _extract_html_to_text(payload))
                    continue

                if ext == ".pdf":
                    local_pdf = tmp_dir / member_path.name
                    local_txt = tmp_dir / f"{member_path.stem}.txt"
                    local_pdf.write_bytes(payload)
                    if _run_pdftotext(local_pdf, local_txt):
                        chunks.append(header + local_txt.read_text(encoding="utf-8", errors="ignore"))
                    continue

                chunks.append(header + payload.decode("utf-8", errors="ignore"))

    if not chunks:
        return False

    text_path.write_text("\n".join(chunks), encoding="utf-8")
    return True


def _determine_target_filename(resource: dict[str, Any]) -> str:
    explicit = resource.get("filename")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    url = resource.get("url")
    if isinstance(url, str):
        path = Path(urlparse(url).path)
        if path.name:
            return path.name
    return f"{resource.get('id', 'resource')}.bin"


def _process_resource(
    resource: dict[str, Any],
    raw_dir: Path,
    text_dir: Path,
    force: bool,
    timeout_seconds: int,
) -> SyncResult:
    resource_id = str(resource.get("id", "unknown"))
    access = str(resource.get("access", "public"))

    if access != "public":
        return SyncResult(
            id=resource_id,
            status="skipped",
            reason=f"access={access}",
            source_url=resource.get("source_url") or resource.get("url"),
        )

    url = resource.get("url")
    if not isinstance(url, str) or not url.strip():
        return SyncResult(id=resource_id, status="failed", reason="missing url")

    filename = _determine_target_filename(resource)
    raw_path = raw_dir / filename
    text_path = text_dir / f"{Path(filename).stem}.txt"

    if raw_path.exists() and text_path.exists() and not force:
        return SyncResult(
            id=resource_id,
            status="up_to_date",
            raw_path=str(raw_path),
            text_path=str(text_path),
            source_url=url,
        )

    try:
        payload, _content_type, final_url = _fetch(url, timeout_seconds=timeout_seconds)
    except (URLError, HTTPError, TimeoutError) as exc:
        return SyncResult(id=resource_id, status="failed", reason=str(exc), source_url=url)

    raw_path.write_bytes(payload)
    sha256 = _sha256_bytes(payload)
    ext = raw_path.suffix.lower()

    extracted = False
    if ext == ".pdf":
        extracted = _run_pdftotext(raw_path, text_path)
    elif ext in {".html", ".htm"}:
        text_path.write_text(_extract_html_to_text(payload), encoding="utf-8")
        extracted = True
    elif ext == ".zip":
        extracted = _extract_zip_to_text(raw_path, text_path)
    else:
        text_path.write_text(payload.decode("utf-8", errors="ignore"), encoding="utf-8")
        extracted = True

    if not extracted:
        return SyncResult(
            id=resource_id,
            status="partial",
            reason="downloaded but text extraction failed",
            raw_path=str(raw_path),
            sha256=sha256,
            bytes=len(payload),
            source_url=final_url,
        )

    return SyncResult(
        id=resource_id,
        status="downloaded",
        raw_path=str(raw_path),
        text_path=str(text_path),
        sha256=sha256,
        bytes=len(payload),
        source_url=final_url,
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Sync ME corpus seed resources and extract searchable text.")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--text-dir", type=Path, default=DEFAULT_TEXT_DIR)
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--force", action="store_true", help="Redownload and re-extract all public resources.")
    parser.add_argument("--timeout", type=int, default=90, help="HTTP timeout per resource in seconds.")
    parser.add_argument("--id", dest="ids", action="append", default=[], help="Restrict sync to one or more resource IDs.")
    args = parser.parse_args(argv)

    resources = _load_manifest(args.manifest)
    wanted_ids = set(args.ids)
    if wanted_ids:
        resources = [r for r in resources if str(r.get("id")) in wanted_ids]

    args.raw_dir.mkdir(parents=True, exist_ok=True)
    args.text_dir.mkdir(parents=True, exist_ok=True)
    args.index.parent.mkdir(parents=True, exist_ok=True)

    results: list[SyncResult] = []
    for resource in resources:
        result = _process_resource(
            resource=resource,
            raw_dir=args.raw_dir,
            text_dir=args.text_dir,
            force=args.force,
            timeout_seconds=args.timeout,
        )
        results.append(result)
        reason = f" ({result.reason})" if result.reason else ""
        print(f"[{result.status}] {result.id}{reason}")

    summary = {
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "manifest": str(args.manifest),
        "results": [r.__dict__ for r in results],
        "counts": {
            "downloaded": sum(1 for r in results if r.status == "downloaded"),
            "up_to_date": sum(1 for r in results if r.status == "up_to_date"),
            "partial": sum(1 for r in results if r.status == "partial"),
            "skipped": sum(1 for r in results if r.status == "skipped"),
            "failed": sum(1 for r in results if r.status == "failed"),
        },
    }
    args.index.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")

    print("\\nSummary:")
    for key, value in summary["counts"].items():
        print(f"  {key}: {value}")
    print(f"  index: {args.index}")

    return 1 if summary["counts"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
