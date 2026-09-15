# Content Index Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cache the `ContentIndex` between `--impact` invocations so that large collections (160+ targets) complete in milliseconds instead of minutes.

**Architecture:** `compute_impact()` checks an XDG-compliant JSON cache file before calling `build_content_index()`. The cache is keyed by a SHA-256 fingerprint of: all YAML file contents in the scan roots, plus the `collection`, `depth`, `kinds`, and schema version constants. On a cache miss, the index is built normally and then saved. Three new CLI flags (`--no-cache`, `--clear-cache`, `--cache-dir`) give users control.

**Tech Stack:** Python 3.11+, `hashlib`, `json`, `os`, `pathlib`. No new dependencies.

**Spec:** `docs/superpowers/plans/2026-09-15-content-index-cache.md` (this document)

## Global Constraints

- Python ≥ 3.11 (project minimum from `pyproject.toml`)
- No new runtime dependencies — use stdlib only (`hashlib`, `json`, `os`, `pathlib`)
- Follow existing test pattern: use `tmp_path` pytest fixture, build mini-collections inline, no mocking
- Run tests with: `pytest tests/ -v`
- Cache schema version constant `_CACHE_SCHEMA_VERSION = 1` — increment manually when serialization format changes

---

### Task 1: Add `to_dict()` / `from_dict()` to `ContentIndex`

**Files:**
- Modify: `src/content_plugin_finder/impact/content_index.py` (the `ContentIndex` dataclass, lines 13–22)
- Test: `tests/test_cache.py` (create new file)

**Interfaces:**
- Produces: `ContentIndex.to_dict() -> dict` and `ContentIndex.from_dict(data: dict) -> ContentIndex` used by Task 2

- [ ] **Step 1: Create `tests/test_cache.py` with a failing roundtrip test**

```python
from content_plugin_finder.impact.content_index import ContentIndex


def test_content_index_roundtrip():
    index = ContentIndex(
        collection="acme.widgets",
        plugin_to_roots={"thing": ["tests/integration/targets/thing_test"]},
        root_kinds={"tests/integration/targets/thing_test": "integration"},
        roots={"/abs/tests/integration/targets/thing_test": "tests/integration/targets/thing_test"},
    )
    data = index.to_dict()
    restored = ContentIndex.from_dict(data)
    assert restored.collection == index.collection
    assert restored.plugin_to_roots == index.plugin_to_roots
    assert restored.root_kinds == index.root_kinds
    assert restored.roots == index.roots
```

- [ ] **Step 2: Run test, verify it fails**

```bash
pytest tests/test_cache.py::test_content_index_roundtrip -v
```

Expected: `AttributeError: 'ContentIndex' object has no attribute 'to_dict'`

- [ ] **Step 3: Add `to_dict()` and `from_dict()` to `ContentIndex`**

In `src/content_plugin_finder/impact/content_index.py`, add these two methods to the `ContentIndex` dataclass (after the existing field declarations at line 22):

```python
    def to_dict(self) -> dict:
        return {
            "collection": self.collection,
            "plugin_to_roots": self.plugin_to_roots,
            "root_kinds": self.root_kinds,
            "roots": self.roots,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ContentIndex":
        return cls(
            collection=data["collection"],
            plugin_to_roots=data["plugin_to_roots"],
            root_kinds=data["root_kinds"],
            roots=data["roots"],
        )
```

- [ ] **Step 4: Run test, verify it passes**

```bash
pytest tests/test_cache.py::test_content_index_roundtrip -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/content_plugin_finder/impact/content_index.py tests/test_cache.py
git commit -m "feat(cache): add ContentIndex.to_dict/from_dict for serialization"
```

---

### Task 2: Create `cache.py` — fingerprint, load, save

**Files:**
- Create: `src/content_plugin_finder/impact/cache.py`
- Test: `tests/test_cache.py`

**Interfaces:**
- Consumes: `ContentIndex.to_dict()` / `ContentIndex.from_dict()` from Task 1
- Produces (used by Task 3):
  - `default_cache_path(collection_root: Path) -> Path`
  - `compute_fingerprint(roots: list[Path], *, collection: str, depth: int, kinds: list[PluginKind]) -> str`
  - `load_cached_index(cache_path: Path, fingerprint: str) -> ContentIndex | None`
  - `save_cached_index(cache_path: Path, index: ContentIndex, fingerprint: str) -> None`

- [ ] **Step 1: Add tests for `default_cache_path`, `compute_fingerprint`, `load_cached_index`, `save_cached_index`**

Append to `tests/test_cache.py`:

```python
import os
from pathlib import Path

from content_plugin_finder.impact.cache import (
    compute_fingerprint,
    default_cache_path,
    load_cached_index,
    save_cached_index,
)
from content_plugin_finder.models import PluginKind


def test_default_cache_path_is_under_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    collection = tmp_path / "my_collection"
    p = default_cache_path(collection)
    assert str(tmp_path) in str(p)
    assert p.suffix == ".json"


def test_compute_fingerprint_stable(tmp_path):
    # Two calls with same roots/args produce same fingerprint
    yaml_file = tmp_path / "tasks" / "main.yml"
    yaml_file.parent.mkdir()
    yaml_file.write_text("- name: hello\n  debug:\n", encoding="utf-8")

    fp1 = compute_fingerprint(
        [tmp_path], collection="acme.widgets", depth=4, kinds=list(PluginKind)
    )
    fp2 = compute_fingerprint(
        [tmp_path], collection="acme.widgets", depth=4, kinds=list(PluginKind)
    )
    assert fp1 == fp2


def test_compute_fingerprint_changes_on_file_content(tmp_path):
    yaml_file = tmp_path / "tasks" / "main.yml"
    yaml_file.parent.mkdir()
    yaml_file.write_text("- name: hello\n  debug:\n", encoding="utf-8")
    fp1 = compute_fingerprint(
        [tmp_path], collection="acme.widgets", depth=4, kinds=list(PluginKind)
    )
    yaml_file.write_text("- name: changed\n  debug:\n", encoding="utf-8")
    fp2 = compute_fingerprint(
        [tmp_path], collection="acme.widgets", depth=4, kinds=list(PluginKind)
    )
    assert fp1 != fp2


def test_compute_fingerprint_changes_on_depth(tmp_path):
    fp1 = compute_fingerprint(
        [tmp_path], collection="acme.widgets", depth=4, kinds=list(PluginKind)
    )
    fp2 = compute_fingerprint(
        [tmp_path], collection="acme.widgets", depth=2, kinds=list(PluginKind)
    )
    assert fp1 != fp2


def test_compute_fingerprint_changes_on_collection(tmp_path):
    fp1 = compute_fingerprint(
        [tmp_path], collection="acme.widgets", depth=4, kinds=list(PluginKind)
    )
    fp2 = compute_fingerprint(
        [tmp_path], collection="other.coll", depth=4, kinds=list(PluginKind)
    )
    assert fp1 != fp2


def test_load_cached_index_returns_none_when_missing(tmp_path):
    result = load_cached_index(tmp_path / "missing.json", "somefingerprint")
    assert result is None


def test_save_and_load_roundtrip(tmp_path):
    index = ContentIndex(
        collection="acme.widgets",
        plugin_to_roots={"thing": ["tests/integration/targets/thing_test"]},
        root_kinds={"tests/integration/targets/thing_test": "integration"},
        roots={"/abs/path": "tests/integration/targets/thing_test"},
    )
    fingerprint = "abc123"
    cache_path = tmp_path / "cache.json"

    save_cached_index(cache_path, index, fingerprint)
    loaded = load_cached_index(cache_path, fingerprint)

    assert loaded is not None
    assert loaded.collection == index.collection
    assert loaded.plugin_to_roots == index.plugin_to_roots


def test_load_returns_none_on_stale_fingerprint(tmp_path):
    index = ContentIndex(collection="acme.widgets")
    cache_path = tmp_path / "cache.json"
    save_cached_index(cache_path, index, "fingerprint-v1")
    result = load_cached_index(cache_path, "fingerprint-v2")
    assert result is None


def test_load_returns_none_on_corrupt_json(tmp_path):
    cache_path = tmp_path / "cache.json"
    cache_path.write_text("not valid json{{{", encoding="utf-8")
    result = load_cached_index(cache_path, "fp")
    assert result is None
```

- [ ] **Step 2: Run tests, verify they fail**

```bash
pytest tests/test_cache.py -v -k "not roundtrip"
```

Expected: `ModuleNotFoundError: No module named 'content_plugin_finder.impact.cache'`

- [ ] **Step 3: Create `src/content_plugin_finder/impact/cache.py`**

```python
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from content_plugin_finder.impact.content_index import ContentIndex
from content_plugin_finder.models import PluginKind

_CACHE_SCHEMA_VERSION = 1
_YAML_SUFFIXES = (".yml", ".yaml")


def default_cache_path(collection_root: Path) -> Path:
    """Return XDG-compliant cache path unique to this collection."""
    xdg_cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    key = hashlib.sha256(str(collection_root.resolve()).encode()).hexdigest()[:16]
    return xdg_cache / "content-plugin-finder" / f"{key}.json"


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
```

- [ ] **Step 4: Run tests, verify they pass**

```bash
pytest tests/test_cache.py -v
```

Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/content_plugin_finder/impact/cache.py tests/test_cache.py
git commit -m "feat(cache): add cache module with fingerprint, load, save"
```

---

### Task 3: Wire cache into `compute_impact()`

**Files:**
- Modify: `src/content_plugin_finder/impact/engine.py` (`compute_impact` function, lines 134–152)
- Test: `tests/test_cache.py`

**Interfaces:**
- Consumes: `default_cache_path`, `compute_fingerprint`, `load_cached_index`, `save_cached_index` from Task 2
- Consumes: `discover_scan_roots` already imported in `engine.py`
- Produces: `compute_impact()` with two new keyword-only params: `cache_path: Path | None = None`, `no_cache: bool = False`

- [ ] **Step 1: Add integration test for cache wiring in `tests/test_cache.py`**

This test verifies that `compute_impact()` reads from and writes to the cache. It reuses the `_mini_collection` helper pattern from `tests/test_impact.py`.

```python
from content_plugin_finder.collection.graph import build_collection_graph
from content_plugin_finder.impact.engine import compute_impact


def _mini_collection_for_cache(tmp_path: Path) -> Path:
    """Minimal collection with one module and one integration target."""
    (tmp_path / "galaxy.yml").write_text(
        "namespace: acme\nname: widgets\n", encoding="utf-8"
    )
    modules = tmp_path / "plugins" / "modules"
    modules.mkdir(parents=True)
    (modules / "thing.py").write_text(
        'DOCUMENTATION = """\nmodule: thing\n"""\n', encoding="utf-8"
    )
    target = tmp_path / "tests" / "integration" / "targets" / "thing_test"
    target.mkdir(parents=True)
    (target / "aliases").write_text("thing\n", encoding="utf-8")
    (target / "tasks").mkdir()
    (target / "tasks" / "main.yml").write_text(
        "- acme.widgets.thing:\n    name: y\n", encoding="utf-8"
    )
    return tmp_path


def test_compute_impact_writes_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    collection = _mini_collection_for_cache(tmp_path / "col")
    compute_impact(
        collection_root=collection,
        changed_files=["plugins/modules/thing.py"],
    )
    cache_dir = tmp_path / "cache" / "content-plugin-finder"
    assert any(cache_dir.glob("*.json")), "cache file should have been written"


def test_compute_impact_uses_cache_on_second_call(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    collection = _mini_collection_for_cache(tmp_path / "col")
    # First call builds and caches
    report1 = compute_impact(
        collection_root=collection,
        changed_files=["plugins/modules/thing.py"],
    )
    # Second call should hit cache — corrupt the source so we know it's not re-scanned
    (collection / "plugins" / "modules" / "thing.py").write_text(
        "# emptied\n", encoding="utf-8"
    )
    report2 = compute_impact(
        collection_root=collection,
        changed_files=["plugins/modules/thing.py"],
    )
    assert report2.integration_targets == report1.integration_targets


def test_compute_impact_no_cache_skips_read_and_write(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    collection = _mini_collection_for_cache(tmp_path / "col")
    compute_impact(
        collection_root=collection,
        changed_files=["plugins/modules/thing.py"],
        no_cache=True,
    )
    cache_dir = tmp_path / "cache" / "content-plugin-finder"
    assert not any(cache_dir.glob("*.json")), "no_cache=True should not write cache"
```

- [ ] **Step 2: Run new tests, verify they fail**

```bash
pytest tests/test_cache.py::test_compute_impact_writes_cache tests/test_cache.py::test_compute_impact_uses_cache_on_second_call tests/test_cache.py::test_compute_impact_no_cache_skips_read_and_write -v
```

Expected: `TypeError: compute_impact() got an unexpected keyword argument 'no_cache'`

- [ ] **Step 3: Update `compute_impact()` in `engine.py`**

Add these imports at the top of `engine.py` (after existing imports):

```python
from content_plugin_finder.impact.cache import (
    compute_fingerprint,
    default_cache_path,
    load_cached_index,
    save_cached_index,
)
```

Change the `compute_impact` signature to add two new optional keyword params:

```python
def compute_impact(
    *,
    collection_root: Path,
    changed_files: list[str],
    parent: Path | None = None,
    depth: int = 4,
    graph: CollectionGraph | None = None,
    content_index: ContentIndex | None = None,
    cache_path: Path | None = None,
    no_cache: bool = False,
) -> ImpactReport:
```

Replace the existing `content_index = content_index or build_content_index(...)` block (lines ~146–152) with:

```python
    if content_index is None:
        if no_cache:
            content_index = build_content_index(
                parent,
                collection=graph.collection,
                depth=depth,
                kinds=list(PluginKind),
            )
        else:
            _roots = discover_scan_roots(parent, depth)
            _fp = compute_fingerprint(
                _roots,
                collection=graph.collection,
                depth=depth,
                kinds=list(PluginKind),
            )
            _cache_path = cache_path or default_cache_path(collection_root)
            content_index = load_cached_index(_cache_path, _fp)
            if content_index is None:
                content_index = build_content_index(
                    parent,
                    collection=graph.collection,
                    depth=depth,
                    kinds=list(PluginKind),
                )
                save_cached_index(_cache_path, content_index, _fp)
```

- [ ] **Step 4: Run all cache tests, verify they pass**

```bash
pytest tests/test_cache.py -v
```

Expected: all PASS

- [ ] **Step 5: Run full test suite to check for regressions**

```bash
pytest tests/ -v
```

Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add src/content_plugin_finder/impact/engine.py tests/test_cache.py
git commit -m "feat(cache): wire ContentIndex cache into compute_impact"
```

---

### Task 4: Add `--no-cache`, `--clear-cache`, `--cache-dir` CLI flags

**Files:**
- Modify: `src/content_plugin_finder/cli.py` (`build_parser()` and `_run_impact()`)
- Test: `tests/test_cli.py` (append)

**Interfaces:**
- Consumes: `default_cache_path` from Task 2's `cache.py`
- Consumes: `compute_impact(cache_path=..., no_cache=...)` from Task 3

- [ ] **Step 1: Add CLI tests to `tests/test_cli.py`**

Read `tests/test_cli.py` first to understand the existing test style, then append:

```python
def test_no_cache_flag_accepted(tmp_path):
    """--no-cache is a valid flag and produces output."""
    col = tmp_path / "col"
    _build_mini_collection(col)          # reuse whatever helper test_cli.py already uses
    result = main(["--impact", str(col), "--from-stdin", "--no-cache"], stdin_lines=[])
    assert result == 0


def test_clear_cache_flag_accepted(tmp_path, monkeypatch):
    """--clear-cache deletes the cache file and exits cleanly."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    col = tmp_path / "col"
    _build_mini_collection(col)
    # First run: populate cache
    main(["--impact", str(col), "--from-stdin"], stdin_lines=[])
    cache_dir = tmp_path / "xdg" / "content-plugin-finder"
    assert any(cache_dir.glob("*.json"))
    # --clear-cache: cache file removed, rebuild succeeds
    result = main(["--impact", str(col), "--from-stdin", "--clear-cache"], stdin_lines=[])
    assert result == 0


def test_cache_dir_override(tmp_path):
    """--cache-dir places the cache in the given directory."""
    col = tmp_path / "col"
    custom = tmp_path / "custom_cache"
    _build_mini_collection(col)
    main(["--impact", str(col), "--from-stdin", "--cache-dir", str(custom)], stdin_lines=[])
    assert any(custom.glob("*.json"))
```

Note: check `tests/test_cli.py` for the existing `_build_mini_collection` (or equivalent) helper name — adapt the call to match whatever that file provides.

- [ ] **Step 2: Run new CLI tests, verify they fail**

```bash
pytest tests/test_cli.py -v -k "no_cache or clear_cache or cache_dir"
```

Expected: `error: unrecognized arguments: --no-cache`

- [ ] **Step 3: Add flags to `build_parser()` in `cli.py`**

Append inside `build_parser()`, after the existing `--from-stdin` argument block:

```python
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="With --impact, skip reading and writing the content index cache",
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="With --impact, delete the existing cache then rebuild",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        metavar="DIR",
        help=(
            "With --impact, directory for the content index cache "
            "(default: $XDG_CACHE_HOME/content-plugin-finder/)"
        ),
    )
```

- [ ] **Step 4: Update `_run_impact()` in `cli.py`**

Add this import at the top of `cli.py` (with existing imports):

```python
from content_plugin_finder.impact.cache import default_cache_path
```

In `_run_impact()`, replace the `report = compute_impact(...)` call (after the `changed = ...` block) with:

```python
        # Resolve cache path (needed for --clear-cache even when --no-cache is set)
        _cache_path: Path | None = None
        if not args.no_cache:
            _cache_path = (
                args.cache_dir / f"{hashlib.sha256(str(collection).encode()).hexdigest()[:16]}.json"
                if args.cache_dir
                else default_cache_path(collection)
            )

        if args.clear_cache and _cache_path and _cache_path.is_file():
            _cache_path.unlink()

        report = compute_impact(
            collection_root=collection,
            changed_files=changed,
            parent=parent,
            depth=args.depth,
            cache_path=_cache_path,
            no_cache=args.no_cache,
        )
```

Also add `import hashlib` to `cli.py`'s imports (it uses stdlib only).

- [ ] **Step 5: Run CLI tests, verify they pass**

```bash
pytest tests/test_cli.py -v
```

Expected: all PASS

- [ ] **Step 6: Run full test suite**

```bash
pytest tests/ -v
```

Expected: all PASS

- [ ] **Step 7: Smoke test on the real collection**

```bash
cd /home/dabrenna/github.com/dbrennand/amazon.aws
# First run — builds and caches (will be slow)
time git diff --name-only HEAD | content-plugin-finder --impact . --from-stdin --format json
# Second run — should be near-instant
time git diff --name-only HEAD | content-plugin-finder --impact . --from-stdin --format json
```

Expected: second run completes in under 2 seconds.

- [ ] **Step 8: Commit**

```bash
git add src/content_plugin_finder/cli.py
git commit -m "feat(cache): add --no-cache, --clear-cache, --cache-dir CLI flags"
```
