"""初始化一个值守目录：config.json + 目录骨架 + .gitignore + 知识库模板。

    guardian init --jira-url https://jira.example.com --token <PAT> \
        --project PROJ --me your.name \
        --repo service-api=D:\\Code\\service-api \
        --repo service-ui=D:\\Code\\service-ui

不覆盖已存在的 config.json（除非 --force），免得把人配好的东西冲掉。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import Config, DEFAULTS, _deep_merge

GITIGNORE = """# 运行时产物
data/
workspace/
*.pyc
__pycache__/
.venv/
"""

KNOWLEDGE_STUB = """# 代码地图

> 这个文件会在每张卡的 prompt 里带上（截断到 1800 字符）。写"卡上的问题该去哪个仓库找"，
> 不要写通用编程常识。子任务靠它直接定位，省掉一轮瞎找。

## 仓库一览

| 仓库 | 语言/栈 | 负责什么 | 本地源码 |
|---|---|---|---|
|  |  |  |  |

## 定位套路

- 报错信息 `XXX` → 先看 `repo/路径/文件.py`
- 接口 `/api/v1/xxx` → 路由层 → service 层 → 数据层
- 前端交互 → `xxx-ui/src/components/...`

## 口径与坑

- （示例）同一张卡可能同时涉及 1.0 与 2.0 两套实现，先确认卡上的版本再动代码。
"""


def scaffold(
    root: str,
    *,
    jira_url: str,
    token: str,
    project: str = "",
    me: str = "",
    repos: dict[str, str] | None = None,
    base_branch: str = "",
    dev: bool = True,
    force: bool = False,
    verify: dict[str, str] | None = None,
) -> dict[str, Any]:
    base = Path(root).expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)
    path = base / "config.json"
    if path.exists() and not force:
        return {"ok": False, "error": f"{path} 已存在；要覆盖加 --force", "path": str(path)}

    jql_parts = []
    if project:
        jql_parts.append(f"project = {project}")
    jql_parts += ["issuetype = Bug", "assignee = currentUser()", "created >= startOfDay()", "resolution = Unresolved"]
    default_jql = " AND ".join(jql_parts) + " ORDER BY created DESC"

    repo_entries: dict[str, Any] = {}
    for name, src in (repos or {}).items():
        entry: dict[str, Any] = {"src": src}
        if verify and verify.get(name):
            entry["verify"] = verify[name]
        repo_entries[name] = entry

    cfg_data = _deep_merge(
        DEFAULTS,
        {
            "jira": {"base_url": jira_url.rstrip("/"), "token": token, "me": me, "default_jql": default_jql},
            "dev": {
                "enabled": bool(dev and repo_entries),
                "base_branch": base_branch,
                "repos": repo_entries,
            },
            "watch": {"pin_cards": [], "ignore_cards": []},
        },
    )
    path.write_text(json.dumps(cfg_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    cfg = Config(base)
    cfg.ensure_dirs()
    created: list[str] = [str(path)]

    gitignore = base / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(GITIGNORE, encoding="utf-8")
        created.append(str(gitignore))

    knowledge = base / "knowledge" / "codebase.md"
    if not knowledge.exists():
        knowledge.parent.mkdir(parents=True, exist_ok=True)
        knowledge.write_text(KNOWLEDGE_STUB, encoding="utf-8")
        created.append(str(knowledge))

    return {
        "ok": True,
        "root": str(base),
        "created": created,
        "jql": default_jql,
        "dev_enabled": cfg.dev.get("enabled"),
        "next": [
            "guardian doctor",
            "guardian jira today",
            "guardian tick seed --jql \"<你现在不想动的存量卡 JQL>\"",
            "guardian card   # 把输出贴成看板卡的 prompt",
        ],
    }
