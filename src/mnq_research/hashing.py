"""Deterministic fingerprints (SHA-256 hashes) for configurations and data.

A hash is a short "fingerprint" of content: change a single character of the
content and the fingerprint changes completely. Recording the fingerprint of
the rule file and data used by every experiment makes it impossible to change
rules or data after seeing results without the change being detectable.

Two kinds of hash are provided:

* **Canonical config hash** - hashes the *meaning* of a YAML file, not its
  formatting. Key order, indentation, quoting style and comments do not
  change it; any change to a key or value does. Values keep their type, so
  ``1`` (integer) and ``1.0`` (decimal) and ``"1"`` (text) hash differently.
* **File hash** - hashes the exact bytes of a file. Used for data files,
  where every byte matters.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from mnq_research.config import load_yaml

_CHUNK = 1024 * 1024


def _json_default(value: Any) -> str:
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    raise TypeError(f"Cannot canonicalise value of type {type(value).__name__}: {value!r}")


def canonical_json(obj: Any) -> str:
    """Serialise ``obj`` to a single canonical JSON string (sorted keys, no whitespace)."""
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=_json_default,
    )


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_object(obj: Any) -> str:
    """Canonical SHA-256 of a parsed configuration object."""
    return sha256_text(canonical_json(obj))


def hash_config_file(path: str | Path) -> str:
    """Canonical SHA-256 of a YAML file's content (formatting-insensitive)."""
    return hash_object(load_yaml(path))


def hash_file_bytes(path: str | Path) -> str:
    """SHA-256 of a file's exact bytes."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_dataframe(df: pd.DataFrame) -> str:
    """Content hash of a DataFrame (column names, order and values).

    Uses a plain CSV rendering with ISO timestamps so the fingerprint does not
    depend on the storage format (Parquet vs CSV) of the same data.
    """
    rendered = df.to_csv(index=False, lineterminator="\n", date_format="%Y-%m-%dT%H:%M:%S%z")
    return sha256_text(rendered)


def build_manifest(paths: Iterable[str | Path], root: str | Path | None = None) -> dict[str, Any]:
    """Build a data manifest: every file's relative path, size and byte hash.

    The manifest hash covers the sorted file list, so it does not depend on the
    order in which files were supplied, but changes if any file is added,
    removed, renamed or modified.
    """
    root_path = Path(root).resolve() if root is not None else None
    entries = []
    for raw in paths:
        path = Path(raw).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Not a file: {path}")
        name = path.relative_to(root_path).as_posix() if root_path else path.name
        entries.append({"path": name, "size_bytes": path.stat().st_size, "sha256": hash_file_bytes(path)})
    entries.sort(key=lambda e: e["path"])
    names = [e["path"] for e in entries]
    if len(names) != len(set(names)):
        raise ValueError("Manifest contains two files with the same relative path.")
    return {"files": entries, "manifest_sha256": hash_object(entries)}


def build_directory_manifest(directory: str | Path) -> dict[str, Any]:
    """Manifest of every non-hidden file under ``directory`` (recursively)."""
    directory = Path(directory)
    files = [
        p
        for p in directory.rglob("*")
        if p.is_file() and not any(part.startswith(".") for part in p.relative_to(directory).parts)
    ]
    return build_manifest(files, root=directory)
