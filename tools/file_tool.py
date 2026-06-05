"""
tools/file_tool.py — 文件操作工具（合并版）

一个工具覆盖所有文件操作：
  list / read / write / append / rename / diff
"""

import os
import difflib
import shutil
import fnmatch
import logging
from typing import List

from engine.tool.base import BaseTool, ToolParameter, ToolResult

logger = logging.getLogger(__name__)


class FileTool(BaseTool):
    """文件操作 — list/read/write/append/rename/diff"""

    # 合并工具：支持读写，按执行为准
    is_read = True
    is_write = True

    @property
    def name(self) -> str:
        return "file"

    @property
    def description(self) -> str:
        return (
            "文件操作。action: list/read/write/append/rename/diff/glob\n"
            "- list: pattern='*.py', recursive=True\n"
            "- read: path='a.txt', start=1, end=50\n"
            "- write: path='a.txt', content='内容' (覆盖)\n"
            "- append: path='a.txt', content='追加内容'\n"
            "- rename: path='old.txt', new_path='new.txt'\n"
            "- diff: path_a='v1.py', path_b='v2.py'\n"
            "- glob: pattern='**/*.py' (文件名匹配，返回路径列表)\n"
            "安全流程: list → read → diff → write"
        )

    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter("action", "string", "list/read/write/append/rename/diff", required=True,
                          enum=["list", "read", "write", "append", "rename", "diff", "glob"]),
            ToolParameter("path", "string", "文件路径", required=False),
            ToolParameter("new_path", "string", "新路径(rename用)", required=False),
            ToolParameter("content", "string", "文件内容(write/append用)", required=False),
            ToolParameter("pattern", "string", "文件匹配模式(list用)，默认*", required=False),
            ToolParameter("recursive", "boolean", "是否递归(list用)，默认true", required=False),
            ToolParameter("start_line", "number", "起始行(read用)，默认1", required=False),
            ToolParameter("end_line", "number", "结束行(read用)，默认全部", required=False),
            ToolParameter("path_a", "string", "文件A(diff用)", required=False),
            ToolParameter("path_b", "string", "文件B(diff用)", required=False),
        ]

    async def execute(self, call_id: str, **kwargs) -> ToolResult:
        action = kwargs.get("action", "list")

        if action == "list":
            return await self._list(call_id, kwargs)
        elif action == "read":
            return await self._read(call_id, kwargs)
        elif action == "write":
            return await self._write(call_id, kwargs)
        elif action == "append":
            return await self._append(call_id, kwargs)
        elif action == "rename":
            return await self._rename(call_id, kwargs)
        elif action == "diff":
            return await self._diff(call_id, kwargs)
        elif action == "glob":
            return await self._glob(call_id, kwargs)
        else:
            return ToolResult.error(call_id, self.name, f"未知操作: {action}")

    # ── list ──

    async def _list(self, call_id, args):
        pattern = args.get("pattern", "*")
        recursive = args.get("recursive", True)
        # 如果 pattern 不带 *，加 * 变成通配
        if "*" not in pattern and "?" not in pattern:
            pattern = f"*{pattern}"

        exclude = {'.git', '__pycache__', 'node_modules', '.venv', 'venv', '.idea', '.vscode', 'dist', 'build', '.egg-info'}
        results = []
        root = "."

        for r, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in exclude and not d.startswith(".")]
            for f in files:
                if fnmatch.fnmatch(f, pattern):
                    fpath = os.path.join(r, f)
                    try:
                        stat = os.stat(fpath)
                        results.append({
                            "name": f,
                            "path": self._safe_relpath(fpath),
                            "size": stat.st_size,
                            "modified": stat.st_mtime,
                        })
                    except Exception:
                        results.append({"name": f, "path": self._safe_relpath(fpath)})
            if not recursive:
                break

        results.sort(key=lambda x: x["path"])
        results = results[:100]  # 最多 100 条

        if not results:
            return ToolResult.success(call_id, self.name, {
                "pattern": pattern,
                "files": [],
                "_hint": f"未匹配到 '{pattern}'。尝试: 1) 不加 pattern 列出全部 2) 使用通配如 *.py 3) 确认目录结构",
            })

        return ToolResult.success(call_id, self.name, {
            "pattern": pattern,
            "count": len(results),
            "files": results,
            "_hint": f"找到 {len(results)} 个文件。用 file(action='read', path='文件名') 查看内容",
        })

    # ── read ──

    async def _read(self, call_id, args):
        path = self._safe_path(args.get("path", ""))
        if not path or not os.path.exists(path):
            return ToolResult.error(call_id, self.name, f"文件不存在: {path}。用 file(action='list') 先确认路径")

        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except Exception as e:
            return ToolResult.error(call_id, self.name, f"读取失败: {e}")

        total = len(lines)
        start = max(0, int(args.get("start_line", 1)) - 1)
        end = min(total, int(args.get("end_line", total)) or total)

        content = "".join(lines[start:end])
        is_large = total > 200

        for_edit_hint = (
            "如需修改，用 file(action='write', path=..., content=...) 覆盖，"
            "或先用 diff 预览变更"
        )

        result = {
            "path": path,
            "total_lines": total,
            "lines": f"{start+1}-{end}",
            "content": content,
        }

        if total > end and not is_large:
            result["_hint"] = f"文件共 {total} 行，以上是第 {start+1}-{end} 行。{for_edit_hint}"
        elif is_large:
            result["_hint"] = f"文件较大（{total} 行），仅显示第 {start+1}-{end} 行。用 start_line/end_line 翻页"
        else:
            result["_hint"] = for_edit_hint

        return ToolResult.success(call_id, self.name, result)

    # ── write ──

    async def _write(self, call_id, args):
        path = args.get("path", "")
        content = args.get("content", "")

        if not path:
            return ToolResult.error(call_id, self.name, "write 需要 path 和 content")

        # 安全路径解析（Windows 跨盘兼容）
        path = self._safe_path(path)
        dirname = os.path.dirname(path)
        if dirname:
            os.makedirs(dirname, exist_ok=True)

        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

        size = os.path.getsize(path)
        return ToolResult.success(call_id, self.name, {
            "path": path,
            "size": size,
            "status": "已写入",
            "_hint": "如需追加内容，用 file(action='append', ...)",
        })

    # ── append ──

    async def _append(self, call_id, args):
        path = args.get("path", "")
        content = args.get("content", "")

        if not path:
            return ToolResult.error(call_id, self.name, "append 需要 path 和 content")

        old_size = os.path.getsize(path) if os.path.exists(path) else 0
        with open(path, "a", encoding="utf-8") as f:
            f.write(content)

        return ToolResult.success(call_id, self.name, {
            "path": path,
            "old_size": old_size,
            "new_size": os.path.getsize(path),
            "_hint": "追加成功",
        })

    def _safe_path(self, path):
        """安全解析路径（Windows 跨盘兼容）"""
        try:
            return os.path.abspath(path)
        except (ValueError, OSError):
            return os.path.normpath(os.path.join(os.getcwd(), path))

    def _safe_relpath(self, path):
        """安全相对路径（Windows 跨盘兼容）"""
        try:
            return os.path.relpath(path)
        except (ValueError, OSError):
            return path

    async def _rename(self, call_id, args):
        path = args.get("path", "")
        new_path = args.get("new_path", "")

        if not path or not os.path.exists(path):
            return ToolResult.error(call_id, self.name, f"源文件不存在: {path}")
        if not new_path:
            return ToolResult.error(call_id, self.name, "rename 需要 new_path")

        try:
            os.makedirs(os.path.dirname(os.path.abspath(new_path)) or ".", exist_ok=True)
            shutil.move(path, new_path)
            return ToolResult.success(call_id, self.name, {
                "from": path,
                "to": new_path,
                "_hint": f"{path} → {new_path} 已完成",
            })
        except Exception as e:
            return ToolResult.error(call_id, self.name, f"重命名失败: {e}")

    # ── diff ──

    async def _diff(self, call_id, args):
        path_a = args.get("path_a", "")
        path_b = args.get("path_b", "")

        if not path_a or not os.path.exists(path_a):
            return ToolResult.error(call_id, self.name, f"文件A不存在: {path_a}")
        if not path_b or not os.path.exists(path_b):
            return ToolResult.error(call_id, self.name, f"文件B不存在: {path_b}")

        with open(path_a, "r", encoding="utf-8", errors="replace") as f:
            lines_a = f.readlines()
        with open(path_b, "r", encoding="utf-8", errors="replace") as f:
            lines_b = f.readlines()

        diff = difflib.unified_diff(
            lines_a, lines_b,
            fromfile=path_a, tofile=path_b,
        )
        diff_text = "".join(diff)

        return ToolResult.success(call_id, self.name, {
            "path_a": path_a,
            "path_b": path_b,
            "diff": diff_text,
            "size": len(diff_text),
            "_hint": "减号(-): 删除的行，加号(+): 新增的行",
        })

    # ── glob ──

    async def _glob(self, call_id, args):
        """文件名通配匹配，返回路径列表"""
        pattern = args.get("pattern", "*")
        path = args.get("path", ".")

        results = []
        base = path if os.path.isdir(path) else os.path.dirname(path) or "."

        if "**" in pattern:
            for root, dirs, files in os.walk(base):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for f in files:
                    fp = os.path.join(root, f)
                    rel = os.path.relpath(fp, base)
                    if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(fp, pattern):
                        results.append(rel)
                        if len(results) >= 200:
                            break
                if len(results) >= 200:
                    break
        else:
            for f in os.listdir(base):
                fp = os.path.join(base, f)
                if os.path.isfile(fp) and fnmatch.fnmatch(f, pattern):
                    results.append(f)

        return ToolResult.success(call_id, self.name, {
            "pattern": pattern,
            "base": base,
            "matches": len(results),
            "results": results,
            "_hint": f"找到 {len(results)} 个匹配文件",
        })
