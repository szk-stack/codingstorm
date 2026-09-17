"""diff 渲染测试。

重点是转义：diff 内容来自被调度的仓库，是**不可信输入**。
"""

from codingstorm.diff_render import render_diff


def test_renders_file_and_hunk():
    diff = (
        "diff --git a/foo.py b/foo.py\n"
        "index 111..222 100644\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1,3 +1,4 @@\n"
        " import os\n"
        "-old line\n"
        "+new line\n"
        "+another\n"
        " tail\n"
    )
    html = render_diff(diff)
    assert 'diff-file-head' in html
    assert "foo.py" in html
    assert 'class="add"' in html and "new line" in html
    assert 'class="del"' in html and "old line" in html
    assert 'class="ctx"' in html


def test_escapes_html_in_content():
    """仓库里可能有任意内容，绝不能当 HTML 解析。"""
    diff = (
        "diff --git a/x.html b/x.html\n"
        "@@ -0,0 +1,2 @@\n"
        "+<script>alert('xss')</script>\n"
        '+<img src=x onerror="alert(1)">\n'
    )
    html = render_diff(diff)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<img" not in html
    assert "&lt;img" in html
    assert "onerror" not in html or "&quot;" in html


def test_escapes_html_in_file_path():
    diff = "diff --git a/<b>weird</b>.py b/<b>weird</b>.py\n@@ -0,0 +1 @@\n+x\n"
    html = render_diff(diff)
    assert "<b>" not in html
    assert "&lt;b&gt;" in html


def test_line_numbers_track_both_sides():
    diff = (
        "diff --git a/f b/f\n"
        "@@ -10,3 +20,3 @@\n"
        " ctx\n"
        "-gone\n"
        "+added\n"
    )
    html = render_diff(diff)
    # 上下文行：老 10 / 新 20
    assert '<td class="ln">10</td><td class="ln">20</td>' in html
    # 删除行只占老侧，新增行只占新侧
    assert '<td class="ln">11</td><td class="ln"></td>' in html
    assert '<td class="ln"></td><td class="ln">21</td>' in html


def test_new_file_diff_has_no_del_lines():
    diff = (
        "diff --git a/new.py b/new.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/new.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+a\n"
        "+b\n"
    )
    html = render_diff(diff)
    assert 'class="add"' in html
    assert 'class="del"' not in html


def test_empty_diff():
    assert "没有改动" in render_diff("")
    assert "没有改动" in render_diff("   \n")


def test_truncation_notice():
    diff = "diff --git a/f b/f\n@@ -0,0 +1 @@\n+x\n\n[diff 已截断，超出 1000 字节]\n"
    assert "已截断" in render_diff(diff)


def test_binary_file_line():
    diff = "diff --git a/x.pyc b/x.pyc\nBinary files /dev/null and b/x.pyc differ\n"
    html = render_diff(diff)
    assert "Binary files" in html
    assert "x.pyc" in html


def test_no_newline_marker_is_handled():
    diff = (
        "diff --git a/f b/f\n"
        "@@ -1 +1 @@\n"
        "-a\n"
        "\\ No newline at end of file\n"
        "+b\n"
    )
    html = render_diff(diff)
    assert "No newline" in html


def test_multiple_files():
    diff = (
        "diff --git a/one.py b/one.py\n@@ -0,0 +1 @@\n+x\n"
        "diff --git a/two.py b/two.py\n@@ -0,0 +1 @@\n+y\n"
    )
    html = render_diff(diff)
    assert html.count('class="diff-file"') == 2
    assert "one.py" in html and "two.py" in html
