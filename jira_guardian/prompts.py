"""Prompt 模板：默认模板打包在 ``jira_guardian/templates/``，实例目录可覆盖。

覆盖方式：config.json 的 ``prompts.dev`` 指到自己的 .md（默认 ``prompts/dev.md``）。
模板里用 ``{{KEY}}`` 占位；拼完之后如果还留着 ``{{...}}``，说明有占位符没提供 —— 直接报错，
免得模型拿到一个带 ``{{SUMMARY}}`` 的 prompt 还照着干活。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .config import Config

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
_PLACEHOLDER = re.compile(r"\{\{([A-Z0-9_]+)\}\}")


class PromptError(RuntimeError):
    pass


def default_template(kind: str) -> str:
    path = _TEMPLATE_DIR / f"{kind}.md"
    if not path.is_file():
        raise PromptError(f"没有内置模板 {kind}.md")
    return path.read_text(encoding="utf-8")


def template_text(cfg: Config, kind: str) -> str:
    """实例里放了同名模板就用实例的，否则用内置的。"""
    configured = (cfg.data.get("prompts") or {}).get(kind)
    candidates: list[Path] = []
    if configured:
        candidates.append(cfg.path_of(configured))
    candidates.append(cfg.path_of(f"prompts/{kind}.md"))
    for path in candidates:
        if path.is_file():
            return path.read_text(encoding="utf-8")
    return default_template(kind)


def render(cfg: Config, kind: str, values: dict[str, Any]) -> str:
    text = template_text(cfg, kind)
    for name, value in values.items():
        text = text.replace("{{" + name + "}}", str(value))
    left = sorted(set(_PLACEHOLDER.findall(text)))
    if left:
        raise PromptError(f"模板 {kind} 里有未提供的占位符：{', '.join(left)}")
    return text
