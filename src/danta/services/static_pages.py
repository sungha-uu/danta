from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class StaticPagesError(RuntimeError):
    """Raised when a sanitized static site cannot be published safely."""


def publish_static_site(
    *,
    source_dir: Path,
    repository: Path,
    commit_message: str,
) -> str:
    if not repository.exists():
        raise StaticPagesError(f"Pages repository does not exist: {repository}")
    _git(repository, "rev-parse", "--is-inside-work-tree")
    if _git(repository, "status", "--porcelain").strip():
        raise StaticPagesError(f"Pages repository has uncommitted changes: {repository}")
    for name in ("index.html", ".nojekyll"):
        source = source_dir / name
        if not source.exists():
            raise StaticPagesError(f"static artifact is missing: {source}")
        temporary = repository / f".{name}.tmp"
        shutil.copy2(source, temporary)
        temporary.replace(repository / name)
    _git(repository, "add", "--", "index.html", ".nojekyll")
    staged = subprocess.run(
        ["git", "-C", str(repository), "diff", "--cached", "--quiet"],
        check=False,
    )
    if staged.returncode not in {0, 1}:
        raise StaticPagesError("failed to inspect staged Pages changes")
    if staged.returncode == 1:
        _git(repository, "commit", "-m", commit_message)
        _git(repository, "push", "origin", "HEAD:main")
    return _git(repository, "rev-parse", "HEAD").strip()


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise StaticPagesError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout
