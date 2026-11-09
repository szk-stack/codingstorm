# 这个文件让 tools 文件夹变成一个 Python "包"（和 plugins/ 一样）。
#
# 以后写新工具：在这个文件夹里新建一个 .py 文件，照着 list_dir.py 的样子写：
#   1. 写一个 DESCRIPTION 字典（工具说明书，是发给模型看的）
#   2. 写一个 run(arguments) 函数（真正干活，返回字符串结果）
#   3. 文件末尾 register(DESCRIPTION, run) 登记一下
# 重启服务，模型就能用上新工具了。
#
# （下划线开头的文件不会被当成工具加载，可以放内部公用代码。）
