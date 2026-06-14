"""
代码编辑工具（原子工具版）

原子工具:
  code_read      — 读取代码文件
  code_diff      — 预览代码修改差异
  code_write     — 提交代码修改（带备份）
  code_rollback  — 回滚代码修改
  code_append    — 追加代码
  code_create    — 创建代码文件
"""

import os
import difflib
import shutil
import logging
from typing import Any, Dict, List

from engine.tool.base import (
    BaseTool, ToolDefinition, ToolParameter, ToolResult,
    CATEGORY_CODE,
)

logger = logging.getLogger(__name__)


class CodeTool(BaseTool):
    """代码编辑工具集（安全流程：read → diff → write → rollback）"""

    _history: List[Dict] = []

    def __init__(self):
        self._handlers = {
            "code_read": self._handle_read,
            "code_diff": self._handle_diff,
            "code_write": self._handle_write,
            "code_rollback": self._handle_rollback,
            "code_append": self._handle_append,
            "code_create": self._handle_create,
        }
        for t in self.tools:
            t.handler = self._handlers.get(t.name)

    @property
    def name(self) -> str:
        return "code"

    @property
    def category(self) -> str:
        return CATEGORY_CODE

    @property
    def tools(self) -> List[ToolDefinition]:
        return [
            ToolDefinition(
                name="code_read",
                description="""读取编程语言代码文件内容，自动提取类/函数/方法结构信息。

使用场景：
- 查看 .py/.js/.ts/.java/.go/.rs/.cpp 等代码文件的内容
- 快速定位函数或类的定义位置
- 在修改前确认文件内容

不适用场景：
- 读取日志/配置/数据/文本文件 → 用 file_read
- 读取 PDF/Word/Excel → 用对应的专用工具
- 仅需知道文件是否存在 → 用 file_glob""",
                parameters=[
                    ToolParameter("path", "string", "文件路径（相对或绝对路径）", required=True),
                    ToolParameter("start_line", "number", "起始行号（从 1 开始），默认从第 1 行", required=False),
                    ToolParameter("end_line", "number", "结束行号（包含），默认到文件末尾", required=False),
                ],
                is_read=True,
                examples=[
                    'code_read(path="app.py")  # 读取整个文件',
                    'code_read(path="src/main.py", start_line=10, end_line=50)  # 读取第10-50行',
                    'code_read(path="utils/helpers.py", start_line=1, end_line=30)  # 只读取文件开头部分',
                ],
                constraints=[
                    "仅支持编程语言代码文件，非代码文件请用 file_read",
                    "指定行范围可以节省 token，但要注意行号从 1 开始",
                    "读取 Python 文件时会自动提取类/函数结构信息",
                ],
            ),
            ToolDefinition(
                name="code_diff",
                description="""预览代码文件的修改差异（不修改文件本身）。
通过 old_text/new_text 精确替换来模拟修改，输出 unified diff 格式。

使用场景：
- 在 code_write 前预览修改效果
- 验证 old_text 能在文件中唯一匹配
- 确认 diff 结果符合预期后再提交

不适用场景：
- 直接修改文件 → 用 code_write
- 对比两个不同文件 → 用 file_diff""",
                parameters=[
                    ToolParameter("path", "string", "文件路径", required=True),
                    ToolParameter("old_text", "string", "要被替换的原始代码段（必须精确匹配，包括缩进和空行）", required=True),
                    ToolParameter("new_text", "string", "替换后的新代码段（缩进风格需与 old_text 一致）", required=True),
                ],
                is_read=True,
                examples=[
                    'code_diff(path="app.py", old_text="def hello():\\n    print(\"hi\")", new_text="def hello(name: str):\\n    print(f\"hi {name}\")")',
                    'code_diff(path="server.js", old_text="app.listen(3000);", new_text="app.listen(process.env.PORT || 3000);")',
                ],
                constraints=[
                    "old_text 必须在文件中唯一匹配，否则会报错",
                    "old_text 必须与文件内容完全一致（包括缩进和空行），建议先 code_read 确认",
                    "匹配到 0 处：请用 code_read 再次确认文件当前内容",
                    "匹配到多处：old_text 太短，增加上下文使其唯一",
                ],
            ),
            ToolDefinition(
                name="code_write",
                description="""提交代码修改到文件（带 .bak 自动备份，可通过 code_rollback 回滚）。

安全流程：先 code_diff 预览 → 再 code_write 提交。

使用场景：
- 修复代码中的 bug
- 修改现有函数的逻辑
- 重构代码结构

不适用场景：
- 创建新文件 → 用 code_create
- 追加内容到文件末尾 → 用 code_append
- 修改非代码文件（日志/配置/数据）→ 用 file_write""",
                parameters=[
                    ToolParameter("path", "string", "文件路径", required=True),
                    ToolParameter("old_text", "string", "要被替换的原始代码段（必须精确匹配）", required=True),
                    ToolParameter("new_text", "string", "替换后的新代码段", required=True),
                ],
                examples=[
                    'code_write(path="app.py", old_text="def hello():\\n    print(\"hi\")", new_text="def hello(name: str):\\n    print(f\"hi {name}\")")',
                    'code_write(path="main.go", old_text="fmt.Println(\"old\")", new_text="fmt.Println(\"new\")")',
                ],
                constraints=[
                    "必须先 code_diff 预览确认 old_text 能唯一匹配，再执行 code_write",
                    "自动备份到 .bak 文件，修改失败可用 code_rollback 回滚",
                    "old_text 找不到匹配时：重新 code_read 确认文件内容",
                    "不要连续修改同一文件超过 3 次而不检查结果和语法",
                    "一次调用只替换一处，多处修改请多次调用",
                ],
            ),
            ToolDefinition(
                name="code_rollback",
                description="""回滚最近的 code_write 修改，将文件恢复到修改前的状态。
优先从 .bak 备份文件恢复，如果 .bak 不存在则尝试文本反向替换。

使用场景：
- code_write 后代码出现语法错误
- 修改结果不符合预期
- 需要恢复到原始版本重新开始""",
                parameters=[
                    ToolParameter("path", "string", "要回滚的文件路径", required=True),
                ],
                examples=[
                    'code_rollback(path="app.py")  # 回滚 app.py 到修改前的状态',
                ],
                constraints=[
                    "只能回滚最近一次 code_write 的修改",
                    "如果连续多次修改了同一个文件，可多次调用 rollback 逐次回滚",
                    "文件被删除或手动修改后可能无法回滚",
                ],
            ),
            ToolDefinition(
                name="code_append",
                description="""追加代码到代码文件末尾（带 .bak 自动备份）。

使用场景：
- 在文件末尾添加新的函数/类/方法
- 添加 import 语句
- 追加配置代码或常量定义

不适用场景：
- 修改文件中间的内容 → 用 code_write
- 创建新文件 → 用 code_create
- 追加非代码内容 → 用 file_append""",
                parameters=[
                    ToolParameter("path", "string", "文件路径", required=True),
                    ToolParameter("new_text", "string", "要追加的代码内容", required=True),
                ],
                examples=[
                    'code_append(path="app.py", new_text="def new_function():\\n    pass")',
                    'code_append(path="utils.py", new_text="import json\\nimport os")',
                ],
                constraints=[
                    "自动备份到 .bak，追加出错可用 code_rollback 回滚",
                    "追加内容会在文件末尾另起新行",
                    "不要在同一个循环中连续追加大量小片段，尽量一次追加完整内容",
                ],
            ),
            ToolDefinition(
                name="code_create",
                description="""创建新的代码文件。
如果文件已存在则自动备份到 .bak 再覆盖。目录路径不存在时会自动创建。

使用场景：
- 从头编写新模块/新脚本
- 生成新的测试文件
- 创建配置文件模板

不适用场景：
- 修改已有文件 → 用 code_write
- 创建非代码文件 → 用 file_write""",
                parameters=[
                    ToolParameter("path", "string", "文件路径（可以包含子目录）", required=True),
                    ToolParameter("new_text", "string", "文件的完整内容", required=True),
                ],
                examples=[
                    'code_create(path="new_module.py", new_text="def main():\\n    pass\\n\\nif __name__ == \"__main__\":\\n    main()")',
                    'code_create(path="tests/test_utils.py", new_text="import pytest\\n\\ndef test_hello():\\n    assert True")',
                ],
                constraints=[
                    "创建新文件时请确保提供完整的文件内容",
                    "如果文件已存在会覆盖（自动备份到 .bak）",
                    "目录路径不存在时会自动创建",
                ],
            ),
        ]

    async def execute(self, call_id: str, tool_name: str, **kwargs) -> ToolResult:
        handler = self._handlers.get(tool_name)
        if not handler:
            return ToolResult.fail(call_id, tool_name, f"未知工具: {tool_name}")
        try:
            return await handler(call_id, **kwargs)
        except Exception as e:
            return ToolResult.fail(call_id, tool_name, str(e))

    async def _handle_read(self, call_id: str, path: str,
                           start_line: int = 1, end_line: int = None) -> ToolResult:
        if not os.path.isfile(path):
            return ToolResult.fail(call_id, "code_read", f"文件不存在: {path}")

        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        total = len(lines)
        start = max(0, int(start_line) - 1)
        end = min(total, int(end_line) if end_line else total)
        content = "".join(lines[start:end])
        structure = self._extract_structure(content) if path.endswith(".py") else []

        return ToolResult.ok(call_id, "code_read", {
            "path": path, "total_lines": total,
            "lines": f"{start+1}-{end}", "content": content,
            "structure": structure[:30],
        })

    async def _handle_diff(self, call_id: str, path: str, old_text: str, new_text: str) -> ToolResult:
        if not os.path.isfile(path):
            return ToolResult.fail(call_id, "code_diff", f"文件不存在: {path}")

        with open(path, "r", encoding="utf-8") as f:
            content = f.read()

        count = content.count(old_text)
        if count == 0:
            return ToolResult.fail(call_id, "code_diff",
                "未找到匹配文本。请用 code_read 确认文件内容，复制原文（含缩进和空行）。")
        if count > 1:
            return ToolResult.fail(call_id, "code_diff",
                f"匹配到 {count} 处。请扩大 old_text 范围使其唯一。")

        new_content = content.replace(old_text, new_text, 1)
        diff = "".join(difflib.unified_diff(
            content.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=f"a/{path}", tofile=f"b/{path}", n=3,
        ))

        return ToolResult.ok(call_id, "code_diff", {
            "path": path, "diff": diff,
        })

    async def _handle_write(self, call_id: str, path: str, old_text: str, new_text: str) -> ToolResult:
        if not os.path.isfile(path):
            return ToolResult.fail(call_id, "code_write", f"文件不存在: {path}")

        with open(path, "r", encoding="utf-8") as f:
            content = f.read()

        count = content.count(old_text)
        if count == 0:
            return ToolResult.fail(call_id, "code_write", "未找到匹配文本。先 code_diff 预览确认。")
        if count > 1:
            return ToolResult.fail(call_id, "code_write", f"匹配到 {count} 处。请扩大 old_text 范围。")

        bak = path + ".bak"
        shutil.copy2(path, bak)

        new_content = content.replace(old_text, new_text, 1)
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)

        self._history.append({"path": path, "backup": bak, "old": old_text, "new": new_text})

        return ToolResult.ok(call_id, "code_write", {
            "path": path, "status": "已修改", "backup": bak,
        })

    async def _handle_rollback(self, call_id: str, path: str) -> ToolResult:
        entry = next((e for e in reversed(self._history) if e["path"] == path), None)
        if not entry:
            bak = path + ".bak"
            if os.path.exists(bak):
                shutil.copy2(bak, path)
                return ToolResult.ok(call_id, "code_rollback", {
                    "path": path, "status": "已从 .bak 恢复",
                })
            return ToolResult.fail(call_id, "code_rollback", f"没有 {path} 的编辑记录")

        if os.path.exists(entry["backup"]):
            shutil.copy2(entry["backup"], path)
        else:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            with open(path, "w", encoding="utf-8") as f:
                f.write(content.replace(entry["new"], entry["old"], 1))

        self._history.remove(entry)
        return ToolResult.ok(call_id, "code_rollback", {"path": path, "status": "已回滚"})

    async def _handle_append(self, call_id: str, path: str, new_text: str) -> ToolResult:
        if not os.path.isfile(path):
            return ToolResult.fail(call_id, "code_append", f"文件不存在: {path}")

        bak = path + ".bak"
        shutil.copy2(path, bak)

        with open(path, "a", encoding="utf-8") as f:
            f.write("\n" + new_text)

        return ToolResult.ok(call_id, "code_append", {"path": path, "status": "已追加"})

    async def _handle_create(self, call_id: str, path: str, new_text: str) -> ToolResult:
        if not path:
            return ToolResult.fail(call_id, "code_create", "需要 path")

        existed = os.path.isfile(path)
        if existed:
            shutil.copy2(path, path + ".bak")

        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_text)

        return ToolResult.ok(call_id, "code_create", {
            "path": path, "status": "已创建" if not existed else "已覆盖",
            "size": len(new_text),
            "backup": path + ".bak" if existed else None,
        })

    @staticmethod
    def _extract_structure(content: str) -> List[Dict]:
        structure = []
        for line in content.splitlines():
            s = line.strip()
            if s.startswith("class ") and s.endswith(":"):
                structure.append({"type": "class", "name": s[6:-1].split("(")[0].split(":")[0].strip()})
            elif s.startswith("def ") and s.endswith(":"):
                structure.append({"type": "function", "name": s[4:-1].split("(")[0].strip()})
            elif s.startswith("async def ") and s.endswith(":"):
                structure.append({"type": "async_function", "name": s[11:-1].split("(")[0].strip()})
        return structure
