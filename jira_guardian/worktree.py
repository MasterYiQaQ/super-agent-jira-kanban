"""给执行会话准备一个干净的 worktree。

为什么需要这个模块：手写 `git worktree add -b KEY origin/KEY` 会踩到"远端分支删了、
本地 remote-tracking ref 还在"的坑——新的一轮会直接开在上一轮的旧提交上，看起来
"改过了"其实什么都没重做。这里显式做两件事：

1. `git fetch --prune origin`：把远端已删除的分支从 `refs/remotes/origin/*` 清掉。
2. 分支一律从 `origin/HEAD`（识别不到就 origin/master → origin/main）开；本地已有同名
   分支时，先报清它比基线多几个提交、远端还在不在，再决定复用还是重开。

主仓库的工作副本永远不动——所有改动都发生在 `<worktree_root>/<仓库名>`。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .config import Config


class GitError(RuntimeError):
    pass


def _git(args: list[str], cwd: Path, *, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError as exc:  # git 不在 PATH
        raise GitError("找不到 git 可执行文件，请先安装并加入 PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {' '.join(args)} 超时（{timeout}s）") from exc


def _out(proc: subprocess.CompletedProcess[str]) -> str:
    return (proc.stdout or "").strip()


def _fail(proc: subprocess.CompletedProcess[str], what: str) -> GitError:
    detail = ((proc.stderr or "") + (proc.stdout or "")).strip()[:600]
    return GitError(f"{what} 失败（exit {proc.returncode}）：{detail}")


def is_repo(path: Path) -> bool:
    return (path / ".git").exists()


def default_base_ref(src: Path) -> str:
    """origin/HEAD 指向哪个分支就用哪个；识别不到就按 master → main 试。"""
    proc = _git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], src)
    ref = _out(proc)
    if proc.returncode == 0 and ref.startswith("origin/"):
        return ref
    for candidate in ("origin/master", "origin/main"):
        check = _git(["rev-parse", "--verify", "--quiet", candidate], src)
        if check.returncode == 0:
            return candidate
    raise GitError(f"{src} 里找不到 origin/master 或 origin/main，请在 config.dev.base_branch 里显式指定")


def _remote_branch_exists(src: Path, branch: str) -> bool:
    proc = _git(["ls-remote", "--heads", "origin", branch], src, timeout=60)
    return proc.returncode == 0 and bool(_out(proc))


def _count(src: Path, rng: str) -> int:
    proc = _git(["rev-list", "--count", rng], src)
    try:
        return int(_out(proc) or 0)
    except ValueError:
        return 0


def prepare(
    cfg: Config,
    repo: str,
    branch: str,
    *,
    base: str | None = None,
    fetch: bool = True,
    fresh: bool = False,
) -> dict[str, Any]:
    """准备好 `<worktree_root>/<repo>`，分支 `<branch>` 从基线开出。

    ``fresh=True`` 时，本地已有的同名分支会被先删掉再重开（确认要"真·从零"时用）。
    """
    src = cfg.repo_src(repo)
    if not src.is_dir():
        raise GitError(f"源码目录不存在：{src}")
    if not is_repo(src):
        raise GitError(f"{src} 不是 git 仓库")
    cfg.worktree_root.mkdir(parents=True, exist_ok=True)
    dest = cfg.worktree_root / repo

    notes: list[str] = []
    pruned = False
    if fetch:
        proc = _git(["fetch", "--prune", "origin"], src, timeout=300)
        if proc.returncode != 0:
            raise _fail(proc, "git fetch --prune origin")
        pruned = True

    base = base or (cfg.dev.get("base_branch") or "").strip() or default_base_ref(src)
    if _git(["rev-parse", "--verify", "--quiet", base], src).returncode != 0:
        raise GitError(f"基线 {base} 不存在（先 fetch，或在 config.dev.base_branch 里改）")

    # 已经挂好的 worktree：复用，但要说清它是否干净、在哪个分支
    if dest.exists():
        listed = _out(_git(["worktree", "list", "--porcelain"], src))
        if f"worktree {dest}" not in listed.replace("\\", "/") and str(dest).replace("\\", "/") not in listed:
            raise GitError(f"{dest} 已存在但不是 git worktree；先手工删掉再重试")
        head_branch = _out(_git(["rev-parse", "--abbrev-ref", "HEAD"], dest))
        dirty = _out(_git(["status", "--porcelain"], dest))
        if head_branch != branch:
            if not fresh:
                raise GitError(
                    f"{dest} 当前在分支 {head_branch}，不是 {branch}；"
                    "加 --fresh 会拆掉这个 worktree 并从基线重开"
                )
            proc = _git(["worktree", "remove", "--force", str(dest)], src)
            if proc.returncode != 0:
                raise _fail(proc, f"git worktree remove {dest}")
            notes.append(f"按 --fresh 拆掉了停在 {head_branch} 上的旧 worktree")
        elif fresh:
            # 同一个分支也要推倒重来：这正是"上一轮的成果还挂在这里"的场景。
            proc = _git(["worktree", "remove", "--force", str(dest)], src)
            if proc.returncode != 0:
                raise _fail(proc, f"git worktree remove {dest}")
            notes.append("按 --fresh 拆掉了已有 worktree，准备从基线重开")
        else:
            if fresh and dirty:
                raise GitError(f"{dest} 有未提交改动（fresh 模式不覆盖），先提交或清理")
            return {
                "ok": True,
                "repo": repo,
                "src": str(src),
                "dest": str(dest),
                "branch": branch,
                "base": base,
                "created": False,
                "reused": True,
                "clean": not dirty,
                "pushed_remote": _remote_branch_exists(src, branch),
                "head": _out(_git(["rev-parse", "--short", "HEAD"], dest)),
                "notes": ["复用已有 worktree"],
            }

    ahead = _count(src, f"{base}..{branch}") if _git(
        ["rev-parse", "--verify", "--quiet", branch], src
    ).returncode == 0 else 0
    local_exists = ahead > 0 or _git(["rev-parse", "--verify", "--quiet", branch], src).returncode == 0
    remote_exists = _remote_branch_exists(src, branch)

    if local_exists and not remote_exists and ahead > 0 and not fresh:
        notes.append(
            f"本地分支 {branch} 比 {base} 多 {ahead} 个提交，但远端已经没有这个分支"
            "（上一轮推过又被删了）——本轮沿用这些提交，不是从零开始。要真·从零请加 --fresh。"
        )
    if local_exists and fresh:
        proc = _git(["branch", "-D", branch], src)
        if proc.returncode != 0:
            raise _fail(proc, f"删除本地分支 {branch}")
        notes.append(f"按 --fresh 删除了本地分支 {branch}")
        local_exists = False

    if local_exists:
        proc = _git(["worktree", "add", str(dest), branch], src, timeout=300)
        if proc.returncode != 0:
            raise _fail(proc, f"git worktree add {dest} {branch}")
        created = False
    else:
        proc = _git(["worktree", "add", "-b", branch, str(dest), base], src, timeout=300)
        if proc.returncode != 0:
            raise _fail(proc, f"git worktree add -b {branch} {dest} {base}")
        created = True
        notes.append(f"新分支 {branch} 从 {base} 开出")

    return {
        "ok": True,
        "repo": repo,
        "src": str(src),
        "dest": str(dest),
        "branch": branch,
        "base": base,
        "created": created,
        "reused": not created,
        "clean": not _out(_git(["status", "--porcelain"], dest)),
        "pushed_remote": remote_exists,
        "head": _out(_git(["rev-parse", "--short", "HEAD"], dest)),
        "pruned": pruned,
        "notes": notes,
    }


def status(cfg: Config, repo: str) -> dict[str, Any]:
    src = cfg.repo_src(repo)
    dest = cfg.worktree_root / repo
    if not dest.exists():
        return {"ok": True, "repo": repo, "dest": str(dest), "exists": False}
    return {
        "ok": True,
        "repo": repo,
        "dest": str(dest),
        "exists": True,
        "branch": _out(_git(["rev-parse", "--abbrev-ref", "HEAD"], dest)),
        "head": _out(_git(["rev-parse", "--short", "HEAD"], dest)),
        "dirty": [l for l in _out(_git(["status", "--porcelain"], dest)).splitlines() if l.strip()],
        "ahead_of_base": _count(dest, "origin/HEAD..HEAD") if _git(
            ["rev-parse", "--verify", "--quiet", "origin/HEAD"], dest
        ).returncode == 0 else None,
        "src": str(src),
    }


def clean(cfg: Config, repo: str, *, branch: str = "", delete_branch: bool = False) -> dict[str, Any]:
    """拆掉 worktree（可选连分支一起删）。改完代码、推完之后用。"""
    src = cfg.repo_src(repo)
    dest = cfg.worktree_root / repo
    removed: list[str] = []
    if dest.exists():
        proc = _git(["worktree", "remove", "--force", str(dest)], src)
        if proc.returncode != 0:
            raise _fail(proc, f"git worktree remove {dest}")
        removed.append(str(dest))
    if branch and delete_branch:
        proc = _git(["branch", "-D", branch], src)
        if proc.returncode == 0:
            removed.append(f"branch {branch}")
    _git(["worktree", "prune"], src)
    return {"ok": True, "repo": repo, "removed": removed}
