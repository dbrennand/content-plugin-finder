from __future__ import annotations

import shlex
from collections import deque
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from content_plugin_finder.crawl.base import Crawler
from content_plugin_finder.crawl.context import CrawlContext
from content_plugin_finder.models import Finding, PluginKind

_YAML_SUFFIXES = frozenset({".yml", ".yaml"})
_ROLE_ACTIONS = frozenset(
    {
        "include_role",
        "import_role",
        "ansible.builtin.include_role",
        "ansible.builtin.import_role",
    }
)
_IMPORT_ACTIONS = frozenset(
    {
        "import_playbook",
        "import_tasks",
        "include_tasks",
        "ansible.builtin.import_playbook",
        "ansible.builtin.import_tasks",
        "ansible.builtin.include_tasks",
    }
)


class _RoleLoader(yaml.SafeLoader):
    """Read Ansible scalars without evaluating templates or decrypting vaults."""


_RoleLoader.add_constructor("!unsafe", _RoleLoader.construct_scalar)
# Encrypted values are opaque and must never become role names or import paths.
_RoleLoader.add_constructor("!vault", lambda loader, node: None)


def _is_role_metadata(path: Path) -> bool:
    return path.parent.name == "meta" and path.name in {"main.yml", "main.yaml", "main"}


def _metadata_roles(document: Any) -> set[str]:
    if not isinstance(document, dict):
        return set()
    dependencies = document.get("dependencies")
    if not isinstance(dependencies, list):
        return set()
    roles = set()
    for dependency in dependencies:
        name = _option(dependency, "role")
        if _literal(name):
            roles.add(name)
    return roles


def _option(value: Any, key: str) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        candidate = value.get(key)
        if isinstance(candidate, str):
            return candidate.strip() or None
    return None


def _literal(value: str | None) -> bool:
    return bool(value) and "{{" not in value and "{%" not in value


def _action_options(value: Any, positional: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    if "=" not in value:
        return {positional: value.strip()}
    # Normalize inline arguments without evaluating templates.
    try:
        arguments = shlex.split(value)
    except ValueError:
        return {positional: None}
    options = {}
    for argument in arguments:
        key, separator, candidate = argument.partition("=")
        if separator:
            options[key] = candidate.strip() or None
    return options


def _task_references(node: dict) -> Iterable[tuple[str, str | None]]:
    actions = [
        (action, node[action])
        for action in _ROLE_ACTIONS | _IMPORT_ACTIONS
        if action in node
    ]
    action = node.get("action")
    if isinstance(action, str) and action.strip():
        name, *arguments = action.split(maxsplit=1)
        if name in _ROLE_ACTIONS | _IMPORT_ACTIONS:
            actions.append((name, arguments[0] if arguments else None))
    for action, value in actions:
        key = "name" if action in _ROLE_ACTIONS else "file"
        # Explicit action arguments override task-level args, even when dynamic.
        defaults = node.get("args")
        options = dict(defaults) if isinstance(defaults, dict) else {}
        options.update(_action_options(value, key))
        yield action, _option(options, key)


def _walk_node(
    node: Any,
    visited: set[int] | None = None,
) -> tuple[set[str], set[str]]:
    roles: set[str] = set()
    imports: set[str] = set()
    if visited is None:
        visited = set()
    if isinstance(node, (list, dict)):
        if id(node) in visited:
            return roles, imports
        visited.add(id(node))

    if isinstance(node, list):
        for item in node:
            child_roles, child_imports = _walk_node(item, visited)
            roles.update(child_roles)
            imports.update(child_imports)
        return roles, imports
    if not isinstance(node, dict):
        return roles, imports

    role_list = node.get("roles")
    if "hosts" in node and isinstance(role_list, list):
        for item in role_list:
            raw = _option(item, "role")
            if _literal(raw):
                roles.add(raw)

    for action, raw in _task_references(node):
        if _literal(raw):
            if action in _ROLE_ACTIONS:
                roles.add(raw)
            else:
                imports.add(raw)

    for value in node.values():
        child_roles, child_imports = _walk_node(value, visited)
        roles.update(child_roles)
        imports.update(child_imports)
    return roles, imports


def _below(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


class RoleCrawler(Crawler):
    name = "role"
    kinds = frozenset({PluginKind.ROLE})

    def crawl(self, ctx: CrawlContext) -> Iterable[Finding]:
        root = ctx.root.resolve()
        parent = (ctx.parent or root).resolve()
        queue: deque[Path] = deque(
            sorted(
                path.resolve()
                for path in root.rglob("*")
                if path.is_file()
                and (path.suffix.lower() in _YAML_SUFFIXES or _is_role_metadata(path))
            )
        )
        visited: set[Path] = set()
        findings: list[Finding] = []
        seen: set[tuple[str, str]] = set()

        while queue:
            path = queue.popleft().resolve()
            if path in visited or not _below(path, parent):
                continue
            visited.add(path)
            try:
                documents = list(
                    yaml.load_all(path.read_text(encoding="utf-8"), Loader=_RoleLoader)
                )
            except (OSError, UnicodeError, yaml.YAMLError):
                continue

            for document in documents:
                if document is None:
                    continue
                roles, imports = _walk_node(document)
                if _is_role_metadata(path):
                    roles.update(_metadata_roles(document))
                for name in sorted(roles):
                    key = (name, str(path))
                    if key in seen:
                        continue
                    seen.add(key)
                    findings.append(
                        Finding(
                            kind=PluginKind.ROLE,
                            name=name,
                            path=str(path),
                            source="yaml-role-reference",
                        )
                    )
                for imported in sorted(imports):
                    imported_path = (path.parent / imported).resolve()
                    if (
                        imported_path.is_file()
                        and imported_path.suffix.lower() in _YAML_SUFFIXES
                        and _below(imported_path, parent)
                    ):
                        queue.append(imported_path)
        return findings
