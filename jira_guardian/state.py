"""水位线（state）读写：谁被看过了、谁还在跑、跑在哪个会话里。

一个 JSON 文件就是全部状态，重启不丢；写入走 tmp+replace，中途崩了不会留下半个文件。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

SCHEMA = 2
LOG_KEEP = 200

CARD_FIELDS: dict[str, Any] = {
    "status": "",
    "summary": "",
    "round": 1,
    "updated": 0,
    "comment_ts": 0,
    "seen_at": 0,
    "active": False,
    "in_flight": False,
    "dispatched_at": 0,
    "session_id": "",
    "board_card": "",
}


def _empty() -> dict[str, Any]:
    return {"schema": SCHEMA, "cards": {}, "last_tick": 0, "log": []}


class State:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.data = self._load()

    # ------------------------------------------------------------------ IO
    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return _empty()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return _empty()
        if not isinstance(raw, dict):
            return _empty()
        return self._migrate(raw)

    def _migrate(self, raw: dict[str, Any]) -> dict[str, Any]:
        data = _empty()
        data["last_tick"] = int(raw.get("last_tick") or 0)
        data["log"] = list(raw.get("log") or [])[-LOG_KEEP:]
        for key, card in (raw.get("cards") or {}).items():
            merged = dict(CARD_FIELDS)
            merged.update({k: v for k, v in (card or {}).items() if k in CARD_FIELDS})
            merged["round"] = int(merged.get("round") or 1)
            data["cards"][str(key)] = merged
        return data

    def save(self) -> None:
        self.data["schema"] = SCHEMA
        self.data["last_tick"] = int(time.time())
        self.data["log"] = list(self.data.get("log") or [])[-LOG_KEEP:]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------ 卡
    @property
    def cards(self) -> dict[str, dict[str, Any]]:
        return self.data.setdefault("cards", {})

    def get(self, key: str) -> dict[str, Any] | None:
        return self.cards.get(key)

    def put(self, key: str, **fields: Any) -> dict[str, Any]:
        card = dict(CARD_FIELDS)
        card.update(self.cards.get(key) or {})
        card.update(fields)
        card["round"] = int(card.get("round") or 1)
        self.cards[key] = card
        return card

    def pop(self, key: str) -> dict[str, Any] | None:
        return self.cards.pop(key, None)

    def active_keys(self) -> list[str]:
        return [k for k, c in self.cards.items() if c.get("active")]

    # ------------------------------------------------------------------ 日志
    def log(self, event: str, *, key: str = "", detail: str = "") -> None:
        self.data.setdefault("log", []).append(
            {"ts": int(time.time()), "event": event, "key": key, "detail": detail[:400]}
        )
