"""Boards: read-only local copies of Altium 365 projects, and their history.

Each board is a git repository. A first clone holds only the latest revision
(``--depth=1``): Altium 365's git server can't leave file contents out of a clone (it
has no partial clone), and most of a board's history is old versions of large binary
files. Older history is fetched when a tool needs it, only as far back as it needs.
Past revisions are checked out into temporary worktrees that hold just the project's
folder.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator
from urllib.parse import urlsplit

from . import checks, config, git, netlist

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

# Files whose change can change the design's connectivity.
DESIGN_SUFFIXES = (".prjpcb", ".schdoc", ".pcbdoc", ".harness", ".netlist.json")
LFS_POINTER = b"version https://git-lfs.github.com/spec/v1"
# What find_commit fetches older history for: a commit id, or HEAD~n and the like.
REVISION = re.compile(r"(?:[0-9a-fA-F]{4,40}|HEAD)(?:[~^][0-9]*)*")
# Written into .git as the last step of a clone. A copy without it was interrupted.
COMPLETE_MARKER = "altium-helper-complete"
# Written into .git once a clone's download finished. The set-up after it can still fail
# (a repository with no single .PrjPcb, a path Windows can't check out); the next sync
# then redoes the set-up instead of downloading again.
DOWNLOADED_MARKER = "altium-helper-downloaded"
# Repack a copy once it has this many packs (each fetch adds one); git's own limit.
MAX_PACKS = 50
# A git lock file older than this was left by a git that died. On POSIX, network git
# commands hold the board lock until they exit (see _exclusive), so any lock file found
# while holding it is stale once a short local command could have finished. Windows
# can't pass the board lock to git, so there an orphaned fetch may still be running:
# wait out git's own 10-minute stall limit (git.NETWORK_SETTINGS).
STALE_GIT_LOCK_SECONDS = 900 if sys.platform == "win32" else 60


class BoardError(RuntimeError):
    """A board can't be found, fetched or read."""


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "board"


def is_design_file(path: str) -> bool:
    return path.lower().endswith(DESIGN_SUFFIXES)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@contextlib.contextmanager
def _exclusive(path: Path) -> Iterator[tuple[int, ...]]:
    """Hold an exclusive lock on ``path`` against other threads and processes.

    The operating system drops the lock once no process holds it open, so a crash can't
    leave a board locked. Yields the descriptors to pass to git (``git.run(keep_fds=)``):
    on POSIX a git that outlives this process then keeps the board locked until it exits,
    so the next call can't delete a copy git is still writing.
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
                yield ()
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield (handle.fileno(),)
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _remove_tree(path: Path) -> None:
    """Delete a folder git wrote. Git's object files are read-only, which Windows won't delete."""

    def make_writable_and_retry(function, failed, error):
        if sys.platform != "win32" or function not in (os.unlink, os.rmdir):
            raise error[1] if isinstance(error, tuple) else error  # 3.10/3.11: exc_info
        os.chmod(failed, stat.S_IWRITE)  # clears Windows' read-only attribute
        function(failed)

    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=make_writable_and_retry)
        else:
            shutil.rmtree(path, onerror=make_writable_and_retry)
    except OSError as e:
        raise BoardError(
            f"Couldn't remove the old copy at {path} ({e}). Delete that folder, then try again."
        ) from e


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

    def _git(
        self, args: list[str], cwd: Path | None = None, keep_fds: tuple[int, ...] = ()
    ) -> str:
        return git.run(
            args,
            cwd or self.repo,
            url=self.git_url,
            credentials=_credentials_for(self.git_url),
            keep_fds=keep_fds,
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

    def _lock(self) -> contextlib.AbstractContextManager[tuple[int, ...]]:
        """One clone, fetch or analysis of this board at a time, in any process.

        Claude often calls two tools at once, and both may be first to fetch a board.
        """
        return _exclusive(self.root / ".lock")

    def cloned(self) -> bool:
        """True once a clone of this board's repository has finished, read-only guards included.

        A clone that was interrupted (git killed, or the server stopped while git ran on)
        has no marker, and the next sync clones again.
        """
        git_dir = self.repo / ".git"
        # Keys follow the API's listing order, so a key can come to name another project.
        if self.meta().get("git_url", self.git_url) != self.git_url:
            return False
        if (git_dir / COMPLETE_MARKER).exists():
            return True
        # Copies made before these markers: the checkout was their last step, and git
        # writes the index only then.
        return (git_dir / "index").exists() and not (
            git_dir / DOWNLOADED_MARKER
        ).exists()

    def ensure_allowed(self) -> None:
        """Refuse boards on the exclude list, whatever route would fetch them."""
        settings = config.Settings.load()
        # A project id never changes; a name can be shared, and a key can move.
        if any(
            self.key == slug(x)
            or self.name.lower() == x.lower()
            or (self.project_id and x == self.project_id)
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
        updated = fetched = False
        with self._lock() as held:
            if not self.cloned():
                self._clone(held)
                updated = fetched = True
            else:
                if not self.project_file:  # another call may have cloned it meanwhile
                    self.project_file = self.meta().get("project_file", "")
                last = self.meta().get("last_sync")
                fresh = last and _now() - datetime.fromisoformat(last) < timedelta(
                    minutes=settings.sync_interval_minutes
                )
                if force or not fresh:
                    updated, fetched = self._update(held), True
            head = self.commit_info("HEAD")
            # Freshness counts from the last real fetch, so a board in constant use
            # still fetches every sync interval.
            stamp = {"last_sync": _now().isoformat()} if fetched else {}
            self._save_meta(head=head["rev"], **stamp)
        return {
            "board": self.name,
            "updated": updated,
            "latest_revision": head,
            "project": str(self.repo / self.project_file),
        }

    def _downloaded(self) -> bool:
        """Did a clone of this board's repository finish downloading, whatever came after?"""
        if not (self.repo / ".git" / DOWNLOADED_MARKER).exists():
            return False
        # An empty repository clones without a branch, and a fetch never adds one.
        if not self.resolve("refs/remotes/origin/HEAD"):
            return False
        try:
            # The stored URL: `remote get-url` applies the user's insteadOf rewrites.
            url = self._git(["config", "--get", "remote.origin.url"]).strip()
        except git.GitError:
            return False
        return url == self.git_url

    def _clone(self, held: tuple[int, ...] = ()) -> None:
        if self._downloaded():
            # Only the set-up failed last time: fetch what's new, then set up again.
            self._clear_stale_git_locks()
            self._git(["fetch", "--quiet", "--prune", "origin"], keep_fds=held)
        else:
            if self.repo.exists():
                # An interrupted clone, or another project's copy; nothing of the user's lives here.
                _remove_tree(self.repo)
            self.root.mkdir(parents=True, exist_ok=True)
            if self.meta().get("git_url", self.git_url) != self.git_url:
                self.meta_path.unlink()  # that project's branch and project file aren't ours
            git.run(
                [
                    "clone",
                    "--quiet",
                    "--depth=1",
                    "--no-checkout",
                    self.git_url,
                    str(self.repo),
                ],
                url=self.git_url,
                credentials=_credentials_for(self.git_url),
                keep_fds=held,
            )
            git.make_read_only(self.repo)
            # Keep each fetch as one pack. Loose, every new version of a large file would
            # be a full copy on disk, and only a pack lets git's own upkeep merge them.
            self._git(["config", "fetch.unpackLimit", "1"])
            (self.repo / ".git" / DOWNLOADED_MARKER).write_text(
                _now().isoformat() + "\n", encoding="utf-8"
            )
        head = self._git(
            ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"]
        ).strip()
        branch = head.split("/", 1)[1] if "/" in head else "master"
        self._save_meta(branch=branch, git_url=self.git_url, name=self.name)
        if not self.project_file:
            self.project_file = self._find_project_file(f"origin/{branch}")
        self._save_meta(project_file=self.project_file)
        self._sparse(self.repo)
        # -B and --force: a failed earlier set-up may have left the branch behind, or
        # files half checked out.
        self._git(
            ["checkout", "--quiet", "--force", "-B", branch, f"origin/{branch}"],
            keep_fds=held,
        )
        (self.repo / ".git" / COMPLETE_MARKER).write_text(
            _now().isoformat() + "\n", encoding="utf-8"
        )

    def _update(self, held: tuple[int, ...] = ()) -> bool:
        self._clear_stale_git_locks()
        if self._git(["status", "--porcelain", "--untracked-files=no"]).strip():
            raise BoardError(
                f"{self.repo} has local changes. altium-helper never changes board files; "
                "undo the changes there, then sync again."
            )
        before = self._git(["rev-parse", "HEAD"]).strip()
        self._git(["fetch", "--quiet", "--prune", "origin"], keep_fds=held)
        self._git(["merge", "--quiet", "--ff-only", f"origin/{self.branch}"])
        self._tidy()
        return self._git(["rev-parse", "HEAD"]).strip() != before

    def _tidy(self) -> None:
        """Repack once fetches have added many packs; git's background upkeep is off."""
        packs = (self.repo / ".git" / "objects" / "pack").glob("*.pack")
        if len(list(packs)) >= MAX_PACKS:
            self._git(["repack", "-a", "-d", "-q"])

    def _clear_stale_git_locks(self) -> None:
        """Delete lock files left by a git that died; each one would stop every later sync.

        Call only while holding the board lock, so no git of ours is using them.
        """
        git_dir = self.repo / ".git"
        found = [*git_dir.glob("*.lock"), *git_dir.glob("refs/**/*.lock")]
        for lock in found:
            with contextlib.suppress(OSError):
                if time.time() - lock.stat().st_mtime > STALE_GIT_LOCK_SECONDS:
                    lock.unlink()

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

    # ------------------------------------------------------ history on demand
    #
    # A copy can be shallow: it starts at some commit whose parents it doesn't hold yet.
    # Altium's server won't send a commit by its id, but it can send older history by
    # count (--deepen) or by date (--shallow-since). Never fetch with --depth, or with a
    # --shallow-since later than the copy's oldest commit: both shorten the history.

    def is_shallow(self) -> bool:
        """True while the copy lacks older history, as after a first clone."""
        return (self.repo / ".git" / "shallow").exists()

    def _oldest_commits(self) -> list[str]:
        """The commits a shallow copy starts at, whose parents it doesn't hold."""
        path = self.repo / ".git" / "shallow"
        return path.read_text(encoding="utf-8").split() if path.exists() else []

    def _fetch_history(self, how: str, needed: Callable[[], bool]) -> None:
        """Fetch older history: ``--deepen=<n>``, ``--shallow-since=<date>`` or ``--unshallow``.

        ``needed`` is checked again under the board lock, since a parallel call may have
        fetched it meanwhile.
        """
        if not self.is_shallow() or not needed():
            return
        self.ensure_allowed()
        with self._lock() as held:
            if not self.is_shallow() or not needed():
                return
            self._clear_stale_git_locks()
            self._git(["fetch", "--quiet", how, "origin"], keep_fds=held)
            self._tidy()

    def _reaches(self, when: datetime) -> bool:
        """Does the copy hold a commit from before ``when``, to the second? Then it holds
        the board as it was at ``when``, and a log from ``when`` on stops before the
        copy's first commit."""
        if not self.is_shallow():
            return True
        before = when.replace(microsecond=0) - timedelta(seconds=1)
        args = ["rev-list", "-1", "--first-parent", f"--before={before.isoformat()}"]
        return bool(self._git([*args, "HEAD"]).strip())

    def ensure_since(self, when: datetime) -> None:
        """Make sure the copy holds the board as it was at ``when``, and everything after."""
        if self._reaches(when):
            return
        # Every commit held is from `when` or later, so this only deepens. It stops at the
        # first commit from `when` on; the commit before needs one more.
        moment = when.replace(microsecond=0).isoformat()
        self._fetch_history(
            f"--shallow-since={moment}", lambda: not self._reaches(when)
        )
        self._fetch_history("--deepen=1", lambda: not self._reaches(when))
        # Commit times come from each member's own clock, so they can be out of order.
        self._fetch_history("--unshallow", lambda: not self._reaches(when))

    def ensure_commits(self, count: int, before: datetime | None = None) -> None:
        """Make sure log() can list ``count`` commits (from before ``before``), or all there are."""
        args = ["rev-list", "HEAD"]
        if before:
            args.insert(1, f"--before={before.isoformat()}")
        if self.project_folder != ".":
            args += ["--", self.project_folder]

        def missing() -> int:
            oldest = set(self._oldest_commits())  # log() leaves these out
            listed = [c for c in self._git(args).split() if c not in oldest]
            return count - len(listed)

        for round in range(5):  # each round deepens by what's missing
            if not self.is_shallow() or missing() <= 0:
                return
            if before and round == 1 and not self._reaches(before):
                # Counting from the latest commit doesn't reach back to `before`: fetch
                # back to it by date, then count from there.
                self.ensure_since(before)
                continue
            self._fetch_history(f"--deepen={missing()}", lambda: missing() > 0)
        self._fetch_history("--unshallow", lambda: missing() > 0)

    def find_commit(self, rev: str) -> str:
        """The full id of commit ``rev``, fetching older history if the copy doesn't reach
        it yet; "" if there's no such commit."""
        full = self.resolve(rev)
        step = 16
        while not full and self.is_shallow() and REVISION.fullmatch(rev):
            how = f"--deepen={step}" if step <= 64 else "--unshallow"
            self._fetch_history(how, lambda: not self.resolve(rev))
            step *= 2
            full = self.resolve(rev)
        return full

    def parent(self, rev: str) -> str | None:
        """The commit before ``rev``, fetched if the copy starts at ``rev``; None for the first."""
        full = self.resolve(rev) or rev
        self._fetch_history("--deepen=1", lambda: full in self._oldest_commits())
        return self.resolve(f"{full}^") or None

    def last_commit_before(self, when: datetime, exact: bool = False) -> str | None:
        """The board as it was at ``when``: its last commit before that time.

        With ``exact``, the commit that last changed the project's folder, not just one
        holding the same files: a shallow copy's first commit seems to change them all.
        """
        self.ensure_since(when)
        args = [
            "rev-list",
            "-1",
            "--first-parent",
            f"--before={when.isoformat()}",
            "HEAD",
        ]
        if self.project_folder == ".":
            return self._git(args).strip() or None
        args += ["--", self.project_folder]
        found = self._git(args).strip() or None
        step = 1
        while exact and found in self._oldest_commits():
            self._fetch_history(
                f"--deepen={step}", lambda: found in self._oldest_commits()
            )
            if found in self._oldest_commits():
                break
            found, step = self._git(args).strip() or None, step * 2
        return found

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
        # A shallow copy's first commit lists every file as added: it has no parent to
        # compare with. Callers fetch enough history first; this keeps it out regardless.
        oldest = set(self._oldest_commits())
        commits = []
        for record in self._git(args).split("\x1e")[1:]:
            full, author, date, subject, body, changes = record.split("\x1f", 5)
            if full in oldest:
                continue
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
        full = self.find_commit(rev)
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
    # Keep the project file found when the board was first cloned, if that copy is
    # still this board's: keys follow the API's listing order and can move.
    for board in boards.values():
        meta = board.meta()
        if (
            not board.project_file
            and meta.get("git_url", board.git_url) == board.git_url
        ):
            board.project_file = meta.get("project_file", "")
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
