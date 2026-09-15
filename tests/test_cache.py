import os
from pathlib import Path

from content_plugin_finder.impact.cache import (
    compute_fingerprint,
    default_cache_path,
    load_cached_index,
    save_cached_index,
)
from content_plugin_finder.impact.content_index import ContentIndex
from content_plugin_finder.models import PluginKind


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
