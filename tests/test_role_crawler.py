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


@pytest.mark.parametrize(
    "tagged_value", ["!unsafe literal", "!vault |\n      encrypted-placeholder"]
)
def test_role_crawler_tolerates_ansible_tags(tmp_path: Path, tagged_value: str):
    source = tmp_path / "playbook.yml"
    source.write_text(
        f"- hosts: localhost\n  vars:\n    unrelated: {tagged_value}\n"
        "  roles:\n    - acme.widgets.agent\n",
        encoding="utf-8",
    )
    ctx = build_context(tmp_path, need_acc=False, need_yaml=False)
    findings = list(RoleCrawler().crawl(ctx))
    assert [finding.name for finding in findings] == ["acme.widgets.agent"]
    assert findings[0].path == str(source)


def test_role_crawler_does_not_treat_vault_payload_as_role(tmp_path: Path):
    (tmp_path / "tasks.yml").write_text(
        "- include_role:\n    name: !vault encrypted-placeholder\n"
        "- include_role:\n    name: !unsafe agent\n",
        encoding="utf-8",
    )
    ctx = build_context(tmp_path, need_acc=False, need_yaml=False)
    assert [f.name for f in RoleCrawler().crawl(ctx)] == ["agent"]
    # Tag handling must remain local to the crawler's loader.
    with pytest.raises(yaml.constructor.ConstructorError):
        yaml.safe_load("value: !unsafe literal")


@pytest.mark.parametrize("filename", ["main.yml", "main.yaml", "main"])
def test_role_crawler_reads_metadata_dependencies(tmp_path: Path, filename: str):
    source = tmp_path / "meta" / filename
    source.parent.mkdir()
    source.write_text(
        "dependencies:\n  - agent\n  - role: acme.widgets.other\n"
        "  - role: '{{ dynamic }}'\n  - 42\n  - role: null\n"
        "vars:\n  dependencies:\n    - not_a_dependency\n",
        encoding="utf-8",
    )
    ctx = build_context(tmp_path, need_acc=False, need_yaml=False)
    findings = list(RoleCrawler().crawl(ctx))
    assert {f.name for f in findings} == {"agent", "acme.widgets.other"}
    assert {f.path for f in findings} == {str(source)}


@pytest.mark.parametrize(
    "relative", ["vars/main.yml", "requirements.yml", "meta/argument_specs.yml"]
)
def test_role_crawler_ignores_dependencies_outside_metadata(
    tmp_path: Path, relative: str
):
    source = tmp_path / relative
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("dependencies:\n  - not_a_role\n", encoding="utf-8")
    ctx = build_context(tmp_path, need_acc=False, need_yaml=False)
    assert list(RoleCrawler().crawl(ctx)) == []


@pytest.mark.parametrize(
    "action",
    [
        "include_role",
        "import_role",
        "ansible.builtin.include_role",
        "ansible.builtin.import_role",
    ],
)
@pytest.mark.parametrize("form", ["args", "action", "action_args"])
def test_role_crawler_task_argument_forms(tmp_path: Path, action: str, form: str):
    if form == "args":
        task = {action: None, "args": {"name": "acme.widgets.agent"}}
    elif form == "action":
        task = {"action": f'{action} name="acme.widgets.agent"'}
    else:
        task = {"action": action, "args": {"name": "acme.widgets.agent"}}
    source = tmp_path / "tasks.yml"
    source.write_text(yaml.safe_dump([task]), encoding="utf-8")
    ctx = build_context(tmp_path, need_acc=False, need_yaml=False)
    findings = list(RoleCrawler().crawl(ctx))
    assert [finding.name for finding in findings] == ["acme.widgets.agent"]
    assert findings[0].path == str(source)


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"name": "explicit"}, ["explicit"]),
        ("name=explicit", ["explicit"]),
        ({"name": "{{ dynamic }}"}, []),
        ({"name": None}, []),
        ("name=", []),
        ('name="broken', []),
    ],
)
def test_role_crawler_explicit_arguments_override_task_args(
    tmp_path, arguments, expected
):
    (tmp_path / "tasks.yml").write_text(
        yaml.safe_dump([{"include_role": arguments, "args": {"name": "fallback"}}]),
        encoding="utf-8",
    )
    ctx = build_context(tmp_path, need_acc=False, need_yaml=False)
    assert [f.name for f in RoleCrawler().crawl(ctx)] == expected


@pytest.mark.parametrize(
    "action",
    [
        "include_tasks",
        "import_tasks",
        "ansible.builtin.include_tasks",
        "ansible.builtin.import_tasks",
    ],
)
@pytest.mark.parametrize("form", ["inline", "args", "action"])
def test_role_crawler_import_argument_forms(tmp_path: Path, action: str, form: str):
    root = tmp_path / "scenario"
    root.mkdir()
    shared = tmp_path / "shared tasks.yml"
    shared.write_text("- include_role:\n    name: agent\n", encoding="utf-8")
    if form == "inline":
        task = {action: 'file="../shared tasks.yml"'}
    elif form == "args":
        task = {action: None, "args": {"file": "../shared tasks.yml"}}
    else:
        task = {"action": f'{action} file="../shared tasks.yml"'}
    (root / "tasks.yml").write_text(yaml.safe_dump([task]), encoding="utf-8")
    ctx = build_context(root, need_acc=False, need_yaml=False, parent=tmp_path)
    findings = list(RoleCrawler().crawl(ctx))
    assert [f.name for f in findings] == ["agent"]
    assert findings[0].path == str(shared)
    # The new syntax must obey the same import boundary as direct filenames.
    ctx = build_context(root, need_acc=False, need_yaml=False)
    assert list(RoleCrawler().crawl(ctx)) == []


@pytest.mark.parametrize("source_kind", ["play", "metadata"])
@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ({"name": " agent "}, ["agent"]),
        ({"name": "acme.widgets.agent"}, ["acme.widgets.agent"]),
        ({"role": "explicit", "name": "fallback"}, ["explicit"]),
        ({"role": None, "name": "fallback"}, []),
        ({"role": "", "name": "fallback"}, []),
        ({"role": "{{ dynamic }}", "name": "fallback"}, []),
        ({"name": "{{ dynamic }}"}, []),
        ({"name": 42}, []),
    ],
)
def test_role_name_alias_and_precedence(tmp_path, source_kind, entry, expected):
    if source_kind == "play":
        source = tmp_path / "playbook.yml"
        document = [{"hosts": "localhost", "roles": [entry]}]
    else:
        source = tmp_path / "meta/main.yml"
        source.parent.mkdir()
        document = {"dependencies": [entry]}
    source.write_text(yaml.safe_dump(document), encoding="utf-8")
    ctx = build_context(tmp_path, need_acc=False, need_yaml=False)

    findings = list(RoleCrawler().crawl(ctx))

    assert [finding.name for finding in findings] == expected
    assert all(finding.path == str(source) for finding in findings)
