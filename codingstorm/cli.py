"""命令行入口。

存在的理由有二：
1. 不开浏览器也能提交和查看任务（脚本、cron、SSH 里都能用）
2. **给消息网关当接口** —— Hermes 这类 agent 有终端工具集，
   有了 CLI 它就能照着自然语言指令提交任务，不需要写插件去改它

    cs submit demo "给 stats.py 加一个 mode 函数"
    cs ls --status awaiting_review
    cs show 6d94534015bc
    cs diff 6d94534015bc
    cs approve 6d94534015bc
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_BASE = os.environ.get("CODINGSTORM_API", "http://127.0.0.1:8788")

STATUS_LABEL = {
    "queued": "排队中",
    "running": "执行中",
    "awaiting_review": "待审阅",
    "merged": "已合并",
    "discarded": "已丢弃",
    "failed": "失败",
    "interrupted": "已中断",
    "cancelled": "已取消",
}


def call(base: str, method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=data,
        method=method,
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail).get("detail", detail)
        except json.JSONDecodeError:
            pass
        print(f"错误（HTTP {exc.code}）：{detail}", file=sys.stderr)
        raise SystemExit(1)
    except urllib.error.URLError as exc:
        print(f"连不上 {base}：{exc.reason}", file=sys.stderr)
        raise SystemExit(1) from exc


def _find_project(base: str, name: str) -> dict:
    for p in call(base, "GET", "/api/projects") or []:
        if p["name"] == name or p["id"] == name:
            return p
    print(f"找不到项目：{name}", file=sys.stderr)
    raise SystemExit(1)


def _resolve_task(base: str, ref: str) -> dict:
    """接受完整 id，也接受唯一前缀 —— 手打 id 太长了。"""
    if len(ref) >= 12:
        return call(base, "GET", f"/api/tasks/{ref}")
    matches = [t for t in (call(base, "GET", "/api/tasks") or []) if t["id"].startswith(ref)]
    if not matches:
        print(f"找不到任务：{ref}", file=sys.stderr)
        raise SystemExit(1)
    if len(matches) > 1:
        print(f"前缀 {ref} 匹配到多条任务，请写长一点", file=sys.stderr)
        raise SystemExit(1)
    return matches[0]


def _status(task: dict) -> str:
    s = task["status"]
    return STATUS_LABEL.get(s, s)


# ---------- 子命令 ----------


def cmd_projects(args) -> int:
    rows = call(args.base, "GET", "/api/projects") or []
    if not rows:
        print("还没有项目。用 `cs projects` 之外的方式注册第一个项目。")
        return 0
    for p in rows:
        print(f"{p['id']}  {p['name']:16s} {p['target_branch']:8s} {p['repo_path']}")
    return 0


def cmd_submit(args) -> int:
    project = _find_project(args.base, args.project)
    task = call(
        args.base,
        "POST",
        f"/api/projects/{project['id']}/tasks",
        {"title": args.title, "body": args.body or "", "kind": args.kind, "priority": args.priority},
    )
    print(f"已入队 {task['id']}（{_status(task)}）：{task['title']}")
    print(f"跑完可看： cs show {task['id']}")
    return 0


def cmd_ls(args) -> int:
    query = []
    if args.project:
        query.append(f"project_id={_find_project(args.base, args.project)['id']}")
    if args.status:
        query.append(f"status={args.status}")
    path = "/api/tasks" + ("?" + "&".join(query) if query else "")
    rows = call(args.base, "GET", path) or []
    if not rows:
        print("（没有任务）")
        return 0
    for t in rows:
        print(f"{t['id']}  {_status(t):6s}  {t['title'][:60]}")
    return 0


def cmd_show(args) -> int:
    t = _resolve_task(args.base, args.task)
    print(f"标题   {t['title']}")
    print(f"状态   {_status(t)}")
    print(f"类型   {t['kind']}")
    if t.get("branch"):
        print(f"分支   {t['branch']}")
    if t.get("commit_sha"):
        print(f"提交   {t['commit_sha'][:8]}")
    if t.get("merge_commit_sha"):
        print(f"已合并 {t['merge_commit_sha'][:8]}")
    if t.get("error_text"):
        print(f"说明   {t['error_text']}")

    attempts = call(args.base, "GET", f"/api/tasks/{t['id']}/attempts") or []
    for a in attempts:
        label = "记录沉淀" if a["origin"] == "sediment" else "执行任务"
        if a.get("input_tokens") is None:
            # 沉淀是在任务转入待审之后才跑的，这时候可能还没结束
            print(f"  {label}  （进行中）")
            continue
        cost = f"  ${a['cost_usd']:.4f}" if a.get("cost_usd") is not None else ""
        print(f"  {label}  in={a['input_tokens']}  out={a['output_tokens']}{cost}")
    return 0


def cmd_events(args) -> int:
    t = _resolve_task(args.base, args.task)
    for e in call(args.base, "GET", f"/api/tasks/{t['id']}/events") or []:
        p = e.get("payload") or {}
        if e["type"] == "assistant":
            if p.get("text"):
                print(f"  {p['text'][:200]}")
            for tu in p.get("tool_uses") or []:
                print(f"  → {tu['name']}: {json.dumps(tu['input'], ensure_ascii=False)[:140]}")
        elif e["type"] == "tool_result":
            mark = "（出错）" if p.get("is_error") else ""
            print(f"  ← {mark}{str(p.get('content'))[:140]}")
        elif e["type"] == "result":
            print(f"  ✓ {p.get('result') or p.get('subtype')}")
    return 0


def cmd_diff(args) -> int:
    t = _resolve_task(args.base, args.task)
    d = call(args.base, "GET", f"/api/tasks/{t['id']}/diff")
    print(d.get("stat") or "")
    print(d.get("diff") or "（没有改动）")
    return 0


def cmd_approve(args) -> int:
    t = _resolve_task(args.base, args.task)
    out = call(args.base, "POST", f"/api/tasks/{t['id']}/approve")
    print(f"已合并到 {out.get('merge_commit_sha', '?')[:8]}")
    return 0


def cmd_discard(args) -> int:
    t = _resolve_task(args.base, args.task)
    call(args.base, "POST", f"/api/tasks/{t['id']}/discard")
    print("已丢弃")
    return 0


def cmd_requeue(args) -> int:
    t = _resolve_task(args.base, args.task)
    call(args.base, "POST", f"/api/tasks/{t['id']}/requeue")
    print("已重新入队")
    return 0


def cmd_usage(args) -> int:
    d = call(args.base, "GET", "/api/usage")
    print(f"任务尝试 {d['task_attempts']} 次，记录沉淀 {d['sediment_attempts']} 次")
    for m in d["by_model"]:
        cost = f"${m['cost_usd']:.4f}" if m.get("cost_usd") is not None else "未配置价目"
        print(f"  {m['model']}: {m['attempts']} 次  in={m['input_tokens']}  out={m['output_tokens']}  {cost}")
    if d.get("note"):
        print(f"提示：{d['note']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cs", description="codingstorm 命令行")
    p.add_argument("--base", default=DEFAULT_BASE, help="API 地址（默认 %(default)s）")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("projects", help="列出项目").set_defaults(func=cmd_projects)

    s = sub.add_parser("submit", help="提交任务（入队即返回，不用等）")
    s.add_argument("project", help="项目名或 id")
    s.add_argument("title", help="任务标题")
    s.add_argument("--body", help="详细描述")
    s.add_argument("--kind", default="task", choices=["requirement", "instruction", "bug", "task"])
    s.add_argument("--priority", type=int, default=0)
    s.set_defaults(func=cmd_submit)

    s = sub.add_parser("ls", help="列出任务")
    s.add_argument("--project")
    s.add_argument("--status")
    s.set_defaults(func=cmd_ls)

    for name, fn, help_text in (
        ("show", cmd_show, "查看任务详情与用量"),
        ("events", cmd_events, "查看执行过程"),
        ("diff", cmd_diff, "查看改动"),
        ("approve", cmd_approve, "批准并合入"),
        ("discard", cmd_discard, "丢弃"),
        ("requeue", cmd_requeue, "重新入队"),
    ):
        s = sub.add_parser(name, help=help_text)
        s.add_argument("task", help="任务 id（支持唯一前缀）")
        s.set_defaults(func=fn)

    sub.add_parser("usage", help="查看用量与成本").set_defaults(func=cmd_usage)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
