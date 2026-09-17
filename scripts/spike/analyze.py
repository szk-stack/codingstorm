#!/usr/bin/env python3
"""解析 claude -p --output-format stream-json 产出的 NDJSON。

用法：
    python3 analyze.py <file.ndjson> [--summary|--result|--tool-results|--lines]
"""
import json
import sys


def iter_events(path):
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield n, json.loads(line)
            except json.JSONDecodeError:
                yield n, None


def summarize(path):
    counts = {}
    max_line = 0
    over_64k = 0
    with open(path, "rb") as fh:
        for raw in fh:
            ln = len(raw)
            if ln > max_line:
                max_line = ln
            if ln > 65536:
                over_64k += 1
    for _, e in iter_events(path):
        if e is None:
            counts["<解析失败>"] = counts.get("<解析失败>", 0) + 1
            continue
        key = e.get("type", "?")
        if e.get("subtype"):
            key += "/" + e["subtype"]
        counts[key] = counts.get(key, 0) + 1
    print(f"  事件类型分布（共 {sum(counts.values())} 条）:")
    for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"    {k:36s} {v}")
    print(f"  最长行 {max_line} 字节；超过 64 KiB 的行 {over_64k} 条")


def show_result(path):
    for _, e in iter_events(path):
        if e and e.get("type") == "result":
            u = e.get("usage") or {}
            print(f"  subtype={e.get('subtype')} is_error={e.get('is_error')} turns={e.get('num_turns')}")
            print(f"  result={str(e.get('result'))[:160]!r}")
            print(f"  usage: in={u.get('input_tokens')} out={u.get('output_tokens')} "
                  f"cache_read={u.get('cache_read_input_tokens')} cache_new={u.get('cache_creation_input_tokens')}")
            print(f"  total_cost_usd={e.get('total_cost_usd')}  ← 第三方中转下不可信")
            return
    print("  没有 result 事件")


def show_tool_results(path):
    for _, e in iter_events(path):
        if not e:
            continue
        if e.get("type") == "user":
            for b in (e.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    print(f"  tool_result is_error={b.get('is_error')}: {str(b.get('content'))[:200]}")
        elif e.get("type") == "assistant":
            for b in (e.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    print(f"  tool_use {b.get('name')}: {str(b.get('input'))[:160]}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    path = sys.argv[1]
    mode = sys.argv[2] if len(sys.argv) > 2 else "--summary"
    if mode == "--summary":
        summarize(path)
    elif mode == "--result":
        show_result(path)
    elif mode == "--tool-results":
        show_tool_results(path)
    else:
        print(f"未知模式 {mode}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
