"""JIRA REST v2 客户端（标准库，零依赖）。

只有一处拼 URL、一处放 token、一处重试策略，其余模块都调这里的方法。
所有 path 都以 ``/rest/api/2`` 开头——少了前缀会命中网页路由，PUT/DELETE 直接 405。
"""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

API = "/rest/api/2"

LIST_FIELDS = "summary,status,issuetype,priority,updated,created,resolution,labels,assignee,reporter"


class JiraError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class Jira:
    def __init__(self, cfg, *, timeout: int = 45, retries: int = 2) -> None:
        jira = cfg.jira
        if not jira.get("base_url"):
            raise JiraError("config.json 里 jira.base_url 没配")
        if not jira.get("token"):
            raise JiraError("config.json 里 jira.token 没配（或用环境变量 JIRA_TOKEN）")
        self.base = str(jira["base_url"]).rstrip("/")
        self.token = str(jira["token"])
        self.scheme = str(jira.get("auth_scheme") or "Bearer")
        self.verify = bool(jira.get("verify_tls", True))
        self.timeout = timeout
        self.retries = retries
        self._ctx = None
        if not self.verify:
            self._ctx = ssl.create_default_context()
            self._ctx.check_hostname = False
            self._ctx.verify_mode = ssl.CERT_NONE

    # ------------------------------------------------------------------ 基础
    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        timeout: int | None = None,
    ) -> Any:
        url = self.base + (path if path.startswith("/") else f"/{path}")
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"Authorization": f"{self.scheme} {self.token}", "Accept": "application/json"}
        if data:
            headers["Content-Type"] = "application/json"

        last: Exception | None = None
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=timeout or self.timeout, context=self._ctx) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                    return json.loads(raw) if raw.strip() else {}
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:600]
                last = JiraError(
                    f"JIRA {method} {path} -> HTTP {exc.code}: {detail}", status=exc.code, body=detail
                )
                if exc.code >= 500 and attempt < self.retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise last from exc
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                last = JiraError(f"JIRA {method} {path} -> {type(exc).__name__}: {exc}")
                if attempt < self.retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise last from exc
        raise last or JiraError("JIRA request failed")

    # ------------------------------------------------------------------ 读
    def me(self) -> dict[str, Any]:
        return self.request(f"{API}/myself")

    def search(self, jql: str, *, fields: str = LIST_FIELDS, limit: int = 50) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode(
            {"jql": jql, "fields": fields, "maxResults": max(1, min(int(limit), 100))}
        )
        res = self.request(f"{API}/search?{query}")
        return res.get("issues") or []

    def issue(self, key: str, *, fields: str = LIST_FIELDS) -> dict[str, Any]:
        query = urllib.parse.urlencode({"fields": fields}) if fields else ""
        suffix = f"?{query}" if query else ""
        return self.request(f"{API}/issue/{urllib.parse.quote(key)}{suffix}")

    def comments(self, key: str, *, limit: int = 40) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({"maxResults": max(1, int(limit)), "orderBy": "-created"})
        res = self.request(f"{API}/issue/{urllib.parse.quote(key)}/comment?{query}")
        return res.get("comments") or []

    def attachment_list(self, key: str) -> list[dict[str, Any]]:
        res = self.request(f"{API}/issue/{urllib.parse.quote(key)}?fields=attachment")
        return ((res.get("fields") or {}).get("attachment")) or []

    def download(self, url: str, dest: Path, *, timeout: int = 120) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(
            url, headers={"Authorization": f"{self.scheme} {self.token}"}, method="GET"
        )
        with urllib.request.urlopen(req, timeout=timeout, context=self._ctx) as resp:
            dest.write_bytes(resp.read())
        return dest

    def transitions(self, key: str) -> list[dict[str, Any]]:
        res = self.request(f"{API}/issue/{urllib.parse.quote(key)}/transitions")
        return res.get("transitions") or []

    def status_of(self, key: str) -> str:
        res = self.issue(key, fields="status")
        return ((res.get("fields") or {}).get("status") or {}).get("name") or ""

    # ------------------------------------------------------------------ 写
    def add_comment(self, key: str, body: str) -> dict[str, Any]:
        return self.request(
            f"{API}/issue/{urllib.parse.quote(key)}/comment", method="POST", body={"body": body}
        )

    def delete_comment(self, key: str, comment_id: str) -> dict[str, Any]:
        return self.request(
            f"{API}/issue/{urllib.parse.quote(key)}/comment/{urllib.parse.quote(str(comment_id))}",
            method="DELETE",
        )

    def transition_to(self, key: str, status_name: str) -> tuple[bool, str]:
        """按名字流转（匹配 transition.name 或 transition.to.name）。幂等性由调用方判断。"""
        options = self.transitions(key)
        target = status_name.strip().lower()
        match = next((t for t in options if str(t.get("name", "")).lower() == target), None) or next(
            (t for t in options if str((t.get("to") or {}).get("name", "")).lower() == target), None
        )
        if not match:
            available = ", ".join(
                f"{t.get('name')}→{(t.get('to') or {}).get('name', '?')}" for t in options
            ) or "（无可用流转）"
            return False, f"没有到 {status_name} 的流转；可用：{available}"
        self.request(
            f"{API}/issue/{urllib.parse.quote(key)}/transitions",
            method="POST",
            body={"transition": {"id": match["id"]}},
        )
        after = self.status_of(key)
        return True, f"已流转：{match.get('name')} → {after or (match.get('to') or {}).get('name', '?')}"

    def browse_url(self, key: str) -> str:
        return f"{self.base}/browse/{key}"
