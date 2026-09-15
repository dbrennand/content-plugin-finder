from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from content_plugin_finder.impact.content_index import ContentIndex
from content_plugin_finder.models import PluginKind

_CACHE_SCHEMA_VERSION = 1
_YAML_SUFFIXES = (".yml", ".yaml")


def cache_filename(collection_root: Path) -> str:
    """Return the cache JSON filename for a given collection root."""
    key = hashlib.sha256(str(collection_root.resolve()).encode()).hexdigest()[:16]
    return f"{key}.json"


def default_cache_path(collection_root: Path) -> Path:
    """Return XDG-compliant cache path unique to this collection."""
    xdg_cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return xdg_cache / "content-plugin-finder" / cache_filename(collection_root)


def compute_fingerprint(
    roots: list[Path],
    *,
    collection: str,
    depth: int,
    kinds: list[PluginKind],
) -> str:
    """SHA-256 fingerprint of call parameters + YAML file contents under roots."""
    h = hashlib.sha256()
    h.update(f"schema:{_CACHE_SCHEMA_VERSION}\n".encode())
    h.update(f"collection:{collection}\n".encode())
    h.update(f"depth:{depth}\n".encode())
    h.update(f"kinds:{','.join(sorted(k.value for k in kinds))}\n".encode())

    entries: list[tuple[str, str]] = []
    for root in roots:
        for dirpath, _, files in os.walk(str(root)):
            for fname in sorted(files):
                if not any(fname.endswith(s) for s in _YAML_SUFFIXES):
                    continue
                fpath = os.path.join(dirpath, fname)
                try:
                    content = Path(fpath).read_bytes()
                    file_hash = hashlib.sha256(content).hexdigest()
                except OSError:
                    file_hash = "unreadable"
                entries.append((fpath, file_hash))

    for fpath, file_hash in sorted(entries):
        h.update(f"{fpath}:{file_hash}\n".encode())

    return h.hexdigest()


def load_cached_index(cache_path: Path, fingerprint: str) -> ContentIndex | None:
    """Return cached ContentIndex if cache exists and fingerprint matches, else None."""
    if not cache_path.is_file():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if data.get("fingerprint") != fingerprint:
        return None
    try:
        return ContentIndex.from_dict(data["index"])
    except (KeyError, TypeError):
        return None


def save_cached_index(
    cache_path: Path, index: ContentIndex, fingerprint: str
) -> None:
    """Write index and fingerprint to cache_path as JSON."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"fingerprint": fingerprint, "index": index.to_dict()}
    cache_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
