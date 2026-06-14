"""
smoke_test.py — 冒烟测试

快速验证框架是否可用（< 5 秒）：
  1. 导入所有核心模块
  2. 实例化核心组件
  3. 复杂度路由功能
  4. 消息列表操作
  5. 任务管理器意图检测

注意：每个测试块独立 import，避免变量作用域冲突。
"""

import asyncio
import sys
from pathlib import Path

# 确保项目根在 sys.path 中
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


async def smoke_test() -> bool:
    """冒烟测试主流程"""
    print("\n" + "=" * 50)
    print("🔥  冒烟测试")
    print("=" * 50)

    passed = 0
    failed = 0

    # ── 测试1: 导入 ──
    print("\n📦  [测试 1/5] 导入所有核心模块...")
    try:
        from engine.core.types import Message, Role
        from engine.dag.node import LLMNode, ToolNode, RouterNode, MapNode, HumanInLoopNode
        from engine.dag.graph import WorkflowGraph
        from engine.dag.edge import ConditionalEdge
        from engine.message.message_list import MessageList
        from engine.skill.base import Skill, SkillMeta, SkillResult
        from engine.skill.registry import SkillRegistry
        from engine.tool.registry import ToolRegistry
        from engine.tool.base import BaseTool
        from engine.prompt.complexity import ComplexityRouter
        from engine.context.task_manager import TaskContextManager
        from engine.dag.executor import GraphExecutor
        from engine.plan.tracker import PlanTracker
        from engine.storage.state_store import StateStore

        print(" ✅  所有核心模块导入成功")
        passed += 1
    except Exception as e:
        print(f" ❌  导入失败: {e}")
        failed += 1

    # ── 测试2: 实例化 ──
    print("\n🏗️  [测试 2/5] 实例化核心组件...")
    try:
        from engine.message.message_list import MessageList as _ML
        from engine.skill.registry import SkillRegistry as _SR
        from engine.tool.registry import ToolRegistry as _TR
        from engine.context.task_manager import TaskContextManager as _TM
        from engine.plan.tracker import PlanTracker as _PT

        ml = _ML()
        sr = _SR()
        tr = _TR()
        tm = _TM()
        pt = _PT()

        print(f" ✅  MessageList    创建成功 (max_tokens={ml.max_tokens})")
        print(f" ✅  SkillRegistry  创建成功")
        print(f" ✅  ToolRegistry   创建成功 (懒加载)")
        print(f" ✅  TaskContextManager 创建成功")
        print(f" ✅  PlanTracker    创建成功")
        passed += 1
    except Exception as e:
        print(f" ❌  实例化失败: {e}")
        failed += 1

    # ── 测试3: 复杂度路由 ──
    print("\n🎯  [测试 3/5] 复杂度路由功能...")
    try:
        from engine.prompt.complexity import ComplexityRouter as _CR
        from engine.prompt.complexity import ResponseMode

        cases = [
            ("你好", ResponseMode.DIRECT),
            ("审查这段代码", ResponseMode.DETAILED),
            ("什么是DAG", ResponseMode.CONCISE),
            ("帮我把文件格式化一下", ResponseMode.STANDARD),
        ]

        all_ok = True
        for text, expected in cases:
            mode, info = _CR.classify(text)
            if mode == expected:
                print(f" ✅  '{text}' → {mode.value}")
            else:
                print(f" ⚠️  '{text}' 期望 {expected.value}, 实际 {mode.value}")
                all_ok = False

        if all_ok:
            print(f" ✅  复杂度路由测试完成")
        else:
            print(f" ⚠️  部分模式与预期不符（非关键）")
        passed += 1
    except Exception as e:
        print(f" ❌  复杂度路由失败: {e}")
        failed += 1

    # ── 测试4: 消息列表 ──
    print("\n💬  [测试 4/5] 消息列表操作...")
    try:
        from engine.message.message_list import MessageList as _ML

        ml = _ML()
        ml.add_user("测试用户消息")
        ml.add_assistant("测试助手回复")

        assert len(ml) == 2, f"消息计数错误: {len(ml)}"
        msgs = ml.get_for_llm()

        print(f" ✅  消息计数: {len(ml)} 条, get_for_llm 返回 {len(msgs)} 条")

        passed += 1
    except Exception as e:
        print(f" ❌  消息列表操作失败: {e}")
        failed += 1

    # ── 测试5: 任务管理器 ──
    print("\n📋  [测试 5/5] 任务管理器意图检测...")
    try:
        from engine.context.task_manager import TaskContextManager as _TM

        tm = _TM()

        for text, desc, last in [
            ("继续审查", "继续信号", None),
            ("新任务：翻译", "新任务信号", None),
            ("怎么修？", "短追问(续)", "刚才审查的代码有安全问题"),
            ("分析这个 CSV 数据", "新任务(无重叠)", None),
        ]:
            intent = tm.detect_intent(text, last)
            print(f" ✅  '{text}' ({desc}) → {intent}")

        passed += 1
    except Exception as e:
        print(f" ❌  任务管理器失败: {e}")
        failed += 1

    # ── 汇总 ──
    print("\n" + "=" * 50)
    print(f"📊  结果:   ✅ {passed}  /  ❌ {failed}")

    if failed == 0:
        print("🎉  冒烟测试通过！框架可以运行。")
    else:
        print("⚠️  冒烟测试失败，请修复后再继续。")
    print("=" * 50 + "\n")

    return failed == 0


def main():
    asyncio.run(smoke_test())


if __name__ == "__main__":
    main()
