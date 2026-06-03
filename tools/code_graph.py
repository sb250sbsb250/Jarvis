"""
tools/code_graph.py — 代码理解工具（Code Graph）

受 Claude Code Graph 启发：项目级代码知识图谱。
只读工具，覆盖搜索、阅读、分析、质量检查。

actions:
  search   — 关键字搜索代码
  read     — 读取文件（含结构索引）
  grep     — 正则搜索（支持 glob）
  analyze  — AST 分析（类/函数/import，单文件）
  symbol   — 符号级分析（定义 + 全项目引用）
  deps     — 模块依赖图（跨文件 import 关系）
  quality  — 代码质量评分
  style    — 项目代码规范检测
"""

import os
import re
import ast
import difflib
import fnmatch
import logging
from collections import defaultdict, Counter
from typing import Any, Dict, List, Optional, Set, Tuple

from engine.tool.base import BaseTool, ToolParameter, ToolResult

logger = logging.getLogger(__name__)

_EXCLUDE_DIRS = {'.venv', 'venv', '__pycache__', '.git', 'node_modules',
                 '.idea', '.vscode', 'dist', 'build', '.pytest_cache',
                 '.mypy_cache', '.ruff_cache', '__pycache__'}


# ─── helpers ───────────────────────────────────────────────

def _collect_py_files(root_dir: str = ".", max_files: int = 200) -> List[str]:
    """收集项目 .py 文件"""
    files = []
    for root, dirs, fnames in os.walk(root_dir):
        dirs[:] = [d for d in dirs if d not in _EXCLUDE_DIRS and not d.startswith(".")]
        for f in fnames:
            if f.endswith(".py"):
                files.append(os.path.join(root, f))
                if len(files) >= max_files:
                    return files
    return files


def _parse_file_safe(fp: str) -> Tuple[Optional[ast.AST], Optional[str]]:
    """安全解析 Python 文件，成功返回 (tree, source), 失败返回 (None, error)"""
    try:
        with open(fp, "r", encoding="utf-8", errors="ignore") as f:
            source = f.read()
        tree = ast.parse(source)
        return tree, source
    except SyntaxError as e:
        return None, f"语法错误: {e}"
    except Exception as e:
        return None, str(e)


def _extract_structure(tree: ast.AST, source: str) -> Dict[str, Any]:
    """从 AST 提取文件结构"""
    classes = []
    functions = []
    imports = []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            methods = []
            for m in node.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods.append({
                        "name": m.name,
                        "line": m.lineno,
                        "async": isinstance(m, ast.AsyncFunctionDef),
                        "decorators": [
                            ast.unparse(d) if hasattr(ast, "unparse") else "?"
                            for d in m.decorator_list
                        ],
                    })
            classes.append({
                "name": node.name,
                "line": node.lineno,
                "methods": len(methods),
                "method_list": methods[:10],
                "bases": [
                    ast.unparse(b) if hasattr(ast, "unparse") else "?"
                    for b in node.bases
                ],
            })
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append({
                "name": node.name,
                "line": node.lineno,
                "async": isinstance(node, ast.AsyncFunctionDef),
                "decorators": [
                    ast.unparse(d) if hasattr(ast, "unparse") else "?"
                    for d in node.decorator_list
                ],
            })
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports.append({"name": alias.name, "as": alias.asname, "line": node.lineno})
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imports.append({
                    "name": f"{node.module}.{alias.name}" if node.module else alias.name,
                    "module": node.module,
                    "symbol": alias.name,
                    "as": alias.asname,
                    "line": node.lineno,
                    "level": node.level,
                })

    return {
        "classes": classes,
        "functions": functions,
        "imports": imports,
    }


def _extract_call_graph(tree: ast.AST, source: str, file_path: str) -> Dict:
    """构建单文件调用图"""
    source_lines = source.split("\n")
    calls = []
    current_func = None
    current_class = None

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            current_class = node.name
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            current_func = node.name
        elif isinstance(node, ast.Call) and current_func:
            callee = None
            if isinstance(node.func, ast.Name):
                callee = node.func.id
            elif isinstance(node.func, ast.Attribute):
                if isinstance(node.func.value, ast.Name):
                    callee = f"{node.func.value.id}.{node.func.attr}"
                else:
                    callee = f"?.{node.func.attr}"
            if callee:
                qualified = f"{current_class}.{current_func}" if current_class else current_func
                line_no = node.lineno
                ctx = source_lines[line_no - 1].strip()[:100] if line_no <= len(source_lines) else ""
                calls.append({
                    "caller": qualified,
                    "callee": callee,
                    "line": line_no,
                    "context": ctx,
                })

    # 按调用者分组
    by_caller = defaultdict(list)
    for c in calls:
        by_caller[c["caller"]].append({"callee": c["callee"], "line": c["line"], "context": c["context"]})

    # 被调频次统计
    callee_count = Counter(c["callee"] for c in calls)
    hot = [{"name": k, "called_by": v} for k, v in callee_count.most_common(15)]

    # 外部调用 vs 内部调用
    internal_names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            internal_names.add(node.name)

    external_calls = [
        {"caller": c["caller"], "callee": c["callee"], "line": c["line"]}
        for c in calls if c["callee"] not in internal_names
        and not c["callee"].startswith("?.")
    ]

    return {
        "total_calls": len(calls),
        "by_caller": {k: v[:10] for k, v in list(by_caller.items())[:20]},
        "hot_functions": hot,
        "external_calls": external_calls[:15],
    }


# ─── CodeGraphTool ────────────────────────────────────────

class CodeGraphTool(BaseTool):
    """代码理解工具 — 搜索 + 阅读 + 分析 + 调用图 + 依赖"""

    def __init__(self, base_dir: str = ".", **kwargs):
        self.base_dir = base_dir

    @property
    def name(self) -> str:
        return "code_graph"

    @property
    def description(self) -> str:
        return (
            "代码理解工具（只读）。actions:\n"
            "- search:   关键字搜索 (keyword, file_pattern, context_lines)\n"
            "- read:     读取文件，自动提取结构 (path, start_line, end_line)\n"
            "- grep:     正则搜索 (pattern, glob)\n"
            "- analyze:  AST 结构分析 + 调用图 (path, focus: all/calls/deps)\n"
            "- symbol:   符号定义+全项目引用 (symbol)\n"
            "- deps:     模块依赖关系图 (path 目录)\n"
            "- quality:  代码质量评分 (path, threshold)\n"
            "- style:    项目代码规范检测 (path 目录)\n"
        )

    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter("action", "string", "操作", required=True,
                          enum=["search", "read", "grep", "analyze", "symbol", "deps", "quality", "style"]),
            ToolParameter("path", "string", "文件路径或目录", required=False),
            ToolParameter("keyword", "string", "搜索关键字 (search)", required=False),
            ToolParameter("pattern", "string", "正则/文件匹配模式 (grep/search)", required=False),
            ToolParameter("glob", "string", "文件 glob (grep, 默认 **/*.py)", required=False),
            ToolParameter("symbol", "string", "符号名 (symbol)", required=False),
            ToolParameter("focus", "string", "分析焦点: all/calls/deps (analyze)", required=False,
                          enum=["all", "calls", "deps"]),
            ToolParameter("start_line", "number", "起始行 (read)", required=False),
            ToolParameter("end_line", "number", "结束行 (read)", required=False),
            ToolParameter("context_lines", "number", "上下文行数 (search, 默认2)", required=False),
            ToolParameter("threshold", "number", "质量阈值 (quality, 默认70)", required=False),
        ]

    async def execute(self, call_id: str, **kwargs) -> ToolResult:
        action = kwargs.get("action", "search")

        handlers = {
            "search":  self._search,
            "read":    self._read,
            "grep":    self._grep,
            "analyze": self._analyze,
            "symbol":  self._symbol,
            "deps":    self._deps,
            "quality": self._quality,
            "style":   self._style,
        }
        handler = handlers.get(action)
        if not handler:
            return ToolResult.error(call_id, self.name, f"未知操作: {action}")
        return await handler(call_id, kwargs)

    # ── search ────────────────────────────────────────────

    async def _search(self, call_id, args):
        keyword = args.get("keyword", "")
        pattern = args.get("pattern", "*.py")
        context_lines = int(args.get("context_lines", 2))

        if not keyword:
            return ToolResult.error(call_id, self.name, "search 需要 keyword")

        results = []
        for root, dirs, files in os.walk(self.base_dir):
            dirs[:] = [d for d in dirs if d not in _EXCLUDE_DIRS and not d.startswith(".")]
            for f in files:
                if not fnmatch.fnmatch(f, pattern):
                    continue
                fp = os.path.join(root, f)
                try:
                    with open(fp, "r", encoding="utf-8", errors="ignore") as fh:
                        lines = fh.readlines()
                    for i, line in enumerate(lines):
                        if keyword in line:
                            ctx_s = max(0, i - context_lines)
                            ctx_e = min(len(lines), i + context_lines + 1)
                            results.append({
                                "file": os.path.relpath(fp, self.base_dir),
                                "line": i + 1,
                                "match": line.strip()[:150],
                                "context": "".join(
                                    f"  L{j+1}: {lines[j].rstrip()}\n"
                                    for j in range(ctx_s, ctx_e)
                                ),
                            })
                            if len(results) >= 20:
                                break
                except Exception:
                    pass
                if len(results) >= 20:
                    break
            if len(results) >= 20:
                break

        return ToolResult.success(call_id, self.name, {
            "keyword": keyword,
            "matches": len(results),
            "results": results,
            "_hint": (
                f"找到 {len(results)} 处匹配。" if results
                else f"未找到 '{keyword}'。试换关键词或扩大 pattern"
            ),
        })

    # ── read ──────────────────────────────────────────────

    async def _read(self, call_id, args):
        path = args.get("path", "")
        if not path or not os.path.isfile(path):
            return ToolResult.error(call_id, self.name, f"文件不存在: {path}")

        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        total = len(lines)
        start = max(0, int(args.get("start_line", 1)) - 1)
        end = min(total, int(args.get("end_line", total)))
        content = "".join(lines[start:end])

        # 提取结构索引
        structure = []
        for i, line in enumerate(lines[:min(500, total)]):
            stripped = line.strip()
            if any(stripped.startswith(kw) for kw in
                   ["class ", "def ", "async def ", "import ", "from ", "@"]):
                if len(stripped) < 120:
                    structure.append(f"L{i+1}: {stripped}")

        result = {
            "path": path,
            "total_lines": total,
            "lines": f"{start+1}-{end}",
            "content": content,
        }
        if total > 200:
            result["structure"] = structure[:30]
            result["_hint"] = f"文件共 {total} 行，用 start_line/end_line 翻页"

        return ToolResult.success(call_id, self.name, result)

    # ── grep ──────────────────────────────────────────────

    async def _grep(self, call_id, args):
        pattern_str = args.get("pattern", "")
        glob_pattern = args.get("glob", "**/*.py")

        if not pattern_str:
            return ToolResult.error(call_id, self.name, "grep 需要 pattern")

        try:
            regex = re.compile(pattern_str)
        except re.error as e:
            return ToolResult.error(call_id, self.name, f"正则无效: {e}")

        results = []
        for root, dirs, files in os.walk(self.base_dir):
            dirs[:] = [d for d in dirs if d not in _EXCLUDE_DIRS and not d.startswith(".")]
            for f in files:
                if not fnmatch.fnmatch(f, glob_pattern):
                    continue
                fp = os.path.join(root, f)
                try:
                    with open(fp, "r", encoding="utf-8", errors="ignore") as fh:
                        lines = fh.readlines()
                    for i, line in enumerate(lines):
                        m = regex.search(line)
                        if m:
                            ctx_s = max(0, i - 1)
                            ctx_e = min(len(lines), i + 2)
                            results.append({
                                "file": os.path.relpath(fp, self.base_dir),
                                "line": i + 1,
                                "match": line.strip()[:200],
                                "groups": list(m.groups()) if m.groups() else [],
                                "context": "".join(
                                    f"{'>' if j == i else ' '} L{j+1}: {lines[j].rstrip()}\n"
                                    for j in range(ctx_s, ctx_e)
                                ),
                            })
                            if len(results) >= 25:
                                break
                except Exception:
                    pass
                if len(results) >= 25:
                    break
            if len(results) >= 25:
                break

        return ToolResult.success(call_id, self.name, {
            "pattern": pattern_str,
            "matches": len(results),
            "results": results[:25],
            "_hint": f"找到 {len(results)} 处匹配。用 code_graph(action='read') 查看具体文件",
        })

    # ── analyze ───────────────────────────────────────────

    async def _analyze(self, call_id, args):
        path = args.get("path", ".")
        focus = args.get("focus", "all")

        if os.path.isfile(path) and path.endswith(".py"):
            files = [path]
        elif os.path.isdir(path):
            files = _collect_py_files(path, max_files=30)
        else:
            return ToolResult.error(call_id, self.name, f"不支持的路径: {path}")

        results = {}
        for fp in files:
            rel = os.path.relpath(fp)
            tree, source_or_err = _parse_file_safe(fp)
            if tree is None:
                results[rel] = {"error": source_or_err}
                continue

            struct = _extract_structure(tree, source_or_err)

            if focus == "calls":
                call_graph = _extract_call_graph(tree, source_or_err, rel)
                results[rel] = {"call_graph": call_graph}
            elif focus == "deps":
                results[rel] = {"imports": struct["imports"][:20]}
            else:  # all
                line_count = len(source_or_err.split("\n"))
                results[rel] = {
                    "lines": line_count,
                    "classes": struct["classes"][:10],
                    "functions": struct["functions"][:20],
                    "imports": struct["imports"][:20],
                }

        return ToolResult.success(call_id, self.name, {
            "path": path,
            "files": len(results),
            "focus": focus,
            "results": results,
            "_hint": (
                f"分析了 {len(results)} 个文件。"
                f"用 focus='calls' 看调用图，focus='deps' 看依赖"
            ),
        })

    # ── symbol ────────────────────────────────────────────

    async def _symbol(self, call_id, args):
        symbol = args.get("symbol", "")
        if not symbol:
            return ToolResult.error(call_id, self.name, "symbol 需要 symbol 参数")

        definitions = []
        references = []
        py_files = _collect_py_files(self.base_dir, max_files=100)

        for fp in py_files:
            rel = os.path.relpath(fp, self.base_dir)
            try:
                with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                    source = f.read()
                tree = ast.parse(source)
                for node in ast.walk(tree):
                    # 定义
                    if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
                        if node.name == symbol:
                            kind = ("class" if isinstance(node, ast.ClassDef)
                                    else "async def" if isinstance(node, ast.AsyncFunctionDef)
                                    else "function")
                            definitions.append({
                                "file": rel, "line": node.lineno, "kind": kind,
                                "doc": ast.get_docstring(node)[:200] if ast.get_docstring(node) else None,
                            })
                    # 引用
                    if isinstance(node, ast.Name) and node.id == symbol:
                        if not isinstance(node.ctx, ast.Store):  # 排除赋值目标
                            references.append({
                                "file": rel, "line": node.lineno,
                                "context": source.split("\n")[node.lineno - 1].strip()[:120],
                            })
            except Exception:
                continue

        return ToolResult.success(call_id, self.name, {
            "symbol": symbol,
            "definitions": definitions,
            "references": references[:50],
            "_hint": (
                f"找到 {len(definitions)} 处定义, {len(references)} 处引用"
            ),
        })

    # ── deps ──────────────────────────────────────────────

    async def _deps(self, call_id, args):
        path = args.get("path", ".")
        if not os.path.isdir(path):
            path = os.path.dirname(path) or "."

        py_files = _collect_py_files(path, max_files=100)

        # 模块名 → 文件路径
        module_to_file: Dict[str, str] = {}
        # 文件路径 → import 的模块列表
        file_imports: Dict[str, List[Dict]] = {}
        # 文件路径 → 本地模块 (当前项目内的)
        file_local_imports: Dict[str, List[str]] = {}

        for fp in py_files:
            rel = os.path.relpath(fp, path)
            # 推断模块名: a/b/c.py → a.b.c
            mod = rel.replace(os.sep, ".").replace(".py", "")
            if mod.startswith("."):
                mod = mod[1:]
            module_to_file[mod] = rel

            tree, _ = _parse_file_safe(fp)
            if tree is None:
                continue

            struct = _extract_structure(tree, "")
            imports = struct["imports"]
            file_imports[rel] = imports

            local = []
            for imp in imports:
                name = imp.get("name", "")
                mod_name = imp.get("module", name)
                if mod_name:
                    # 检查是否是本地模块
                    parts = mod_name.split(".")
                    if parts[0] in module_to_file or any(
                        m.startswith(mod_name) for m in module_to_file
                    ):
                        local.append(mod_name)
            file_local_imports[rel] = local

        # 检测循环依赖
        cycles = []
        adjacency = defaultdict(set)
        for f, imports in file_local_imports.items():
            for mod in imports:
                for m2f, file_path in module_to_file.items():
                    if m2f == mod or m2f.startswith(mod + "."):
                        adjacency[f].add(file_path)

        # 简单 DFS 检测循环
        visited = set()
        stack = set()

        def dfs(node, path_stack):
            visited.add(node)
            stack.add(node)
            path_stack.append(node)
            for neighbor in adjacency.get(node, set()):
                if neighbor not in visited:
                    cycle = dfs(neighbor, path_stack[:])
                    if cycle:
                        return cycle
                elif neighbor in stack:
                    idx = path_stack.index(neighbor)
                    return path_stack[idx:] + [neighbor]
            stack.discard(node)
            return None

        for f in list(adjacency.keys())[:50]:
            if f not in visited:
                cycle = dfs(f, [])
                if cycle:
                    cycles.append(cycle)

        # 最依赖 / 最被依赖
        dependents = {f: len(imps) for f, imps in adjacency.items()}
        depended_by = defaultdict(int)
        for f, deps in adjacency.items():
            for d in deps:
                depended_by[d] += 1

        return ToolResult.success(call_id, self.name, {
            "path": path,
            "modules": len(module_to_file),
            "analyzed": len(file_imports),
            "cycles": cycles[:5],
            "most_dependents": sorted(dependents.items(), key=lambda x: -x[1])[:5],
            "most_depended": sorted(depended_by.items(), key=lambda x: -x[1])[:5],
            "_hint": (
                f"分析了 {len(file_imports)} 个文件的依赖关系。"
                + (f" 发现 {len(cycles)} 个循环依赖!" if cycles else " 未发现循环依赖。")
            ),
        })

    # ── quality ───────────────────────────────────────────

    async def _quality(self, call_id, args):
        path = args.get("path", "")
        threshold = int(args.get("threshold", 70))

        if not os.path.isfile(path):
            return ToolResult.error(call_id, self.name, f"文件不存在: {path}")

        try:
            with open(path, "r", encoding="utf-8") as f:
                code = f.read()
        except Exception as e:
            return ToolResult.error(call_id, self.name, f"读取失败: {e}")

        lines = code.split("\n")
        score = 100
        issues = []

        # 类型注解
        func_defs = [l for l in lines if l.strip().startswith(("def ", "async def "))]
        if func_defs:
            annotated = sum(1 for l in func_defs if " -> " in l)
            rate = annotated / len(func_defs)
            if rate < 0.5:
                score -= 20
                issues.append({"severity": "major", "type": "typing",
                               "desc": f"类型注解覆盖率仅 {rate:.0%} ({annotated}/{len(func_defs)})"})
            elif rate < 0.8:
                score -= 10
                issues.append({"severity": "minor", "type": "typing",
                               "desc": f"部分函数缺少返回类型 ({annotated}/{len(func_defs)})"})

        # 异常处理
        io_keywords = ["open(", "requests.", "httpx.", ".read()", ".write("]
        if any(kw in code for kw in io_keywords) and "try:" not in code:
            score -= 15
            issues.append({"severity": "major", "type": "error_handling",
                           "desc": "存在 I/O 操作但无异常处理"})

        # docstring
        if any(l.strip().startswith("def ") for l in lines) and '"""' not in code:
            score -= 10
            issues.append({"severity": "minor", "type": "docs",
                           "desc": "函数缺少 docstring"})

        # 安全检查
        danger = {
            "eval(": ("critical", "用 ast.literal_eval 替代 eval"),
            "exec(": ("critical", "避免使用 exec"),
            "shell=True": ("major", "使用参数列表替代 shell=True"),
            "password": ("warning", "从环境变量读取密码"),
            "secret": ("warning", "从环境变量读取密钥"),
        }
        for pattern, (sev, fix) in danger.items():
            if pattern in code.lower():
                score -= 20 if sev == "critical" else 15 if sev == "major" else 10
                issues.append({"severity": sev, "type": "security",
                               "desc": f"发现 {pattern}", "fix": fix})

        # 函数长度
        long = []
        cur_name, cur_len = None, 0
        for line in lines:
            if line.strip().startswith(("def ", "async def ")):
                if cur_name and cur_len > 50:
                    long.append(f"{cur_name} ({cur_len}行)")
                cur_name = line.strip()[:60]
                cur_len = 0
            elif cur_name is not None:
                cur_len += 1
        if cur_name and cur_len > 50:
            long.append(f"{cur_name} ({cur_len}行)")
        if long:
            score -= 5 * min(len(long), 4)
            for lf in long:
                issues.append({"severity": "minor", "type": "length",
                               "desc": f"函数过长: {lf}"})

        score = max(0, score)

        return ToolResult.success(call_id, self.name, {
            "path": path, "score": score, "threshold": threshold,
            "passed": score >= threshold, "issues": issues,
            "_hint": f"评分 {score}/100。" + (
                f" {len(issues)} 个问题待修复。" if issues else " 无问题!"
            ),
        })

    # ── style ─────────────────────────────────────────────

    async def _style(self, call_id, args):
        path = args.get("path", ".")
        if not os.path.isdir(path):
            path = os.path.dirname(path) or "."

        rules = []

        # pyproject.toml
        pp_path = os.path.join(path, "pyproject.toml")
        if os.path.isfile(pp_path):
            try:
                import tomllib
                with open(pp_path, "rb") as f:
                    pp = tomllib.load(f)
            except ImportError:
                pp = None
            if pp:
                ruff = pp.get("tool", {}).get("ruff", {})
                if ruff:
                    rules.append({"key": "line_length", "value": ruff.get("line-length", 88)})
                    rules.append({"key": "linter", "value": "ruff"})
                mypy = pp.get("tool", {}).get("mypy", {})
                if mypy:
                    rules.append({"key": "type_checker", "value": "mypy"})
                req_py = pp.get("project", {}).get("requires-python", "")
                if req_py:
                    rules.append({"key": "python", "value": req_py})

        if not rules:
            rules.append({"key": "default", "value": "line_length=88, indent=4"})

        return ToolResult.success(call_id, self.name, {
            "path": path, "has_config": bool(rules),
            "rules": rules,
            "_hint": "生成新代码时请遵循以上规范",
        })
