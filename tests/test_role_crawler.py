from pathlib import Path

import pytest
import yaml

from content_plugin_finder.crawl.context import build_context
from content_plugin_finder.crawl.roles import (
    RoleCrawler,
    _directory_files,
    _DirectoryOptions,
)
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
        "- hosts: localhost\n"
        "  vars:\n"
        "    recursive: &recursive\n"
        "      child: *recursive\n"
        "  roles: [agent]\n"
        "  tasks: &tasks\n"
        "    - block: *tasks\n",
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


def _write_files(root: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


@pytest.mark.parametrize(
    "action",
    [
        "import_tasks",
        "include_tasks",
        "ansible.builtin.import_tasks",
        "ansible.builtin.include_tasks",
    ],
)
@pytest.mark.parametrize("nearest", [False, True])
def test_nested_import_uses_playbook_fallback_and_nearest_match(
    tmp_path, action, nearest
):
    files = {
        "scenario/converge.yml": "- import_playbook: ../shared/play.yml\n",
        "shared/play.yml": "- hosts: localhost\n  tasks:\n    - import_tasks: sub/child.yml\n",
        "shared/sub/child.yml": f"- {action}: common.yml\n",
        "shared/common.yml": "- import_role:\n    name: fallback\n",
    }
    if nearest:
        files["shared/sub/common.yml"] = "- import_role:\n    name: nearest\n"
    _write_files(tmp_path, files)
    ctx = build_context(
        tmp_path / "scenario", need_acc=False, need_yaml=False, parent=tmp_path
    )

    findings = list(RoleCrawler().crawl(ctx))

    assert [finding.name for finding in findings] == [
        "nearest" if nearest else "fallback"
    ]
    expected = "shared/sub/common.yml" if nearest else "shared/common.yml"
    assert findings[0].path == str(tmp_path / expected)


def test_shared_import_retains_each_playbook_context_and_terminates_cycles(tmp_path):
    _write_files(
        tmp_path,
        {
            "scenario/converge.yml": "- import_playbook: ../one/play.yml\n- import_playbook: ../two/play.yml\n",
            "one/play.yml": "- hosts: localhost\n  tasks:\n    - import_tasks: ../shared/child.yml\n",
            "two/play.yml": "- hosts: localhost\n  tasks:\n    - import_tasks: ../shared/child.yml\n",
            "shared/child.yml": "- import_tasks: roles.yml\n- import_tasks: child.yml\n",
            "one/roles.yml": "- import_role:\n    name: one\n",
            "two/roles.yml": "- import_role:\n    name: two\n",
        },
    )
    ctx = build_context(
        tmp_path / "scenario", need_acc=False, need_yaml=False, parent=tmp_path
    )

    findings = list(RoleCrawler().crawl(ctx))

    assert sorted(finding.name for finding in findings) == ["one", "two"]
    assert len(findings) == 2


def test_imported_playbook_resets_fallback_directories(tmp_path):
    _write_files(
        tmp_path,
        {
            "scenario/converge.yml": "- import_playbook: ../outer/play.yml\n",
            "outer/play.yml": "- import_playbook: ../inner/play.yml\n",
            "inner/play.yml": "- hosts: localhost\n  tasks:\n    - import_tasks: ../shared/child.yml\n",
            "shared/child.yml": "- import_tasks: roles.yml\n",
            "outer/roles.yml": "- import_role:\n    name: wrong_playbook\n",
        },
    )
    ctx = build_context(
        tmp_path / "scenario", need_acc=False, need_yaml=False, parent=tmp_path
    )

    assert list(RoleCrawler().crawl(ctx)) == []


@pytest.mark.parametrize("symlink", [False, True])
def test_import_fallback_cannot_escape_boundary(tmp_path, symlink):
    parent = tmp_path / "content"
    _write_files(
        parent,
        {
            "scenario/converge.yml": "- import_playbook: ../shared/play.yml\n",
            "shared/play.yml": "- hosts: localhost\n  tasks:\n    - import_tasks: sub/child.yml\n",
            "shared/sub/child.yml": "- import_tasks: roles.yml\n"
            if symlink
            else "- import_tasks: ../../../outside.yml\n",
        },
    )
    outside = tmp_path / "outside.yml"
    outside.write_text("- import_role:\n    name: outside\n", encoding="utf-8")
    if symlink:
        (parent / "shared/roles.yml").symlink_to(outside)
    ctx = build_context(
        parent / "scenario", need_acc=False, need_yaml=False, parent=parent
    )

    assert list(RoleCrawler().crawl(ctx)) == []


@pytest.mark.parametrize("subdir", ["tasks", "handlers"])
def test_extensionless_role_entry_points_are_scanned(tmp_path, subdir):
    _write_files(
        tmp_path,
        {
            f"role/{subdir}/main": "- import_role:\n    name: agent\n",
            "role/files/main": "- import_role:\n    name: unrelated\n",
            "role/README": "- import_role:\n    name: unrelated\n",
        },
    )
    ctx = build_context(tmp_path / "role", need_acc=False, need_yaml=False)

    findings = list(RoleCrawler().crawl(ctx))

    assert [finding.name for finding in findings] == ["agent"]
    assert findings[0].path == str(tmp_path / f"role/{subdir}/main")


@pytest.mark.parametrize("filename", ["tasks", "tasks.txt"])
def test_explicit_import_does_not_require_yaml_suffix(tmp_path, filename):
    _write_files(
        tmp_path,
        {
            "scenario/converge.yml": f"- hosts: localhost\n  tasks:\n    - import_tasks: ../shared/{filename}\n",
            f"shared/{filename}": "- import_role:\n    name: agent\n",
        },
    )
    ctx = build_context(
        tmp_path / "scenario", need_acc=False, need_yaml=False, parent=tmp_path
    )

    findings = list(RoleCrawler().crawl(ctx))

    assert [finding.name for finding in findings] == ["agent"]
    assert findings[0].path == str(tmp_path / "shared" / filename)
    ctx = build_context(tmp_path / "scenario", need_acc=False, need_yaml=False)
    assert list(RoleCrawler().crawl(ctx)) == []


def _variable_collection(tmp_path: Path, document, files=None):
    _write_files(
        tmp_path,
        {
            "galaxy.yml": "namespace: acme\nname: widgets\n",
            "scenario/play.yml": yaml.safe_dump(document),
            "roles/agent/defaults/main.yml": "enabled: true\n",
            "roles/other/vars/main.yml": "other: true\n",
            **(files or {}),
        },
    )
    return build_context(
        tmp_path / "scenario", need_acc=False, need_yaml=False, parent=tmp_path
    )


@pytest.mark.parametrize("action", ["include_vars", "ansible.builtin.include_vars"])
@pytest.mark.parametrize("form", ["free", "mapping", "inline", "args", "action", "raw"])
def test_include_vars_file_argument_forms(tmp_path, action, form):
    relative = "../roles/agent/defaults/main.yml"
    task = {
        "free": {action: relative},
        "mapping": {action: {"file": relative}},
        "inline": {action: f"file='{relative}' name=loaded"},
        "args": {action: None, "args": {"file": relative}},
        "action": {"action": f"{action} {relative}"},
        "raw": {action: {"_raw_params": relative}},
    }[form]
    ctx = _variable_collection(tmp_path, [{"hosts": "localhost", "tasks": [task]}])
    findings = list(RoleCrawler().crawl(ctx))
    assert [(f.name, f.path, f.source) for f in findings] == [
        (
            "acme.widgets.agent",
            str(tmp_path / "scenario/play.yml"),
            "yaml-variable-role-reference",
        )
    ]


@pytest.mark.parametrize(
    ("entries", "expected"),
    [
        ("../roles/agent/defaults/main.yml", {"agent"}),
        (
            ["../roles/agent/defaults/main.yml", "../roles/other/vars/main.yml"],
            {"agent", "other"},
        ),
        (
            [["../roles/agent/defaults/main.yml", "../roles/other/vars/main.yml"]],
            {"agent"},
        ),
        ([["missing.yml", "../roles/agent/defaults/main.yml"]], {"agent"}),
        ([["local.yml", "../roles/agent/defaults/main.yml"]], set()),
        ([["{{ unknown }}", "../roles/agent/defaults/main.yml"]], set()),
        ([["../roles/agent/defaults/main.yml", "{{ unknown }}"]], {"agent"}),
        (["{{ unknown }}", "../roles/agent/defaults/main.yml"], {"agent"}),
        ([[42, "../roles/agent/defaults/main.yml"]], set()),
        ([[]], set()),
    ],
)
def test_vars_files_candidate_lists(tmp_path, entries, expected):
    ctx = _variable_collection(
        tmp_path,
        [{"hosts": "localhost", "vars_files": entries}],
        {"scenario/local.yml": "local: true\n"},
    )
    assert {f.name for f in RoleCrawler().crawl(ctx)} == {
        f"acme.widgets.{name}" for name in expected
    }


@pytest.mark.parametrize("kind", ["vars_files", "file", "dir"])
def test_variable_paths_cannot_escape_collection(tmp_path, kind):
    outside = tmp_path / "outside/roles/escape/vars"
    outside.mkdir(parents=True)
    (outside / "main.yml").write_text("value: true\n")
    collection = tmp_path / "collection"
    raw = "../../outside/roles/escape/vars"
    if kind != "dir":
        raw += "/main.yml"
    document = (
        [{"hosts": "localhost", "vars_files": [raw]}]
        if kind == "vars_files"
        else [{"include_vars": {kind: raw}}]
    )
    ctx = _variable_collection(collection, document)
    assert list(RoleCrawler().crawl(ctx)) == []
    # A symlink into another collection must not be attributed to local agent.
    link = collection / "roles/agent/defaults/linked"
    link.symlink_to(outside, target_is_directory=True)
    raw = "../roles/agent/defaults/linked" + ("/main.yml" if kind != "dir" else "")
    document = (
        [{"hosts": "localhost", "vars_files": [raw]}]
        if kind == "vars_files"
        else [{"include_vars": {kind: raw}}]
    )
    (collection / "scenario/play.yml").write_text(yaml.safe_dump(document))
    assert list(RoleCrawler().crawl(ctx)) == []


@pytest.mark.parametrize(
    "raw",
    [
        "{{ role_path }}/vars/main.yml",
        "../roles/{{ role }}/vars/main.yml",
        "{% if x %}a{% endif %}",
        "{# comment #}../roles/agent/defaults/main.yml",
        "~/roles/agent/defaults/main.yml",
    ],
)
@pytest.mark.parametrize("kind", ["vars_files", "file", "dir"])
def test_dynamic_variable_paths_are_not_evaluated(tmp_path, raw, kind):
    document = (
        [{"hosts": "localhost", "vars_files": [raw]}]
        if kind == "vars_files"
        else [{"include_vars": {kind: raw}}]
    )
    ctx = _variable_collection(tmp_path, document)
    assert list(RoleCrawler().crawl(ctx)) == []


@pytest.mark.parametrize("raw", ["../../agent/defaults", "vars/../../agent/defaults"])
def test_role_include_vars_directory_context(tmp_path, raw):
    ctx = _variable_collection(
        tmp_path,
        [],
        {
            "roles/wrapper/tasks/nested/main.yml": yaml.safe_dump(
                [{"include_vars": {"dir": raw}}]
            ),
        },
    )
    ctx.root = tmp_path / "roles/wrapper"
    assert {f.name for f in RoleCrawler().crawl(ctx)} == {"acme.widgets.agent"}


def test_include_vars_directory_outside_role_uses_task_source_not_playbook(tmp_path):
    ctx = _variable_collection(
        tmp_path,
        [{"import_playbook": "../shared/play.yml"}],
        {
            "shared/play.yml": "- hosts: localhost\n  tasks:\n    - import_tasks: nested/tasks.yml\n",
            "shared/nested/tasks.yml": "- include_vars:\n    dir: ../../roles/agent/defaults\n",
        },
    )
    assert {f.name for f in RoleCrawler().crawl(ctx)} == {"acme.widgets.agent"}


@pytest.mark.parametrize("role_file", ["main.yml", "vars/main.yml"])
def test_include_vars_searches_role_before_task_directory(tmp_path, role_file):
    ctx = _variable_collection(
        tmp_path,
        [],
        {
            "roles/wrapper/tasks/main.yml": yaml.safe_dump(
                [{"include_vars": "main.yml"}]
            ),
        },
    )
    (tmp_path / "roles/wrapper" / role_file).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "roles/wrapper" / role_file).symlink_to(
        tmp_path / "roles/agent/defaults/main.yml"
    )
    ctx.root = tmp_path / "roles/wrapper"
    assert {f.name for f in RoleCrawler().crawl(ctx)} == {"acme.widgets.agent"}


def test_include_vars_file_uses_task_then_playbook_without_include_ancestor(tmp_path):
    ctx = _variable_collection(
        tmp_path,
        [{"hosts": "localhost", "tasks": [{"import_tasks": "../shared/outer.yml"}]}],
        {
            "shared/outer.yml": "- import_tasks: nested/child.yml\n",
            "shared/nested/child.yml": "- include_vars: values.yml\n",
            "shared/values.yml": "wrong: true\n",
        },
    )
    (tmp_path / "scenario/vars").mkdir()
    (tmp_path / "scenario/vars/values.yml").symlink_to(
        tmp_path / "roles/agent/defaults/main.yml"
    )
    assert {f.name for f in RoleCrawler().crawl(ctx)} == {"acme.widgets.agent"}


@pytest.mark.parametrize("kind", ["vars_files", "file", "dir"])
def test_variable_payloads_are_never_executable_even_when_initially_scanned(
    tmp_path, kind
):
    raw = "data" if kind == "dir" else "data/values.yml"
    document = (
        [{"hosts": "localhost", "vars_files": [raw]}]
        if kind == "vars_files"
        else [{"include_vars": {kind: raw}}]
    )
    ctx = _variable_collection(
        tmp_path,
        document,
        {
            # Sorted before play.yml: discovery must discard any findings/imports
            # obtained from the variable payload, regardless of queue ordering.
            "scenario/data/values.yml": "hosts: localhost\nroles: [bogus]\nimport_tasks: ../../shared/bogus.yml\n",
            "shared/bogus.yml": "- include_role: indirect_bogus\n",
            "roles/agent/defaults/misleading.yml": "hosts: localhost\nroles: [also_bogus]\n",
        },
    )
    assert list(RoleCrawler().crawl(ctx)) == []
    ctx.root = tmp_path / "roles/agent"
    assert list(RoleCrawler().crawl(ctx)) == []


def test_inline_variable_mappings_are_not_executable(tmp_path):
    ctx = _variable_collection(
        tmp_path,
        [
            {
                "hosts": "localhost",
                "vars": {
                    "hosts": "localhost",
                    "roles": ["bogus"],
                    "include_vars": "../roles/agent/defaults/main.yml",
                },
            }
        ],
    )
    assert list(RoleCrawler().crawl(ctx)) == []


def test_vars_search_precedence_and_explicit_vars_prefix(tmp_path):
    ctx = _variable_collection(
        tmp_path,
        [{"hosts": "localhost", "vars_files": ["values.yml"]}],
        {
            "scenario/values.yml": "local: true\n",
        },
    )
    (tmp_path / "scenario/vars").mkdir()
    (tmp_path / "scenario/vars/values.yml").symlink_to(
        tmp_path / "roles/agent/defaults/main.yml"
    )
    assert {f.name for f in RoleCrawler().crawl(ctx)} == {"acme.widgets.agent"}
    (tmp_path / "scenario/play.yml").write_text(
        "- hosts: localhost\n  vars_files: vars/values.yml\n"
    )
    assert {f.name for f in RoleCrawler().crawl(ctx)} == {"acme.widgets.agent"}


def test_nested_collection_is_not_owned_by_outer_role(tmp_path):
    ctx = _variable_collection(
        tmp_path,
        [{"include_vars": "../roles/agent/vendor/vars.yml"}],
        {
            "roles/agent/vendor/galaxy.yml": "namespace: other\nname: collection\n",
            "roles/agent/vendor/vars.yml": "value: true\n",
        },
    )
    assert list(RoleCrawler().crawl(ctx)) == []


@pytest.mark.parametrize("kind", ["vars_files", "file", "dir"])
def test_deleted_variable_import_retains_unique_role_owner(tmp_path, kind):
    raw = "../roles/agent/defaults" + ("/main.yml" if kind != "dir" else "")
    document = (
        [{"hosts": "localhost", "vars_files": [raw]}]
        if kind == "vars_files"
        else [{"include_vars": {kind: raw}}]
    )
    ctx = _variable_collection(tmp_path, document)
    (tmp_path / "roles/agent/defaults/main.yml").unlink()
    if kind == "dir":
        (tmp_path / "roles/agent/defaults").rmdir()
    assert {f.name for f in RoleCrawler().crawl(ctx)} == {"acme.widgets.agent"}


def test_missing_alternatives_do_not_guess_between_roles(tmp_path):
    ctx = _variable_collection(
        tmp_path,
        [
            {
                "hosts": "localhost",
                "vars_files": [
                    [
                        "../roles/agent/defaults/deleted.yml",
                        "../roles/other/vars/deleted.yml",
                    ]
                ],
            }
        ],
    )
    assert list(RoleCrawler().crawl(ctx)) == []


def test_existing_alternative_wins_over_deleted_role_file(tmp_path):
    ctx = _variable_collection(
        tmp_path,
        [
            {
                "hosts": "localhost",
                "vars_files": [
                    [
                        "../roles/agent/defaults/deleted.yml",
                        "../roles/other/vars/main.yml",
                    ]
                ],
            }
        ],
    )
    assert {f.name for f in RoleCrawler().crawl(ctx)} == {"acme.widgets.other"}


def test_role_context_survives_task_import_outside_role(tmp_path):
    ctx = _variable_collection(
        tmp_path,
        [],
        {
            "roles/wrapper/tasks/main.yml": "- import_tasks: ../../../shared/tasks.yml\n",
            "shared/tasks.yml": "- include_vars: ../../agent/defaults/main.yml\n",
            "roles/wrapper/vars/placeholder.yml": "value: true\n",
        },
    )
    ctx.root = tmp_path / "roles/wrapper"
    assert {f.name for f in RoleCrawler().crawl(ctx)} == {"acme.widgets.agent"}


def test_imported_playbook_resets_variable_search_base(tmp_path):
    ctx = _variable_collection(
        tmp_path,
        [{"import_playbook": "../shared/play.yml"}],
        {
            "shared/play.yml": "- hosts: localhost\n  tasks:\n    - import_tasks: nested/tasks.yml\n",
            "shared/nested/tasks.yml": "- include_vars: values.yml\n",
            "scenario/values.yml": "wrong: true\n",
        },
    )
    (tmp_path / "shared/vars").mkdir()
    (tmp_path / "shared/vars/values.yml").symlink_to(
        tmp_path / "roles/agent/defaults/main.yml"
    )
    assert {f.name for f in RoleCrawler().crawl(ctx)} == {"acme.widgets.agent"}


@pytest.mark.parametrize("filename", ["values", "values.json"])
def test_variable_file_imports_do_not_require_yaml_suffix(tmp_path, filename):
    ctx = _variable_collection(
        tmp_path,
        [{"include_vars": f"../roles/agent/defaults/{filename}"}],
        {
            f"roles/agent/defaults/{filename}": '{"enabled": true}\n',
        },
    )
    assert {f.name for f in RoleCrawler().crawl(ctx)} == {"acme.widgets.agent"}


def test_explicit_dynamic_include_vars_file_overrides_literal_args(tmp_path):
    ctx = _variable_collection(
        tmp_path,
        [
            {
                "include_vars": {"file": "{{ unknown }}"},
                "args": {"file": "../roles/agent/defaults/main.yml"},
            }
        ],
    )
    assert list(RoleCrawler().crawl(ctx)) == []


@pytest.mark.parametrize(
    "metadata",
    ["[]", "namespace: acme", "not: [valid", "namespace: !vault secret\nname: widgets"],
)
def test_unusable_collection_metadata_does_not_infer_role(tmp_path, metadata):
    ctx = _variable_collection(
        tmp_path, [{"include_vars": "../roles/agent/defaults/main.yml"}]
    )
    (tmp_path / "galaxy.yml").write_text(metadata)
    assert list(RoleCrawler().crawl(ctx)) == []


def test_filtered_directory_import_preserves_executable_sources(tmp_path):
    ctx = _variable_collection(
        tmp_path,
        [
            {
                "hosts": "localhost",
                "roles": ["agent"],
                "tasks": [
                    {"include_vars": {"dir": ".", "files_matching": "^settings[.]yml$"}}
                ],
            }
        ],
        {"scenario/settings.yml": "enabled: true\n"},
    )
    assert {f.name for f in RoleCrawler().crawl(ctx)} == {"agent"}


@pytest.mark.parametrize("action", ["include_vars", "ansible.builtin.include_vars"])
def test_missing_external_search_candidate_does_not_hide_local_import(tmp_path, action):
    ctx = _variable_collection(
        tmp_path,
        [],
        {
            "roles/wrapper/tasks/main.yml": yaml.safe_dump(
                [{action: "../../../roles/agent/defaults/main.yml"}]
            ),
        },
    )
    ctx.root = tmp_path / "roles/wrapper"
    assert not (ctx.root / "vars").exists()
    assert {f.name for f in RoleCrawler().crawl(ctx)} == {"acme.widgets.agent"}


@pytest.mark.parametrize("action", ["include_vars", "ansible.builtin.include_vars"])
@pytest.mark.parametrize(
    "arguments",
    [
        "../roles/agent/defaults/main.yml name=loaded",
        "name=loaded ../roles/agent/defaults/main.yml",
        "'../roles/agent/defaults/main.yml' name=loaded",
    ],
)
def test_include_vars_combines_freeform_and_named_options(tmp_path, action, arguments):
    ctx = _variable_collection(tmp_path, [{action: arguments}])
    assert {f.name for f in RoleCrawler().crawl(ctx)} == {"acme.widgets.agent"}


@pytest.mark.parametrize(
    "data_name,play_name",
    [
        ("a_data.yml", "z_play.yml"),
        ("z_data.yml", "a_play.yml"),
    ],
)
def test_variable_payload_cannot_poison_executable_source_exclusions(
    tmp_path, data_name, play_name
):
    ctx = _variable_collection(tmp_path, [])
    _write_files(
        tmp_path,
        {
            f"scenario/{data_name}": "some_variable:\n  - import_tasks: ../shared/fake_tasks.yml\n",
            f"scenario/{play_name}": yaml.safe_dump(
                [
                    {
                        "hosts": "localhost",
                        "vars_files": [data_name],
                        "roles": ["agent"],
                    }
                ]
            ),
            "shared/fake_tasks.yml": yaml.safe_dump(
                [{"include_vars": f"../scenario/{play_name}"}]
            ),
        },
    )
    findings = list(RoleCrawler().crawl(ctx))
    assert [(f.name, f.path) for f in findings] == [
        ("agent", str(tmp_path / f"scenario/{play_name}"))
    ]


@pytest.mark.parametrize(
    "options,expected",
    [
        (_DirectoryOptions(depth=1), {"settings.yml", "ignored.yaml", "settings.json"}),
        (_DirectoryOptions(extensions=("json",)), {"settings.json"}),
        (
            _DirectoryOptions(files_matching="^settings"),
            {"settings.yml", "settings.json", "sub/settings.yml"},
        ),
        (
            _DirectoryOptions(ignore_files=("ignored[.]yaml",)),
            {"settings.yml", "settings.json", "sub/settings.yml"},
        ),
        (None, set()),
    ],
)
def test_directory_payload_classification_honors_literal_filters(
    tmp_path, options, expected
):
    _write_files(
        tmp_path,
        {
            "settings.yml": "enabled: true\n",
            "ignored.yaml": "ignored: true\n",
            "settings.json": '{"enabled": true}',
            "notes.txt": "notes\n",
            "sub/settings.yml": "nested: true\n",
        },
    )
    actual = _directory_files(tmp_path, options, tmp_path)
    assert {str(path.relative_to(tmp_path)) for path in actual} == expected


def test_directory_payload_classification_bounds_symlinks_and_cycles(tmp_path):
    collection = tmp_path / "collection"
    _write_files(collection, {"data/settings.yml": "enabled: true\n"})
    _write_files(tmp_path, {"outside/escaped.yml": "outside: true\n"})
    (collection / "data/escape").symlink_to(
        tmp_path / "outside", target_is_directory=True
    )
    (collection / "data/cycle").symlink_to(
        collection / "data", target_is_directory=True
    )
    assert _directory_files(collection / "data", _DirectoryOptions(), collection) == {
        collection / "data/settings.yml"
    }


@pytest.mark.parametrize(
    "options",
    [
        {"files_matching": "{{ selected_pattern }}"},
        {"depth": "{{ selected_depth }}"},
        {"extensions": "{{ selected_extensions }}"},
        {"ignore_files": "{{ ignored }}"},
    ],
)
def test_dynamic_directory_filters_preserve_executable_sources(tmp_path, options):
    ctx = _variable_collection(
        tmp_path,
        [
            {
                "hosts": "localhost",
                "roles": ["agent"],
                "tasks": [{"include_vars": {"dir": ".", **options}}],
            }
        ],
    )
    assert {f.name for f in RoleCrawler().crawl(ctx)} == {"agent"}


@pytest.mark.parametrize("data_first", [False, True])
def test_task_shaped_variable_sequence_is_classified_before_traversal(
    tmp_path, data_first
):
    data_name = "a_data.yml" if data_first else "z_data.yml"
    play_name = "z_play.yml" if data_first else "a_play.yml"
    ctx = _variable_collection(
        tmp_path,
        [],
        {
            f"scenario/{data_name}": yaml.safe_dump([{"include_vars": play_name}]),
            f"scenario/{play_name}": yaml.safe_dump(
                [
                    {
                        "hosts": "localhost",
                        "roles": ["agent"],
                        "vars_files": [data_name],
                    }
                ]
            ),
        },
    )
    assert {f.name for f in RoleCrawler().crawl(ctx)} == {"agent"}


def test_existing_external_search_candidate_blocks_local_fallback(tmp_path):
    collection = tmp_path / "collection"
    ctx = _variable_collection(
        collection,
        [],
        {
            "roles/wrapper/tasks/main.yml": "- include_vars: ../../../roles/agent/defaults/main.yml\n",
        },
    )
    _write_files(tmp_path, {"roles/agent/defaults/main.yml": "outside: true\n"})
    ctx.root = collection / "roles/wrapper"
    assert list(RoleCrawler().crawl(ctx)) == []


def test_executable_walker_ignores_module_payloads_and_keeps_block_tasks(tmp_path):
    ctx = _variable_collection(
        tmp_path,
        [
            {
                "hosts": "localhost",
                "tasks": [
                    {"debug": {"msg": {"include_role": "bogus"}}},
                    {
                        "block": [{"include_role": "agent"}],
                        "rescue": [{"include_role": "rescue_role"}],
                        "always": [{"include_role": "always_role"}],
                    },
                ],
            }
        ],
    )
    assert {f.name for f in RoleCrawler().crawl(ctx)} == {
        "agent",
        "rescue_role",
        "always_role",
    }
