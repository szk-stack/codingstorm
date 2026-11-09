"""
插件示例②：每次 AI 回答后，在终端打印一行日志

用来演示 after_llm 空槽。以后你可以照着它的样子写更多插件。
"""

from hooks import after_llm


def print_log(ctx):
    """在终端打印"用户问了什么、AI 答了什么"（太长就截断）。"""
    question = ""
    if ctx["messages"]:
        question = ctx["messages"][-1]["content"]

    answer = ctx["reply"] or ""

    print(f"[对话] 用户问：{question[:40]}")
    print(f"[对话] AI 答：{answer[:40]}")


after_llm.append(print_log)
