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
