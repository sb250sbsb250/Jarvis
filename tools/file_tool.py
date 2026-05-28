"""
tool/file_tool.py — 文件操作工具

从 engine/dag/node.py 迁移：
  - ListFilesNode → ListFilesTool
  - FileProcessorNode → 不再需要 Tool（由 Skill DAG 的 MapNode 调度）
  - FileRenameNode → FileRenameTool
"""

import asyncio
import fnmatch
import os
import shutil
import time
from typing import Any, Dict, List, Optional

from engine.tool.base import BaseTool, ToolParameter
from engine.core.types import ToolResult


class ListFilesTool(BaseTool):
    """列出目录中匹配模式的文件"""

    @property
    def name(self) -> str:
        return "list_files"

    @property
    def description(self) -> str:
        return (
            "列出目录中匹配后缀模式的文件（自动过滤隐藏文件和常见忽略目录）\n"
            "\n"
            "📖 使用示例：\n"
            "  # 列出所有 .py 文件（递归）:\n"
            "  list_files(folder='src', patterns='.py', recursive=True)\n"
            "  # 同时找多种类型:\n"
            "  list_files(folder='.', patterns='.py,.txt,.yaml', max_files=50)\n"
            "  # 只搜顶层（不递归）:\n"
            "  list_files(folder='.', patterns='.py', recursive=False)\n"
            "  💡 返回格式: {files: [{file_name, file_path, folder}], count: N}\n"
        )

    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter("folder", "string", "要搜索的目录路径", required=True, default="."),
            ToolParameter("patterns", "string", "文件后缀模式，逗号分隔，如 .py,.txt", required=True, default=".py"),
            ToolParameter("recursive", "boolean", "是否递归搜索子目录", required=False, default=True),
            ToolParameter("max_files", "number", "最大返回文件数", required=False, default=200),
        ]

    async def execute(self, call_id: str, **kwargs) -> ToolResult:
        folder = kwargs.get("folder", ".")
        patterns_str = kwargs.get("patterns", ".py")
        recursive = kwargs.get("recursive", True)
        max_files = kwargs.get("max_files", 200)

        patterns = [p.strip().lower() for p in patterns_str.split(",")]

        if not os.path.isdir(folder):
            return ToolResult.error(call_id, self.name, f"文件夹不存在: {folder}")

        exclude_dirs = {'.venv', 'venv', '__pycache__', '.git', 'node_modules', '.idea', '.vscode'}
        files = []

        if recursive:
            for root, dirs, fnames in os.walk(folder):
                dirs[:] = [d for d in dirs if d not in exclude_dirs]
                for fname in sorted(fnames):
                    if fname.startswith("_"):
                        continue
                    if any(fname.lower().endswith(p) for p in patterns):
                        files.append({
                            "file_name": fname,
                            "file_path": os.path.join(root, fname),
                            "folder": root,
                        })
                        if len(files) >= max_files:
                            return ToolResult.success(call_id, self.name, {
                                "files": files,
                                "count": len(files),
                                "truncated": True,
                            })
        else:
            for fname in sorted(os.listdir(folder)):
                full = os.path.join(folder, fname)
                if not os.path.isfile(full):
                    continue
                if fname.startswith("_"):
                    continue
                if any(fname.lower().endswith(p) for p in patterns):
                    files.append({
                        "file_name": fname,
                        "file_path": full,
                        "folder": folder,
                    })

        return ToolResult.success(call_id, self.name, {
            "files": files,
            "count": len(files),
        })


class ReadFileTool(BaseTool):
    """读取文件内容（支持分页，自动返回结构化摘要而非原始全文）"""

    # 单次读取最大字符数（超过此数则返回摘要+分页提示）
    MAX_CHARS_DIRECT = 8000
    MAX_LINES_DIRECT = 200

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return (
            "读取文件内容，返回结构化信息（摘要+前若干行）。"
            "支持 offset/limit 分页。对于大文件，首次只返回前 "
            f"{self.MAX_LINES_DIRECT} 行或前 {self.MAX_CHARS_DIRECT} 字符的摘要，"
            "可通过 offset/limit 分页获取后续内容。\n"
            "\n"
            "📖 使用示例：\n"
            "  # 读小文件（直接返回）:\n"
            "  read_file(path='app.py')\n"
            "  # 读大文件，从第50行开始读100行:\n"
            "  read_file(path='app.py', offset=50, limit=100)\n"
            "  # 读大文件前 200 行（摘要模式，看文件结构）:\n"
            "  read_file(path='app.py')\n"
            "  # 强制读全部（慎用，大文件会撑爆上下文）:\n"
            "  read_file(path='app.py', full=True)\n"
            "  💡 offset 不传 = 摘要模式（首 200 行）。先看摘要，再 offset 分页读需要的部分。\n"
        )

    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter("path", "string", "文件路径", required=True),
            ToolParameter("offset", "number", "起始行号（从1开始），不传则自动返回摘要+前若干行", required=False),
            ToolParameter("limit", "number", "最大读取行数", required=False, default=200),
            ToolParameter("full", "boolean", "强制返回完整内容（谨慎使用，会导致消息体积暴增）", required=False, default=False),
        ]

    async def execute(self, call_id: str, **kwargs) -> ToolResult:
        path = kwargs["path"]
        offset = kwargs.get("offset")  # 不传则走摘要模式
        limit = int(kwargs.get("limit", 200))
        full = kwargs.get("full", False)

        if not os.path.exists(path):
            return ToolResult.error(call_id, self.name, f"文件不存在: {path}")

        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()

        total = len(lines)
        total_chars = sum(len(l) for l in lines)

        # ── 分页模式：指定了 offset ──
        if offset is not None:
            start = max(0, int(offset) - 1)
            end = min(total, start + limit)
            content = "".join(lines[start:end])
            has_more = end < total
            return ToolResult.success(call_id, self.name, {
                "path": path,
                "total_lines": total,
                "start_line": int(offset),
                "end_line": end,
                "has_more": has_more,
                "next_offset": end + 1 if has_more else None,
                "content": content,
            })

        # ── 首次读取（无 offset）：智能摘要模式 ──
        preview_lines = min(limit, total)
        preview = "".join(lines[:preview_lines])

        # 检测文件类型
        ext = os.path.splitext(path)[1].lower()
        lang_map = {
            ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript",
            ".java": "Java", ".cpp": "C++", ".c": "C", ".h": "C Header",
            ".html": "HTML", ".css": "CSS", ".json": "JSON",
            ".yaml": "YAML", ".yml": "YAML", ".xml": "XML",
            ".md": "Markdown", ".rst": "reStructuredText",
            ".sql": "SQL", ".sh": "Shell", ".bat": "Batch",
            ".txt": "文本", ".csv": "CSV", ".toml": "TOML",
        }
        lang = lang_map.get(ext, "")

        # 提取结构信息（类/函数定义行）
        structure = []
        for i, line in enumerate(lines[:500], 1):
            stripped = line.strip()
            if any(stripped.startswith(kw) for kw in ["class ", "def ", "async def ",
                                                       "function ", "func ",
                                                       "public ", "private ",
                                                       "@", "# ", "// "]):
                if len(stripped) < 120:
                    structure.append(f"L{i}: {stripped}")
        if len(structure) > 30:
            structure = structure[:30]
            structure.append(f"... 共 {sum(1 for l in lines[:500] if l.strip()[:1] in 'cdfpa@#/')} 处")

        # 判断内容大小
        is_large = total_chars > self.MAX_CHARS_DIRECT or total > 500
        needs_pagination = is_large or full is False

        result = {
            "path": path,
            "total_lines": total,
            "total_chars": total_chars,
            "language": lang,
            "file_type": lang or ext,
            "preview": preview if not is_large else preview[:self.MAX_CHARS_DIRECT],
            "preview_lines": min(preview_lines, total),
        }

        if is_large:
            result.update({
                "is_large": True,
                "has_more": True,
                "next_offset": preview_lines + 1,
                "structure": structure,
                "how_to_read_more": (
                    f"文件较大（{total_chars} 字符，{total} 行）。"
                    f"如需查看后续内容，请调用 read_file offset={preview_lines + 1} limit=200"
                ),
            })
            if full:
                # 用户明确要求全文
                result["content"] = "".join(lines)

        return ToolResult.success(call_id, self.name, result)


class WriteFileTool(BaseTool):
    """写入或修改文件内容（带备份）"""

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return (
            "对指定文件做精确替换写入，自动备份。需提供 old_text 定位要替换的原文。\n"
            "\n"
            "📖 使用示例：\n"
            "  # 替换文件中的某段代码:\n"
            "  write_file(path='app.py', old_text='print(\"old\")', new_text='print(\"new\")')\n"
            "  # 或者用大段替换来修改函数体:\n"
            "  write_file(path='app.py', old_text='def old_func():\\n    pass', new_text='def new_func():\\n    return 42')\n"
            "  💡 old_text 必须是文件中唯一匹配的文本（不能有2处相同的原文）。\n"
            "  💡 如果文件是新创建的，先用 write_file + 整个文件内容作 old_text 是不行的——"
            "因为文件不存在。创建新文件：先写空内容或用 edit_tool 的 insert 模式。\n"
        )

    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter("path", "string", "文件路径", required=True),
            ToolParameter("old_text", "string", "要替换的原文（必须是唯一的精确匹配）", required=True),
            ToolParameter("new_text", "string", "替换后的新文本", required=True),
            ToolParameter("create_backup", "boolean", "是否创建 .bak 备份", required=False, default=True),
        ]

    async def execute(self, call_id: str, **kwargs) -> ToolResult:
        path = kwargs["path"]
        old_text = kwargs["old_text"]
        new_text = kwargs.get("new_text", "")
        create_backup = kwargs.get("create_backup", True)

        if not os.path.exists(path):
            # 文件不存在，直接创建
            os.makedirs(os.path.dirname(os.path.abspath(path)) or '.', exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_text)
            return ToolResult.success(call_id, self.name, {"path": path, "created": True, "size": len(new_text)})

        with open(path, "r", encoding="utf-8") as f:
            content = f.read()

        if not content and old_text:
            # 文件为空但 old_text 不为空，直接写入
            os.makedirs(os.path.dirname(os.path.abspath(path)) or '.', exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_text)
            return ToolResult.success(call_id, self.name, {"path": path, "created": True, "size": len(new_text)})

        count = content.count(old_text)
        if count == 0:
            return ToolResult.error(call_id, self.name, "未找到匹配文本")
        if count > 1:
            return ToolResult.error(call_id, self.name, f"匹配到 {count} 处，请提供更精确的定位")

        bak = None
        if create_backup:
            bak = path + ".bak"
            shutil.copy2(path, bak)

        new_content = content.replace(old_text, new_text, 1)
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)

        return ToolResult.success(call_id, self.name, {
            "path": path,
            "edit_id": hash(path + old_text) & 0xFFFFFFFF,
            "backup": bak,
        })


class AppendFileTool(BaseTool):
    """追加内容到文件末尾"""

    @property
    def name(self) -> str:
        return "append_file"

    @property
    def description(self) -> str:
        return (
            "追加内容到指定文件末尾（如果文件不存在则创建）\n"
            "\n"
            "📖 使用示例：\n"
            "  # 在文件末尾加一行:\n"
            "  append_file(path='log.txt', content='新记录: 完成')\n"
            "  # 创建新文件并写入首行:\n"
            "  append_file(path='new.py', content='# 新文件')\n"
            "  💡 如果要追加多行，在 content 里用 \\n 分隔。\n"
        )

    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter("path", "string", "文件路径", required=True),
            ToolParameter("content", "string", "要追加的内容", required=True),
        ]

    async def execute(self, call_id: str, **kwargs) -> ToolResult:
        path = kwargs["path"]
        content = kwargs["content"]

        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(content)
            if not content.endswith("\n"):
                f.write("\n")

        return ToolResult.success(call_id, self.name, {
            "path": path,
            "appended": True,
        })


class FileRenameTool(BaseTool):
    """批量重命名文件（支持试运行模式）"""

    @property
    def name(self) -> str:
        return "rename_files"

    @property
    def description(self) -> str:
        return (
            "批量重命名文件。支持试运行模式（dry_run=true 仅预览不执行）。\n"
            "\n"
            "📖 使用示例：\n"
            "  # 先试运行看看效果:\n"
            "  rename_files(file_mappings='[{\"from\":\"old.py\",\"to\":\"new.py\"}]', dry_run=True)\n"
            "  # 实际执行:\n"
            "  rename_files(file_mappings='[{\"from\":\"old.py\",\"to\":\"new.py\"}]')\n"
            "  💡 先 dry_run=True 预览，确认没问题再执行。\n"
        )

    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter("renames", "array", "重命名列表，每项为 {old_name, new_name} 或 {file_path, new_name}", required=True),
            ToolParameter("folder", "string", "文件所在文件夹", required=True),
            ToolParameter("dry_run", "boolean", "试运行模式（仅预览不执行）", required=False, default=False),
        ]

    async def execute(self, call_id: str, **kwargs) -> ToolResult:
        renames = kwargs.get("renames", [])
        folder = kwargs.get("folder", ".")
        dry_run = kwargs.get("dry_run", False)

        if not isinstance(renames, list):
            return ToolResult.error(call_id, self.name, "重命名列表必须是数组")

        renamed = []
        failed = []

        for item in renames:
            if not isinstance(item, dict):
                failed.append("每项必须是对象")
                continue

            old_name = item.get("old_name", item.get("file_name", ""))
            new_name = item.get("new_name", f"renamed_{old_name}")

            if not old_name:
                failed.append("缺少 old_name")
                continue

            old_path = item.get("file_path", os.path.join(folder, old_name))
            new_path = os.path.join(folder, new_name)

            if dry_run:
                renamed.append(f"[DRY RUN] {old_name} → {new_name}")
            else:
                if not os.path.exists(old_path):
                    failed.append(f"文件不存在: {old_path}")
                    continue
                try:
                    os.rename(old_path, new_path)
                    renamed.append(f"{old_name} → {new_name}")
                except Exception as e:
                    failed.append(f"{old_name}: {str(e)[:100]}")

        return ToolResult.success(call_id, self.name, {
            "renamed": renamed,
            "failed": failed,
            "dry_run": dry_run,
        })


class DiffFileTool(BaseTool):
    """预览文件修改差异（不实际写入）"""

    @property
    def name(self) -> str:
        return "diff_file"

    @property
    def description(self) -> str:
        return (
            "预览对文件的修改效果（dryrun），返回 unified diff，不实际写入\n"
            "\n"
            "📖 使用示例：\n"
            "  # 预览替换效果（不改文件）:\n"
            "  diff_file(path='app.py', old_text='旧代码', new_text='新代码')\n"
            "  💡 确认 diff 没问题后再用 write_file 实际写入。\n"
        )

    retryable_exceptions = (FileNotFoundError, PermissionError, OSError)
    max_retries = 1

    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter("path", "string", "文件路径", required=True),
            ToolParameter("old_text", "string", "要替换的原文", required=True),
            ToolParameter("new_text", "string", "替换后的新文本", required=True),
        ]

    async def execute(self, call_id: str, **kwargs) -> ToolResult:
        import difflib

        path = kwargs["path"]
        old_text = kwargs["old_text"]
        new_text = kwargs["new_text"]

        if not os.path.exists(path):
            return ToolResult.error(call_id, self.name, f"文件不存在: {path}")

        with open(path, "r", encoding="utf-8") as f:
            content = f.read()

        if old_text not in content:
            return ToolResult.error(call_id, self.name, "未找到匹配文本")

        new_content = content.replace(old_text, new_text, 1)
        old_lines = content.splitlines(keepends=True)
        new_lines = new_content.splitlines(keepends=True)
        diff = "".join(difflib.unified_diff(
            old_lines, new_lines,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
        ))

        return ToolResult.success(call_id, self.name, {
            "diff": diff,
            "path": path,
            "match_count": content.count(old_text),
        })


class ReadImageTool(BaseTool):
    """读取图片文件，返回 base64（供 LLM 视觉分析）"""

    @property
    def name(self) -> str:
        return "read_image"

    @property
    def description(self) -> str:
        return (
            "读取图片文件，返回图片信息（尺寸、格式、base64 编码），供 LLM 视觉分析\n"
            "\n"
            "📖 使用示例：\n"
            "  read_image(path='screenshot.png')\n"
            "  💡 返回 base64 + 图片元信息。配合 vision 模型看图。\n"
        )

    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter("path", "string", "图片文件路径", required=True),
        ]

    async def execute(self, call_id: str, **kwargs) -> ToolResult:
        import base64

        path = kwargs.get("path", "")
        if not path or not os.path.exists(path):
            return ToolResult.error(call_id, self.name, f"文件不存在: {path}")

        ext = os.path.splitext(path)[1].lower()
        if ext not in (".png", ".jpg", ".jpeg", ".bmp", ".webp"):
            return ToolResult.error(call_id, self.name, f"不支持的图片格式: {ext}")

        try:
            from PIL import Image
            img = Image.open(path)
            size = os.path.getsize(path)

            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("utf-8")

            mime_types = {".png": "image/png", ".jpg": "image/jpeg",
                          ".jpeg": "image/jpeg", ".bmp": "image/bmp",
                          ".webp": "image/webp"}

            return ToolResult.success(call_id, self.name, {
                "path": path,
                "width": img.width,
                "height": img.height,
                "format": img.format,
                "size_bytes": size,
                "base64": b64,
                "mime_type": mime_types.get(ext, "image/png"),
            })
        except ImportError:
            return ToolResult.error(call_id, self.name, "需要 Pillow: pip install Pillow")
        except Exception as e:
            return ToolResult.error(call_id, self.name, str(e))


class ReadPdfTool(BaseTool):
    """读取 PDF 文件，提取文本内容"""

    @property
    def name(self) -> str:
        return "read_pdf"

    @property
    def description(self) -> str:
        return (
            "读取 PDF 文件，提取文本内容，返回每页文本\n"
            "\n"
            "📖 使用示例：\n"
            "  # 读全部页:\n"
            "  read_pdf(path='report.pdf')\n"
            "  # 指定页码范围:\n"
            "  read_pdf(path='report.pdf', start_page=1, max_pages=3)\n"
            "  💡 返回 {total_pages: N, pages: [{page: 1, content: ...}]}\n"
        )

    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter("path", "string", "PDF 文件路径", required=True),
            ToolParameter("page_limit", "number", "最多读取页数", required=False, default=10),
        ]

    async def execute(self, call_id: str, **kwargs) -> ToolResult:
        path = kwargs.get("path", "")
        page_limit = int(kwargs.get("page_limit", 10))

        if not path or not os.path.exists(path):
            return ToolResult.error(call_id, self.name, f"文件不存在: {path}")

        try:
            import PyPDF2
            pages = []
            with open(path, "rb") as f:
                reader = PyPDF2.PdfReader(f)
                total = len(reader.pages)
                limit = min(page_limit, total)
                for i in range(limit):
                    text = reader.pages[i].extract_text()
                    pages.append({"page": i + 1, "text": text[:3000]})

            return ToolResult.success(call_id, self.name, {
                "path": path,
                "total_pages": total,
                "extracted_pages": len(pages),
                "pages": pages,
            })
        except ImportError:
            # 降级：用 pdftotext
            import subprocess
            try:
                result = subprocess.run(
                    ["pdftotext", "-l", str(page_limit), path, "-"],
                    capture_output=True, text=True, timeout=15
                )
                return ToolResult.success(call_id, self.name, {
                    "path": path,
                    "text": result.stdout[:10000],
                })
            except Exception:
                return ToolResult.error(call_id, self.name, "需要 PyPDF2: pip install PyPDF2")
        except Exception as e:
            return ToolResult.error(call_id, self.name, str(e))
