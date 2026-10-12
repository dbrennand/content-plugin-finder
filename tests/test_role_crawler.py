from pathlib import Path

import pytest
import yaml

from content_plugin_finder.crawl.context import build_context
from content_plugin_finder.crawl.roles import RoleCrawler
from content_plugin_finder.models import PluginKind


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("- hosts: localhost\n  roles:\n    - agent\n", {"agent"}),
        (
            "- hosts: localhost\n  roles:\n    - ' agent '\n    - '   '\n    - 12\n",
            {"agent"},
        ),
        (
            "- hosts: localhost\n  roles:\n    - role: acme.widgets.agent\n",
            {"acme.widgets.agent"},
        ),
        (
            "- hosts: localhost\n  tasks:\n    - include_role:\n        name: agent\n",
            {"agent"},
        ),
        (
            "- hosts: localhost\n  tasks:\n    - ansible.builtin.include_role: agent\n",
            {"agent"},
        ),
        (
            "- hosts: localhost\n  tasks:\n    - import_role:\n        name: acme.widgets.agent\n",
            {"acme.widgets.agent"},
        ),
        (
            "- hosts: localhost\n  tasks:\n    - ansible.builtin.import_role: external.vendor.agent\n",
            {"external.vendor.agent"},
        ),
    ],
)
def test_role_crawler_extracts_supported_syntax(
    tmp_path: Path, content: str, expected: set[str]
):
    root = tmp_path / "molecule" / "default"
    root.mkdir(parents=True)
    source = root / "converge.yml"
    source.write_text(content, encoding="utf-8")

    ctx = build_context(root, need_acc=False, need_yaml=False, parent=tmp_path)
    findings = list(RoleCrawler().crawl(ctx))
    assert {finding.name for finding in findings} == expected
    assert all(finding.kind is PluginKind.ROLE for finding in findings)
    assert {finding.path for finding in findings} == {str(source)}


def test_role_crawler_follows_static_imports_once(tmp_path: Path):
    root = tmp_path / "molecule" / "default"
    shared = tmp_path / "molecule" / "shared"
    tasks = shared / "tasks"
    root.mkdir(parents=True)
    tasks.mkdir(parents=True)
    (root / "converge.yml").write_text(
        "- import_playbook: ../shared/playbook.yml\n", encoding="utf-8"
    )
    (shared / "playbook.yml").write_text(
        "- hosts: localhost\n  tasks:\n    - import_tasks: tasks/roles.yml\n",
        encoding="utf-8",
    )
    source = tasks / "roles.yml"
    source.write_text(
        "- include_role:\n    name: agent\n- import_tasks: ../playbook.yml\n",
        encoding="utf-8",
    )

    ctx = build_context(root, need_acc=False, need_yaml=False, parent=tmp_path)
    findings = list(RoleCrawler().crawl(ctx))
    assert {finding.name for finding in findings} == {"agent"}
    assert {finding.path for finding in findings} == {str(source)}


def test_role_crawler_skips_dynamic_and_escaping_imports(tmp_path: Path):
    parent = tmp_path / "content"
    root = parent / "molecule" / "default"
    root.mkdir(parents=True)
    (tmp_path / "outside.yml").write_text(
        "- include_role:\n    name: outside\n", encoding="utf-8"
    )
    (root / "converge.yml").write_text(
        "- import_playbook: '{{ selected_playbook }}'\n"
        "- import_playbook: ../../../outside.yml\n",
        encoding="utf-8",
    )
    source = root / "tasks.yml"
    source.write_text("- include_role:\n    name: local\n", encoding="utf-8")

    ctx = build_context(root, need_acc=False, need_yaml=False, parent=parent)
    findings = list(RoleCrawler().crawl(ctx))
    assert {finding.name for finding in findings} == {"local"}
    assert {finding.path for finding in findings} == {str(source)}


def test_role_crawler_skips_invalid_and_missing_yaml(tmp_path: Path):
    root = tmp_path / "molecule" / "default"
    root.mkdir(parents=True)
    (root / "converge.yml").write_text(
        "- import_playbook: missing.yml\n", encoding="utf-8"
    )
    (root / "broken.yml").write_text("[not valid", encoding="utf-8")
    source = root / "valid.yaml"
    source.write_text("- import_role:\n    name: agent\n", encoding="utf-8")

    ctx = build_context(root, need_acc=False, need_yaml=False, parent=tmp_path)
    findings = list(RoleCrawler().crawl(ctx))
    assert {finding.name for finding in findings} == {"agent"}
    assert {finding.path for finding in findings} == {str(source)}


def test_role_crawler_skips_invalid_utf8_and_keeps_valid_roles(tmp_path: Path):
    root = tmp_path / "molecule" / "default"
    root.mkdir(parents=True)
    (root / "invalid.yml").write_bytes(b"- hosts: localhost\n  roles: \xff\n")
    source = root / "valid.yml"
    source.write_text("- include_role:\n    name: agent\n", encoding="utf-8")

    ctx = build_context(root, need_acc=False, need_yaml=False, parent=tmp_path)
    findings = list(RoleCrawler().crawl(ctx))
    assert {finding.name for finding in findings} == {"agent"}
    assert {finding.path for finding in findings} == {str(source)}


def test_role_crawler_handles_recursive_yaml_aliases(tmp_path: Path):
    root = tmp_path / "molecule" / "default"
    root.mkdir(parents=True)
    source = root / "converge.yml"
    source.write_text(
        "recursive: &recursive\n"
        "  child: *recursive\n"
        "playbook:\n"
        "  - hosts: localhost\n"
        "    roles:\n"
        "      - agent\n",
        encoding="utf-8",
    )

    ctx = build_context(root, need_acc=False, need_yaml=False, parent=tmp_path)
    findings = list(RoleCrawler().crawl(ctx))
    assert {finding.name for finding in findings} == {"agent"}
    assert {finding.path for finding in findings} == {str(source)}


@pytest.mark.parametrize(
    "action",
    [
        "include_role",
        "import_role",
        "ansible.builtin.include_role",
        "ansible.builtin.import_role",
    ],
)
@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ("name=agent", {"agent"}),
        ('name="acme.widgets.agent" tasks_from=install', {"acme.widgets.agent"}),
        ("tasks_from=install name='acme.widgets.agent'", {"acme.widgets.agent"}),
        ('name=agent tasks_from="{{ selected_tasks }}"', {"agent"}),
        ('name="{{ selected_role }}"', set()),
        ("tasks_from=install", set()),
        ("name=", set()),
        ('name="agent', set()),
    ],
)
def test_role_crawler_key_value_arguments(tmp_path: Path, action, arguments, expected):
    source = tmp_path / "tasks.yml"
    source.write_text(yaml.safe_dump([{action: arguments}]), encoding="utf-8")
    ctx = build_context(tmp_path, need_acc=False, need_yaml=False)

    findings = list(RoleCrawler().crawl(ctx))

    assert {finding.name for finding in findings} == expected
    assert all(finding.path == str(source) for finding in findings)
