"""命令行入口。

存在的理由有二：
1. 不开浏览器也能提交和查看任务（脚本、cron、SSH 里都能用）
2. **给消息网关当接口** —— Hermes 这类 agent 有终端工具集，
   有了 CLI 它就能照着自然语言指令提交任务，不需要写插件去改它

    cs submit demo "给 stats.py 加一个 mode 函数"
    cs ls --status awaiting_review
    cs show 6d94534015bc
    cs diff 6d94534015bc
    cs say 6d94534015bc "改成返回列表，不要返回单个值"
    cs approve 6d94534015bc
    cs schedule add demo "每天生成日报" --daily 09:00
    cs window
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

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
    w = _window(args.base)
    if w["enabled"] and not w["open"]:
        print(f"注意：执行窗口现在关着，要等到 {_local(w['next_open_at'], w['timezone'])} 才会跑")
        print("急着跑的话： cs window off")
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

    # 排队中的任务在窗口外一条都跑不了，不说清楚会让人以为是卡住了
    if any(t["status"] == "queued" for t in rows):
        w = _window(args.base)
        if w["enabled"] and not w["open"]:
            print(f"\n（有任务在排队 —— {_window_line(w)}）")
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
        if a.get("finished_at") is None:
            # 沉淀是在任务转入待审之后才跑的，这时候可能还没结束
            print(f"  {label}  （进行中）")
            continue
        if a.get("input_tokens") is None:
            # 没跑起来就失败了（比如仓库还是空的），没有用量可报
            print(f"  {label}  未执行：{a.get('error_text') or '没有产生用量'}")
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


def cmd_say(args) -> int:
    """接着上一轮说一句。任务回到队列，AI 带着之前的上下文继续。"""
    t = _resolve_task(args.base, args.task)
    call(
        args.base,
        "POST",
        f"/api/tasks/{t['id']}/messages",
        {"text": args.text},
    )
    print(f"已入队 {t['id']} —— 接着上面那轮继续跑")
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


# ---------- 执行窗口 ----------


def _window(base: str) -> dict:
    return call(base, "GET", "/api/window")


def _local(iso: str | None, tz: str) -> str:
    """UTC 串按配置的时区显示 —— 用户配的「9 点」指的是这个时区的 9 点，
    和跑 CLI 的这台机器在哪个时区无关。"""
    if not iso:
        return "—"
    moment = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return moment.astimezone(ZoneInfo(tz)).strftime("%m-%d %H:%M")


def _window_line(w: dict) -> str:
    if not w["enabled"]:
        return "执行窗口：未启用（任务随时可跑）"
    spans = "、".join(f"{a}–{b}" for a, b in w["windows"])
    if w["disabled"]:
        return f"执行窗口：{spans}（{w['timezone']}）· **已被临时关闭**，任务不排队等窗口"
    if w["open"]:
        return f"执行窗口：{spans}（{w['timezone']}）· 开启中，{_local(w['next_close_at'], w['timezone'])} 关闭"
    return f"执行窗口：{spans}（{w['timezone']}）· 已关闭，{_local(w['next_open_at'], w['timezone'])} 开启"


def cmd_window(args) -> int:
    if args.project:
        return _project_window(args)

    if args.action == "off":
        w = call(args.base, "POST", "/api/window", {"disabled": True})
        print("已临时关闭执行窗口 —— 所有任务立即恢复执行")
    elif args.action == "on":
        w = call(args.base, "POST", "/api/window", {"disabled": False})
        print("已恢复执行窗口限制")
    else:
        w = _window(args.base)
    print(_window_line(w))
    if w["disabled"]:
        print("注意：这是内存态开关，平台重启后会自动恢复窗口限制。")
    return 0


def _parse_hours(raw: str) -> list[list[str]]:
    """`"09:00-18:00,20:00-22:00"` → `[["09:00","18:00"], ["20:00","22:00"]]`"""
    spans = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = [p.strip() for p in chunk.replace("–", "-").replace("—", "-").split("-")]
        if len(parts) != 2:
            print(f"时段要写成 09:00-18:00 这样，收到的是 {chunk!r}", file=sys.stderr)
            raise SystemExit(1)
        spans.append(parts)
    if not spans:
        print("--windows 是空的", file=sys.stderr)
        raise SystemExit(1)
    return spans


def _project_window(args) -> int:
    project = _find_project(args.base, args.project)
    mode = args.action or "show"

    if mode == "show":
        current = project.get("window_override")
        print(f"{project['name']} 的窗口覆盖：{_describe_override(current)}")
        print(_window_line(_window(args.base)))
        return 0

    if mode == "inherit":
        body = {"mode": "inherit"}
    elif mode == "always":
        body = {"mode": "always"}
    elif mode == "custom":
        if not args.windows:
            print('custom 模式要配 --windows "09:00-18:00"', file=sys.stderr)
            raise SystemExit(1)
        body = {"mode": "custom", "windows": _parse_hours(args.windows)}
    else:
        print(
            f"认不出 {mode!r}。项目窗口模式只有三种：always（不受限）、inherit（跟随全局）"
            "，或 custom（配 --windows）",
            file=sys.stderr,
        )
        raise SystemExit(1)

    updated = call(args.base, "PUT", f"/api/projects/{project['id']}/window", body)
    print(f"{updated['name']} 的窗口覆盖已设为：{_describe_override(updated['window_override'])}")
    return 0


def _describe_override(raw: str | None) -> str:
    if not raw:
        return "跟随全局"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return f"（读不懂：{raw}）"
    if data.get("mode") == "always":
        return "不受窗口限制"
    if data.get("mode") == "custom":
        spans = "、".join(f"{a}–{b}" for a, b in data.get("windows", []))
        return f"自定义时段 {spans}"
    return raw


# ---------- 定时任务 ----------


def _rule_from_args(args) -> dict:
    """命令行 → 规则。三种形态互斥，argparse 的互斥组已经保证只来一个。"""
    if args.at:
        text = args.at.strip().replace(" ", "T")
        try:
            datetime.strptime(text, "%Y-%m-%dT%H:%M")
        except ValueError:
            print(f"--at 要写成「2026-09-23 09:00」这种，收到的是 {args.at!r}", file=sys.stderr)
            raise SystemExit(1) from None
        return {"type": "once", "at": text}
    if args.daily:
        return {"type": "daily", "time": args.daily}
    days = [d.strip() for d in (args.days or "").split(",") if d.strip()]
    if not days:
        print("--weekly 还要配一个 --days，比如 --days mon,wed", file=sys.stderr)
        raise SystemExit(1)
    return {"type": "weekly", "time": args.weekly, "days": days}


def cmd_schedule_ls(args) -> int:
    query = []
    if args.project:
        query.append(f"project_id={_find_project(args.base, args.project)['id']}")
    path = "/api/schedules" + ("?" + "&".join(query) if query else "")
    rows = call(args.base, "GET", path) or []
    tz = _window(args.base)["timezone"]
    if not rows:
        print("（没有定时任务）")
        return 0

    for s in rows:
        if s["missed_at"]:
            state = "已错过"
        elif not s["enabled"]:
            state = "已停用"
        elif s["next_run_at"] is None:
            state = "已完成"
        else:
            state = _local(s["next_run_at"], tz)
        print(f"{s['id']}  {state:16s}  {s['rule_text']:22s}  {s['title'][:40]}")
        if s["last_task_id"] and s["last_task_status"] == "awaiting_review":
            # 未合并的改动对后续任务不可见，这是每天跑一次的定时任务最容易踩的坑
            print(f"{'':14s}上一轮 {s['last_task_id']} 还没审，本轮会切在旧主干上")
    return 0


def cmd_schedule_add(args) -> int:
    project = _find_project(args.base, args.project)
    schedule = call(
        args.base,
        "POST",
        f"/api/projects/{project['id']}/schedules",
        {
            "title": args.title,
            "body": args.body or "",
            "kind": args.kind,
            "priority": args.priority,
            "rule": _rule_from_args(args),
        },
    )
    tz = _window(args.base)["timezone"]
    print(f"已建立 {schedule['id']}：{schedule['rule_text']}")
    print(f"下一次 {_local(schedule['next_run_at'], tz)}（{tz}）")
    return 0


def cmd_schedule_rm(args) -> int:
    call(args.base, "DELETE", f"/api/schedules/{args.schedule}")
    print(f"已删除 {args.schedule}（它已经生成的任务不受影响）")
    return 0


def cmd_schedule_toggle(args) -> int:
    on = args.action == "resume"
    s = call(args.base, "PATCH", f"/api/schedules/{args.schedule}", {"enabled": on})
    tz = _window(args.base)["timezone"]
    if on:
        print(f"已启用，下一次 {_local(s['next_run_at'], tz)}")
    else:
        print("已停用")
    return 0


def cmd_schedule_run(args) -> int:
    task = call(args.base, "POST", f"/api/schedules/{args.schedule}/run")
    w = _window(args.base)
    print(f"已入队 {task['id']}：{task['title']}")
    if w["enabled"] and not w["open"]:
        print(f"注意：执行窗口现在关着，要等到 {_local(w['next_open_at'], w['timezone'])} 才会跑")
    return 0


def build_parser() -> argparse.ArgumentParser:
    # 顶层解析器单独起个名字：下面每一段都要复用短变量名，
    # 用 p 的话最后 return 回去的就是最后那个子解析器（踩过）
    top = argparse.ArgumentParser(prog="cs", description="codingstorm 命令行")
    top.add_argument("--base", default=DEFAULT_BASE, help="API 地址（默认 %(default)s）")
    sub = top.add_subparsers(dest="command", required=True)

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

    s = sub.add_parser("say", help="接着上一轮说一句（多轮对话）")
    s.add_argument("task", help="任务 id（支持唯一前缀）")
    s.add_argument("text", help="要说的话")
    s.set_defaults(func=cmd_say)

    sub.add_parser("usage", help="查看用量与成本").set_defaults(func=cmd_usage)

    # ---- 定时任务 ----
    s = sub.add_parser("schedule", help="定时任务：到点自动提交")
    ssub = s.add_subparsers(dest="action", required=True)

    p = ssub.add_parser("ls", help="列出定时任务")
    p.add_argument("--project")
    p.set_defaults(func=cmd_schedule_ls)

    p = ssub.add_parser("add", help="新建定时任务")
    p.add_argument("project", help="项目名或 id")
    p.add_argument("title", help="每次生成的任务标题")
    when = p.add_mutually_exclusive_group(required=True)
    when.add_argument("--at", metavar="时间", help='一次性，如 --at "2026-09-23 09:00"')
    when.add_argument("--daily", metavar="HH:MM", help="每天这个点")
    when.add_argument("--weekly", metavar="HH:MM", help="每周这个点，配合 --days")
    p.add_argument("--days", help="星期几，如 mon,wed（仅配合 --weekly）")
    p.add_argument("--body", help="任务正文")
    p.add_argument("--kind", default="task", choices=["requirement", "instruction", "bug", "task"])
    p.add_argument("--priority", type=int, default=0)
    p.set_defaults(func=cmd_schedule_add)

    for name, fn, help_text in (
        ("rm", cmd_schedule_rm, "删除（已生成的任务不受影响）"),
        ("pause", cmd_schedule_toggle, "停用，不再触发"),
        ("resume", cmd_schedule_toggle, "重新启用并按规则重算下一次"),
        ("run", cmd_schedule_run, "立刻跑一次，不影响原定排期"),
    ):
        p = ssub.add_parser(name, help=help_text)
        p.add_argument("schedule", help="定时任务 id")
        p.set_defaults(func=fn)

    # ---- 执行窗口 ----
    p = sub.add_parser("window", help="查看执行窗口、临时开关它，或给单个项目配例外")
    p.add_argument(
        "action",
        nargs="?",
        default="",
        help="off = 临时关闭（急事用），on = 恢复；"
        "配了 --project 时这里填 always / inherit / custom",
    )
    p.add_argument("--project", help="改单个项目的窗口覆盖，不填就看全局")
    p.add_argument("--windows", help='custom 模式的时段，如 "09:00-18:00"，多段用逗号分隔')
    p.set_defaults(func=cmd_window)
    return top


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
