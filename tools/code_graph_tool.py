"""
Code Graph 工具（原子工具版）

原子工具:
  code_graph_related    — 文件相关查询（import/被谁import）
  code_graph_symbol     — 搜索符号定义
  code_graph_callers    — 追踪谁调用了某函数
  code_graph_callees    — 追踪某函数调用了谁
  code_graph_impact     — 分析修改文件的影响范围
  code_graph_folder     — 列出文件夹代码结构
  code_graph_stats      — 项目图谱概览
"""

from __future__ import annotations

import ast
import os
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from engine.tool.base import (
    BaseTool, ToolDefinition, ToolParameter, ToolResult,
    CATEGORY_CODE,
)

logger = logging.getLogger(__name__)


@dataclass
class SymbolInfo:
    name: str
    kind: str
    file: str
    line: int
    docstring: Optional[str] = None
    calls: List[str] = field(default_factory=list)
    called_by: List[str] = field(default_factory=list)
    decorators: List[str] = field(default_factory=list)


class ProjectGraph:
    """全项目代码图谱 — 一次构建，全局缓存"""

    def __init__(self, project_root: str = "."):
        self.root = Path(project_root).resolve()
        self._files: Dict[str, Dict] = {}
        self._symbols: Dict[str, List[SymbolInfo]] = {}
        self._file_mtimes: Dict[str, float] = {}
        self._built = False

    def build(self, force: bool = False) -> "ProjectGraph":
        if self._built and not force:
            return self._incremental_update()
        py_files = [f for f in self.root.rglob("*.py")
                    if "__pycache__" not in str(f) and ".venv" not in str(f)
                    and "site-packages" not in str(f)]
        logger.info(f"[CodeGraph] 扫描 {len(py_files)} 个文件...")
        for fpath in py_files:
            self._parse_file(fpath)
            self._file_mtimes[str(fpath)] = fpath.stat().st_mtime
        self._build_reverse()
        self._built = True
        return self

    def _incremental_update(self) -> "ProjectGraph":
        current_files = [f for f in self.root.rglob("*.py")
                         if "__pycache__" not in str(f) and ".venv" not in str(f)
                         and "site-packages" not in str(f)]
        current_set = {str(f) for f in current_files}
        cached_set = set(self._file_mtimes.keys())
        new = current_set - cached_set
        changed = {p for p in cached_set & current_set
                   if os.path.getmtime(p) != self._file_mtimes.get(p, 0)}
        deleted = cached_set - current_set
        if not new and not changed and not deleted:
            return self
        for fp in deleted:
            try:
                rel = str(Path(fp).relative_to(self.root))
            except ValueError:
                rel = fp
            self._files.pop(rel, None)
            self._file_mtimes.pop(fp, None)
        logger.info(f"[CodeGraph] 增量: +{len(new)} ~{len(changed)} -{len(deleted)}")
        for fp in sorted(new | changed):
            fpath = Path(fp)
            try:
                rel = str(fpath.relative_to(self.root))
            except ValueError:
                rel = str(fpath)
            old_info = self._files.get(rel)
            if old_info:
                for sym_name in list(old_info.get("symbols", {}).keys()):
                    self._symbols.pop(sym_name, None)
            self._parse_file(fpath)
            self._file_mtimes[fp] = fpath.stat().st_mtime
        self._build_reverse()
        return self

    def _parse_file(self, fpath: Path) -> None:
        try:
            rel = str(fpath.relative_to(self.root))
        except ValueError:
            rel = str(fpath)
        try:
            source = fpath.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except Exception:
            return
        info: Dict[str, Any] = {"imports": {}, "imported_by": set(), "symbols": {}}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    target = self._resolve_module(alias.name)
                    if target:
                        info["imports"][alias.name] = target
            elif isinstance(node, ast.ImportFrom) and node.module:
                target = self._resolve_module(node.module)
                if target:
                    info["imports"][node.module] = target
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                sym = self._extract_symbol(node, rel)
                info["symbols"][sym.name] = sym
                self._add_symbol(sym.name, sym)
                if isinstance(node, ast.ClassDef):
                    for item in node.body:
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            msym = self._extract_symbol(item, rel, name=f"{node.name}.{item.name}")
                            info["symbols"][msym.name] = msym
                            self._add_symbol(msym.name, msym)
        self._files[rel] = info

    def _extract_symbol(self, node, fp: str, name: str = None) -> SymbolInfo:
        sn = name or node.name
        kind = ("async_function" if isinstance(node, ast.AsyncFunctionDef)
                else "class" if isinstance(node, ast.ClassDef) else "function")
        doc = ast.get_docstring(node)
        decos = [d.id if isinstance(d, ast.Name) else d.attr if isinstance(d, ast.Attribute) else "@"
                 for d in getattr(node, 'decorator_list', [])]
        calls = []
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                if isinstance(child.func, ast.Name):
                    calls.append(child.func.id)
                elif isinstance(child.func, ast.Attribute):
                    parts = []
                    cur = child.func
                    while isinstance(cur, ast.Attribute):
                        parts.append(cur.attr)
                        cur = cur.value
                    if isinstance(cur, ast.Name):
                        parts.append(cur.id)
                    calls.append(".".join(reversed(parts)))
        return SymbolInfo(name=sn, kind=kind, file=fp, line=node.lineno,
                          docstring=doc[:300] if doc else None, calls=calls, decorators=decos)

    def _add_symbol(self, name, sym):
        self._symbols.setdefault(name, []).append(sym)

    def _build_reverse(self):
        for rel, info in self._files.items():
            for module, target in info["imports"].items():
                if target in self._files:
                    self._files[target]["imported_by"].add(rel)
        for name, syms in self._symbols.items():
            for sym in syms:
                for called in sym.calls:
                    if called in self._symbols:
                        for target in self._symbols[called]:
                            target.called_by.append(f"{sym.file}::{sym.name}")

    def _resolve_module(self, module: str) -> Optional[str]:
        parts = module.split(".")
        for c in [str(Path(*parts).with_suffix(".py")), str(Path(*parts) / "__init__.py")]:
            if c in self._files or (self.root / c).exists():
                return c
        return None

    def _match_file(self, path):
        if path in self._files:
            return path
        fname = Path(path).name
        candidates = [p for p in self._files if p.endswith(path) or Path(p).name == fname]
        return candidates[0] if len(candidates) == 1 else None

    def _find_symbol(self, name):
        if name in self._symbols:
            return self._symbols[name]
        for sn, syms in self._symbols.items():
            if name in sn:
                return syms
        return []

    def find_related(self, file_path: str) -> Dict:
        target = self._match_file(file_path)
        if not target:
            return {"error": f"文件未找到: {file_path}"}
        info = self._files[target]
        return {
            "target": target,
            "imports": [{"module": m, "file": f} for m, f in info["imports"].items()],
            "imported_by": sorted(info["imported_by"]),
            "symbol_count": len(info["symbols"]),
            "key_symbols": [{"name": s.name, "kind": s.kind, "line": s.line}
                            for s in list(info["symbols"].values())[:10]],
            "same_package": [p for p in self._files
                             if p != target and str(Path(p).parent) == str(Path(target).parent)],
        }

    def search_symbol(self, name: str) -> Dict:
        nl = name.lower()
        results = []
        for sn, syms in self._symbols.items():
            if nl in sn.lower():
                for s in syms:
                    results.append({"name": s.name, "kind": s.kind, "file": s.file,
                                    "line": s.line, "docstring": s.docstring})
        results.sort(key=lambda x: len(x["name"]))
        return {"query": name, "count": len(results), "results": results[:15]}

    def trace_callers(self, func_name: str) -> Dict:
        syms = self._find_symbol(func_name)
        if not syms:
            return {"error": f"符号未找到: {func_name}"}
        callers = []
        for sym in syms:
            for c in sym.called_by:
                parts = c.split("::")
                callers.append({"file": parts[0], "function": parts[1] if len(parts) > 1 else "?"})
        return {"target": func_name,
                "defined_in": [{"file": s.file, "line": s.line} for s in syms],
                "callers": callers, "count": len(callers),
                "impact_note": f"修改 {func_name} 可能影响 {len(callers)} 个调用点"}

    def trace_callees(self, func_name: str) -> Dict:
        syms = self._find_symbol(func_name)
        if not syms:
            return {"error": f"符号未找到: {func_name}"}
        callees = []
        seen = set()
        for sym in syms:
            for c in sym.calls:
                if c in self._symbols and c not in seen:
                    seen.add(c)
                    for t in self._symbols[c]:
                        callees.append({"name": c, "file": t.file, "line": t.line})
        return {"target": func_name, "callees": callees, "count": len(callees)}

    def analyze_impact(self, file_path: str) -> Dict:
        target = self._match_file(file_path)
        if not target:
            return {"error": f"文件未找到: {file_path}"}
        info = self._files[target]
        callers = {}
        for sn, sym in info["symbols"].items():
            if sym.called_by:
                callers[sn] = sym.called_by
        return {
            "target": target,
            "direct_dependents": sorted(info["imported_by"]),
            "callers_of_my_symbols": callers,
            "same_package_files": [p for p in self._files
                                   if p != target and str(Path(p).parent) == str(Path(target).parent)],
            "summary": f"修改 {target} 可能影响 {len(info['imported_by'])} 个直接依赖文件"
                       + (f" 和 {len(callers)} 个函数的调用者" if callers else ""),
        }

    def get_stats(self) -> Dict:
        return {"total_files": len(self._files), "total_symbols": len(self._symbols),
                "total_import_edges": sum(len(info["imports"]) for info in self._files.values())}

    def list_folder(self, folder_path: str = "") -> Dict:
        td = folder_path.replace("\\", "/").rstrip("/")
        dir_files = {}
        for rel, info in self._files.items():
            rn = rel.replace("\\", "/")
            parent = "/".join(rn.split("/")[:-1])
            if td == "" or parent == td or rn.startswith(td + "/") or rn == td:
                dir_files[rel] = info
        if not dir_files:
            return {"folder": folder_path, "files": 0, "modules": []}
        modules = []
        for rel, info in sorted(dir_files.items()):
            syms = [{"name": sn, "kind": s.kind, "line": s.line, "calls": s.calls[:5]}
                    for sn, s in sorted(info["symbols"].items())]
            modules.append({"file": rel, "imports": [m for m in info["imports"].keys()][:8],
                            "imported_by": sorted(info["imported_by"])[:5], "symbols": syms})
        return {"folder": folder_path or "(root)", "files": len(dir_files),
                "total_symbols": sum(len(m["symbols"]) for m in modules), "modules": modules}


_graph_cache: Dict[str, ProjectGraph] = {}

def get_graph(project_root: str = ".") -> ProjectGraph:
    root_abs = str(Path(project_root).resolve())
    if root_abs not in _graph_cache:
        _graph_cache[root_abs] = ProjectGraph(root_abs).build()
    return _graph_cache[root_abs]


class CodeGraphTool(BaseTool):
    """代码图谱分析工具集"""

    def __init__(self, project_root: str = "."):
        self._project_root = project_root
        self._graph: Optional[ProjectGraph] = None
        self._handlers = {
            "code_graph_related": self._handle_related,
            "code_graph_symbol": self._handle_symbol,
            "code_graph_callers": self._handle_callers,
            "code_graph_callees": self._handle_callees,
            "code_graph_impact": self._handle_impact,
            "code_graph_folder": self._handle_folder,
            "code_graph_stats": self._handle_stats,
        }
        for t in self.tools:
            t.handler = self._handlers.get(t.name)

    @property
    def name(self) -> str:
        return "code_graph"

    @property
    def category(self) -> str:
        return CATEGORY_CODE

    @property
    def is_read(self) -> bool:
        return True

    @property
    def tools(self) -> List[ToolDefinition]:
        return [
            ToolDefinition(
                name="code_graph_related",
                description="""查询指定文件的 import 依赖关系和同包文件列表。
显示该文件导入了哪些模块，以及哪些模块导入了该文件。

使用场景：
- 修改前了解文件的依赖关系
- 重构时确认哪些文件会受影响""",
                parameters=[ToolParameter("path", "string", "文件路径（相对于项目根目录）", required=True)],
                is_read=True,
                examples=[
                    'code_graph_related(path="app.py")',
                    'code_graph_related(path="engine/agent_loop.py")',
                ],
                constraints=["只支持 Python 文件分析", "首次使用需要扫描项目（大型项目可能较慢）"],
            ),
            ToolDefinition(
                name="code_graph_symbol",
                description="""搜索函数/类/方法的定义位置。
支持模糊匹配，返回所有匹配符号的文件路径和行号。

使用场景：
- 找某个函数是在哪里定义的
- 确认类的继承关系
- 导航到代码的特定位置""",
                parameters=[ToolParameter("symbol", "string", "函数名或类名（支持部分匹配）", required=True)],
                is_read=True,
                examples=[
                    'code_graph_symbol(symbol="main")',
                    'code_graph_symbol(symbol="UserService")',
                    'code_graph_symbol(symbol="validate_")  # 模糊搜索',
                ],
                constraints=["只支持 Python 文件", "返回最多 15 个结果"],
            ),
            ToolDefinition(
                name="code_graph_callers",
                description="""追踪某个函数被哪些地方的代码调用了（反向调用链）。

使用场景：
- 重构/重命名函数前确认所有调用点
- 分析函数变更的潜在影响范围""",
                parameters=[ToolParameter("symbol", "string", "函数名（精确匹配）", required=True)],
                is_read=True,
                examples=[
                    'code_graph_callers(symbol="validate_user")',
                ],
                constraints=["只支持 Python 文件", "只能追踪同一项目内的调用", "外部库的调用无法追踪"],
            ),
            ToolDefinition(
                name="code_graph_callees",
                description="""追踪某个函数内部调用了哪些其他函数（正向调用链）。

使用场景：
- 理解函数的执行流程
- 分析函数的逻辑分支和依赖""",
                parameters=[ToolParameter("symbol", "string", "函数名", required=True)],
                is_read=True,
                examples=[
                    'code_graph_callees(symbol="main")',
                ],
                constraints=["只支持 Python 文件", "只能追踪同一项目内的调用"],
            ),
            ToolDefinition(
                name="code_graph_impact",
                description="""分析修改某个文件可能的影响范围。
列出直接依赖该文件的其他文件，以及该文件中被外部调用的函数。

使用场景：
- 重大重构前评估影响
- 安全修改已有代码""",
                parameters=[ToolParameter("path", "string", "要修改的文件路径（相对于项目根目录）", required=True)],
                is_read=True,
                examples=[
                    'code_graph_impact(path="app.py")',
                    'code_graph_impact(path="engine/core/guard.py")',
                ],
                constraints=["只支持 Python 文件", "只统计直接依赖（间接依赖不包含）"],
            ),
            ToolDefinition(
                name="code_graph_folder",
                description="""列出指定文件夹的代码结构概览。
显示模块文件 → 类 → 函数 的树形结构。

使用场景：
- 快速了解一个模块的组织结构
- 找某个文件夹下有哪些类和函数""",
                parameters=[ToolParameter("path", "string", "文件夹路径（相对路径，不传则扫描根目录）", required=False)],
                is_read=True,
                examples=[
                    'code_graph_folder()',
                    'code_graph_folder(path="engine/tool")',
                ],
            ),
            ToolDefinition(
                name="code_graph_stats",
                description="""获取项目代码图谱的全景概要：
总文件数、总符号数（函数/类）、import 依赖关系总数。

使用场景：
- 了解项目规模
- 评估项目的模块复杂度""",
                parameters=[],
                is_read=True,
                examples=['code_graph_stats()'],
            ),
        ]

    async def execute(self, call_id: str, tool_name: str, **kwargs) -> ToolResult:
        handler = self._handlers.get(tool_name)
        if not handler:
            return ToolResult.fail(call_id, tool_name, f"未知工具: {tool_name}")
        try:
            graph = self._get_graph()
            result = handler(graph, **kwargs)
            if isinstance(result, dict) and "error" in result:
                return ToolResult.fail(call_id, tool_name, result["error"])
            return ToolResult.ok(call_id, tool_name, result)
        except Exception as e:
            return ToolResult.fail(call_id, tool_name, str(e))

    def _get_graph(self) -> ProjectGraph:
        if self._graph is None:
            self._graph = get_graph(self._project_root)
        return self._graph

    @staticmethod
    def _handle_related(graph: ProjectGraph, path: str) -> Dict:
        return graph.find_related(path)

    @staticmethod
    def _handle_symbol(graph: ProjectGraph, symbol: str) -> Dict:
        return graph.search_symbol(symbol)

    @staticmethod
    def _handle_callers(graph: ProjectGraph, symbol: str) -> Dict:
        return graph.trace_callers(symbol)

    @staticmethod
    def _handle_callees(graph: ProjectGraph, symbol: str) -> Dict:
        return graph.trace_callees(symbol)

    @staticmethod
    def _handle_impact(graph: ProjectGraph, path: str) -> Dict:
        return graph.analyze_impact(path)

    @staticmethod
    def _handle_folder(graph: ProjectGraph, path: str = "") -> Dict:
        return graph.list_folder(path)

    @staticmethod
    def _handle_stats(graph: ProjectGraph) -> Dict:
        return graph.get_stats()
