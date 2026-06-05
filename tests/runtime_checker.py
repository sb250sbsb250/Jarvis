"""
runtime_checker.py — 运行时诊断器

每次代码修改后运行，自动检查框架健康状态：
  1. 关键模块导入
  2. 循环导入
  3. 关键组件实例化
  4. 核心功能测试
  5. 日志配置
  6. Skill 示例兼容性
"""

import asyncio
import importlib
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field
from enum import Enum

# 确保项目根在 sys.path 中
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class Severity(Enum):
    OK = "✅"
    WARNING = "⚠️"
    ERROR = "❌"
    CRITICAL = "💀"


@dataclass
class CheckResult:
    name: str
    severity: Severity
    message: str
    details: Dict[str, Any] = field(default_factory=dict)


class RuntimeChecker:
    """
    运行时诊断器 — 自动检测导入、实例化、核心功能
    """

    def __init__(self):
        self.results: List[CheckResult] = []

    async def run_all(self) -> bool:
        """运行所有检查，返回是否全部通过"""
        self.results.clear()

        self._check_imports()
        self._check_circular_imports()
        self._check_config()
        self._check_instantiation()
        await self._check_core_functions()
        self._check_logging()
        self._check_skill_examples()
        self._check_legacy_refs()

        self.print_report()

        return all(
            r.severity not in (Severity.ERROR, Severity.CRITICAL)
            for r in self.results
        )

    # ── 检查项 ──

    def _check_imports(self):
        """检查所有核心模块是否能正常导入"""
        modules = [
            "engine.core.types",
            "engine.core.errors",
            "engine.core.token_estimator",
            "engine.dag.node",
            "engine.dag.graph",
            "engine.dag.context",
            "engine.dag.executor",
            "engine.dag.builder",
            "engine.dag.planner",
            "engine.message.message_list",
            "engine.skill.base",
            "engine.skill.registry",
            "engine.skill.router",
            "engine.skill.injector",
            "engine.skill.loader",
            "engine.tool.registry",
            "engine.tool.base",
            "engine.tool.file_tool",
            "engine.tool.code_tool",
            "engine.tool.executor",
            "engine.prompt.complexity",
            "engine.context.task_manager",
            "engine.context.builder",
            "engine.storage.state_store",
            "engine.plan.tracker",
        ]
        for mod_name in modules:
            try:
                importlib.import_module(mod_name)
                self._ok(f"导入: {mod_name}")
            except Exception as e:
                self._error(
                    f"导入: {mod_name}",
                    f"导入失败: {e}",
                    {"traceback": traceback.format_exc()},
                )

    def _check_circular_imports(self):
        """检查循环导入 — 尝试导入 engine 包顶层"""
        try:
            import engine  # noqa: F401
            self._ok("循环导入检查")
        except ImportError as e:
            if "circular import" in str(e).lower():
                self._error("循环导入检查", f"检测到循环导入: {e}")
            else:
                self._warning("循环导入检查", f"其他导入问题: {e}")

    def _check_config(self):
        """检查环境配置"""
        import os

        api_key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get(
            "OPENAI_API_KEY"
        )
        if api_key:
            self._ok("配置: API_KEY", f"已配置 ({api_key[:8]}...)")
        else:
            self._warning("配置: API_KEY", "未设置，使用默认值")

        model = os.environ.get("LLM_MODEL")
        if model:
            self._ok("配置: LLM_MODEL", f"{model}")
        else:
            self._warning("配置: LLM_MODEL", "未设置，默认 deepseek-chat")

    def _check_instantiation(self):
        """检查关键组件能否正常实例化"""
        from engine.message.message_list import MessageList

        try:
            ml = MessageList()
            self._ok("实例化: MessageList", f"max_tokens={ml.max_tokens}")
        except Exception as e:
            self._error("实例化: MessageList", str(e))

        from engine.skill.registry import SkillRegistry

        try:
            sr = SkillRegistry()
            self._ok("实例化: SkillRegistry", "空注册表创建成功")
        except Exception as e:
            self._error("实例化: SkillRegistry", str(e))

        from engine.tool.registry import ToolRegistry

        try:
            tr = ToolRegistry()
            self._ok("实例化: ToolRegistry", "懒加载模式创建成功")
        except Exception as e:
            self._error("实例化: ToolRegistry", str(e))

        from engine.plan.tracker import PlanTracker

        try:
            pt = PlanTracker()
            self._ok("实例化: PlanTracker", "创建成功")
        except Exception as e:
            self._error("实例化: PlanTracker", str(e))

        from engine.storage.state_store import StateStore

        try:
            import tempfile
            ss = StateStore(db_path=tempfile.mktemp(suffix=".db"))
            self._ok("实例化: StateStore", "SQLite 存储创建成功")
        except Exception as e:
            self._error("实例化: StateStore", str(e))

    async def _check_core_functions(self):
        """测试核心功能"""
        # 1. TaskContextManager 意图检测
        from engine.context.task_manager import TaskContextManager

        tm = TaskContextManager()
        intent = tm.detect_intent("继续审查", None)
        self._ok("功能: 意图检测", f"'继续审查' → {intent}")

        intent_new = tm.detect_intent("新任务：翻译这个文件", None)
        self._ok("功能: 意图检测(新任务)", f"'新任务：翻译' → {intent_new}")

        intent_short = tm.detect_intent("怎么修？", None)
        self._ok("功能: 意图检测(追问)", f"'怎么修？' → {intent_short}")

        # 2. ComplexityRouter
        from engine.prompt.complexity import ComplexityRouter, ResponseMode

        tests = [
            ("你好", ResponseMode.DIRECT),
            ("审查这段代码", ResponseMode.DETAILED),
            ("今天几号", ResponseMode.CONCISE),
        ]
        for text, expected in tests:
            mode, info = ComplexityRouter.classify(text)
            if mode == expected:
                self._ok("功能: 复杂度路由", f"'{text}' → {mode.value}")
            else:
                self._error(
                    "功能: 复杂度路由",
                    f"'{text}' 应为 {expected.value}，实际 {mode.value}",
                )

        # 3. MessageList 基础操作
        from engine.message.message_list import MessageList as _ML
        ml = _ML()
        ml.add_user("测试消息")
        if len(ml) == 1:
            self._ok("功能: MessageList.add_user", f"消息计数: {len(ml)}")
        else:
            self._error(
                "功能: MessageList.add_user",
                f"预期 1 条，实际 {len(ml)} 条",
            )

        ml.add_assistant("测试回复")
        if len(ml) == 2:
            self._ok("功能: MessageList.add_assistant", f"消息计数: {len(ml)}")
        else:
            self._error(
                "功能: MessageList.add_assistant",
                f"预期 2 条，实际 {len(ml)} 条",
            )

        # 4. MessageList get_for_llm
        msgs = ml.get_for_llm(include_system=True)
        if len(msgs) > 0:
            self._ok(
                "功能: MessageList.get_for_llm",
                f"返回 {len(msgs)} 条消息",
            )
        else:
            self._error("功能: MessageList.get_for_llm", "返回空列表")

        # 5. Skill 基类
        from engine.skill.base import SkillMeta as _SM
        meta = _SM(name="test", display_name="测试", description="测试用")
        self._ok("功能: SkillMeta", f"创建成功: {meta.name}")

        # 6. LLMNode 导入
        from engine.dag.node import LLMNode
        self._ok("功能: LLMNode 导入", "成功")

        # 7. SkillInjector
        from engine.skill.injector import SkillInjector as _SI
        _si = _SI()
        self._ok("功能: SkillInjector 实例化", "成功")

        # 8. StateStore 持久化
        import tempfile
        from engine.storage.state_store import StateStore as _SS
        ss = _SS(db_path=tempfile.mktemp(suffix=".db"))
        await ss.save_state("test_thread", 0, "start", {"msg": "hello"})
        states = await ss.list_states("test_thread")
        if len(states) == 1:
            self._ok("功能: StateStore 持久化", "保存/读取状态成功")
        else:
            self._error(
                "功能: StateStore 持久化",
                f"预期 1 条状态，实际 {len(states)} 条",
            )

    def _check_logging(self):
        """检查日志配置"""
        import logging

        root = logging.getLogger()
        if root.handlers:
            self._ok("日志配置", f"{len(root.handlers)} 个处理器")
        else:
            self._warning("日志配置", "未配置日志处理器")

    def _check_skill_examples(self):
        """检查 skill/examples.py 兼容性"""
        try:
            from engine.skill import examples  # noqa: F401

            self._ok("Skill 示例导入", "engine.skill.examples 导入成功")
        except ImportError as e:
            err = str(e)
            if any(
                n in err
                for n in [
                    "ListFilesNode",
                    "CodeSearchNode",
                    "CodeEditorNode",
                    "FileRenameNode",
                ]
            ):
                self._error(
                    "Skill 示例导入",
                    f"仍引用已删除节点: {err}",
                    {"建议": "删除已移除节点的 import"},
                )
            else:
                self._error("Skill 示例导入", f"导入失败: {err}")

    def _check_legacy_refs(self):
        """检查是否有对已删除代码的遗留引用"""
        # 检查 dag/__init__.py 是否导出已删除节点
        try:
            from engine.dag.node import FileProcessorNode  # noqa: F401
        except ImportError:
            self._warning(
                "遗留检查: FileProcessorNode",
                "已从 node.py 移除（转移到 tool/ 或删除）",
            )

        # 检查 expert/ 目录是否存在
        from pathlib import Path

        expert_dir = Path(__file__).resolve().parent.parent / "engine" / "expert"
        if expert_dir.exists():
            self._warning("遗留检查: expert/", "该目录应已删除")

    # ── 辅助方法 ──

    def _ok(self, name: str, message: str = ""):
        self.results.append(
            CheckResult(name=name, severity=Severity.OK, message=message or "通过")
        )

    def _warning(self, name: str, message: str):
        self.results.append(
            CheckResult(name=name, severity=Severity.WARNING, message=message)
        )

    def _error(self, name: str, message: str, details: Optional[Dict] = None):
        self.results.append(
            CheckResult(
                name=name,
                severity=Severity.ERROR,
                message=message,
                details=details or {},
            )
        )

    def _critical(self, name: str, message: str, details: Optional[Dict] = None):
        self.results.append(
            CheckResult(
                name=name,
                severity=Severity.CRITICAL,
                message=message,
                details=details or {},
            )
        )

    # ── 报告 ──

    def print_report(self):
        """打印诊断报告"""
        print("\n" + "=" * 72)
        print("🔍  运行时诊断报告")
        print("=" * 72)

        errors = 0
        criticals = 0

        for r in self.results:
            if r.severity == Severity.ERROR:
                errors += 1
            elif r.severity == Severity.CRITICAL:
                criticals += 1

            icon = r.severity.value
            print(f"\n{icon}  {r.name}")
            print(f"   {r.message}")
            if r.details:
                for k, v in r.details.items():
                    print(f"   [{k}] {v}")

        print("\n" + "=" * 72)
        ok_count = sum(1 for r in self.results if r.severity == Severity.OK)
        warn_count = sum(1 for r in self.results if r.severity == Severity.WARNING)

        print(
            f"📊  统计: ✅ {ok_count}  |  ⚠️ {warn_count}  |  "
            f"❌ {errors}  |  💀 {criticals}  |  总计 {len(self.results)}"
        )

        if errors == 0 and criticals == 0:
            print("🎉  所有检查通过！框架健康。")
        else:
            print("⚠️  发现问题，请修复后再提交。")
        print("=" * 72 + "\n")


# ── 快速入口 ──


async def run_diagnostics() -> bool:
    """运行完整诊断"""
    checker = RuntimeChecker()
    return await checker.run_all()


def main():
    """命令行入口"""
    asyncio.run(run_diagnostics())


if __name__ == "__main__":
    main()
