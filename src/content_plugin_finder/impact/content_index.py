from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from content_plugin_finder.crawl.orchestrator import Orchestrator, default_workers
from content_plugin_finder.discover import discover_scan_roots
from content_plugin_finder.models import PluginKind


@dataclass
class ContentIndex:
    """Maps plugin FQCNs/names to scan roots that use them."""

    collection: str
    # plugin name (as found in content, plus FQCN when normalizable) -> root relpaths
    plugin_to_roots: dict[str, list[str]] = field(default_factory=dict)
    # root relpath -> root kind
    root_kinds: dict[str, str] = field(default_factory=dict)
    # role FQCN -> root relpaths
    role_to_roots: dict[str, list[str]] = field(default_factory=dict)
    # absolute root path -> relpath
    roots: dict[str, str] = field(default_factory=dict)


def classify_root(root: Path, parent: Path) -> str:
    """Return ``molecule`` or ``integration`` for a scan root."""
    if (root / "molecule.yml").is_file():
        return "molecule"
    try:
        rel = root.resolve().relative_to(parent.resolve())
    except ValueError:
        rel_parts = root.parts
    else:
        rel_parts = rel.parts
    if len(rel_parts) >= 4 and rel_parts[-4:-1] == ("tests", "integration", "targets"):
        return "integration"
    if (root / "aliases").is_file() or (root / "tasks").is_dir():
        return "integration"
    return "unknown"


def build_content_index(
    parent: Path,
    *,
    collection: str,
    depth: int = 4,
    kinds: list[PluginKind] | None = None,
    workers: int = default_workers(),
    collection_root: Path | None = None,
) -> ContentIndex:
    parent = parent.resolve()
    scan_roots = discover_scan_roots(parent, depth)
    report = Orchestrator().scan(
        scan_roots,
        kinds=set(kinds or list(PluginKind)) | {PluginKind.ROLE},
        workers=workers,
        parent=parent,
    )

    index = ContentIndex(collection=collection)
    plugin_to_roots: dict[str, set[str]] = defaultdict(set)
    role_to_roots: dict[str, set[str]] = defaultdict(set)

    for abs_dir, dir_report in report.directories.items():
        root = Path(abs_dir)
        try:
            rel = str(root.resolve().relative_to(parent))
        except ValueError:
            rel = str(root)
        index.roots[str(root.resolve())] = rel
        kind = classify_root(root, parent)
        index.root_kinds[rel] = kind
        if kind in {"molecule", "integration"}:
            for found_role in dir_report.roles:
                name = found_role.name
                role = name if "." in name else f"{collection}.{name}"
                role_to_roots[role].add(rel)

        names: list[str] = []
        for group in (dir_report.modules, dir_report.filters, dir_report.lookups):
            names.extend(p.name for p in group)

        for name in names:
            plugin_to_roots[name].add(rel)
            # Also index FQCN form when content used a short name from this collection
            if "." not in name:
                plugin_to_roots[f"{collection}.{name}"].add(rel)
            elif name.startswith(collection + "."):
                short = name[len(collection) + 1 :]
                plugin_to_roots[short].add(rel)

    index.plugin_to_roots = {k: sorted(v) for k, v in sorted(plugin_to_roots.items())}
    _expand_role_dependencies(
        role_to_roots,
        collection_root=(collection_root or parent).resolve(),
        collection=collection,
        workers=workers,
    )
    index.role_to_roots = {
        role: sorted(roots) for role, roots in sorted(role_to_roots.items())
    }
    return index


def _expand_role_dependencies(
    role_to_roots: dict[str, set[str]],
    *,
    collection_root: Path,
    collection: str,
    workers: int,
) -> None:
    """Propagate roots through local role references, including dependency cycles.

    Test discovery may use a narrower parent (for example extensions/), while
    role sources and their static imports are bounded by the collection root.
    Scan each reachable role once using the same crawler as scenario discovery.
    """
    pending = set(role_to_roots)
    dependencies: dict[str, set[str]] = {}
    prefix = f"{collection}."
    while pending:
        paths: dict[str, Path] = {}
        for role in sorted(pending):
            if not role.startswith(prefix):
                continue
            short = role[len(prefix) :]
            if not short or "/" in short or "\\" in short or "." in short:
                continue
            path = (collection_root / "roles" / short).resolve()
            if path.is_relative_to(collection_root) and path.is_dir():
                paths[role] = path
        dependencies.update((role, set()) for role in pending)
        report = Orchestrator().scan(
            list(paths.values()),
            kinds=[PluginKind.ROLE],
            workers=workers,
            parent=collection_root,
        )
        for role, path in paths.items():
            dependencies[role] = {
                found.name if "." in found.name else f"{collection}.{found.name}"
                for found in report.directories[str(path)].roles
            }
        pending = {
            dependency
            for role in paths
            for dependency in dependencies[role]
            if dependency not in dependencies
        }

    for role, roots in list(role_to_roots.items()):
        roots = set(roots)
        seen = {role}
        pending = set(dependencies.get(role, ()))
        while pending:
            dependency = pending.pop()
            if dependency in seen:
                continue
            seen.add(dependency)
            role_to_roots.setdefault(dependency, set()).update(roots)
            pending.update(dependencies.get(dependency, ()))


def roots_using_plugins(index: ContentIndex, plugins: set[str]) -> dict[str, list[str]]:
    """Return root_relpath -> list of matching plugin names."""
    hits: dict[str, list[str]] = defaultdict(list)
    for plugin in plugins:
        for root in index.plugin_to_roots.get(plugin, []):
            hits[root].append(plugin)
        # try FQCN / short variants
        if "." not in plugin:
            fqcn = f"{index.collection}.{plugin}"
            for root in index.plugin_to_roots.get(fqcn, []):
                if plugin not in hits[root]:
                    hits[root].append(plugin)
        elif plugin.startswith(index.collection + "."):
            short = plugin[len(index.collection) + 1 :]
            for root in index.plugin_to_roots.get(short, []):
                if plugin not in hits[root]:
                    hits[root].append(plugin)
    return {k: sorted(set(v)) for k, v in sorted(hits.items())}


def roots_using_roles(index: ContentIndex, roles: set[str]) -> dict[str, list[str]]:
    """Return root relpath to matching normalized role FQCNs."""
    hits: dict[str, set[str]] = defaultdict(set)
    for role in roles:
        for root in index.role_to_roots.get(role, []):
            hits[root].add(role)
    return {root: sorted(found) for root, found in sorted(hits.items())}
