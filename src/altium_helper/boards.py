"""Boards: read-only local copies of Altium 365 projects, and their history.

Each board is a git repository cloned without file contents (``--filter=blob:none``),
so the full history arrives in seconds and file contents download only when a
revision is read. Past revisions are checked out into temporary worktrees that hold
just the project's folder.
"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Iterator
from urllib.parse import urlsplit

from . import checks, config, git, netlist

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

# Files whose change can change the design's connectivity.
DESIGN_SUFFIXES = (".prjpcb", ".schdoc", ".pcbdoc", ".harness", ".netlist.json")
LFS_POINTER = b"version https://git-lfs.github.com/spec/v1"


class BoardError(RuntimeError):
    """A board can't be found, fetched or read."""


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "board"


def is_design_file(path: str) -> bool:
    return path.lower().endswith(DESIGN_SUFFIXES)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@contextlib.contextmanager
def _exclusive(path: Path) -> Iterator[None]:
    """Hold an exclusive lock on ``path`` against other threads and processes.

    The operating system drops the lock if the process dies, so a crash can't leave
    a board locked.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as handle:
        if sys.platform == "win32":
            handle.seek(0)  # msvcrt locks bytes from the current position
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _credentials_for(url: str) -> git.Credentials | None:
    """Credentials for Altium 365 hosts only, so they're never sent anywhere else."""
    host = (urlsplit(url).hostname or "").lower()
    if not (host == "altium.com" or host.endswith(".altium.com")):
        return None
    settings = config.Settings.load()
    stored = config.load_credentials().get("git", {})
    if settings.git_auth in ("password", "token") and stored.get("password"):
        return git.Credentials(stored.get("username") or "token", stored["password"])
    return None


def test_git_access(url: str) -> tuple[bool, str]:
    """Can git read ``url`` with the saved sign-in? Returns (ok, git's message)."""
    try:
        git.run(
            ["ls-remote", "--heads", url], url=url, credentials=_credentials_for(url)
        )
    except git.GitError as e:
        return False, str(e)
    return True, "ok"


@dataclass
class Board:
    key: str
    name: str
    git_url: str
    project_file: str = ""  # path of the .PrjPcb inside the repository
    project_id: str = ""  # Altium 365 project id, when the board came from an API

    # ------------------------------------------------------------------ paths

    @property
    def root(self) -> Path:
        return config.designs_dir() / self.key

    @property
    def repo(self) -> Path:
        return self.root / "repo"

    @property
    def revisions_dir(self) -> Path:
        return self.root / "revisions"

    @property
    def meta_path(self) -> Path:
        return self.root / "board.json"

    def meta(self) -> dict:
        if not self.meta_path.exists():
            return {}
        return json.loads(self.meta_path.read_text(encoding="utf-8"))

    def _save_meta(self, **changes) -> None:
        meta = {**self.meta(), **changes}
        self.root.mkdir(parents=True, exist_ok=True)
        self.meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    def _git(self, args: list[str], cwd: Path | None = None) -> str:
        return git.run(
            args,
            cwd or self.repo,
            url=self.git_url,
            credentials=_credentials_for(self.git_url),
        )

    @property
    def branch(self) -> str:
        return self.meta().get("branch", "master")

    @property
    def project_folder(self) -> str:
        return (
            str(PurePosixPath(self.project_file).parent) if self.project_file else "."
        )

    # ------------------------------------------------------------------- sync

    def _lock(self) -> contextlib.AbstractContextManager[None]:
        """One clone, fetch or analysis of this board at a time, in any process.

        Claude often calls two tools at once, and both may be first to fetch a board.
        """
        return _exclusive(self.root / ".lock")

    def cloned(self) -> bool:
        return (self.repo / ".git").exists()

    def ensure_allowed(self) -> None:
        """Refuse boards on the exclude list, whatever route would fetch them."""
        settings = config.Settings.load()
        if any(
            self.key == slug(x) or self.name.lower() == x.lower()
            for x in settings.exclude
        ):
            raise BoardError(
                f"{self.name} is in the exclude list, so it's never fetched"
            )

    def sync(self, force: bool = False) -> dict:
        """Clone the board, or fetch new revisions if the copy is older than the sync interval."""
        self.ensure_allowed()
        if not self.git_url:
            raise BoardError(f"Altium 365 has no git repository for {self.name}")
        settings = config.Settings.load()
        updated = False
        with self._lock():
            if not self.project_file:  # another call may have cloned it meanwhile
                self.project_file = self.meta().get("project_file", "")
            if not self.cloned():
                self._clone()
                updated = True
            else:
                last = self.meta().get("last_sync")
                fresh = last and _now() - datetime.fromisoformat(last) < timedelta(
                    minutes=settings.sync_interval_minutes
                )
                if force or not fresh:
                    updated = self._update()
            head = self.commit_info("HEAD")
            self._save_meta(last_sync=_now().isoformat(), head=head["rev"])
        return {
            "board": self.name,
            "updated": updated,
            "latest_revision": head,
            "project": str(self.repo / self.project_file),
        }

    def _clone(self) -> None:
        if self.repo.exists():
            shutil.rmtree(
                self.repo
            )  # a clone that failed half-way; nothing of the user's lives here
        self.root.mkdir(parents=True, exist_ok=True)
        git.run(
            [
                "clone",
                "--quiet",
                "--filter=blob:none",
                "--no-checkout",
                self.git_url,
                str(self.repo),
            ],
            url=self.git_url,
            credentials=_credentials_for(self.git_url),
        )
        git.make_read_only(self.repo)
        head = self._git(
            ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"]
        ).strip()
        branch = head.split("/", 1)[1] if "/" in head else "master"
        self._save_meta(branch=branch, git_url=self.git_url, name=self.name)
        if not self.project_file:
            self.project_file = self._find_project_file(f"origin/{branch}")
        self._save_meta(project_file=self.project_file)
        self._sparse(self.repo)
        self._git(["checkout", "--quiet", branch])

    def _update(self) -> bool:
        if self._git(["status", "--porcelain", "--untracked-files=no"]).strip():
            raise BoardError(
                f"{self.repo} has local changes. altium-helper never changes board files; "
                "undo the changes there, then sync again."
            )
        before = self._git(["rev-parse", "HEAD"]).strip()
        self._git(["fetch", "--quiet", "--prune", "origin"])
        self._git(["merge", "--quiet", "--ff-only", f"origin/{self.branch}"])
        return self._git(["rev-parse", "HEAD"]).strip() != before

    def _find_project_file(self, rev: str) -> str:
        files = self._git(["ls-tree", "-r", "--name-only", rev]).splitlines()
        projects = [
            f
            for f in files
            if f.lower().endswith(".prjpcb") and "/history/" not in f.lower()
        ]
        if not projects:
            raise BoardError(f"{self.name}: no .PrjPcb in the repository")
        if len(projects) > 1:
            listed = ", ".join(projects)
            raise BoardError(
                f"{self.name} has several projects ({listed}). "
                "Add it with --project to choose one."
            )
        return projects[0]

    def _sparse(self, tree: Path) -> None:
        """Check out only the project's folder when the repository holds more than the project."""
        if self.project_folder != ".":
            self._git(
                ["sparse-checkout", "set", "--no-cone", f"/{self.project_folder}/"],
                tree,
            )

    # ---------------------------------------------------------------- history

    def commit_info(self, rev: str) -> dict:
        # Not .strip(): Python counts \x1f as whitespace, so a commit with no message
        # (Altium doesn't ask for one) would lose its last field.
        fields = self._git(
            ["show", "-s", "--format=%H%x1f%an%x1f%aI%x1f%s", rev]
        ).rstrip("\n")
        full, author, date, message = fields.split("\x1f", 3)
        return {
            "rev": full[:10],
            "full_rev": full,
            "date": date,
            "author": author,
            "message": message,
        }

    def resolve(self, rev: str) -> str:
        """The full commit id of ``rev``, or "" if there's no such revision."""
        try:
            return self._git(
                ["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"]
            ).strip()
        except git.GitError:
            return ""

    def last_commit_before(self, when: datetime) -> str | None:
        """The board as it was at ``when``: its last commit before that time."""
        args = [
            "rev-list",
            "-1",
            "--first-parent",
            f"--before={when.isoformat()}",
            "HEAD",
        ]
        if self.project_folder != ".":
            args += ["--", self.project_folder]
        return self._git(args).strip() or None

    def log(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        revision_range: str | None = None,
    ) -> list[dict]:
        """Commits newest first, each with the files it changed."""
        args = [
            "log",
            "--format=%x1e%H%x1f%an%x1f%aI%x1f%s%x1f%b%x1f",
            "--name-status",
        ]
        if since:
            args.append(f"--since={since.isoformat()}")
        if until:
            args.append(f"--until={until.isoformat()}")
        args += [f"-n{limit}", revision_range or "HEAD"]
        if self.project_folder != ".":
            args += ["--", self.project_folder]  # only this board's commits
        commits = []
        for record in self._git(args).split("\x1e")[1:]:
            full, author, date, subject, body, changes = record.split("\x1f", 5)
            files = []
            for line in changes.strip().splitlines():
                status, *paths = line.split("\t")
                files.append({"status": status[:1], "path": paths[-1]})
            commits.append(
                {
                    "rev": full[:10],
                    "full_rev": full,
                    "date": date,
                    "author": author,
                    "message": (
                        subject + ("\n\n" + body.strip() if body.strip() else "")
                    ).strip(),
                    "files": files,
                }
            )
        return commits

    @contextlib.contextmanager
    def worktree(self, rev: str) -> Iterator[Path]:
        """A temporary checkout of ``rev`` holding only the project's folder."""
        self.root.mkdir(parents=True, exist_ok=True)
        tree = Path(tempfile.mkdtemp(prefix="rev-", dir=self.root))
        try:
            self._git(
                [
                    "worktree",
                    "add",
                    "--quiet",
                    "--no-checkout",
                    "--detach",
                    str(tree),
                    rev,
                ]
            )
            self._sparse(tree)
            self._git(["checkout", "--quiet"], tree)
            yield tree
        finally:
            with contextlib.suppress(git.GitError):
                self._git(["worktree", "remove", "--force", str(tree)])
            shutil.rmtree(tree, ignore_errors=True)
            with contextlib.suppress(git.GitError):
                self._git(["worktree", "prune"])

    def analyze(self, rev: str) -> dict:
        """Netlist and schematic-vs-PCB check of one revision, cached by commit.

        Returns ``{"rev", "netlist_path", "netlist", "check"}``; ``netlist`` is None when the
        project doesn't exist at that revision.
        """
        full = self.resolve(rev)
        if not full:
            raise BoardError(f"{self.name}: no revision {rev!r}")
        with self._lock():  # a parallel call waits, then reads the cache
            self.revisions_dir.mkdir(parents=True, exist_ok=True)
            netlist_path = self.revisions_dir / f"{full}.netlist.json"
            check_path = self.revisions_dir / f"{full}.check.json"
            if not netlist_path.exists() or not check_path.exists():
                with self.worktree(full) as tree:
                    design = tree / self.project_file
                    if not design.exists():
                        return {
                            "rev": full[:10],
                            "netlist_path": None,
                            "netlist": None,
                            "check": None,
                        }
                    _refuse_lfs_pointers(tree / self.project_folder)
                    netlist.export_json(design, netlist_path)
                    loaded = json.loads(netlist_path.read_text(encoding="utf-8"))
                    if design.suffix.lower() == ".prjpcb":
                        report = checks.check_project(design, netlist=loaded)
                    else:  # a netlist file (e.g. from Nexar) has no documents or PCB
                        report = {
                            "project": design.name,
                            "documents": 0,
                            "missing_documents": [],
                            "pcb": [],
                        }
                    check_path.write_text(
                        json.dumps(report, indent=2), encoding="utf-8"
                    )
            return {
                "rev": full[:10],
                "netlist_path": str(netlist_path),
                "netlist": json.loads(netlist_path.read_text(encoding="utf-8")),
                "check": json.loads(check_path.read_text(encoding="utf-8")),
            }


def _refuse_lfs_pointers(folder: Path) -> None:
    for path in folder.rglob("*"):
        if path.is_file() and is_design_file(path.name):
            with path.open("rb") as f:
                if f.read(len(LFS_POINTER)) == LFS_POINTER:
                    raise BoardError(
                        "This repository keeps design files in Git LFS. Install git-lfs "
                        "(e.g. `sudo pacman -S git-lfs`), run `git lfs install`, then try again."
                    )


# --------------------------------------------------------------------- registry


def all_boards() -> dict[str, Board]:
    """Boards added by hand plus boards discovered through an API, keyed by slug."""
    boards: dict[str, Board] = {}
    discovered = config.designs_dir() / "boards.json"
    if discovered.exists():
        for entry in json.loads(discovered.read_text(encoding="utf-8")):
            board = Board(**entry)
            boards[board.key] = board
    for key, entry in config.Settings.load().boards.items():
        boards[key] = Board(
            key=key,
            name=entry.name or key,
            git_url=entry.git_url,
            project_file=entry.project_file,
        )
    # Keep the project file found when the board was first cloned.
    for board in boards.values():
        if not board.project_file:
            board.project_file = board.meta().get("project_file", "")
    return boards


def find(name: str) -> Board:
    """Find a board by key or name, ignoring case; a unique partial match also counts."""
    boards = all_boards()
    if not boards:
        raise BoardError(
            "No boards yet. Run `altium-helper login`, or add one with `altium-helper add-board`."
        )
    wanted = name.strip().lower()
    for board in boards.values():
        if wanted in (board.key, board.name.lower()) or slug(name) == board.key:
            return board
    partial = [
        b for b in boards.values() if wanted in b.name.lower() or wanted in b.key
    ]
    if len(partial) == 1:
        return partial[0]
    names = ", ".join(sorted(b.name for b in (partial or boards.values())))
    if partial:
        raise BoardError(f"{name!r} matches several boards: {names}")
    raise BoardError(f"No board called {name!r}. Boards: {names}")
