"""体检：接手别人的环境时先跑这个，别等 tick 报错才猜。

    guardian doctor

每项返回 ok / warn / fail + 一句人能看懂的话；有 fail 时退出码 1。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import Config, ConfigError
from .jira import Jira, JiraError


def _run(args: list[str], *, cwd: Path | None = None, timeout: int = 30, stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        input=stdin or None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"},
    )


def _check(name: str, status: str, detail: str) -> dict[str, str]:
    return {"name": name, "status": status, "detail": detail}


def run(root: str | None = None) -> dict[str, Any]:
    checks: list[dict[str, str]] = []

    major, minor = sys.version_info[:2]
    checks.append(
        _check("python", "ok" if (major, minor) >= (3, 10) else "fail", f"{sys.version.split()[0]}（需要 >= 3.10）")
    )

    try:
        cfg = Config(root)
    except ConfigError as exc:
        checks.append(_check("config", "fail", str(exc)))
        return _summary(checks)
    checks.append(_check("config", "ok", str(cfg.path)))

    jira = cfg.jira
    if not jira.get("base_url"):
        checks.append(_check("jira.base_url", "fail", "没配（或设 JIRA_BASE_URL）"))
    if not jira.get("token"):
        checks.append(_check("jira.token", "fail", "没配（或设 JIRA_TOKEN）"))

    client: Jira | None = None
    if jira.get("base_url") and jira.get("token"):
        try:
            client = Jira(cfg)
            me = client.me()
            who = me.get("name") or me.get("key") or "?"
            display = me.get("displayName") or ""
            checks.append(_check("jira.auth", "ok", f"{who} {display}".strip()))
        except (JiraError, Exception) as exc:  # noqa: BLE001
            checks.append(_check("jira.auth", "fail", f"{type(exc).__name__}: {exc}"))

    if client is not None:
        try:
            issues = client.search(cfg.jira.get("default_jql") or "", limit=1)
            n = len(issues)
            checks.append(
                _check("jira.jql", "ok" if n else "warn", f"默认 JQL 命中 {n} 张" + ("（今天没活）" if not n else ""))
            )
        except Exception as exc:  # noqa: BLE001
            checks.append(_check("jira.jql", "fail", f"{type(exc).__name__}: {exc}"))

    git = shutil.which("git")
    checks.append(_check("git", "ok" if git else "fail", git or "PATH 里没有 git"))

    repos = cfg.dev.get("repos") or {}
    if not repos:
        checks.append(_check("dev.repos", "warn", "没登记仓库：只能做只读分析，dev.enabled 应为 false"))
    for name, meta in repos.items():
        meta = meta if isinstance(meta, dict) else {"src": meta}
        src = Path(str(meta.get("src") or "")).expanduser()
        if not src.is_dir():
            checks.append(_check(f"repo:{name}", "fail", f"源码目录不存在：{src}"))
            continue
        if not (src / ".git").exists():
            checks.append(_check(f"repo:{name}", "fail", f"{src} 不是 git 仓库"))
            continue
        head = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=src)
        url = _run(["git", "remote", "get-url", "origin"], cwd=src)
        branch = (head.stdout or "").strip() or "?"
        checks.append(_check(f"repo:{name}", "ok", f"{branch} ← {(url.stdout or '').strip() or '无 origin'}"))
        if git:
            checks.append(_credential_check(name, url.stdout.strip()))

    try:
        cfg.worktree_root.mkdir(parents=True, exist_ok=True)
        probe = cfg.worktree_root / ".doctor-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        checks.append(_check("worktree_root", "ok", str(cfg.worktree_root)))
    except OSError as exc:
        checks.append(_check("worktree_root", "fail", f"不可写：{exc}"))

    llm = cfg.data.get("llm") or {}
    if cfg.dev.get("enabled"):
        if llm.get("base_url") and llm.get("api_key"):
            checks.append(_check("llm", "ok", f"{llm.get('model') or '?'} / vision {llm.get('vision_model') or '未配'}"))
        else:
            checks.append(_check("llm", "warn", "没配 llm：看图 / vision 用不了，其余功能不受影响"))

    # 模板占位符最容易写错，这里用一张假卡拉一遍渲染
    try:
        from .state import State
        from .tick import Tick

        if client is None:
            client = Jira(cfg)
        tick = Tick(cfg, client, State(cfg.state_file))
        text = tick.build_prompt("PROJ-1", "模板渲染自检")
        leftover = "{{" in text and "}}" in text
        checks.append(_check("prompts", "fail" if leftover else "ok", f"{len(text)} 字符"))
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("prompts", "fail", f"{type(exc).__name__}: {exc}"))

    try:
        from .state import State

        state = State(cfg.state_file)
        inflight = [k for k, c in state.cards.items() if c.get("in_flight")]
        checks.append(
            _check("state", "ok", f"{len(state.cards)} 张卡，占位 {len(inflight)} 个" + (f"：{', '.join(inflight)}" if inflight else ""))
        )
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("state", "fail", f"{type(exc).__name__}: {exc}"))

    return _summary(checks)


def _credential_check(repo: str, remote_url: str) -> dict[str, str]:
    """git 凭据能不能免交互取到——取不到说明 clone/push 会卡住等人输入。"""
    if not remote_url:
        return _check(f"credential:{repo}", "warn", "没有 origin，跳过")
    parsed = urlparse(remote_url)
    host = parsed.hostname or ""
    if not host:
        return _check(f"credential:{repo}", "warn", f"看不懂的 remote：{remote_url}")
    user = parsed.username or ""
    payload = f"protocol=https\nhost={host}\n" + (f"username={user}\n" if user else "") + "\n"
    try:
        proc = _run(["git", "credential", "fill"], stdin=payload, timeout=20)
    except Exception as exc:  # noqa: BLE001
        return _check(f"credential:{repo}", "warn", f"{type(exc).__name__}: {exc}")
    out = proc.stdout or ""
    if "password=" in out:
        return _check(f"credential:{repo}", "ok", f"{host} 已存凭据（免交互可用）")
    return _check(f"credential:{repo}", "warn", f"{host} 取不到凭据：push/clone 会要求输入密码")


def _summary(checks: list[dict[str, str]]) -> dict[str, Any]:
    fails = [c for c in checks if c["status"] == "fail"]
    warns = [c for c in checks if c["status"] == "warn"]
    return {
        "ok": not fails,
        "counts": {"ok": len(checks) - len(fails) - len(warns), "warn": len(warns), "fail": len(fails)},
        "checks": checks,
    }
