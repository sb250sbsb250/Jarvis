"""
文件工具（原子工具版）

原子工具:
  file_list    — 列出目录文件
  file_read    — 读取文件内容
  file_glob    — 通配匹配文件
  file_write   — 写入文件（覆盖）
  file_append  — 追加内容到文件
  file_rename  — 重命名/移动文件
  file_diff    — 对比两个文件
"""

import os
import difflib
import shutil
import fnmatch
import logging
from typing import List

from engine.tool.base import (
    BaseTool, ToolDefinition, ToolParameter, ToolResult,
    CATEGORY_FILE,
)

logger = logging.getLogger(__name__)

_EXCLUDE_DIRS = {'.git', '__pycache__', 'node_modules', '.venv', 'venv',
                 '.idea', '.vscode', 'dist', 'build', '.egg-info'}


class FileTool(BaseTool):
    """文件操作工具集"""

    def __init__(self):
        self._handlers = {
            "file_list": self._handle_list,
            "file_read": self._handle_read,
            "file_glob": self._handle_glob,
            "file_write": self._handle_write,
            "file_append": self._handle_append,
            "file_rename": self._handle_rename,
            "file_diff": self._handle_diff,
        }
        for t in self.tools:
            t.handler = self._handlers.get(t.name)

    @property
    def name(self) -> str:
        return "file"

    @property
    def category(self) -> str:
        return CATEGORY_FILE

    @property
    def tools(self) -> List[ToolDefinition]:
        return [
            ToolDefinition(
                name="file_list",
                description="""列出指定目录下的文件列表，支持通配符过滤。

使用场景：
- 查看目录下有哪些文件
- 按扩展名筛选文件（如所有 .py 文件）
- 确认文件名和路径后再进行其他操作""",
                parameters=[
                    ToolParameter("pattern", "string", "文件匹配模式，如 '*.py' 或 '*.{py,js}'，默认 '*' 匹配所有", required=False),
                    ToolParameter("path", "string", "目标目录路径，默认当前工作目录", required=False),
                    ToolParameter("recursive", "boolean", "是否递归子目录，默认 true。设为 false 只列出当前目录", required=False),
                ],
                is_read=True,
                examples=[
                    'file_list()  # 列出当前目录所有文件（递归）',
                    'file_list(pattern="*.py")  # 列出所有 Python 文件',
                    'file_list(path="C:/Downloads", pattern="*.pdf", recursive=False)  # 仅在 Downloads 目录下找 PDF',
                ],
                constraints=[
                    "默认递归子目录，如果目录深度很大可能会慢",
                    "自动排除 .git, __pycache__, node_modules 等目录",
                    "最多返回 100 个结果",
                    "不传 pattern 时仅返回文件名，不包含扩展名过滤",
                ],
            ),
            ToolDefinition(
                name="file_read",
                description="""读取任意文件内容（日志/配置/文档/数据/文本等）。支持指定行范围。

使用场景：
- 读取 .txt, .log, .json, .yaml, .md, .env, .gitignore 等文本文件
- 读取代码文件的内容（但推荐用 code_read，会自动提取类/函数结构）
- 读取大数据文件的部分行

不适用场景：
- 读取编程语言代码文件准备修改 → 用 code_read（会提取类/函数结构）
- 读取 PDF/Word/Excel → 用对应的专用工具""",
                parameters=[
                    ToolParameter("path", "string", "文件路径（相对或绝对路径）", required=True),
                    ToolParameter("start_line", "number", "起始行号（从 1 开始），默认从第 1 行", required=False),
                    ToolParameter("end_line", "number", "结束行号（包含），默认到文件末尾", required=False),
                ],
                is_read=True,
                examples=[
                    'file_read(path="config.json")',
                    'file_read(path="log.txt", start_line=100, end_line=200)  # 读取第100-200行',
                    'file_read(path=".env")  # 读取环境变量配置',
                ],
                constraints=[
                    "文件不存在时会报错，先用 file_glob 或 file_list 确认路径",
                    "默认 UTF-8 编码，其他编码会自动转换",
                    "超大文件（>50MB）建议指定行范围读取",
                ],
            ),
            ToolDefinition(
                name="file_glob",
                description="""使用通配符模式搜索匹配的文件路径。支持 ** 递归匹配。

使用场景：
- 找项目中所有匹配某个模式的文件
- 确认文件路径是否存在
- 在全项目中搜索文件""",
                parameters=[
                    ToolParameter("pattern", "string", "通配符模式，如 '**/*.py' 找所有 Python 文件", required=True),
                    ToolParameter("path", "string", "搜索起始目录，默认当前工作目录", required=False),
                ],
                is_read=True,
                examples=[
                    'file_glob(pattern="**/*.py")  # 查找所有 Python 文件',
                    'file_glob(pattern="src/**/*.ts")  # 查找 src 下所有 TypeScript 文件',
                    'file_glob(pattern="*config*")  # 查找文件名包含 config 的文件',
                ],
                constraints=[
                    "最多返回 200 个结果",
                    "自动排除隐藏目录（以 . 开头的目录）",
                    "pattern 必须明确：纯文件名（不含通配符）不一定能匹配到",
                ],
            ),
            ToolDefinition(
                name="file_write",
                description="""写入或覆盖文件内容。自动创建不存在的目录。
适合写入日志、配置、数据、文档等非代码文件。

使用场景：
- 写入配置文件（JSON/YAML/INI/Toml）
- 写入数据文件（CSV/TSV/txt）
- 写入文档（Markdown）

不适用场景：
- 修改编程语言代码文件 → 用 code_write（有备份和回滚）
- 追加内容 → 用 file_append
- 创建代码文件 → 用 code_create（有备份）""",
                parameters=[
                    ToolParameter("path", "string", "文件路径（相对或绝对路径）", required=True),
                    ToolParameter("content", "string", "要写入的文件内容", required=True),
                ],
                examples=[
                    'file_write(path="config.json", content="{\"key\": \"value\"}")',
                    'file_write(path="output.txt", content="Hello World\\n第二行内容")',
                    'file_write(path="docs/README.md", content="# 项目介绍\\n\\n这是一个示例项目")',
                ],
                constraints=[
                    "⚠️ 此操作会直接覆盖已有文件，没有自动备份！",
                    "如需追加内容请用 file_append，不要一次性读写全文件",
                    "修改代码文件请用 code_write（有 .bak 自动备份）",
                    "目录路径不存在时会自动创建",
                ],
            ),
            ToolDefinition(
                name="file_append",
                description="""追加内容到文件末尾。如果文件不存在会自动创建。

使用场景：
- 写日志文件
- 向数据文件追加新行
- 大数据处理时用 file_append 逐步写入中间结果到汇总文件

不适用场景：
- 追加代码到代码文件 → 用 code_append（有 .bak 备份）""",
                parameters=[
                    ToolParameter("path", "string", "文件路径", required=True),
                    ToolParameter("content", "string", "要追加的内容（自动在末尾换行）", required=True),
                ],
                examples=[
                    'file_append(path="log.txt", content="2024-01-01 任务完成")',
                    'file_append(path="_summary.jsonl", content="{\"id\": 1, \"status\": \"ok\"}")  # 大数据处理中间结果',
                ],
                constraints=[
                    "如果文件不存在会自动创建新文件",
                    "自动在文件末尾换行后再追加，每条内容独立成行",
                    "追加代码文件建议用 code_append（带 .bak 备份）",
                ],
            ),
            ToolDefinition(
                name="file_rename",
                description="""重命名或移动文件。支持跨目录移动。

使用场景：
- 重命名文件
- 移动文件到其他目录
- 整理文件目录结构""",
                parameters=[
                    ToolParameter("path", "string", "当前文件路径", required=True),
                    ToolParameter("new_path", "string", "新文件路径（可以是不同的目录+文件名）", required=True),
                ],
                examples=[
                    'file_rename(path="old.txt", new_path="new.txt")',
                    'file_rename(path="temp/data.csv", new_path="archive/data_2024.csv")',
                ],
                constraints=[
                    "目标路径已存在时会报错",
                    "跨目录移动会自动创建目标目录",
                    "只支持文件，不支持目录重命名",
                ],
            ),
            ToolDefinition(
                name="file_diff",
                description="""对比两个文件的差异，输出 unified diff 格式。

使用场景：
- 对比两个配置文件的不同
- 对比两个版本的数据文件
- 对比代码文件（代码文件建议用 code_diff 获得更准确的行内差异）""",
                parameters=[
                    ToolParameter("path_a", "string", "第一个文件路径", required=True),
                    ToolParameter("path_b", "string", "第二个文件路径", required=True),
                ],
                is_read=True,
                examples=[
                    'file_diff(path_a="config_dev.json", path_b="config_prod.json")',
                    'file_diff(path_a="output_v1.txt", path_b="output_v2.txt")',
                ],
                constraints=[
                    "两个文件都必须存在",
                    "如果文件完全相同，diff 输出为空",
                    "代码文件的差异预览建议用 code_diff（更精确的行匹配）",
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

    async def _handle_list(self, call_id: str, pattern: str = "*", path: str = "", recursive: bool = True) -> ToolResult:
        if "*" not in pattern and "?" not in pattern:
            pattern = f"*{pattern}*"

        results = []
        root = path.strip() if path and path.strip() else "."
        for r, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in _EXCLUDE_DIRS and not d.startswith(".")]
            for f in files:
                if fnmatch.fnmatch(f, pattern):
                    fpath = os.path.join(r, f)
                    try:
                        stat = os.stat(fpath)
                        results.append({
                            "name": f, "path": self._rel(fpath),
                            "size": stat.st_size, "modified": stat.st_mtime,
                        })
                    except Exception:
                        results.append({"name": f, "path": self._rel(fpath)})
            if not recursive:
                break

        results.sort(key=lambda x: x["path"])
        results = results[:100]

        return ToolResult.ok(call_id, "file_list", {
            "pattern": pattern, "count": len(results), "files": results,
        })

    async def _handle_read(self, call_id: str, path: str,
                           start_line: int = 1, end_line: int = None) -> ToolResult:
        path = self._safe(path)
        if not path or not os.path.exists(path):
            return ToolResult.fail(call_id, "file_read", f"文件不存在: {path}")

        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except Exception as e:
            return ToolResult.fail(call_id, "file_read", str(e))

        total = len(lines)
        start = max(0, int(start_line) - 1)
        end = min(total, int(end_line) if end_line else total)
        content = "".join(lines[start:end])

        return ToolResult.ok(call_id, "file_read", {
            "path": path, "total_lines": total,
            "lines": f"{start+1}-{end}", "content": content,
        })

    async def _handle_glob(self, call_id: str, pattern: str, path: str = ".") -> ToolResult:
        results = []
        base = path if os.path.isdir(path) else os.path.dirname(path) or "."

        if "**" in pattern:
            for root, dirs, files in os.walk(base):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for f in files:
                    fp = os.path.join(root, f)
                    rel = self._rel(os.path.relpath(fp, base))
                    if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(fp, pattern):
                        results.append(rel)
                        if len(results) >= 200:
                            break
                if len(results) >= 200:
                    break
        else:
            try:
                for f in os.listdir(base):
                    fp = os.path.join(base, f)
                    if os.path.isfile(fp) and fnmatch.fnmatch(f, pattern):
                        results.append(f)
            except FileNotFoundError:
                return ToolResult.fail(call_id, "file_glob", f"目录不存在: {base}")

        return ToolResult.ok(call_id, "file_glob", {
            "pattern": pattern, "base": base,
            "matches": len(results), "results": results,
        })

    async def _handle_write(self, call_id: str, path: str, content: str) -> ToolResult:
        if not path:
            return ToolResult.fail(call_id, "file_write", "需要 path 参数")

        path = self._safe(path)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        existed = os.path.exists(path)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

        return ToolResult.ok(call_id, "file_write", {
            "path": path, "size": len(content),
            "status": "已覆盖" if existed else "已创建",
        })

    async def _handle_append(self, call_id: str, path: str, content: str) -> ToolResult:
        path = self._safe(path)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

        old_size = os.path.getsize(path) if os.path.exists(path) else 0
        need_newline = False
        if old_size > 0:
            with open(path, "rb") as f:
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":
                    need_newline = True

        with open(path, "a", encoding="utf-8") as f:
            if need_newline:
                f.write("\n")
            f.write(content)
            if not content.endswith("\n"):
                f.write("\n")

        return ToolResult.ok(call_id, "file_append", {
            "path": path, "old_size": old_size,
            "new_size": os.path.getsize(path), "status": "已追加",
        })

    async def _handle_rename(self, call_id: str, path: str, new_path: str) -> ToolResult:
        path = self._safe(path)
        new_path = self._safe(new_path)

        if not os.path.exists(path):
            return ToolResult.fail(call_id, "file_rename", f"源文件不存在: {path}")
        if os.path.normpath(path) == os.path.normpath(new_path):
            return ToolResult.fail(call_id, "file_rename", "源路径和目标路径相同")
        if os.path.exists(new_path):
            return ToolResult.fail(call_id, "file_rename", f"目标路径已存在: {new_path}")

        try:
            os.makedirs(os.path.dirname(new_path) or ".", exist_ok=True)
            shutil.move(path, new_path)
            return ToolResult.ok(call_id, "file_rename", {
                "from": path, "to": new_path, "status": "已完成",
            })
        except Exception as e:
            return ToolResult.fail(call_id, "file_rename", str(e))

    async def _handle_diff(self, call_id: str, path_a: str, path_b: str) -> ToolResult:
        if not path_a or not os.path.exists(path_a):
            return ToolResult.fail(call_id, "file_diff", f"文件 A 不存在: {path_a}")
        if not path_b or not os.path.exists(path_b):
            return ToolResult.fail(call_id, "file_diff", f"文件 B 不存在: {path_b}")

        try:
            with open(path_a, "r", encoding="utf-8", errors="replace") as f:
                lines_a = f.readlines()
            with open(path_b, "r", encoding="utf-8", errors="replace") as f:
                lines_b = f.readlines()
        except Exception as e:
            return ToolResult.fail(call_id, "file_diff", str(e))

        diff = "".join(difflib.unified_diff(
            lines_a, lines_b, fromfile=path_a, tofile=path_b, n=3,
        ))

        return ToolResult.ok(call_id, "file_diff", {
            "path_a": path_a, "path_b": path_b,
            "diff": diff, "identical": not diff.strip(),
        })

    @staticmethod
    def _safe(path: str) -> str:
        if not path:
            return path
        try:
            return os.path.abspath(path)
        except (ValueError, OSError):
            return os.path.normpath(os.path.join(os.getcwd(), path))

    @staticmethod
    def _rel(path: str) -> str:
        try:
            return os.path.relpath(path)
        except (ValueError, OSError):
            return path
