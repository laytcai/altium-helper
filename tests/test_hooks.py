"""The repository's pre-commit hook must block board files and secrets."""

import subprocess
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[1] / "hooks"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    _git(tmp_path, "config", "core.hooksPath", str(HOOKS))
    return tmp_path


@pytest.mark.parametrize(
    "name",
    [
        "Board.SchDoc",
        "sub/Board.PcbDoc",
        "Board.PrjPcb",
        "out.netlist.json",
        "designs/x.txt",
        "sources.zip",
        ".env",
    ],
)
def test_board_files_and_secrets_are_blocked(repo, name):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    _git(repo, "add", "-f", name)
    result = _git(repo, "commit", "-q", "-m", "try")
    assert result.returncode != 0
    assert "refusing to commit" in result.stderr


def test_code_is_allowed(repo):
    (repo / "tool.py").write_text("print('hi')\n")
    _git(repo, "add", "tool.py")
    assert _git(repo, "commit", "-q", "-m", "Add tool").returncode == 0
