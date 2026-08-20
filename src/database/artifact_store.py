from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional


MARKER = "__agentquant_external_artifact__"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INLINE_MAX_BYTES = 4096


@dataclass(frozen=True)
class ExternalizedValue:
    inline_value: Optional[str]
    artifact_path: Optional[str]
    sha256: Optional[str]
    size_bytes: Optional[int]
    summary_json: Optional[str]


@dataclass(frozen=True)
class _OriginalArtifactState:
    existed: bool
    data: Optional[bytes]


class _ArtifactWriteTransaction:
    def __init__(self) -> None:
        self._originals: dict[Path, _OriginalArtifactState] = {}
        self._created_directories: set[Path] = set()

    def write_bytes(self, path: Path, data: bytes) -> None:
        resolved = path.resolve()
        if resolved not in self._originals:
            self._originals[resolved] = _OriginalArtifactState(
                existed=resolved.exists(),
                data=resolved.read_bytes() if resolved.exists() else None,
            )
        self._remember_missing_directories(resolved.parent)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        _replace_file_bytes(resolved, data)

    def rollback(self) -> None:
        for path, original in reversed(tuple(self._originals.items())):
            if original.existed:
                path.parent.mkdir(parents=True, exist_ok=True)
                _replace_file_bytes(path, original.data or b"")
            elif path.exists():
                path.unlink()
        for directory in sorted(self._created_directories, key=lambda item: len(item.parts), reverse=True):
            try:
                directory.rmdir()
            except (FileNotFoundError, OSError):
                pass

    def _remember_missing_directories(self, directory: Path) -> None:
        current = directory
        while not current.exists() and current.parent != current:
            self._created_directories.add(current)
            current = current.parent


_ACTIVE_ARTIFACT_WRITE_TRANSACTION: ContextVar[Optional[_ArtifactWriteTransaction]] = ContextVar(
    "agentquant_artifact_write_transaction",
    default=None,
)


def _replace_file_bytes(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_artifact_bytes(path: str | Path, data: bytes) -> None:
    target = Path(path)
    transaction = _ACTIVE_ARTIFACT_WRITE_TRANSACTION.get()
    if transaction is not None:
        transaction.write_bytes(target, data)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    _replace_file_bytes(target, data)


def write_artifact_text(path: str | Path, value: str, *, encoding: str = "utf-8") -> None:
    write_artifact_bytes(path, str(value).encode(encoding))


@contextmanager
def artifact_write_transaction() -> Iterator[None]:
    if _ACTIVE_ARTIFACT_WRITE_TRANSACTION.get() is not None:
        raise RuntimeError("nested_artifact_write_transaction_not_supported")
    transaction = _ArtifactWriteTransaction()
    token = _ACTIVE_ARTIFACT_WRITE_TRANSACTION.set(transaction)
    try:
        yield
    except BaseException:
        transaction.rollback()
        raise
    finally:
        _ACTIVE_ARTIFACT_WRITE_TRANSACTION.reset(token)


def _artifact_root() -> Path:
    configured = os.getenv("AGENTQUANT_ARTIFACT_ROOT")
    if configured:
        path = Path(configured)
        return path if path.is_absolute() else PROJECT_ROOT / path
    return SRC_ROOT / "logs" / "artifacts"


def _inline_max_bytes(value: Optional[int]) -> int:
    if value is not None:
        return int(value)
    return int(os.getenv("AGENTQUANT_ARTIFACT_INLINE_MAX_BYTES", str(DEFAULT_INLINE_MAX_BYTES)))


def _json_default(value: Any) -> str:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def dumps_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=_json_default)


def _slug(value: Any, default: str = "unknown") -> str:
    text = str(value or default)
    text = re.sub(r"[^A-Za-z0-9_.=-]+", "_", text).strip("_")
    return (text or default)[:96]


def _project_relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT.resolve())).replace("\\", "/")
    except ValueError:
        return str(path.resolve())


def _resolve_path(path_value: str | Path | None) -> Optional[Path]:
    if not path_value:
        return None
    path = Path(str(path_value))
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def _digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def summarize_payload(value: Any) -> dict:
    if isinstance(value, dict):
        primitive_preview = {}
        for key, item in list(value.items())[:24]:
            if isinstance(item, (str, int, float, bool)) or item is None:
                primitive_preview[str(key)] = item
            elif isinstance(item, dict):
                primitive_preview[str(key)] = {
                    "type": "dict",
                    "keys": [str(child_key) for child_key in list(item.keys())[:16]],
                    "len": len(item),
                }
            elif isinstance(item, list):
                primitive_preview[str(key)] = {"type": "list", "len": len(item)}
            else:
                primitive_preview[str(key)] = {"type": type(item).__name__}
        return {
            "type": "dict",
            "keys": [str(key) for key in list(value.keys())[:48]],
            "len": len(value),
            "preview": primitive_preview,
        }
    if isinstance(value, list):
        return {
            "type": "list",
            "len": len(value),
            "item_types": sorted({type(item).__name__ for item in value[:24]}),
        }
    if isinstance(value, str):
        return {"type": "text", "chars": len(value), "preview": value[:500]}
    return {"type": type(value).__name__, "preview": str(value)[:500]}


def _artifact_file_path(
    *,
    category: str,
    record_id: str,
    field_name: str,
    config_id: Optional[str] = None,
    trading_date: Optional[str] = None,
    extension: str = ".json",
) -> Path:
    root = _artifact_root()
    return (
        root
        / _slug(config_id, "no_config")
        / _slug(trading_date, "no_date")
        / _slug(category, "artifact")
        / f"{_slug(record_id, 'record')}_{_slug(field_name, 'field')}{extension}"
    )


def _stub(*, artifact_path: str, sha256: str, size_bytes: int, summary: dict) -> str:
    return dumps_json(
        {
            MARKER: True,
            "artifact_path": artifact_path,
            "sha256": sha256,
            "size_bytes": size_bytes,
            "summary": summary,
        }
    )


def externalize_json_for_db(
    value: Any,
    *,
    category: str,
    record_id: str,
    field_name: str,
    config_id: Optional[str] = None,
    trading_date: Optional[str] = None,
    inline_max_bytes: Optional[int] = None,
) -> ExternalizedValue:
    if value is None:
        return ExternalizedValue(None, None, None, None, None)

    text = dumps_json(value)
    data = text.encode("utf-8")
    size_bytes = len(data)
    if size_bytes <= _inline_max_bytes(inline_max_bytes):
        return ExternalizedValue(text, None, None, size_bytes, None)

    path = _artifact_file_path(
        category=category,
        record_id=record_id,
        field_name=field_name,
        config_id=config_id,
        trading_date=trading_date,
        extension=".json",
    )
    write_artifact_bytes(path, data)
    sha256 = _digest_bytes(data)
    rel_path = _project_relative(path)
    summary = summarize_payload(value)
    return ExternalizedValue(
        _stub(artifact_path=rel_path, sha256=sha256, size_bytes=size_bytes, summary=summary),
        rel_path,
        sha256,
        size_bytes,
        dumps_json(summary),
    )


def externalize_text_for_db(
    value: Any,
    *,
    category: str,
    record_id: str,
    field_name: str,
    config_id: Optional[str] = None,
    trading_date: Optional[str] = None,
    inline_max_bytes: Optional[int] = None,
) -> ExternalizedValue:
    if value is None:
        return ExternalizedValue(None, None, None, None, None)

    text = str(value)
    data = text.encode("utf-8")
    size_bytes = len(data)
    if size_bytes <= _inline_max_bytes(inline_max_bytes):
        return ExternalizedValue(text, None, None, size_bytes, None)

    path = _artifact_file_path(
        category=category,
        record_id=record_id,
        field_name=field_name,
        config_id=config_id,
        trading_date=trading_date,
        extension=".txt",
    )
    write_artifact_bytes(path, data)
    sha256 = _digest_bytes(data)
    rel_path = _project_relative(path)
    summary = summarize_payload(text)
    return ExternalizedValue(
        f"[agent-future-trading external artifact] path={rel_path} sha256={sha256} size_bytes={size_bytes}",
        rel_path,
        sha256,
        size_bytes,
        dumps_json(summary),
    )


def _read_artifact_text(path_value: str | Path | None, expected_sha256: str | None = None) -> Optional[str]:
    path = _resolve_path(path_value)
    if not path or not path.exists():
        return None
    data = path.read_bytes()
    if expected_sha256 and _digest_bytes(data) != expected_sha256:
        return None
    return data.decode("utf-8")


def load_externalized_json(
    inline_value: Any,
    artifact_path: str | None = None,
    expected_sha256: str | None = None,
) -> Any:
    if artifact_path:
        text = _read_artifact_text(artifact_path, expected_sha256)
        if text is not None:
            try:
                return json.loads(text)
            except Exception:
                return text

    if inline_value is None or isinstance(inline_value, (dict, list)):
        value = inline_value
    else:
        try:
            value = json.loads(inline_value)
        except Exception:
            return inline_value

    if isinstance(value, dict) and value.get(MARKER):
        text = _read_artifact_text(value.get("artifact_path"), value.get("sha256"))
        if text is not None:
            try:
                return json.loads(text)
            except Exception:
                return text
    return value


def load_externalized_text(
    inline_value: Any,
    artifact_path: str | None = None,
    expected_sha256: str | None = None,
) -> Optional[str]:
    if artifact_path:
        text = _read_artifact_text(artifact_path, expected_sha256)
        if text is not None:
            return text
    return None if inline_value is None else str(inline_value)
