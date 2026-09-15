import io
from pathlib import Path

from content_plugin_finder.cli import main

FIXTURES = Path(__file__).parent / "fixtures"
MOLECULE = FIXTURES / "molecule_scenario"


def test_list_crawlers(capsys):
    assert main(["--list-crawlers"]) == 0
    out = capsys.readouterr().out
    assert "module" in out
    assert "filter" in out
    assert "lookup" in out


def test_cli_text_scan(capsys):
    assert main([str(MOLECULE), "--types", "filter,lookup"]) == 0
    out = capsys.readouterr().out
    assert "filter: default" in out
    assert "lookup: file" in out


def test_cli_json_scan(capsys):
    assert main([str(MOLECULE), "--format", "json", "--types", "filter"]) == 0
    out = capsys.readouterr().out
    assert '"filters"' in out
    assert "default" in out


def test_cli_missing_dir():
    assert main(["/no/such/directory/content_plugin_finder"]) == 2


def test_cli_list_roots(tmp_path: Path, capsys):
    mol = tmp_path / "extensions" / "molecule" / "default"
    mol.mkdir(parents=True)
    (mol / "molecule.yml").write_text("driver:\n  name: default\n")
    assert main(["--parent", str(tmp_path), "--depth", "3", "--list-roots"]) == 0
    out = capsys.readouterr().out
    assert "molecule/default" in out


def test_cli_parent_scan(tmp_path: Path, capsys):
    mol = tmp_path / "extensions" / "molecule" / "default"
    mol.mkdir(parents=True)
    (mol / "molecule.yml").write_text("driver:\n  name: default\n")
    (mol / "converge.yml").write_text(
        "- hosts: localhost\n"
        "  tasks:\n"
        "    - debug:\n"
        "        msg: \"{{ 'x' | default('y') }}\"\n"
    )
    assert (
        main(
            [
                "--parent",
                str(tmp_path),
                "--depth",
                "3",
                "--types",
                "filter",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "filter: default" in out


def _build_mini_collection(col: Path) -> None:
    """Create minimal collection structure for impact tests."""
    # Create galaxy.yml
    col.mkdir(parents=True, exist_ok=True)
    (col / "galaxy.yml").write_text(
        "namespace: test\nname: collection\nversion: 1.0.0\n"
    )

    # Create a simple module
    plugins_dir = col / "plugins" / "modules"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    (plugins_dir / "test_module.py").write_text(
        "DOCUMENTATION = ''\n"
        "EXAMPLES = ''\n"
        "def main():\n"
        "    pass\n"
    )

    # Create integration test structure
    test_dir = col / "tests" / "integration" / "targets" / "test_target"
    test_dir.mkdir(parents=True, exist_ok=True)
    (test_dir / "aliases").write_text("test_target\n")
    tasks_dir = test_dir / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    (tasks_dir / "main.yml").write_text(
        "- name: Test task\n"
        "  debug:\n"
        "    msg: test\n"
    )


def test_no_cache_flag_accepted(tmp_path: Path, monkeypatch):
    """--no-cache is a valid flag and produces output."""
    col = tmp_path / "col"
    _build_mini_collection(col)
    # Mock stdin with empty input
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    result = main(["--impact", str(col), "--from-stdin", "--no-cache"])
    assert result == 0


def test_clear_cache_flag_accepted(tmp_path: Path, monkeypatch):
    """--clear-cache deletes the cache file and exits cleanly."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    col = tmp_path / "col"
    _build_mini_collection(col)

    # First run: populate cache
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert main(["--impact", str(col), "--from-stdin"]) == 0

    cache_dir = tmp_path / "xdg" / "content-plugin-finder"
    assert any(cache_dir.glob("*.json")), "Cache should be created after first run"

    # --clear-cache: cache file removed, rebuild succeeds
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    result = main(["--impact", str(col), "--from-stdin", "--clear-cache"])
    assert result == 0


def test_cache_dir_override(tmp_path: Path, monkeypatch):
    """--cache-dir places the cache in the given directory."""
    col = tmp_path / "col"
    custom = tmp_path / "custom_cache"
    _build_mini_collection(col)

    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert main(["--impact", str(col), "--from-stdin", "--cache-dir", str(custom)]) == 0

    assert any(custom.glob("*.json")), "Cache should be created in custom directory"
