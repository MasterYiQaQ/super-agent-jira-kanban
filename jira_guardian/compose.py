"""评论排版：JIRA 不渲染 `###`，所以要先把标题压成加粗行，再盖上 bot 标记。

bot 标记有两个用途：让人一眼看出这条是机器写的；让值守判定把"我们自己发的"排除在
"新评论"之外（否则每发一条评论就会触发下一轮，自己咬自己）。
"""

from __future__ import annotations

import re

DEFAULT_MARK = "[super-agent]"

_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*\S)\s*$")


def flatten_headings(body: str) -> str:
    """代码块外：`### 标题` → `**标题**`。代码块内原样保留。"""
    out: list[str] = []
    in_fence = False
    for line in (body or "").splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            out.append(line)
            continue
        if not in_fence:
            m = _HEADING_RE.match(line)
            if m:
                out.append(f"**{m.group(1).strip()}**")
                continue
        out.append(line)
    return "\n".join(out)


def stamp(body: str, mark: str = DEFAULT_MARK) -> str:
    """排版规整 + 末尾补标记（已经有就不重复加）。"""
    text = flatten_headings(body).rstrip()
    if not text:
        return text
    if text.startswith(mark) or text.endswith(mark) or f"\n{mark}" in text:
        return text
    return f"{text}\n\n{mark}"
