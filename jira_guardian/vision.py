"""看图：把卡上的截图交给多模态模型描述一遍。

很多 bug 卡只有 before/after 两张图，光读文字会得出错误结论（我们踩过：文字侧已修好，
图片侧还是错的）。这里用 OpenAI 兼容的 /chat/completions，图片走 base64 内联。

注意：不传 temperature。某些网关（例如公司内网的 gpt-5.6-*）只接受默认值，
带 0.1 会 400 `Unsupported value: 'temperature'`。
"""

from __future__ import annotations

import base64
import json
import mimetypes
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .config import Config


class VisionError(RuntimeError):
    pass


def _mime(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "image/png"


def describe(cfg: Config, prompt: str, images: list[str], *, model: str | None = None,
             timeout: int | None = None) -> str:
    llm = cfg.data.get("llm") or {}
    base = str(llm.get("base_url") or "").rstrip("/")
    key = str(llm.get("api_key") or "")
    use = model or llm.get("vision_model") or llm.get("model")
    if not (base and key and use):
        raise VisionError("config.json 的 llm.base_url / api_key / vision_model 没配全")
    files = [Path(p).expanduser() for p in images]
    missing = [str(p) for p in files if not p.is_file()]
    if missing:
        raise VisionError(f"图片不存在：{', '.join(missing)}")

    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for path in files:
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        content.append({"type": "image_url", "image_url": {"url": f"data:{_mime(path)};base64,{b64}"}})

    body = {"model": use, "messages": [{"role": "user", "content": content}]}
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout or int(llm.get("timeout") or 180)) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:800]
        raise VisionError(f"多模态请求失败 HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise VisionError(f"多模态请求失败：{type(exc).__name__}: {exc}") from exc

    choices = payload.get("choices") or []
    if not choices:
        raise VisionError(f"返回里没有 choices：{json.dumps(payload, ensure_ascii=False)[:400]}")
    message = choices[0].get("message") or {}
    text = message.get("content")
    if isinstance(text, list):
        text = "\n".join(part.get("text", "") for part in text if isinstance(part, dict))
    return (text or "").strip()
