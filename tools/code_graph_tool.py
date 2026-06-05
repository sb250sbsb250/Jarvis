"""
code_graph_tool.py — Code Graph 工具引擎

纯代码图谱：全项目 AST 解析 + 依赖/调用链/影响分析
无 Skill 耦合，可独立注册使用。
"""

from __future__ import annotations

import ast
import os
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from engine.tool.base import BaseTool, ToolParameter, ToolResult

logger = logging.getLogger(__name__)


@dataclass
class SymbolInfo:
    """符号信息"""
    name: str
    kind: str  # function | async_function | class | method
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
        py_files = [
            f for f in self.root.rglob("*.py")
            if "__pycache__" not in str(f)
            and ".venv" not in str(f)
            and "site-packages" not in str(f)
        ]
        logger.info(f"[CodeGraph] 全量扫描 {len(py_files)} 个文件...")
        for fpath in py_files:
            self._parse_file(fpath)
            self._file_mtimes[str(fpath)] = fpath.stat().st_mtime
        self._build_reverse()
        self._built = True
        logger.info(f"[CodeGraph] 图谱就绪: {len(self._files)} 文件, {len(self._symbols)} 符号")
        return self

    def _incremental_update(self) -> "ProjectGraph":
        """增量更新：只重新解析修改/新增/删除的文件"""
        current_files = [
            f for f in self.root.rglob("*.py")
            if "__pycache__" not in str(f)
            and ".venv" not in str(f)
            and "site-packages" not in str(f)
        ]
        current_set = {str(f) for f in current_files}
        cached_set = set(self._file_mtimes.keys())

        new = current_set - cached_set
        changed = {p for p in cached_set & current_set
                   if os.path.getmtime(p) != self._file_mtimes.get(p, 0)}
        deleted = cached_set - current_set

        if not new and not changed and not deleted:
            return self

        # 清理删除
        for fp in deleted:
            try:
                rel = str(Path(fp).relative_to(self.root))
            except ValueError:
                rel = fp
            self._files.pop(rel, None)
            self._file_mtimes.pop(fp, None)

        logger.info(f"[CodeGraph] 增量: +{len(new)}新 ~{len(changed)}改 -{len(deleted)}删")
        for fp in sorted(new | changed):
            fpath = Path(fp)
            # 移除旧符号
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

    def _extract_symbol(self, node, file_path: str, name: Optional[str] = None) -> SymbolInfo:
        sym_name = name or node.name
        kind = ("async_function" if isinstance(node, ast.AsyncFunctionDef)
                else "class" if isinstance(node, ast.ClassDef) else "function")
        docstring = ast.get_docstring(node)
        decorators = [
            d.id if isinstance(d, ast.Name) else d.attr if isinstance(d, ast.Attribute) else "@"
            for d in getattr(node, 'decorator_list', [])
        ]
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
        return SymbolInfo(name=sym_name, kind=kind, file=file_path, line=node.lineno,
                          docstring=docstring[:300] if docstring else None,
                          calls=calls, decorators=decorators)

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
        for sym_name, syms in self._symbols.items():
            if name in sym_name:
                return syms
        return []

    # ── 公开查询 API ──

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
        name_lower = name.lower()
        results = []
        for sym_name, syms in self._symbols.items():
            if name_lower in sym_name.lower():
                for s in syms:
                    results.append({"name": s.name, "kind": s.kind,
                                    "file": s.file, "line": s.line,
                                    "docstring": s.docstring})
        results.sort(key=lambda x: len(x["name"]))
        return {"query": name, "count": len(results), "results": results[:15]}

    def trace_callers(self, func_name: str) -> Dict:
        syms = self._find_symbol(func_name)
        if not syms:
            return {"error": f"符号未找到: {func_name}"}
        all_callers = []
        for sym in syms:
            for caller in sym.called_by:
                parts = caller.split("::")
                all_callers.append({"file": parts[0],
                                    "function": parts[1] if len(parts) > 1 else "?"})
        return {"target": func_name,
                "defined_in": [{"file": s.file, "line": s.line} for s in syms],
                "callers": all_callers, "count": len(all_callers),
                "impact_note": f"修改 {func_name} 可能影响以上 {len(all_callers)} 个调用点"}

    def trace_callees(self, func_name: str) -> Dict:
        syms = self._find_symbol(func_name)
        if not syms:
            return {"error": f"符号未找到: {func_name}"}
        all_callees, seen = [], set()
        for sym in syms:
            for called in sym.calls:
                if called in self._symbols and called not in seen:
                    seen.add(called)
                    for target in self._symbols[called]:
                        all_callees.append({"name": called, "file": target.file, "line": target.line})
        return {"target": func_name, "callees": all_callees, "count": len(all_callees)}

    def analyze_impact(self, file_path: str) -> Dict:
        target = self._match_file(file_path)
        if not target:
            return {"error": f"文件未找到: {file_path}"}
        info = self._files[target]
        callers = {}
        for sym_name, sym in info["symbols"].items():
            if sym.called_by:
                callers[sym_name] = sym.called_by
        return {
            "target": target,
            "direct_dependents": sorted(info["imported_by"]),
            "callers_of_my_symbols": callers,
            "same_package_files": [p for p in self._files
                                   if p != target and str(Path(p).parent) == str(Path(target).parent)],
            "summary": (f"修改 {target} 可能影响 {len(info['imported_by'])} 个直接依赖文件"
                        + (f" 和 {len(callers)} 个函数的调用者" if callers else "")),
        }

    def get_stats(self) -> Dict:
        return {
            "total_files": len(self._files), "total_symbols": len(self._symbols),
            "total_import_edges": sum(len(info["imports"]) for info in self._files.values()),
        }

    def list_folder(self, folder_path: str = "") -> Dict:
        """
        列出文件夹的代码索引：模块 → 类 → 函数 的层级结构。

        Args:
            folder_path: 相对路径，如 "engine/tool"，空字符串表示根目录
        """
        target_dir = folder_path.replace("\\", "/").rstrip("/")

        # 找到该目录下的所有文件
        dir_files = {}
        for rel, info in self._files.items():
            rel_normalized = rel.replace("\\", "/")
            parent = "/".join(rel_normalized.split("/")[:-1])
            if target_dir == "" or parent == target_dir or rel_normalized.startswith(target_dir + "/") or rel_normalized == target_dir:
                dir_files[rel] = info

        if not dir_files:
            return {"folder": folder_path, "files": 0, "modules": []}

        modules = []
        for rel, info in sorted(dir_files.items()):
            symbols = []
            for sym_name, sym in sorted(info["symbols"].items()):
                symbols.append({
                    "name": sym_name, "kind": sym.kind, "line": sym.line,
                    "calls": sym.calls[:5],
                })
            modules.append({
                "file": rel,
                "imports": [m for m in info["imports"].keys()][:8],
                "imported_by": sorted(info["imported_by"])[:5],
                "symbols": symbols,
            })

        return {
            "folder": folder_path or "(root)",
            "files": len(dir_files),
            "total_symbols": sum(len(m["symbols"]) for m in modules),
            "modules": modules,
        }


# ── 全局单例缓存（按 project_root）──
_graph_cache: Dict[str, ProjectGraph] = {}


def get_graph(project_root: str = ".") -> ProjectGraph:
    root_abs = str(Path(project_root).resolve())
    if root_abs not in _graph_cache:
        _graph_cache[root_abs] = ProjectGraph(root_abs).build()
    return _graph_cache[root_abs]


# ══════════════════════════════════════
# Code Graph Tool（注册为独立 BaseTool）
# ══════════════════════════════════════

class CodeGraphTool(BaseTool):
    """Code Graph 工具 — 代码图谱查询（只读）"""

    @property
    def is_read(self) -> bool:
        """只分析代码结构，不修改文件"""
        return True

    def __init__(self, project_root: str = "."):
        self._project_root = project_root
        self._graph: Optional[ProjectGraph] = None

    @property
    def name(self) -> str:
        return "code_graph"

    @property
    def description(self) -> str:
        return (
            "代码图谱分析工具。action: find_related/search_symbol/trace_callers/trace_callees/analyze_impact/list_folder/stats\n"
            "- find_related: file='app.py' — 查文件的所有 import 关系和同包文件\n"
            "- search_symbol: name='函数名' — 搜索函数/类定义的位置\n"
            "- trace_callers: name='func' — 追溯谁调用了这个函数\n"
            "- trace_callees: name='func' — 追溯这个函数调用了谁\n"
            "- analyze_impact: file='app.py' — 分析修改某文件的影响范围\n"
            "- list_folder: file='engine/tool' — 列出文件夹的所有模块/类/函数\n"
            "- stats: 无参数 — 项目图谱概览"
        )

    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter("action", "string",
                          "find_related/search_symbol/trace_callers/trace_callees/analyze_impact/list_folder/stats",
                          required=True,
                          enum=["find_related", "search_symbol", "trace_callers",
                                "trace_callees", "analyze_impact", "list_folder", "stats"]),
            ToolParameter("file", "string", "文件路径", required=False),
            ToolParameter("name", "string", "函数/类名称", required=False),
        ]

    async def execute(self, call_id: str, **kwargs) -> ToolResult:
        graph = self._get_graph()
        action = kwargs.get("action", "stats")

        handlers = {
            "find_related": lambda: graph.find_related(kwargs.get("file", "")),
            "search_symbol": lambda: graph.search_symbol(kwargs.get("name", "")),
            "trace_callers": lambda: graph.trace_callers(kwargs.get("name", "")),
            "trace_callees": lambda: graph.trace_callees(kwargs.get("name", "")),
            "analyze_impact": lambda: graph.analyze_impact(kwargs.get("file", "")),
            "list_folder": lambda: graph.list_folder(kwargs.get("file", "")),
            "stats": lambda: graph.get_stats(),
        }

        handler = handlers.get(action)
        if not handler:
            return ToolResult.error(call_id, self.name, f"未知 action: {action}")
        try:
            result = handler()
            if isinstance(result, dict) and "error" in result:
                return ToolResult.error(call_id, self.name, result["error"])
            return ToolResult.success(call_id, self.name, result)
        except Exception as e:
            return ToolResult.error(call_id, self.name, str(e))

    def _get_graph(self) -> ProjectGraph:
        if self._graph is None:
            self._graph = get_graph(self._project_root)
        return self._graph
