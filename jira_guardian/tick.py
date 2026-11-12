"""值守判定：谁是新卡、谁的评论比上一轮新、该给执行会话什么任务说明。

判定的全部逻辑都在这里（而不是在卡片 prompt 里），所以换项目、换状态机只需要改
config.json，不动调度卡。调度会话只负责"把 items 派出去 + 把回执发回 JIRA"。

术语
- 水位线（state）：每张卡上次看到的状态与最新真人评论时间戳。
- 占位（in_flight）：这张卡正挂在某个执行会话里跑。跑着就不再派第二摊活。
- 回执（outbox）：执行会话写的 JSON 结论，主路由读它去发评论。
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Config
from .jira import Jira
from .prompts import render
from .state import State

FIELDS = "summary,status,issuetype,priority,updated,created,resolution,labels"
MAX_KNOWN_COMMENTS = 40


# --------------------------------------------------------------------- 工具
def jira_ts(value: str | None) -> int:
    """JIRA 的 `2026-11-11T09:04:00.000+0800` → epoch 秒。"""
    if not value:
        return 0
    text = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", str(value).strip())
    try:
        return int(datetime.fromisoformat(text).timestamp())
    except ValueError:
        return 0


def now_ts() -> int:
    return int(time.time())


_ORDER_RE = re.compile(r"\s+order\s+by\s+.*$", re.IGNORECASE)


class Tick:
    def __init__(self, cfg: Config, jira: Jira, state: State) -> None:
        self.cfg = cfg
        self.jira = jira
        self.state = state
        self.mark = str(cfg.jira.get("bot_mark") or "[super-agent]")
        self._own_trailer = re.compile(
            re.escape(self.mark) + r"\s*值守链路第\s*\d+\s*轮[^\n]*$|（" + re.escape(self.mark) + r"[^）]*）\s*$"
        )

    # ------------------------------------------------------------- 配置派生
    def ignore_cards(self) -> set[str]:
        raw = self.cfg.watch.get("ignore_cards") or []
        return {str(k).strip().upper() for k in raw if str(k).strip()}

    def pin_cards(self) -> list[str]:
        return [str(k).strip() for k in (self.cfg.watch.get("pin_cards") or []) if str(k).strip()]

    def done_statuses(self) -> set[str]:
        raw = self.cfg.watch.get("done_statuses") or []
        return {str(s).strip().lower() for s in raw}

    def is_done_status(self, status: str) -> bool:
        return (status or "").strip().lower() in self.done_statuses()

    def is_return_to_work(self, prev: str, cur: str) -> bool:
        return self.is_done_status(prev) and not self.is_done_status(cur)

    def in_flight_timeout(self) -> int:
        minutes = int(self.cfg.watch.get("in_flight_timeout_minutes") or 40)
        return max(minutes, 5) * 60

    def in_flight(self, known: dict[str, Any]) -> tuple[bool, int]:
        if not known.get("in_flight"):
            return False, 0
        since = int(known.get("dispatched_at") or 0)
        if not since:
            return False, 0
        age = now_ts() - since
        return age < self.in_flight_timeout(), max(age, 0)

    def effective_jql(self, jql: str | None = None) -> str:
        base = (jql or self.cfg.jira.get("default_jql") or "").strip()
        m = _ORDER_RE.search(base)
        order = m.group(0).strip() if m else ""
        body = base[: m.start()].strip() if m else base
        pins = self.pin_cards()
        if pins:
            body = f"({body}) OR key in ({', '.join(pins)})"
        return f"{body} {order}".strip()

    # ------------------------------------------------------------- bot 识别
    def is_bot(self, comment: dict[str, Any]) -> bool:
        body = (comment.get("body") or "").strip()
        if body.startswith(self.mark) or body.endswith(self.mark):
            return True
        # 老格式：没有独立标记行，靠尾部"（bot 值守链路第 N 轮）"认。
        return bool(self._own_trailer.search(body))

    # ------------------------------------------------------------- 取数
    def fetch(self, jql: str | None, limit: int) -> list[dict[str, Any]]:
        issues = self.jira.search(self.effective_jql(jql), fields=FIELDS, limit=limit)
        skip = self.ignore_cards()
        return [i for i in issues if str(i.get("key") or "").upper() not in skip]

    def comments_of(self, key: str) -> list[dict[str, Any]]:
        return self.jira.comments(key, limit=MAX_KNOWN_COMMENTS)

    def tracked(self, have: set[str]) -> tuple[list[dict[str, Any]], list[str]]:
        """补拉"已经派过单、但掉出 JQL 窗口"的卡，让评论续跑不受时间窗限制。"""
        extra: list[dict[str, Any]] = []
        closed: list[str] = []
        skip = self.ignore_cards()
        budget = int(self.cfg.watch.get("max_tracked_per_tick") or 12)
        for key, card in list(self.state.cards.items()):
            if key in have or str(key).upper() in skip:
                continue
            if not (card or {}).get("active"):
                continue
            if len(extra) >= budget:
                break
            try:
                full = self.jira.issue(key, fields=FIELDS)
            except Exception:  # noqa: BLE001 - 单卡拉不到不该拖垮整轮
                continue
            if (full.get("fields") or {}).get("resolution"):
                closed.append(key)
                continue
            extra.append(full)
        for key in closed:
            self.state.pop(key)
            self.state.log("closed", key=key, detail="有 resolution，从跟进清单摘掉")
        return extra, closed

    # ------------------------------------------------------------- prompt
    def knowledge_text(self, max_chars: int = 1800) -> str:
        base = self.cfg.knowledge_dir
        if not base.is_dir():
            return ""
        chunks = []
        for path in sorted(base.glob("*.md")):
            try:
                chunks.append(path.read_text(encoding="utf-8").strip())
            except OSError:
                continue
        text = "\n\n".join(chunks)
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + "\n…（知识库已截断，完整内容见 knowledge/）"
        return text

    def repos_text(self) -> str:
        lines = []
        for name, meta in (self.cfg.dev.get("repos") or {}).items():
            meta = meta if isinstance(meta, dict) else {"src": meta}
            verify = meta.get("verify") or "（自己选一个能证明改动可用的命令）"
            lines.append(f"- {name}（{meta.get('lang') or '?'}）：{meta.get('src') or '?'} / 验证：{verify}")
        return "\n".join(lines) if lines else "- （config.json 的 dev.repos 还没配，先用只读模式）"

    def prompt_file(self, key: str, text: str = "") -> Path:
        """每轮的"任务说明"落文件：看板卡 prompt 一开跑就冻结，改不动。"""
        path = self.cfg.prompt_dir / f"{key}.md"
        if text:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return path

    def _context_block(self, key: str, new_comments: list[dict[str, Any]] | None) -> str:
        if not new_comments:
            return ""
        quoted = "\n\n".join(
            f"- {(c.get('author') or {}).get('displayName') or '?'} @ {c.get('created')}\n"
            + ((c.get("body") or "").strip()[:1000] or "(空)")
            for c in new_comments[-4:]
        )
        return (
            f"上一轮底稿：{self.cfg.brief_dir / (key + '.md')}（存在就读）\n"
            "卡上新增评论（与卡面冲突时以这里为准）：\n"
            f"{quoted}\n"
            "这一轮只做增量：针对新评论未满足的部分继续改，别推翻已经通过的改动。\n"
        )

    def build_prompt(
        self, key: str, summary: str, *, round_no: int = 1, new_comments: list[dict[str, Any]] | None = None
    ) -> str:
        kind = "dev" if self.cfg.dev.get("enabled") else "readonly"
        knowledge = self.knowledge_text()
        base_branch = self.cfg.dev.get("base_branch") or self._default_base_ref()
        values = {
            "KEY": key,
            "ROUND": round_no,
            "SUMMARY": summary,
            "ROOT": str(self.cfg.root),
            "CLI": self.cfg.get("cli", "guardian"),
            "REPOS": self.repos_text(),
            "KNOWLEDGE_BLOCK": f"\n必须参考的代码地图（knowledge/ 里的长期记忆）：\n{knowledge}\n" if knowledge else "",
            "CONTEXT": self._context_block(key, new_comments),
            "WORKTREE_ROOT": str(self.cfg.worktree_root),
            "BASE_BRANCH": base_branch,
            "IN_PROGRESS": self.cfg.watch.get("status_in_progress") or "In Progress",
            "DONE_STATUS": self.cfg.watch.get("status_done") or "Ready for Testing",
            "COMMIT_TYPES": " / ".join(self.cfg.dev.get("commit_types") or []),
            "BRIEF_FILE": str(self.cfg.brief_dir / f"{key}.md"),
            "OUTBOX_FILE": str(self.cfg.outbox_dir / f"{key}.json"),
            "ATTACH_DIR": str(self.cfg.attach_dir / key),
            "BOT_MARK": self.mark,
        }
        return render(self.cfg, kind, values)

    def _default_base_ref(self) -> str:
        branch = (self.cfg.dev.get("base_branch") or "").strip()
        return branch or "origin/master（worktree prepare 会自动识别 origin/HEAD）"

    # ------------------------------------------------------------- 判定
    def build_items(
        self, issues: list[dict[str, Any]], *, with_comments: bool = True
    ) -> list[dict[str, Any]]:
        skip = self.ignore_cards()
        items: list[dict[str, Any]] = []
        for issue in issues:
            fields = issue.get("fields") or {}
            key = issue.get("key")
            if str(key or "").upper() in skip:
                continue
            summary = fields.get("summary") or ""
            status = (fields.get("status") or {}).get("name") or ""
            updated_ts = jira_ts(fields.get("updated"))
            known = self.state.get(key)

            if known is None:
                # 首轮把卡上已有评论记成已读，否则下一轮会当成"新评论"再派一次。
                seen = 0
                if with_comments:
                    try:
                        seen = max(
                            (jira_ts(c.get("created")) for c in self.comments_of(key) if not self.is_bot(c)),
                            default=0,
                        )
                    except Exception:  # noqa: BLE001
                        seen = now_ts()
                items.append(
                    {
                        "key": key,
                        "kind": "new",
                        "dispatch": True,
                        "round": 1,
                        "status": status,
                        "summary": summary,
                        "url": self.jira.browse_url(key),
                        "cursor": {"updated": updated_ts, "comment": seen},
                        "prompt": self.build_prompt(key, summary),
                    }
                )
                continue

            if not with_comments:
                continue

            busy, age = self.in_flight(known)
            if busy:
                items.append(
                    {
                        "key": key,
                        "kind": "in_flight",
                        "dispatch": False,
                        "round": int(known.get("round") or 1),
                        "status": status,
                        "summary": summary,
                        "session_id": known.get("session_id") or "",
                        "board_card": known.get("board_card") or "",
                        "note": f"上一轮仍在处理中（{age // 60} 分钟，会话 {known.get('session_id') or '未记录'}），本轮跳过",
                        "prompt": "",
                    }
                )
                continue

            try:
                comments = self.comments_of(key)
            except Exception as exc:  # noqa: BLE001 - 一张坏卡不能拖垮整轮
                items.append(
                    {"key": key, "kind": "error", "dispatch": False, "status": status,
                     "summary": summary, "error": f"读评论失败：{exc}", "prompt": ""}
                )
                continue

            human = [c for c in comments if not self.is_bot(c)]
            last_seen = int(known.get("comment_ts") or 0)
            fresh = [c for c in human if jira_ts(c.get("created")) > last_seen]
            newest_human = max([jira_ts(c.get("created")) for c in human] or [0])
            round_no = int(known.get("round") or 1)

            if self.cfg.watch.get("follow_up_on_new_comment", True) and fresh and newest_human > last_seen:
                round_no += 1
                items.append(
                    {
                        "key": key,
                        "kind": "comment",
                        "dispatch": True,
                        "round": round_no,
                        "status": status,
                        "summary": summary,
                        "url": self.jira.browse_url(key),
                        "new_comments": [
                            {
                                "author": (c.get("author") or {}).get("displayName"),
                                "created": c.get("created"),
                                "body": (c.get("body") or "")[:1200],
                            }
                            for c in fresh[-4:]
                        ],
                        "cursor": {"updated": updated_ts, "comment": newest_human},
                        "prompt": self.build_prompt(key, summary, round_no=round_no, new_comments=fresh),
                    }
                )
            elif status != (known.get("status") or ""):
                # 状态变化从不单独派单：一半是我们自己挪的，另一半是测试打回但没留话。
                prev = known.get("status") or ""
                if self.is_return_to_work(prev, status):
                    note = f"测试打回（{prev} → {status}）但没有追加评论，本轮不动手，等测试补充说明"
                elif self.is_done_status(status):
                    note = f"已进入完成态（{prev} → {status}），只同步状态不派单"
                else:
                    note = f"状态变化 {prev} → {status}（多为执行会话自己挪的），只同步不派单"
                items.append(
                    {
                        "key": key,
                        "kind": "status",
                        "dispatch": False,
                        "round": round_no,
                        "status": status,
                        "summary": summary,
                        "url": self.jira.browse_url(key),
                        "note": note,
                        "cursor": {"updated": updated_ts, "comment": last_seen},
                        "prompt": "",
                    }
                )

        for item in items:
            if item.get("prompt"):
                item["prompt_file"] = str(self.prompt_file(item["key"], item["prompt"]))
        return items

    def commit(self, items: list[dict[str, Any]]) -> None:
        now = now_ts()
        for item in items:
            if item.get("kind") in ("error", "in_flight"):
                # error 不动账；in_flight 是只读提示，写回会把自己刚立的占位清掉。
                continue
            cursor = item.get("cursor") or {}
            key = item["key"]
            prev = self.state.get(key) or {}
            dispatching = bool(item.get("dispatch"))
            self.state.put(
                key,
                status=item.get("status") or "",
                summary=item.get("summary") or "",
                round=int(item.get("round") or 1),
                updated=int(cursor.get("updated") or 0),
                comment_ts=int(cursor.get("comment") or 0),
                seen_at=now,
                active=True,
                in_flight=dispatching,
                dispatched_at=now if dispatching else 0,
                session_id=prev.get("session_id") or "",
                board_card=prev.get("board_card") or "",
            )

    # ------------------------------------------------------------- 命令
    def scan(
        self,
        *,
        jql: str | None = None,
        limit: int = 50,
        commit: bool = False,
        with_comments: bool = True,
        limit_items: bool = False,
    ) -> dict[str, Any]:
        issues = self.fetch(jql, limit)
        extra, closed = self.tracked({i.get("key") for i in issues})
        all_issues = issues + extra
        items = self.build_items(all_issues, with_comments=with_comments)
        cap = int(self.cfg.watch.get("max_items_per_tick") or 3)
        queued = items[:cap] if limit_items else items
        if commit:
            self.commit(queued)
            self.state.log(
                "scan",
                detail=f"scanned={len(all_issues)} changed={len(items)} queued={len(queued)} closed={len(closed)}",
            )
            self.state.save()
        return {
            "ok": True,
            "jql": self.effective_jql(jql),
            "scanned": len(all_issues),
            "tracked": len(extra),
            "closed": closed,
            "ignored": sorted(self.ignore_cards()),
            "changed": len(items),
            "returned": len(queued),
            "committed": commit,
            "none": not queued,
            "items": queued,
        }

    def seed(self, *, jql: str | None = None, limit: int = 100) -> dict[str, Any]:
        issues = self.fetch(jql, limit)
        keys = []
        for issue in issues:
            key = issue.get("key")
            fields = issue.get("fields") or {}
            comment_ts = 0
            try:
                human = [c for c in self.comments_of(key) if not self.is_bot(c)]
                comment_ts = max([jira_ts(c.get("created")) for c in human] or [0])
            except Exception:  # noqa: BLE001
                pass
            prev = self.state.get(key) or {}
            self.state.put(
                key,
                status=(fields.get("status") or {}).get("name") or "",
                summary=fields.get("summary") or "",
                round=int(prev.get("round") or 1),
                updated=jira_ts(fields.get("updated")),
                comment_ts=comment_ts,
                seen_at=now_ts(),
                active=False,
                in_flight=False,
                dispatched_at=0,
            )
            keys.append(key)
        self.state.log("seed", detail=f"seeded={len(keys)}")
        self.state.save()
        return {"ok": True, "seeded": len(keys), "keys": keys, "total_cards": len(self.state.cards)}

    def outbox(self) -> dict[str, Any]:
        pending = []
        for path in sorted(self.cfg.outbox_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {"key": path.stem, "status": "failed", "error": "回执 JSON 解析失败", "comment": ""}
            data["_path"] = str(path)
            pending.append(data)
        return {"ok": True, "count": len(pending), "items": pending}

    def ack(self, key: str) -> dict[str, Any]:
        path = self.cfg.outbox_dir / f"{key}.json"
        card = self.state.get(key)
        released = False
        if card and card.get("in_flight"):
            card["in_flight"] = False
            card["dispatched_at"] = 0
            released = True
            self.state.log("ack", key=key, detail="回执已消费，释放占位")
            self.state.save()
        if path.exists():
            path.unlink()
            return {"ok": True, "removed": str(path), "released": released}
        return {"ok": True, "removed": None, "released": released, "note": f"{path} 不存在（可能已经 ack 过）"}

    def claim(self, key: str, *, session: str = "", board: str = "") -> dict[str, Any]:
        card = self.state.get(key)
        if card is None:
            return {"ok": False, "error": f"{key} 不在水位线里（先 tick scan --commit）"}
        card["in_flight"] = True
        card["dispatched_at"] = now_ts()
        if session:
            card["session_id"] = session
        if board:
            card["board_card"] = board
        self.state.log("claim", key=key, detail=f"session={session or '-'} board={board or '-'}")
        self.state.save()
        return {
            "ok": True,
            "key": key,
            "session_id": card.get("session_id") or "",
            "board_card": card.get("board_card") or "",
            "round": int(card.get("round") or 1),
        }

    def release(self, key: str) -> dict[str, Any]:
        card = self.state.get(key)
        if card is None:
            return {"ok": False, "error": f"{key} 不在水位线里"}
        card["in_flight"] = False
        card["dispatched_at"] = 0
        self.state.log("release", key=key, detail="人工放开占位")
        self.state.save()
        return {"ok": True, "key": key, "released": True}

    def summary(self) -> dict[str, Any]:
        return {"ok": True, "cards": len(self.state.cards), "last_tick": self.state.data.get("last_tick"),
                "in_flight": [k for k, c in self.state.cards.items() if c.get("in_flight")],
                "state": self.state.data}
