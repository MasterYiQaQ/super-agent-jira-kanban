"""命令行入口：``guardian <group> <cmd>``。

约定：所有命令都把结果打成 UTF-8 JSON 到 stdout（`vision` / `card` 除外，它们是文本），
失败时 JSON 里 ``ok: false`` 并 exit 1——调用方（模型）直接 json.loads，不用去猜日志。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

from . import doctor as doctor_mod
from . import scaffold as scaffold_mod
from . import vision as vision_mod
from . import worktree as worktree_mod
from .compose import stamp
from .config import Config, ConfigError
from .jira import Jira, JiraError
from .prompts import PromptError, render
from .state import State
from .tick import Tick

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2


# --------------------------------------------------------------------- 公共
def _emit(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _utf8_stdout() -> None:
    """宿主控制台可能是 cp936，中文过管道会变问号；强制 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def _ctx(args) -> tuple[Config, Jira, State]:
    cfg = Config(args.root)
    cfg.ensure_dirs()
    return cfg, Jira(cfg), State(cfg.state_file)


def _tick(args) -> Tick:
    cfg, jira, state = _ctx(args)
    return Tick(cfg, jira, state)


def _brief(issue: dict[str, Any], *, base: str = "") -> dict[str, Any]:
    f = issue.get("fields") or {}
    return {
        "key": issue.get("key"),
        "summary": f.get("summary"),
        "type": (f.get("issuetype") or {}).get("name"),
        "status": (f.get("status") or {}).get("name"),
        "priority": (f.get("priority") or {}).get("name"),
        "resolution": (f.get("resolution") or {}).get("name"),
        "created": f.get("created"),
        "updated": f.get("updated"),
        "labels": f.get("labels") or [],
        "url": f"{base.rstrip('/')}/browse/{issue.get('key')}" if base else "",
    }


# --------------------------------------------------------------------- jira
def cmd_jira_me(args) -> dict[str, Any]:
    cfg, jira, _ = _ctx(args)
    me = jira.me()
    return {"ok": True, "name": me.get("name"), "displayName": me.get("displayName"),
            "key": me.get("key"), "timeZone": me.get("timeZone")}


def cmd_jira_today(args) -> dict[str, Any]:
    cfg, jira, _ = _ctx(args)
    jql = args.jql or cfg.jira.get("default_jql") or ""
    issues = jira.search(jql, limit=args.limit)
    return {"ok": True, "jql": jql, "count": len(issues),
            "cards": [_brief(i, base=cfg.jira.get("base_url") or "") for i in issues]}


def cmd_jira_get(args) -> dict[str, Any]:
    cfg, jira, _ = _ctx(args)
    issue = jira.issue(args.key, fields="*all")
    f = issue.get("fields") or {}
    out = _brief(issue, base=cfg.jira.get("base_url") or "")
    out["description"] = f.get("description") or ""
    try:
        comments = jira.comments(args.key, limit=args.comments)
        out["comments"] = [
            {"id": c.get("id"), "author": (c.get("author") or {}).get("displayName"),
             "created": c.get("created"), "body": (c.get("body") or "")}
            for c in comments
        ]
    except JiraError as exc:
        out["comments"] = [{"error": str(exc)}]
    out["attachments"] = [
        {"filename": a.get("filename"), "bytes": a.get("size"), "created": a.get("created")}
        for a in ((f.get("attachment")) or [])
    ]
    return out


def cmd_jira_comments(args) -> dict[str, Any]:
    cfg, jira, _ = _ctx(args)
    mark = str(cfg.jira.get("bot_mark") or "[super-agent]")
    items = jira.comments(args.key, limit=args.limit)
    return {
        "ok": True,
        "key": args.key,
        "comments": [
            {"id": c.get("id"), "author": (c.get("author") or {}).get("displayName"),
             "created": c.get("created"), "bot": mark in (c.get("body") or ""), "body": c.get("body") or ""}
            for c in items
        ],
    }


def cmd_jira_comment(args) -> dict[str, Any]:
    cfg, jira, _ = _ctx(args)
    if not cfg.jira.get("allow_writes", True):
        return {"ok": False, "error": "config.jira.allow_writes = false，拒绝写评论"}
    body = Path(args.file).read_text(encoding="utf-8") if args.file else (args.body or "")
    if not body.strip():
        return {"ok": False, "error": "评论内容为空"}
    body = stamp(body, str(cfg.jira.get("bot_mark") or "[super-agent]"))
    if args.dry_run:
        return {"ok": True, "dry_run": True, "chars": len(body), "body": body}
    res = jira.add_comment(args.key, body)
    return {"ok": True, "key": args.key, "comment_id": (res or {}).get("id"), "chars": len(body)}


def cmd_jira_delcomment(args) -> dict[str, Any]:
    cfg, jira, _ = _ctx(args)
    jira.delete_comment(args.key, args.comment_id)
    return {"ok": True, "key": args.key, "deleted": args.comment_id}


def cmd_jira_transitions(args) -> dict[str, Any]:
    cfg, jira, _ = _ctx(args)
    items = jira.transitions(args.key)
    return {"ok": True, "key": args.key,
            "transitions": [{"id": t.get("id"), "name": t.get("name"), "to": (t.get("to") or {}).get("name")}
                            for t in items]}


def cmd_jira_transition(args) -> dict[str, Any]:
    cfg, jira, _ = _ctx(args)
    current = jira.status_of(args.key)
    if current.strip().lower() == args.to.strip().lower():
        return {"ok": True, "key": args.key, "to": args.to, "changed": False, "status": current}
    ok, message = jira.transition_to(args.key, args.to)
    return {"ok": ok, "key": args.key, "to": args.to, "changed": ok,
            "status": jira.status_of(args.key), "message": message}


def cmd_jira_attach(args) -> dict[str, Any]:
    cfg, jira, _ = _ctx(args)
    items = jira.attachment_list(args.key)
    out_dir = Path(args.out).expanduser() if args.out else cfg.attach_dir / args.key
    saved = []
    for a in items:
        name = a.get("filename") or f"att-{a.get('id')}"
        dest = out_dir / name
        entry: dict[str, Any] = {"filename": name, "mime": a.get("mimeType"), "created": a.get("created")}
        try:
            if args.list:
                entry["skipped"] = "只列不下载"
            elif dest.exists() and not args.force:
                entry["path"] = str(dest)
                entry["bytes"] = dest.stat().st_size
                entry["cached"] = True
            else:
                jira.download(a["content"], dest)
                entry["path"] = str(dest)
                entry["bytes"] = dest.stat().st_size
        except Exception as exc:  # noqa: BLE001
            entry["error"] = str(exc)
        saved.append(entry)
    return {"ok": True, "key": args.key, "dir": str(out_dir), "count": len(saved), "attachments": saved}


def cmd_jira_search(args) -> dict[str, Any]:
    cfg, jira, _ = _ctx(args)
    issues = jira.search(args.jql, limit=args.limit)
    return {"ok": True, "jql": args.jql, "count": len(issues),
            "cards": [_brief(i, base=cfg.jira.get("base_url") or "") for i in issues]}


# --------------------------------------------------------------------- tick
def cmd_tick_scan(args) -> dict[str, Any]:
    return _tick(args).scan(
        jql=args.jql, limit=args.limit, commit=args.commit,
        with_comments=not args.no_comments, limit_items=args.limit_items,
    )


def cmd_tick_outbox(args) -> dict[str, Any]:
    return _tick(args).outbox()


def cmd_tick_ack(args) -> dict[str, Any]:
    return _tick(args).ack(args.key)


def cmd_tick_claim(args) -> dict[str, Any]:
    return _tick(args).claim(args.key, session=args.session or "", board=args.board or "")


def cmd_tick_release(args) -> dict[str, Any]:
    return _tick(args).release(args.key)


def cmd_tick_seed(args) -> dict[str, Any]:
    return _tick(args).seed(jql=args.jql, limit=args.limit)


def cmd_tick_state(args) -> dict[str, Any]:
    return _tick(args).summary()


# ------------------------------------------------------------------ worktree
def cmd_worktree_prepare(args) -> dict[str, Any]:
    cfg = Config(args.root)
    return worktree_mod.prepare(
        cfg, args.repo, args.branch, base=args.base, fetch=not args.no_fetch, fresh=args.fresh
    )


def cmd_worktree_status(args) -> dict[str, Any]:
    return worktree_mod.status(Config(args.root), args.repo)


def cmd_worktree_clean(args) -> dict[str, Any]:
    return worktree_mod.clean(Config(args.root), args.repo, branch=args.branch or "",
                              delete_branch=args.delete_branch)


# --------------------------------------------------------------------- 其它
def cmd_vision(args) -> dict[str, Any]:
    cfg = Config(args.root)
    text = vision_mod.describe(cfg, args.prompt, args.images, model=args.model)
    return {"ok": True, "model": args.model or cfg.data["llm"].get("vision_model"),
            "images": args.images, "text": text}


def cmd_doctor(args) -> dict[str, Any]:
    return doctor_mod.run(args.root)


def cmd_init(args) -> dict[str, Any]:
    repos: dict[str, str] = {}
    for item in args.repo or []:
        if "=" not in item:
            raise ConfigError(f"--repo 要写成 名字=路径，收到：{item}")
        name, src = item.split("=", 1)
        repos[name.strip()] = src.strip()
    return scaffold_mod.scaffold(
        args.root or str(Path.cwd()),
        jira_url=args.jira_url, token=args.token, project=args.project or "", me=args.me or "",
        repos=repos, base_branch=args.base_branch or "", dev=not args.no_dev, force=args.force,
    )


def cmd_card(args) -> dict[str, Any]:
    """打印可以直接贴进看板卡的 prompt（看板卡 prompt 开跑即冻结，所以先在这里定稿）。"""
    cfg = Config(args.root)
    text = render(
        cfg,
        "router",
        {
            "CLI": cfg.get("cli", "guardian"),
            "JQL": cfg.jira.get("default_jql") or "",
            "DONE_STATUS": cfg.watch.get("status_done") or "Ready for Testing",
            "IN_PROGRESS": cfg.watch.get("status_in_progress") or "In Progress",
            "PROMPT_DIR": str(cfg.prompt_dir),
            "BOT_MARK": cfg.jira.get("bot_mark") or "[super-agent]",
        },
    )
    return {"ok": True, "chars": len(text), "prompt": text}


# --------------------------------------------------------------------- 解析
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="guardian", description="JIRA 值守链路")
    ap.add_argument("--root", help="值守目录（含 config.json），默认当前目录")
    sub = ap.add_subparsers(dest="group", required=True)

    jira = sub.add_parser("jira", help="JIRA 读写").add_subparsers(dest="cmd", required=True)

    jira.add_parser("me").set_defaults(fn=cmd_jira_me)

    p = jira.add_parser("today")
    p.add_argument("--jql")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(fn=cmd_jira_today)

    p = jira.add_parser("get")
    p.add_argument("key")
    p.add_argument("--comments", type=int, default=10)
    p.set_defaults(fn=cmd_jira_get)

    p = jira.add_parser("comments")
    p.add_argument("key")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(fn=cmd_jira_comments)

    p = jira.add_parser("comment")
    p.add_argument("key")
    p.add_argument("--file")
    p.add_argument("--body")
    p.add_argument("--dry-run", action="store_true", help="只排版不发送")
    p.set_defaults(fn=cmd_jira_comment)

    p = jira.add_parser("delcomment")
    p.add_argument("key")
    p.add_argument("comment_id")
    p.set_defaults(fn=cmd_jira_delcomment)

    p = jira.add_parser("transitions")
    p.add_argument("key")
    p.set_defaults(fn=cmd_jira_transitions)

    p = jira.add_parser("transition")
    p.add_argument("key")
    p.add_argument("--to", required=True, help='目标状态名，如 "In Progress"')
    p.set_defaults(fn=cmd_jira_transition)

    p = jira.add_parser("attach")
    p.add_argument("key")
    p.add_argument("--out")
    p.add_argument("--list", action="store_true")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_jira_attach)

    p = jira.add_parser("search")
    p.add_argument("jql")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(fn=cmd_jira_search)

    tick = sub.add_parser("tick", help="值守判定").add_subparsers(dest="cmd", required=True)

    p = tick.add_parser("scan")
    p.add_argument("--jql")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--commit", action="store_true", help="推进水位线（真派单时必须加）")
    p.add_argument("--no-comments", action="store_true", help="不读评论（离线调试）")
    p.add_argument("--limit-items", action="store_true", help="按 watch.max_items_per_tick 截断")
    p.set_defaults(fn=cmd_tick_scan)

    tick.add_parser("outbox").set_defaults(fn=cmd_tick_outbox)

    p = tick.add_parser("ack")
    p.add_argument("key")
    p.set_defaults(fn=cmd_tick_ack)

    p = tick.add_parser("claim")
    p.add_argument("key")
    p.add_argument("--session")
    p.add_argument("--board")
    p.set_defaults(fn=cmd_tick_claim)

    p = tick.add_parser("release")
    p.add_argument("key")
    p.set_defaults(fn=cmd_tick_release)

    p = tick.add_parser("seed")
    p.add_argument("--jql")
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(fn=cmd_tick_seed)

    tick.add_parser("state").set_defaults(fn=cmd_tick_state)

    wt = sub.add_parser("worktree", help="给执行会话准备干净的 worktree").add_subparsers(dest="cmd", required=True)

    p = wt.add_parser("prepare")
    p.add_argument("repo", help="config.dev.repos 里的仓库名")
    p.add_argument("branch", help="分支名（约定用卡号）")
    p.add_argument("--base", help="基线，默认 origin/HEAD")
    p.add_argument("--no-fetch", action="store_true")
    p.add_argument("--fresh", action="store_true", help="删掉本地同名分支重开（真·从零）")
    p.set_defaults(fn=cmd_worktree_prepare)

    p = wt.add_parser("status")
    p.add_argument("repo")
    p.set_defaults(fn=cmd_worktree_status)

    p = wt.add_parser("clean")
    p.add_argument("repo")
    p.add_argument("--branch")
    p.add_argument("--delete-branch", action="store_true")
    p.set_defaults(fn=cmd_worktree_clean)

    p = sub.add_parser("vision", help="看图")
    p.add_argument("prompt")
    p.add_argument("images", nargs="+")
    p.add_argument("--model")
    p.set_defaults(fn=cmd_vision)

    sub.add_parser("doctor", help="环境体检").set_defaults(fn=cmd_doctor)

    p = sub.add_parser("init", help="初始化一个值守目录")
    p.add_argument("--jira-url", required=True)
    p.add_argument("--token", required=True)
    p.add_argument("--project", default="")
    p.add_argument("--me", default="")
    p.add_argument("--repo", action="append", help="名字=源码路径，可重复")
    p.add_argument("--base-branch", default="")
    p.add_argument("--no-dev", action="store_true", help="只读模式")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_init)

    sub.add_parser("card", help="打印看板卡 prompt").set_defaults(fn=cmd_card)
    return ap


def main(argv: list[str] | None = None) -> int:
    _utf8_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    fn: Callable[[Any], dict[str, Any]] = args.fn
    try:
        out = fn(args)
    except ConfigError as exc:
        _emit({"ok": False, "error": f"ConfigError: {exc}"})
        return EXIT_CONFIG
    except (JiraError, PromptError, worktree_mod.GitError, vision_mod.VisionError) as exc:
        _emit({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 - CLI 契约：永远给 JSON
        _emit({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return EXIT_ERROR
    _emit(out)
    return EXIT_OK if out.get("ok", True) else EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
