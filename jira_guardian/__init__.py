"""jira-guardian —— 把 JIRA 上的卡变成能干的活。

三件套：
- ``tick``：判定谁新、谁的评论没回过、该给执行会话什么任务说明（写进 workspace/prompt/<KEY>.md）。
- ``worktree``：从 ``origin/HEAD`` 开一条干净的分支给执行会话改代码。
- ``jira``：读卡、看图、发评论、挪卡。

调度（谁每 15 分钟叫一次）交给宿主，比如 DSH 的看板卡 cron。
"""

from __future__ import annotations

__version__ = "0.3.0"

from .config import Config, ConfigError  # noqa: E402
from .jira import Jira, JiraError  # noqa: E402
from .state import State  # noqa: E402
from .tick import Tick  # noqa: E402

__all__ = ["Config", "ConfigError", "Jira", "JiraError", "State", "Tick", "__version__"]
