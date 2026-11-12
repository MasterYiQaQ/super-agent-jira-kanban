"""配置：一个目录 = 一份 config.json + data/ + workspace/ + knowledge/。

设计要点
- 所有路径都相对"项目根"解析，根默认是当前工作目录，可用 ``--root`` 或 ``JG_ROOT`` 覆盖。
  这样工具包可以装在任意位置（pip install -e），实例目录只放配置与产物。
- 缺省值写在 ``DEFAULTS`` 里，用户 config.json 只需写想改的部分（深合并）。
- 敏感值可走环境变量：``JIRA_TOKEN`` / ``JIRA_BASE_URL`` / ``JG_LLM_API_KEY``。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

CONFIG_NAME = "config.json"

DEFAULTS: dict[str, Any] = {
    "jira": {
        "base_url": "",
        "auth_scheme": "Bearer",
        "token": "",
        "verify_tls": True,
        "bot_mark": "[super-agent]",
        "me": "",
        "allow_writes": True,
        "default_jql": "assignee = currentUser() AND created >= startOfDay() AND resolution = Unresolved ORDER BY created DESC",
    },
    "llm": {
        "base_url": "",
        "api_key": "",
        "model": "",
        "vision_model": "",
        "timeout": 180,
    },
    "watch": {
        "state_file": "data/watch-state.json",
        "outbox_dir": "workspace/outbox",
        "brief_dir": "workspace/jira",
        "prompt_dir": "workspace/prompt",
        "attach_dir": "workspace/attach",
        "max_items_per_tick": 3,
        "max_tracked_per_tick": 12,
        "in_flight_timeout_minutes": 40,
        "post_comment": True,
        "follow_up_on_new_comment": True,
        "pin_cards": [],
        "ignore_cards": [],
        "status_in_progress": "In Progress",
        "status_done": "Ready for Testing",
        "done_statuses": [
            "Ready for Testing",
            "Ready For UAT",
            "Ready For Deploy",
            "UAT",
            "Done",
            "Closed",
            "Resolved",
        ],
    },
    "dev": {
        "enabled": False,
        "worktree_root": "workspace/repos",
        "base_branch": "",
        "commit_types": ["feat", "fix", "chore", "docs", "refactor", "test"],
        "repos": {},
    },
    "git": {
        "user_name": "",
        "user_email": "",
        "credential_helper": "manager",
    },
    "prompts": {
        "dev": "prompts/dev.md",
        "readonly": "prompts/readonly.md",
        "router": "prompts/router.md",
    },
}


class ConfigError(RuntimeError):
    """配置缺失或写错——这一类错误要直接告诉人，不要吞。"""


def _deep_merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def find_root(explicit: str | os.PathLike[str] | None = None) -> Path:
    """根目录：显式参数 > JG_ROOT > 当前目录（含 config.json）> 逐级向上找。"""
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("JG_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    here = Path.cwd().resolve()
    for candidate in (here, *here.parents):
        if (candidate / CONFIG_NAME).is_file():
            return candidate
    return here


class Config:
    def __init__(self, root: str | os.PathLike[str] | None = None) -> None:
        self.root = find_root(root)
        self.path = self.root / CONFIG_NAME
        raw: dict[str, Any] = {}
        if self.path.is_file():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ConfigError(f"{self.path} 不是合法 JSON：{exc}") from exc
        self.data = _deep_merge(DEFAULTS, raw)
        self._apply_env()

    # ---------------------------------------------------------------- 读
    def _apply_env(self) -> None:
        jira = self.data["jira"]
        jira["base_url"] = (os.environ.get("JIRA_BASE_URL") or jira.get("base_url") or "").rstrip("/")
        jira["token"] = os.environ.get("JIRA_TOKEN") or jira.get("token") or ""
        llm = self.data["llm"]
        llm["api_key"] = os.environ.get("JG_LLM_API_KEY") or llm.get("api_key") or ""
        if os.environ.get("JG_LLM_BASE_URL"):
            llm["base_url"] = os.environ["JG_LLM_BASE_URL"]
        if os.environ.get("JG_LLM_MODEL"):
            llm["model"] = os.environ["JG_LLM_MODEL"]

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @property
    def jira(self) -> dict[str, Any]:
        return self.data["jira"]

    @property
    def watch(self) -> dict[str, Any]:
        return self.data["watch"]

    @property
    def dev(self) -> dict[str, Any]:
        return self.data["dev"]

    def path_of(self, rel: str | os.PathLike[str]) -> Path:
        p = Path(rel)
        return (p if p.is_absolute() else self.root / p).resolve()

    # 常用目录（都相对根）
    @property
    def state_file(self) -> Path:
        return self.path_of(self.watch.get("state_file") or "data/watch-state.json")

    @property
    def outbox_dir(self) -> Path:
        return self.path_of(self.watch.get("outbox_dir") or "workspace/outbox")

    @property
    def brief_dir(self) -> Path:
        return self.path_of(self.watch.get("brief_dir") or "workspace/jira")

    @property
    def prompt_dir(self) -> Path:
        return self.path_of(self.watch.get("prompt_dir") or "workspace/prompt")

    @property
    def attach_dir(self) -> Path:
        return self.path_of(self.watch.get("attach_dir") or "workspace/attach")

    @property
    def worktree_root(self) -> Path:
        return self.path_of(self.dev.get("worktree_root") or "workspace/repos")

    @property
    def knowledge_dir(self) -> Path:
        return self.path_of("knowledge")

    def ensure_dirs(self) -> None:
        for d in (
            self.state_file.parent,
            self.outbox_dir,
            self.brief_dir,
            self.prompt_dir,
            self.attach_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- 写
    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def repo(self, name: str) -> dict[str, Any]:
        repos = self.dev.get("repos") or {}
        meta = repos.get(name)
        if meta is None:
            raise ConfigError(f"dev.repos 里没有仓库 {name}；已登记：{', '.join(sorted(repos)) or '（空）'}")
        return meta if isinstance(meta, dict) else {"src": meta}

    def repo_src(self, name: str) -> Path:
        src = self.repo(name).get("src")
        if not src:
            raise ConfigError(f"dev.repos.{name}.src 没配")
        return Path(str(src)).expanduser()
