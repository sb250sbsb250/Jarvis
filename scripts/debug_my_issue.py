"""
scripts/debug_my_issue.py — 调试助手

运行这个脚本，把完整输出复制给我。
这样我就能看到你的环境、LLM 连接、具体任务的执行细节。
"""

import asyncio
import json
import os
import sys
import time

# 加入项目根目录
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


async def debug_environment():
    """1. 环境检查"""
    print("\n" + "=" * 60)
    print("🔍 调试模式 — 环境检查")
    print("=" * 60)

    print(f"  当前目录: {os.getcwd()}")
    print(f"  Python 版本: {sys.version.split()[0]}")

    # API Key 检查（只显示是否配置）
    api_key = os.getenv("DEEPSEEK_API_KEY", "")
    if api_key:
        masked = f"****{api_key[-4:]}" if len(api_key) > 4 else "****"
        print(f"  DEEPSEEK_API_KEY: ✅ 已配置 ({masked})")
    else:
        print(f"  DEEPSEEK_API_KEY: ❌ 未配置")

    # 导入基础设施
    try:
        from engine import ToolRegistry
        from engine.llm_client import LLMClient
        from tools import register_all_tools
        print("  engine 导入: ✅")
    except Exception as e:
        print(f"  engine 导入: ❌ {e}")
        return None, None

    # 工具注册检查
    try:
        registry = ToolRegistry()
        register_all_tools(registry)
        tools = registry.list_tools()
        print(f"  工具注册: ✅ ({len(tools)} 个)")
        for t in tools[:5]:
            print(f"    • {t.name}: {t.description[:50]}")
        if len(tools) > 5:
            print(f"    ... 还有 {len(tools) - 5} 个")
    except Exception as e:
        print(f"  工具注册: ❌ {e}")
        registry = None

    # LLM 客户端
    client = None
    try:
        client = LLMClient()
        print("  LLMClient: ✅")
    except Exception as e:
        print(f"  LLMClient: ❌ {e}")

    return registry, client


async def debug_llm(client):
    """2. LLM 连接测试"""
    print("\n" + "=" * 60)
    print("🔍 LLM 连接测试")
    print("=" * 60)

    if not client:
        print("  ❌ 无 LLM 客户端，跳过")
        return False

    try:
        start = time.time()
        resp = await client.chat_completion(
            messages=[{"role": "user", "content": "回复 OK"}],
            max_tokens=10,
        )
        duration = (time.time() - start) * 1000

        content = ""
        if isinstance(resp, dict):
            content = (resp.get("choices", [{}])[0]
                       .get("message", {})
                       .get("content", ""))
        print(f"  ✅ 连接成功 ({duration:.0f}ms): {content}")
        return True
    except Exception as e:
        print(f"  ❌ 连接失败: {e}")
        return False


async def debug_task(registry, client):
    """3. 执行具体任务测试"""
    print("\n" + "=" * 60)
    print("🔍 任务执行测试")
    print("=" * 60)

    user_input = input("  请输入你想测试的任务（直接回车跳过）:\n  > ").strip()

    if not user_input:
        print("  已跳过")
        return

    if not client:
        print("  ❌ 无 LLM 客户端，无法执行")
        return

    # 导入 Skill 系统
    try:
        from engine.skill import SkillRegistry, SkillRouter
        from skills import ALL_SKILLS
        sr = SkillRegistry()
        for skill in ALL_SKILLS:
            sr.register(skill)
        router = SkillRouter(client, sr)
        print(f"  Skill 注册: ✅ ({len(ALL_SKILLS)} 个)")
        print(f"  匹配分析: {[m.meta.display_name for m in sr.list_all()][:5]}...")
    except Exception as e:
        print(f"  Skill 导入: ❌ {e}")
        return

    # DAG 执行器
    try:
        from engine import GraphExecutor, WorkflowGraph, LLMNode, ToolNode
        executor = GraphExecutor(
            llm_client=client,
            tool_registry=registry,
            max_parallel=3,
            default_node_timeout=30.0,
        )
        print(f"  GraphExecutor: ✅")
    except Exception as e:
        print(f"  GraphExecutor: ❌ {e}")
        return

    # 执行
    print(f"\n  执行: {user_input}")
    print(f"  {'─' * 50}")
    try:
        # 先看有没有匹配的 Skill
        candidates = sr.route(user_input, top_k=1)
        if candidates:
            s, conf = candidates[0]
            print(f"  匹配: {s.meta.display_name} (置信度: {conf:.2f})")

        start = time.time()
        result = await router.process(
            user_input=user_input,
            mode="single",
            enable_tracing=True,
        )
        duration = (time.time() - start) * 1000

        print(f"\n  ⏱  耗时: {duration:.0f}ms")
        print(f"  成功: {result.success}")
        print(f"  内容 ({len(result.content)} 字):")
        print(f"  {'─' * 50}")
        if result.content:
            print(result.content[:1000])
        else:
            print("  (空)")
        print(f"  {'─' * 50}")
        if result.error:
            print(f"  错误: {result.error}")
        if result.data:
            print(f"  数据: {json.dumps(result.data, ensure_ascii=False)[:300]}")

    except Exception as e:
        import traceback
        print(f"  ❌ 异常: {e}")
        traceback.print_exc()

    print("\n" + "=" * 60)
    print("✅ 调试完成，请将以上完整输出复制给我")
    print("=" * 60)


async def debug_simple(registry, client):
    """4. 简单 DAG 测试（检查执行器是否正常）"""
    print("\n" + "=" * 60)
    print("🔍 简单 DAG 执行测试")
    print("=" * 60)

    if not client:
        print("  ❌ 无 LLM 客户端，跳过")
        return

    from engine import WorkflowGraph, LLMNode, GraphExecutor

    graph = WorkflowGraph("test_simple")
    node = LLMNode(
        name="test",
        system_prompt="请回复一句话。",
        max_tokens=100,
    )
    graph.add_node(node)
    graph.set_entry("test")
    graph.set_exit("test")

    executor = GraphExecutor(
        llm_client=client,
        tool_registry=registry,
        max_parallel=1,
        default_node_timeout=15.0,
    )

    try:
        ctx = await executor.run(graph, {
            "messages": [{"role": "user", "content": "你好，测试一下"}]
        })
        output = ctx.get_node_output("test", "output")
        if output:
            data = output.data if hasattr(output, "data") else output
            content = data.get("content", "") if isinstance(data, dict) else str(data)
            print(f"  ✅ DAG 执行成功: {content[:100]}")
        else:
            print(f"  ❌ DAG 输出为空")
    except Exception as e:
        import traceback
        print(f"  ❌ DAG 异常: {e}")
        traceback.print_exc()


async def main():
    print("Jarvis V3 调试脚本")
    print(f"运行时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    # 1. 环境
    registry, client = await debug_environment()

    # 2. LLM 连接
    if client:
        await debug_llm(client)

    # 3. 简单 DAG
    if client:
        await debug_simple(registry, client)

    # 4. 任务测试
    await debug_task(registry, client)

    print("\n完毕。")


if __name__ == "__main__":
    asyncio.run(main())
