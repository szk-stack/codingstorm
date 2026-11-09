"""和 codingstorm 主服务说话的小客户端。

只给 tools/ 里的助手工具用；不依赖第三方库。
所有错误都转成给模型看的文字，不让对话崩掉。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import config


class CodingstormError(RuntimeError):
    pass


def request(method: str, path: str, body: dict | None = None):
    base = config.CODINGSTORM_URL.rstrip("/")
    url = base + path
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        try:
            parsed = json.loads(detail)
            detail = parsed.get("detail") or detail
        except Exception:
            pass
        raise CodingstormError(f"codingstorm 返回 HTTP {exc.code}：{detail}") from exc
    except urllib.error.URLError as exc:
        raise CodingstormError(
            f"连不上 codingstorm（{base}）：{exc.reason}。请确认主服务已启动。"
        ) from exc


def list_projects() -> list[dict]:
    return request("GET", "/api/projects") or []


def resolve_project(value: str | None) -> dict:
    projects = list_projects()
    if not projects:
        raise CodingstormError("还没有注册任何项目。请先在任务队列页注册一个项目。")
    value = (value or "").strip()
    if not value:
        if len(projects) == 1:
            return projects[0]
        names = "、".join(p["name"] for p in projects)
        raise CodingstormError(f"有多个项目，请先问用户在哪个项目做：{names}")
    for p in projects:
        if value in (p.get("id"), p.get("name")):
            return p
    lowered = value.lower()
    matches = [p for p in projects if str(p.get("name", "")).lower() == lowered]
    if len(matches) == 1:
        return matches[0]
    names = "、".join(p["name"] for p in projects)
    raise CodingstormError(f"没有找到项目「{value}」。现有项目：{names}")
