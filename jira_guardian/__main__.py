"""`python -m jira_guardian ...` 等价于 `guardian ...`（没装 entry point 时用）。"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
