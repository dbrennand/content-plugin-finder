from __future__ import annotations

import re
import shlex
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from content_plugin_finder.collection.galaxy import read_collection_fqcn
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

_VAR_ACTIONS = frozenset({"include_vars", "ansible.builtin.include_vars"})
_TASK_CONTAINERS = (
    "tasks",
    "pre_tasks",
    "post_tasks",
    "handlers",
    "block",
    "rescue",
    "always",
)


@dataclass(frozen=True)
class _DirectoryOptions:
    depth: int = 0
    extensions: tuple[str, ...] = ("yaml", "yml", "json")
    files_matching: str | None = None
    ignore_files: tuple[str, ...] = ()


@dataclass(frozen=True)
class _VariableImport:
    kind: str
    # None preserves an unsupported candidate instead of selecting a later one.
    candidates: tuple[str | None, ...]
    # None means dynamic/invalid filters: ownership is known, payloads are not.
    directory_options: _DirectoryOptions | None = None


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
        name = _role_reference_name(dependency)
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


def _role_reference_name(value: Any) -> str | None:
    # Match Ansible's ds.get('role', ds.get('name')): only an absent role key
    # allows the alias, not an empty, null, or dynamic role value.
    key = "name" if isinstance(value, dict) and "role" not in value else "role"
    return _option(value, key)


def _literal(value: str | None) -> bool:
    return bool(value) and "{{" not in value and "{%" not in value and "{#" not in value


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
    raw_params = []
    for argument in arguments:
        key, separator, candidate = argument.partition("=")
        if separator:
            options[key] = candidate.strip() or None
        elif positional == "_raw_params":
            raw_params.append(argument)
    if raw_params:
        options[positional] = " ".join(raw_params)
    return options


def _task_options(node: dict) -> Iterable[tuple[str, dict[str, Any]]]:
    actions = [
        (action, node[action])
        for action in _ROLE_ACTIONS | _IMPORT_ACTIONS | _VAR_ACTIONS
        if action in node
    ]
    action = node.get("action")
    if isinstance(action, str) and action.strip():
        name, *arguments = action.split(maxsplit=1)
        if name in _ROLE_ACTIONS | _IMPORT_ACTIONS | _VAR_ACTIONS:
            actions.append((name, arguments[0] if arguments else None))
    for action, value in actions:
        key = (
            "name"
            if action in _ROLE_ACTIONS
            else "_raw_params"
            if action in _VAR_ACTIONS
            else "file"
        )
        # Explicit action arguments override task-level args, even when dynamic.
        defaults = node.get("args")
        options = dict(defaults) if isinstance(defaults, dict) else {}
        options.update(_action_options(value, key))
        yield action, options


def _task_references(node: dict) -> Iterable[tuple[str, str | None]]:
    for action, options in _task_options(node):
        if action not in _VAR_ACTIONS:
            yield (
                action,
                _option(options, "name" if action in _ROLE_ACTIONS else "file"),
            )


def _directory_options(options: dict[str, Any]) -> _DirectoryOptions | None:
    depth = options.get("depth", 0)
    extensions = options.get("extensions", ["yaml", "yml", "json"])
    ignored = options.get("ignore_files", [])
    matching = options.get("files_matching") or None
    if isinstance(extensions, str):
        if not _literal(extensions):
            return None
        extensions = list(extensions)  # Match include_vars' string normalization.
    if isinstance(ignored, str):
        ignored = ignored.split()
    if (
        not isinstance(depth, int)
        or isinstance(depth, bool)
        or depth < 0
        or not isinstance(extensions, list)
        or not isinstance(ignored, list)
        or any(
            not isinstance(value, str) or (value and not _literal(value))
            for value in [*extensions, *ignored]
        )
        or (
            matching is not None
            and (not isinstance(matching, str) or not _literal(matching))
        )
        or not isinstance(options.get("ignore_unknown_extensions", False), bool)
    ):
        return None
    try:
        if matching:
            re.compile(matching)
        for pattern in ignored:
            re.compile(pattern + "$")
    except re.error:
        return None
    return _DirectoryOptions(depth, tuple(extensions), matching, tuple(ignored))


def _executable_nodes(node: Any, visited: set[int] | None = None) -> Iterable[dict]:
    """Walk play/task sequences, never arbitrary variable or module mappings."""
    if not isinstance(node, list):
        return
    if visited is None:
        visited = set()
    if id(node) in visited:
        return
    visited.add(id(node))
    for item in node:
        if not isinstance(item, dict) or id(item) in visited:
            continue
        visited.add(id(item))
        yield item
        for key in _TASK_CONTAINERS:
            yield from _executable_nodes(item.get(key), visited)


def _variable_references(node: Any) -> set[_VariableImport]:
    references: set[_VariableImport] = set()
    for item in _executable_nodes(node):
        if "hosts" in item:
            entries = item.get("vars_files", [])
            if not isinstance(entries, list):
                entries = [entries]
            for entry in entries:
                candidates = entry if isinstance(entry, list) else [entry]
                references.add(
                    _VariableImport(
                        "vars_files",
                        tuple(
                            value
                            if isinstance(value, str) and _literal(value)
                            else None
                            for value in candidates
                        ),
                    )
                )
        for action, options in _task_options(item):
            if action not in _VAR_ACTIONS:
                continue
            if "dir" in options and ("file" in options or "_raw_params" in options):
                continue  # Mutually exclusive Ansible options.
            kind = "dir" if "dir" in options else "file"
            raw = _option(options, kind)
            if kind == "file" and "file" not in options:
                raw = _option(options, "_raw_params")
            if _literal(raw):
                directory = _directory_options(options) if kind == "dir" else None
                references.add(_VariableImport(kind, (raw,), directory))
    return references


def _walk_node(node: Any) -> tuple[set[str], set[tuple[str, str]]]:
    roles: set[str] = set()
    imports: set[tuple[str, str]] = set()
    for item in _executable_nodes(node):
        role_list = item.get("roles")
        if "hosts" in item and isinstance(role_list, list):
            for role in role_list:
                raw = _role_reference_name(role)
                if _literal(raw):
                    roles.add(raw)
        for action, raw in _task_references(item):
            if _literal(raw):
                if action in _ROLE_ACTIONS:
                    roles.add(raw)
                else:
                    imports.add((action, raw))
    return roles, imports


def _below(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _resolve_import(
    imported: str, directories: tuple[Path, ...], parent: Path
) -> Path | None:
    for directory in directories:
        candidate = (directory / imported).resolve()
        if _below(candidate, parent) and candidate.is_file():
            return candidate
    return None


def _collection_for(root: Path) -> tuple[Path, str] | None:
    # Test discovery may be rooted at extensions/, below the collection root.
    for directory in (root, *root.parents):
        if (directory / "galaxy.yml").exists():
            try:
                return directory, read_collection_fqcn(directory)
            except (OSError, UnicodeError, ValueError, AttributeError, yaml.YAMLError):
                return None
    return None


def _role_owner(path: Path, collection: Path) -> str | None:
    # Resolve before ownership checks, including for missing/deleted files.
    if not _below(path, collection):
        return None
    parts = path.relative_to(collection).parts
    if len(parts) < 2 or parts[0] != "roles":
        return None
    # A nested collection is not content owned by the outer collection.
    for directory in (path, *path.parents):
        if directory == collection:
            break
        if (directory / "galaxy.yml").exists():
            return None
    role = collection / "roles" / parts[1]
    if not role.is_dir() or role.resolve() != role:
        return None
    return parts[1]


def _variable_candidates(
    reference: _VariableImport,
    raw: str,
    path: Path,
    play_base: Path,
    role: Path | None,
) -> tuple[Path, ...]:
    """Mirror Ansible's variable loaders, without templating or reading payloads.

    See ansible/vars/manager.py, parsing/dataloader.py:path_dwim_relative_stack,
    playbook/base.py:get_search_path and plugins/action/include_vars.py:_set_root_dir.
    """
    if raw.startswith("~"):
        return ()  # Depends on the controller's home; not a collection-local literal.
    if Path(raw).is_absolute():
        return (Path(raw),)
    if reference.kind == "dir":
        base = path.parent if role is None else role
        if role is not None and raw.split("/")[0] != "vars":
            base = base / "vars"
        return (base / raw,)
    # Play vars_files do not inherit the task/include search stack.
    stack = (
        [path.parent]
        if reference.kind == "vars_files"
        else [*([role] if role else []), path.parent]
    )
    candidates = []
    for directory in dict.fromkeys([*stack, play_base]):
        if role and directory.parent.name == "tasks":
            candidates.extend(
                (directory.parent.parent / "vars" / raw, directory.parent / raw)
            )
        if raw.split("/")[0] != "vars":
            candidates.append(directory / "vars" / raw)
        candidates.append(directory / raw)
    return tuple(dict.fromkeys(candidates))


def _directory_files(
    root: Path,
    options: _DirectoryOptions | None,
    collection: Path,
) -> set[Path]:
    """Classify literal directory payloads without opening them or following escapes."""
    if options is None:
        return set()
    files: set[Path] = set()
    visited: dict[Path, int] = {}
    pending = [(root, 1)]
    while pending:
        directory, depth = pending.pop()
        try:
            real = directory.resolve()
            if visited.get(real, depth + 1) <= depth or not _below(real, collection):
                continue
            visited[real] = depth
            children = sorted(directory.iterdir())
            for child in children:
                resolved = child.resolve()
                if not _below(resolved, collection):
                    continue
                if child.is_dir():
                    if options.depth == 0 or depth < options.depth:
                        pending.append((child, depth + 1))
                elif (
                    child.is_file()
                    and child.suffix[1:] in options.extensions
                    and (
                        not options.files_matching
                        or re.search(options.files_matching, child.name)
                    )
                    and not any(
                        re.search(pattern + "$", child.name)
                        for pattern in options.ignore_files
                    )
                ):
                    files.add(resolved)
        except (OSError, RuntimeError, ValueError):
            continue
    return files


def _symlink_escapes(path: Path, collection: Path) -> bool:
    # Missing ordinary search candidates can lie outside the collection, but a
    # symlink escape (including a broken link) must not fall through to a local file.
    for component in (path, *path.parents):
        if component.is_symlink() and not _below(component.resolve(), collection):
            return True
    return False


def _resolve_variables(
    reference: _VariableImport,
    path: Path,
    play_base: Path,
    collection: Path,
    role: Path | None,
) -> tuple[set[Path], set[str]]:
    missing: list[Path] = []
    for raw in reference.candidates:
        if raw is None or raw.startswith("~"):
            return set(), set()  # Cannot statically choose beyond a dynamic candidate.
        candidates = _variable_candidates(reference, raw, path, play_base, role)
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except (OSError, RuntimeError, ValueError):
                return set(), set()
            if not _below(resolved, collection):
                if candidate.exists() or _symlink_escapes(candidate, collection):
                    return set(), set()
                continue  # A missing search candidate is not the selected import.
            if candidate.exists():
                expected = (
                    candidate.is_dir()
                    if reference.kind == "dir"
                    else candidate.is_file()
                )
                if not expected:
                    return set(), set()
                owner = _role_owner(resolved, collection)
                data = (
                    _directory_files(resolved, reference.directory_options, collection)
                    if reference.kind == "dir"
                    else {resolved}
                )
                return data, {owner} if owner else set()
            missing.append(resolved)
    # If nothing exists, retain a conservative deleted-file dependency when
    # the possible local role owners agree. Missing non-role candidates cannot
    # identify an owner; existing non-role files above always take precedence.
    owners = {_role_owner(candidate, collection) for candidate in missing} - {None}
    if len(owners) == 1:
        return set(), {owner for owner in owners if owner is not None}
    return set(), set()


@dataclass
class _ParsedReferences:
    roles: set[str]
    imports: set[tuple[str, str]]
    variables: set[_VariableImport]
    playbook: bool = False


def _parse_source(path: Path) -> _ParsedReferences:
    result = _ParsedReferences(set(), set(), set())
    try:
        documents = list(
            yaml.load_all(path.read_text(encoding="utf-8"), Loader=_RoleLoader)
        )
    except (OSError, UnicodeError, yaml.YAMLError):
        return result
    for document in documents:
        result.variables.update(_variable_references(document))
        roles, imports = _walk_node(document)
        result.roles.update(roles)
        result.imports.update(imports)
        if _is_role_metadata(path):
            result.roles.update(_metadata_roles(document))
        if isinstance(document, list):
            result.playbook |= any(
                isinstance(item, dict)
                and (
                    "hosts" in item
                    or any(
                        action.endswith("import_playbook")
                        for action, _ in _task_references(item)
                    )
                )
                for item in document
            )
    return result


_CollectionIdentity = tuple[Path, str]


def _source_role(path: Path, collection: _CollectionIdentity | None) -> Path | None:
    owner = _role_owner(path, collection[0]) if collection else None
    return collection[0] / "roles" / owner if collection and owner else None


class RoleCrawler(Crawler):
    name = "role"
    kinds = frozenset({PluginKind.ROLE})

    def crawl(self, ctx: CrawlContext) -> Iterable[Finding]:
        root = ctx.root.resolve()
        parent = (ctx.parent or root).resolve()
        collection = _collection_for(root)
        parsed: dict[Path, _ParsedReferences] = {}
        variable_data: set[Path] = set()
        history: set[frozenset[Path]] = set()
        while frozenset(variable_data) not in history:
            history.add(frozenset(variable_data))
            findings, current_data = self._crawl(
                root, parent, collection, parsed, variable_data
            )
            if current_data == variable_data:
                return findings
            # Recompute contributions from the surviving sources. Exclusions
            # discovered through a discarded source must not survive this pass.
            variable_data = current_data
        # Contradictory dual-use cycles have no unique static classification.
        # Treat the disputed files as data rather than interpreting their payloads.
        disputed = set().union(*history)
        return self._crawl(root, parent, collection, parsed, disputed)[0]

    def _crawl(
        self,
        root: Path,
        parent: Path,
        collection: _CollectionIdentity | None,
        parsed: dict[Path, _ParsedReferences],
        variable_data: set[Path],
    ) -> tuple[list[Finding], set[Path]]:
        candidates = sorted(
            path.resolve()
            for path in root.rglob("*")
            if path.is_file()
            and (
                path.suffix.lower() in _YAML_SUFFIXES
                or _is_role_metadata(path)
                or (path.name == "main" and path.parent.name in {"tasks", "handlers"})
            )
        )
        for path in candidates:
            if _below(path, parent) and path not in parsed:
                parsed[path] = _parse_source(path)
        # Resolve declarations in playbooks before incidental task-shaped YAML.
        # Imported executable children are also processed before fallback seeds.
        candidates.sort(
            key=lambda path: (
                not parsed.get(path, _ParsedReferences(set(), set(), set())).playbook,
                str(path),
            )
        )
        queue = deque(
            (path, (), path.parent, _source_role(path, collection))
            for path in candidates
        )
        current_data: set[Path] = set()
        visited: set[tuple[Path, tuple[Path, ...], Path, Path | None]] = set()
        findings: list[Finding] = []
        seen: set[tuple[str, str]] = set()

        while queue:
            path, ancestors, play_base, role = queue.popleft()
            context = (path, ancestors, play_base, role)
            if context in visited or not _below(path, parent):
                continue
            visited.add(context)
            if path in variable_data or path in current_data:
                continue
            if collection:
                owner = _role_owner(path, collection[0])
                if owner and path.relative_to(collection[0]).parts[2:3] in {
                    ("vars",),
                    ("defaults",),
                }:
                    continue
            if path not in parsed:
                parsed[path] = _parse_source(path)
            references = parsed[path]
            roles, imports, variables = (
                references.roles,
                references.imports,
                references.variables,
            )
            inferred: set[str] = set()
            if collection and _below(path, collection[0]):
                for reference in variables:
                    data, owners = _resolve_variables(
                        reference, path, play_base, collection[0], role
                    )
                    current_data.update(data)
                    inferred.update(f"{collection[1]}.{owner}" for owner in owners)
            for name in sorted(roles | inferred):
                key = (name, str(path))
                if key in seen:
                    continue
                seen.add(key)
                findings.append(
                    Finding(
                        kind=PluginKind.ROLE,
                        name=name,
                        path=str(path),
                        source="yaml-variable-role-reference"
                        if name in inferred
                        else "yaml-role-reference",
                    )
                )
            # Keep actual include directories, rather than searching arbitrary
            # filesystem parents. Ordered deduplication also bounds cyclic imports.
            directories = tuple(dict.fromkeys((path.parent, *ancestors)))
            for action, imported in sorted(imports):
                imported_path = _resolve_import(imported, directories, parent)
                if imported_path is not None:
                    # A new playbook establishes its own base directory.
                    fallbacks = (
                        () if action.endswith("import_playbook") else directories
                    )
                    queue.appendleft(
                        (
                            imported_path,
                            fallbacks,
                            imported_path.parent
                            if action.endswith("import_playbook")
                            else play_base,
                            None if action.endswith("import_playbook") else role,
                        )
                    )
        return findings, current_data
