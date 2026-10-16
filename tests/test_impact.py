import json
from pathlib import Path

import pytest
import yaml

from content_plugin_finder.cli import main
from content_plugin_finder.collection.graph import build_collection_graph
from content_plugin_finder.impact.content_index import (
    build_content_index,
    roots_using_roles,
)
from content_plugin_finder.impact.engine import (
    compute_impact,
    format_impact_json,
    format_impact_text,
)
from content_plugin_finder.impact.git import read_changed_files_from_lines
from content_plugin_finder.models import PluginKind


def _mini_collection(tmp_path: Path) -> Path:
    (tmp_path / "galaxy.yml").write_text(
        "namespace: acme\nname: widgets\n",
        encoding="utf-8",
    )
    modules = tmp_path / "plugins" / "modules"
    action = tmp_path / "plugins" / "action"
    utils = tmp_path / "plugins" / "module_utils"
    for d in (modules, action, utils):
        d.mkdir(parents=True)

    (utils / "core.py").write_text("X = 1\n", encoding="utf-8")
    (action / "base.py").write_text(
        "from ansible_collections.acme.widgets.plugins.module_utils.core import X\n"
        "class Base:\n"
        "    pass\n",
        encoding="utf-8",
    )
    (action / "thing.py").write_text(
        "from ansible_collections.acme.widgets.plugins.action.base import Base\n"
        "class ActionModule(Base):\n"
        '    MODULE_NAME = "thing"\n',
        encoding="utf-8",
    )
    (modules / "thing.py").write_text(
        'DOCUMENTATION = """\nmodule: thing\n"""\n',
        encoding="utf-8",
    )

    # molecule scenario using the module
    mol = tmp_path / "extensions" / "molecule" / "thing_mock"
    mol.mkdir(parents=True)
    (mol / "molecule.yml").write_text("driver:\n  name: default\n", encoding="utf-8")
    role = tmp_path / "roles" / "agent" / "tasks"
    role.mkdir(parents=True)
    (role / "main.yml").write_text(
        "- ansible.builtin.debug:\n    msg: agent\n",
        encoding="utf-8",
    )
    (mol / "converge.yml").write_text(
        "- hosts: localhost\n"
        "  roles:\n"
        "    - role: acme.widgets.agent\n"
        "  tasks:\n"
        "    - acme.widgets.thing:\n"
        "        name: x\n",
        encoding="utf-8",
    )

    # integration target using the module
    target = tmp_path / "tests" / "integration" / "targets" / "thing_test"
    target.mkdir(parents=True)
    (target / "aliases").write_text("thing\n", encoding="utf-8")
    (target / "tasks").mkdir()
    (target / "tasks" / "main.yml").write_text(
        "- acme.widgets.thing:\n    name: y\n"
        "- ansible.builtin.import_tasks: role.yml\n",
        encoding="utf-8",
    )
    (target / "tasks" / "role.yml").write_text(
        "- ansible.builtin.include_role:\n    name: agent\n",
        encoding="utf-8",
    )

    # unrelated scenario
    other = tmp_path / "extensions" / "molecule" / "other"
    other.mkdir(parents=True)
    (other / "molecule.yml").write_text("driver:\n  name: default\n", encoding="utf-8")
    (other / "converge.yml").write_text(
        "- hosts: localhost\n"
        "  roles:\n"
        "    - external.vendor.agent\n"
        "  tasks:\n"
        "    - ansible.builtin.debug:\n"
        "        msg: hi\n",
        encoding="utf-8",
    )
    return tmp_path


def test_read_changed_files_from_lines():
    assert read_changed_files_from_lines(
        ["plugins/action/thing.py\n", "# comment\n", "\n", "  foo.yml  \n"]
    ) == ["plugins/action/thing.py", "foo.yml"]


def test_content_index_maps_roles_to_molecule_and_integration_roots(tmp_path: Path):
    root = _mini_collection(tmp_path)
    graph = build_collection_graph(root)
    index = build_content_index(root, collection=graph.collection, depth=4)

    assert index.role_to_roots["acme.widgets.agent"] == [
        "extensions/molecule/thing_mock",
        "tests/integration/targets/thing_test",
    ]
    assert index.role_to_roots["external.vendor.agent"] == [
        "extensions/molecule/other",
    ]
    assert roots_using_roles(index, {"acme.widgets.agent"}) == {
        "extensions/molecule/thing_mock": ["acme.widgets.agent"],
        "tests/integration/targets/thing_test": ["acme.widgets.agent"],
    }


def test_content_index_always_scans_roles_and_keeps_them_out_of_plugin_map(
    tmp_path: Path,
):
    root = _mini_collection(tmp_path)
    graph = build_collection_graph(root)
    index = build_content_index(
        root,
        collection=graph.collection,
        depth=4,
        kinds=[PluginKind.FILTER],
    )

    assert index.role_to_roots["acme.widgets.agent"] == [
        "extensions/molecule/thing_mock",
        "tests/integration/targets/thing_test",
    ]
    assert not any("agent" in name for name in index.plugin_to_roots)


def test_impact_plugin_change_selects_scenarios(tmp_path: Path):
    root = _mini_collection(tmp_path)
    graph = build_collection_graph(root)
    index = build_content_index(root, collection=graph.collection, depth=4)

    report = compute_impact(
        collection_root=root,
        changed_files=["plugins/module_utils/core.py"],
        parent=root,
        graph=graph,
        content_index=index,
    )
    assert "acme.widgets.thing" in report.affected_plugins
    assert "extensions/molecule/thing_mock" in report.molecule_scenarios
    assert "tests/integration/targets/thing_test" in report.integration_targets
    assert "extensions/molecule/other" not in report.molecule_scenarios


def test_impact_direct_scenario_edit(tmp_path: Path):
    root = _mini_collection(tmp_path)
    report = compute_impact(
        collection_root=root,
        changed_files=["extensions/molecule/thing_mock/converge.yml"],
        parent=root,
    )
    assert report.molecule_scenarios == ["extensions/molecule/thing_mock"]
    assert report.integration_targets == []


def test_impact_direct_integration_edit(tmp_path: Path):
    root = _mini_collection(tmp_path)
    report = compute_impact(
        collection_root=root,
        changed_files=["tests/integration/targets/thing_test/tasks/main.yml"],
        parent=root,
    )
    assert report.integration_targets == ["tests/integration/targets/thing_test"]
    assert report.molecule_scenarios == []


def test_impact_path_prefix_when_depth_too_low(tmp_path: Path):
    """Scenario/target edits still map via path layout when discovery finds nothing."""
    root = _mini_collection(tmp_path)
    report = compute_impact(
        collection_root=root,
        changed_files=[
            "extensions/molecule/thing_mock/converge.yml",
            "tests/integration/targets/thing_test/tasks/main.yml",
        ],
        parent=root,
        depth=0,
    )
    assert report.molecule_scenarios == ["extensions/molecule/thing_mock"]
    assert report.integration_targets == ["tests/integration/targets/thing_test"]


def test_impact_shared_molecule_selects_all_scenarios(tmp_path: Path):
    root = _mini_collection(tmp_path)
    (root / "extensions" / "molecule" / "requirements.yml").write_text(
        "collections: []\n",
        encoding="utf-8",
    )
    report = compute_impact(
        collection_root=root,
        changed_files=["extensions/molecule/requirements.yml"],
        parent=root,
    )
    assert set(report.molecule_scenarios) == {
        "extensions/molecule/thing_mock",
        "extensions/molecule/other",
    }
    assert report.integration_targets == []
    for root_rel in report.molecule_scenarios:
        assert any(r.endswith("(shared molecule)") for r in report.reasons[root_rel])


def test_impact_scenario_local_still_only_that_scenario(tmp_path: Path):
    root = _mini_collection(tmp_path)
    report = compute_impact(
        collection_root=root,
        changed_files=["extensions/molecule/other/converge.yml"],
        parent=root,
    )
    assert report.molecule_scenarios == ["extensions/molecule/other"]
    assert "extensions/molecule/thing_mock" not in report.molecule_scenarios


def test_format_impact_text_names_only(tmp_path: Path):
    root = _mini_collection(tmp_path)
    report = compute_impact(
        collection_root=root,
        changed_files=["plugins/action/thing.py"],
        parent=root,
    )
    text = format_impact_text(report, emit="molecule", names_only=True)
    assert text.strip() == "thing_mock"


def test_cli_impact_from_stdin(tmp_path: Path, monkeypatch, capsys):
    root = _mini_collection(tmp_path)
    monkeypatch.setattr(
        "sys.stdin",
        type("S", (), {"readlines": lambda self: ["plugins/action/base.py\n"]})(),
    )
    assert (
        main(
            [
                "--impact",
                str(root),
                "--parent",
                str(root),
                "--from-stdin",
                "--emit",
                "all",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "molecule\textensions/molecule/thing_mock" in out
    assert "integration\ttests/integration/targets/thing_test" in out


@pytest.mark.parametrize(
    "changed",
    [
        "roles/agent/defaults/main.yml",
        "roles/agent/handlers/main.yml",
        "roles/agent/meta/main.yml",
        "roles/agent/tasks/main.yml",
        "roles/agent/tasks/agent_present.yml",
        "roles/agent/templates/service.j2",
        "roles/agent/vars/main.yml",
        "roles/agent/files/archive.tar.gz",
    ],
)
def test_role_file_selects_molecule_and_integration_roots(
    tmp_path: Path,
    changed: str,
):
    root = _mini_collection(tmp_path)
    report = compute_impact(
        collection_root=root,
        changed_files=[changed],
        parent=root,
    )

    assert report.affected_roles == ["acme.widgets.agent"]
    assert report.affected_plugins == []
    assert report.molecule_scenarios == ["extensions/molecule/thing_mock"]
    assert report.integration_targets == ["tests/integration/targets/thing_test"]
    expected_reason = f"role:acme.widgets.agent via {changed}"
    assert report.reasons == {
        "extensions/molecule/thing_mock": [expected_reason],
        "tests/integration/targets/thing_test": [expected_reason],
    }


def test_role_change_selects_all_rfe_molecule_scenarios(tmp_path: Path):
    (tmp_path / "galaxy.yml").write_text(
        "namespace: community\nname: beszel\n",
        encoding="utf-8",
    )
    for name in ("agent_airgap", "agent_default", "agent_token"):
        scenario = tmp_path / "extensions" / "molecule" / name
        scenario.mkdir(parents=True)
        (scenario / "molecule.yml").write_text(
            "driver:\n  name: default\n",
            encoding="utf-8",
        )
        (scenario / "converge.yml").write_text(
            "- hosts: localhost\n  roles:\n    - role: community.beszel.agent\n",
            encoding="utf-8",
        )

    report = compute_impact(
        collection_root=tmp_path,
        changed_files=["roles/agent/tasks/agent_present.yml"],
        parent=tmp_path / "extensions",
    )

    assert report.affected_roles == ["community.beszel.agent"]
    assert report.molecule_scenarios == [
        "molecule/agent_airgap",
        "molecule/agent_default",
        "molecule/agent_token",
    ]


def test_affected_roles_are_sorted(tmp_path: Path):
    root = _mini_collection(tmp_path)
    report = compute_impact(
        collection_root=root,
        changed_files=[
            "roles/zebra/tasks/main.yml",
            "roles/alpha/tasks/main.yml",
        ],
        parent=root,
    )

    assert report.affected_roles == [
        "acme.widgets.alpha",
        "acme.widgets.zebra",
    ]


def test_reason_lists_are_deterministic_for_reversed_changed_files(tmp_path: Path):
    root = _mini_collection(tmp_path)
    changed_files = [
        "roles/agent/vars/main.yml",
        "roles/agent/tasks/main.yml",
    ]

    forward = compute_impact(
        collection_root=root,
        changed_files=changed_files,
        parent=root,
    )
    reverse = compute_impact(
        collection_root=root,
        changed_files=list(reversed(changed_files)),
        parent=root,
    )

    expected_reasons = [
        "role:acme.widgets.agent via roles/agent/tasks/main.yml",
        "role:acme.widgets.agent via roles/agent/vars/main.yml",
    ]
    assert forward.reasons == reverse.reasons
    assert forward.reasons == {
        "extensions/molecule/thing_mock": expected_reasons,
        "tests/integration/targets/thing_test": expected_reasons,
    }


def test_impact_json_always_contains_affected_roles(tmp_path: Path):
    root = _mini_collection(tmp_path)
    report = compute_impact(
        collection_root=root,
        changed_files=["plugins/module_utils/core.py"],
        parent=root,
    )

    payload = json.loads(format_impact_json(report))
    assert (
        list(payload).index("affected_roles")
        == list(payload).index("affected_plugins") + 1
    )
    assert payload["affected_roles"] == []


def test_unreferenced_local_role_has_no_affected_roots(tmp_path: Path):
    root = _mini_collection(tmp_path)
    report = compute_impact(
        collection_root=root,
        changed_files=["roles/unrelated/tasks/main.yml"],
        parent=root,
    )

    assert report.affected_roles == ["acme.widgets.unrelated"]
    assert report.molecule_scenarios == []
    assert report.integration_targets == []
    assert report.reasons == {}


@pytest.mark.parametrize(
    "changed",
    [
        "roles/agent",
        "roles//tasks/main.yml",
        "other/roles/agent/tasks/main.yml",
    ],
)
def test_malformed_role_path_does_not_affect_roles(
    tmp_path: Path,
    changed: str,
):
    root = _mini_collection(tmp_path)
    report = compute_impact(
        collection_root=root,
        changed_files=[changed],
        parent=root,
    )

    assert report.affected_roles == []
    assert report.molecule_scenarios == []
    assert report.integration_targets == []
    assert report.reasons == {}


def test_mixed_plugin_and_role_changes_are_deduplicated(tmp_path: Path):
    root = _mini_collection(tmp_path)
    report = compute_impact(
        collection_root=root,
        changed_files=[
            "plugins/module_utils/core.py",
            "roles/agent/tasks/main.yml",
        ],
        parent=root,
    )

    assert report.affected_plugins == ["acme.widgets.thing"]
    assert report.affected_roles == ["acme.widgets.agent"]
    assert report.molecule_scenarios == ["extensions/molecule/thing_mock"]
    assert report.integration_targets == ["tests/integration/targets/thing_test"]
    for reasons in report.reasons.values():
        assert len(reasons) == 2
        assert len(reasons) == len(set(reasons))


@pytest.mark.parametrize(
    "action",
    [
        "include_role",
        "import_role",
        "ansible.builtin.include_role",
        "ansible.builtin.import_role",
    ],
)
def test_role_key_value_arguments_select_molecule_and_integration(
    tmp_path: Path, action
):
    root = _mini_collection(tmp_path)
    molecule = "extensions/molecule/thing_mock"
    integration = "tests/integration/targets/thing_test"
    (root / molecule / "converge.yml").write_text(
        f'- hosts: localhost\n  tasks:\n    - {action}: name="acme.widgets.agent" tasks_from=main\n',
        encoding="utf-8",
    )
    (root / integration / "tasks" / "role.yml").write_text(
        f"- {action}: tasks_from=main name=agent\n", encoding="utf-8"
    )
    changed = "roles/agent/tasks/main.yml"

    report = compute_impact(collection_root=root, changed_files=[changed], workers=1)

    assert report.affected_roles == ["acme.widgets.agent"]
    assert report.affected_plugins == []
    assert report.molecule_scenarios == [molecule]
    assert report.integration_targets == [integration]
    assert report.reasons == {
        molecule: [f"role:acme.widgets.agent via {changed}"],
        integration: [f"role:acme.widgets.agent via {changed}"],
    }


@pytest.mark.parametrize(
    "tagged_value", ["!unsafe literal", "!vault encrypted-placeholder"]
)
def test_role_impact_preserves_references_with_ansible_tags(
    tmp_path: Path, tagged_value: str
):
    root = _mini_collection(tmp_path)
    molecule = "extensions/molecule/thing_mock"
    (root / molecule / "converge.yml").write_text(
        "- import_playbook: ../../shared.yml\n", encoding="utf-8"
    )
    (root / "extensions/shared.yml").write_text(
        f"- hosts: localhost\n  vars:\n    unrelated: {tagged_value}\n"
        "  roles:\n    - acme.widgets.agent\n",
        encoding="utf-8",
    )
    report = compute_impact(
        collection_root=root, changed_files=["roles/agent/tasks/main.yml"], workers=1
    )
    assert report.affected_roles == ["acme.widgets.agent"]
    assert report.molecule_scenarios == [molecule]
    assert report.reasons[molecule] == [
        "role:acme.widgets.agent via roles/agent/tasks/main.yml"
    ]


@pytest.mark.parametrize(
    "dependency",
    ["agent", "acme.widgets.agent", "role: agent", "role: acme.widgets.agent"],
)
def test_role_impact_selects_integration_metadata_dependency(
    tmp_path: Path, dependency: str
):
    root = _mini_collection(tmp_path)
    integration = "tests/integration/targets/thing_test"
    (root / integration / "tasks/role.yml").write_text("[]\n", encoding="utf-8")
    metadata = root / integration / "meta/main.yml"
    metadata.parent.mkdir()
    metadata.write_text(f"dependencies:\n  - {dependency}\n", encoding="utf-8")
    report = compute_impact(
        collection_root=root, changed_files=["roles/agent/tasks/main.yml"], workers=1
    )
    assert report.affected_roles == ["acme.widgets.agent"]
    assert report.integration_targets == [integration]
    assert report.reasons[integration] == [
        "role:acme.widgets.agent via roles/agent/tasks/main.yml"
    ]


@pytest.mark.parametrize(
    "task",
    [
        {"ansible.builtin.include_role": None, "args": {"name": "acme.widgets.agent"}},
        {"action": "ansible.builtin.include_role name=acme.widgets.agent"},
    ],
)
def test_role_task_forms_select_both_target_types(tmp_path: Path, task):
    root = _mini_collection(tmp_path)
    molecule = "extensions/molecule/thing_mock"
    integration = "tests/integration/targets/thing_test"
    (root / molecule / "converge.yml").write_text(
        yaml.safe_dump([{"hosts": "localhost", "tasks": [task]}]), encoding="utf-8"
    )
    (root / integration / "tasks/role.yml").write_text(
        yaml.safe_dump([task]), encoding="utf-8"
    )
    report = compute_impact(
        collection_root=root, changed_files=["roles/agent/tasks/main.yml"], workers=1
    )
    assert report.molecule_scenarios == [molecule]
    assert report.integration_targets == [integration]
    assert report.reasons == {
        target: ["role:acme.widgets.agent via roles/agent/tasks/main.yml"]
        for target in (molecule, integration)
    }


def test_inline_file_import_selects_both_target_types(tmp_path: Path):
    root = _mini_collection(tmp_path)
    molecule = "extensions/molecule/thing_mock"
    integration = "tests/integration/targets/thing_test"
    shared = root / "shared/roles.yml"
    shared.parent.mkdir()
    shared.write_text(
        "- ansible.builtin.include_role:\n    name: acme.widgets.agent\n",
        encoding="utf-8",
    )
    (root / molecule / "converge.yml").write_text(
        "- hosts: localhost\n  tasks:\n"
        "    - ansible.builtin.import_tasks: file=../../../shared/roles.yml\n",
        encoding="utf-8",
    )
    (root / integration / "tasks/role.yml").write_text(
        "- ansible.builtin.import_tasks: file=../../../../../shared/roles.yml\n",
        encoding="utf-8",
    )
    report = compute_impact(
        collection_root=root, changed_files=["roles/agent/tasks/main.yml"], workers=1
    )
    assert report.molecule_scenarios == [molecule]
    assert report.integration_targets == [integration]
    assert report.reasons == {
        target: ["role:acme.widgets.agent via roles/agent/tasks/main.yml"]
        for target in (molecule, integration)
    }


@pytest.mark.parametrize("workers", [1, 2])
@pytest.mark.parametrize("narrow_parent", [False, True])
@pytest.mark.parametrize("dependency", ["agent", "role: acme.widgets.agent"])
def test_transitive_roles_select_referencing_roots(
    tmp_path: Path, workers: int, narrow_parent: bool, dependency: str
):
    root = _mini_collection(tmp_path)
    molecule = "extensions/molecule/thing_mock"
    integration = "tests/integration/targets/thing_test"
    (root / molecule / "converge.yml").write_text(
        "- hosts: localhost\n  roles:\n    - acme.widgets.wrapper\n", encoding="utf-8"
    )
    (root / integration / "tasks/role.yml").write_text(
        "- include_role:\n    name: wrapper\n", encoding="utf-8"
    )
    metadata = root / "roles/wrapper/meta/main.yml"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(f"dependencies:\n  - {dependency}\n", encoding="utf-8")
    parent = root / "extensions" if narrow_parent else root
    report = compute_impact(
        collection_root=root,
        parent=parent,
        changed_files=["roles/agent/tasks/main.yml"],
        workers=workers,
    )
    selected_molecule = "molecule/thing_mock" if narrow_parent else molecule
    assert report.molecule_scenarios == [selected_molecule]
    assert report.integration_targets == ([] if narrow_parent else [integration])
    assert report.affected_roles == ["acme.widgets.agent"]
    assert report.reasons == {
        target: ["role:acme.widgets.agent via roles/agent/tasks/main.yml"]
        for target in report.molecule_scenarios + report.integration_targets
    }


@pytest.mark.parametrize("workers", [1, 2])
def test_role_dependency_closure_handles_cycles_and_external_roles(tmp_path, workers):
    root = _mini_collection(tmp_path)
    files = {
        "roles/agent/meta/main.yml": "dependencies:\n  - acme.widgets.middle\n  - external.vendor.shadow\n",
        "roles/middle/tasks/main.yml": "- include_role:\n    name: leaf\n",
        "roles/leaf/meta/main.yml": "dependencies:\n  - agent\n  - missing\n",
        # An external FQCN must never resolve to a local role with the same name.
        "roles/shadow/meta/main.yml": "dependencies:\n  - unrelated\n",
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    index = build_content_index(
        root, collection="acme.widgets", kinds=[PluginKind.ROLE], workers=workers
    )
    expected = [
        "extensions/molecule/thing_mock",
        "tests/integration/targets/thing_test",
    ]
    for role in ("agent", "middle", "leaf", "missing"):
        assert index.role_to_roots[f"acme.widgets.{role}"] == expected
    assert "acme.widgets.shadow" not in index.role_to_roots
    assert "acme.widgets.unrelated" not in index.role_to_roots
    report = compute_impact(
        collection_root=root,
        content_index=index,
        changed_files=["roles/leaf/tasks/deleted.yml"],
        workers=workers,
    )
    assert report.molecule_scenarios == expected[:1]
    assert report.integration_targets == expected[1:]


def test_role_dependencies_do_not_follow_symlinks_outside_collection(tmp_path):
    root = tmp_path / "collection"
    root.mkdir()
    _mini_collection(root)
    outside = tmp_path / "outside"
    (outside / "meta").mkdir(parents=True)
    (outside / "meta/main.yml").write_text(
        "dependencies:\n  - unrelated\n", encoding="utf-8"
    )
    (root / "roles/escape").symlink_to(outside, target_is_directory=True)
    metadata = root / "roles/agent/meta/main.yml"
    metadata.parent.mkdir()
    metadata.write_text("dependencies:\n  - escape\n", encoding="utf-8")
    index = build_content_index(root, collection="acme.widgets", workers=1)
    assert "acme.widgets.escape" in index.role_to_roots
    assert "acme.widgets.unrelated" not in index.role_to_roots
