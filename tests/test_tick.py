"""离线测试：用 FakeJira 顶替真 JIRA，验证"该派 / 不该派"的每条判定。

    .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jira_guardian.config import Config  # noqa: E402
from jira_guardian.state import State  # noqa: E402
from jira_guardian.tick import Tick, jira_ts  # noqa: E402

TZ = timezone(timedelta(hours=8))


def iso(minutes_ago: int = 0) -> str:
    return (datetime.now(TZ) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S.000+0800")


class FakeJira:
    """只实现 Tick 用到的方法。cards: key -> {status, summary, updated, comments:[{body, created}]}"""

    def __init__(self, cards: dict[str, dict]) -> None:
        self.cards = cards
        self.comment_calls: list[str] = []

    def _issue(self, key: str) -> dict:
        card = self.cards[key]
        return {
            "key": key,
            "fields": {
                "summary": card.get("summary", f"标题 {key}"),
                "status": {"name": card.get("status", "New")},
                "issuetype": {"name": card.get("type", "Bug")},
                "updated": card.get("updated", iso(30)),
                "created": card.get("created", iso(30)),
                "resolution": {"name": "Done"} if card.get("resolution") else None,
                "labels": [],
            },
        }

    def search(self, jql: str, *, fields: str = "", limit: int = 50):
        return [self._issue(k) for k in list(self.cards)[:limit]]

    def issue(self, key: str, *, fields: str = ""):
        return self._issue(key)

    def comments(self, key: str, *, limit: int = 40):
        self.comment_calls.append(key)
        return [
            {"id": str(i), "author": {"displayName": c.get("author", "tester")},
             "created": c.get("created", iso(20)), "body": c.get("body", "")}
            for i, c in enumerate(reversed(self.cards[key].get("comments", [])))
        ]

    def add_comment(self, key: str, body: str):
        return {"id": "1"}

    def transitions(self, key: str):
        return [{"id": "11", "name": "In Progress", "to": {"name": "In Progress"}}]

    def status_of(self, key: str) -> str:
        return self.cards[key].get("status", "New")

    def browse_url(self, key: str) -> str:
        return f"https://jira.example.com/browse/{key}"


class Base(unittest.TestCase):
    bot_mark = "[super-agent]"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "knowledge").mkdir()
        (self.root / "knowledge" / "codebase.md").write_text("仓库地图：foo → repo/foo", encoding="utf-8")
        self.write_config()
        self.cfg = Config(self.root)
        self.cfg.ensure_dirs()
        self.state = State(self.cfg.state_file)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_config(self, **over) -> None:
        jira = {"base_url": "https://jira.example.com", "token": "t", "default_jql": "project = P", **over.pop("jira", {})}
        watch = {
            "max_items_per_tick": 3,
            "pin_cards": ["P-9"],
            "ignore_cards": [],
            "done_statuses": ["Ready for Testing", "Done"],
            **over.pop("watch", {}),
        }
        dev = {"enabled": True, "repos": {"repo": {"src": str(self.root / "src")}}, **over.pop("dev", {})}
        data = {"jira": jira, "watch": watch, "dev": dev, "llm": {"base_url": "x", "api_key": "k", "model": "m",
                                                                 "vision_model": "m"}, **over}
        (self.root / "config.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def tick(self, jira: FakeJira) -> Tick:
        return Tick(self.cfg, jira, self.state)

    def reload(self) -> None:
        self.cfg = Config(self.root)
        self.state = State(self.cfg.state_file)

    def settle(self, *keys: str) -> None:
        """模拟"这一轮执行完了"：清占位，否则下一轮会被判成 in_flight（这正是设计意图）。"""
        for key in keys or tuple(self.state.cards):
            self.state.put(key, in_flight=False, dispatched_at=0, session_id="", board_card="")
        self.state.save()


class FirstRound(Base):
    def test_new_card_dispatches_and_watermarks_existing_comments(self) -> None:
        jira = FakeJira({"P-1": {"comments": [{"body": "老评论", "created": iso(120)}]}})
        out = self.tick(jira).scan(limit_items=True)
        self.assertTrue(out["ok"])
        self.assertEqual(out["changed"], 1)
        item = out["items"][0]
        self.assertEqual(item["kind"], "new")
        self.assertTrue(item["dispatch"])
        self.assertEqual(item["cursor"]["comment"], jira_ts(iso(120)))
        self.assertIn("P-1", Path(item["prompt_file"]).read_text(encoding="utf-8"))

    def test_prompt_contains_rules_not_placeholders(self) -> None:
        jira = FakeJira({"P-1": {}})
        item = self.tick(jira).scan()["items"][0]
        prompt = Path(item["prompt_file"]).read_text(encoding="utf-8")
        for needle in ("worktree prepare", "commit -m", "不要用 # / ## / ###", "回执", "仓库地图"):
            self.assertIn(needle, prompt)
        self.assertNotIn("{{", prompt)

    def test_ignore_beats_pin(self) -> None:
        self.write_config(watch={"ignore_cards": ["P-9"], "pin_cards": ["P-9"]})
        self.reload()
        jira = FakeJira({"P-9": {}})
        tick = self.tick(jira)
        self.assertEqual(tick.fetch(None, 10), [])
        self.assertEqual(tick.scan()["changed"], 0)


class Quiet(Base):
    def setUp(self) -> None:
        super().setUp()
        jira = FakeJira({"P-1": {"comments": [{"body": "老评论", "created": iso(120)}]}})
        self.tick(jira).scan(commit=True)
        self.settle()

    def test_second_scan_is_silent(self) -> None:
        self.reload()
        out = self.tick(FakeJira({"P-1": {"comments": [{"body": "老评论", "created": iso(120)}]}})).scan()
        self.assertTrue(out["none"])
        self.assertEqual(out["changed"], 0)

    def test_own_comment_does_not_trigger(self) -> None:
        self.reload()
        jira = FakeJira({"P-1": {"comments": [
            {"body": "老评论", "created": iso(120)},
            {"body": "我们发的\n\n[super-agent]", "created": iso(5)},
        ]}})
        self.assertEqual(self.tick(jira).scan()["changed"], 0)

    def test_human_comment_triggers_next_round(self) -> None:
        self.reload()
        jira = FakeJira({"P-1": {"comments": [
            {"body": "老评论", "created": iso(120)},
            {"body": "图片的修改还需要", "created": iso(5)},
        ]}})
        item = self.tick(jira).scan()["items"][0]
        self.assertEqual(item["kind"], "comment")
        self.assertEqual(item["round"], 2)
        self.assertEqual(item["new_comments"][0]["body"], "图片的修改还需要")
        prompt = Path(item["prompt_file"]).read_text(encoding="utf-8")
        self.assertIn("图片的修改还需要", prompt)
        self.assertIn("只做增量", prompt)

    def test_own_status_move_does_not_dispatch(self) -> None:
        self.reload()
        jira = FakeJira({"P-1": {"status": "In Progress", "comments": [{"body": "老评论", "created": iso(120)}]}})
        item = self.tick(jira).scan()["items"][0]
        self.assertEqual(item["kind"], "status")
        self.assertFalse(item["dispatch"])
        self.assertIn("只同步不派单", item["note"])

    def test_return_to_work_without_comment_waits(self) -> None:
        self.reload()
        self.state.put("P-1", status="Ready for Testing", comment_ts=jira_ts(iso(120)))
        jira = FakeJira({"P-1": {"status": "In Progress", "comments": [{"body": "老评论", "created": iso(120)}]}})
        item = self.tick(jira).scan()["items"][0]
        self.assertEqual(item["kind"], "status")
        self.assertFalse(item["dispatch"])
        self.assertIn("测试打回", item["note"])

    def test_return_to_work_with_comment_dispatches(self) -> None:
        self.reload()
        self.state.put("P-1", status="Ready for Testing", comment_ts=jira_ts(iso(120)))
        jira = FakeJira({"P-1": {"status": "In Progress", "comments": [
            {"body": "老评论", "created": iso(120)},
            {"body": "这版还是不行，图上的 DB710S 没拆出来", "created": iso(3)},
        ]}})
        item = self.tick(jira).scan()["items"][0]
        self.assertEqual(item["kind"], "comment")
        self.assertTrue(item["dispatch"])


class InFlight(Base):
    def test_claim_blocks_second_dispatch_then_ack_releases(self) -> None:
        jira = FakeJira({"P-1": {"comments": [{"body": "老评论", "created": iso(30)}]}})
        tick = self.tick(jira)
        tick.scan(commit=True)
        self.settle("P-1")
        tick.claim("P-1", session="session-x", board="board-y")

        self.reload()
        jira2 = FakeJira({"P-1": {"comments": [
            {"body": "老评论", "created": iso(30)},
            {"body": "新要求", "created": iso(1)},
        ]}})
        item = self.tick(jira2).scan()["items"][0]
        self.assertEqual(item["kind"], "in_flight")
        self.assertFalse(item["dispatch"])
        self.assertIn("session-x", item["note"])

        self.tick(jira2).ack("P-1")
        self.reload()
        self.assertEqual(self.tick(jira2).scan()["items"][0]["kind"], "comment")

    def test_timeout_lets_it_run_again(self) -> None:
        jira = FakeJira({"P-1": {}})
        tick = self.tick(jira)
        tick.scan(commit=True)
        self.settle("P-1")
        tick.claim("P-1", session="s")
        self.state.get("P-1")["dispatched_at"] -= tick.in_flight_timeout() + 60  # 假装超时
        self.state.save()
        self.reload()
        jira2 = FakeJira({"P-1": {"comments": [{"body": "补充说明", "created": iso(1)}]}})
        item = self.tick(jira2).scan()["items"][0]
        self.assertEqual(item["kind"], "comment")


class Tracking(Base):
    def test_card_outside_jql_is_still_followed(self) -> None:
        jira = FakeJira({"P-1": {}})
        tick = self.tick(jira)
        tick.scan(commit=True)
        self.settle("P-1")
        self.reload()

        class Empty(FakeJira):
            def search(self, jql: str, *, fields: str = "", limit: int = 50):
                return []  # 卡掉出了 JQL 窗口（比如"只看今天新建"）

        jira2 = Empty({"P-1": {"comments": [{"body": "评论又来了", "created": iso(2)}]}})
        out = self.tick(jira2).scan()
        self.assertEqual(out["tracked"], 1)
        self.assertEqual(out["items"][0]["kind"], "comment")

    def test_resolved_card_is_dropped(self) -> None:
        jira = FakeJira({"P-1": {}})
        tick = self.tick(jira)
        tick.scan(commit=True)
        self.settle("P-1")
        self.reload()

        class Empty(FakeJira):
            def search(self, jql: str, *, fields: str = "", limit: int = 50):
                return []

        jira2 = Empty({"P-1": {"resolution": "Done"}})
        out = self.tick(jira2).scan()
        self.assertEqual(out["closed"], ["P-1"])
        self.assertNotIn("P-1", self.state.cards)


class Tracing(Base):
    def test_receipt_roundtrip(self) -> None:
        jira = FakeJira({"P-1": {}})
        tick = self.tick(jira)
        tick.scan(commit=True)
        self.state.save()
        receipt = self.cfg.outbox_dir / "P-1.json"
        receipt.write_text(json.dumps({"key": "P-1", "status": "ok", "comment": "结论"}, ensure_ascii=False),
                           encoding="utf-8")
        self.reload()
        out = self.tick(jira).outbox()
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["items"][0]["comment"], "结论")
        tick2 = self.tick(jira)
        tick2.ack("P-1")
        self.assertEqual(tick2.outbox()["count"], 0)
        self.assertFalse(self.state.get("P-1")["in_flight"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
