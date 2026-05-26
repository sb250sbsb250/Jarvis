"""
快速上手示例 - 配置好 API Key 直接运行
"""

import asyncio
import os

# 从环境变量读取 API Key（你也可以硬编码，但不要提交到 git）
# Windows (cmd): set DEEPSEEK_API_KEY=sk-xxx
# Windows (ps):  $env:DEEPSEEK_API_KEY="sk-xxx"
# Linux/Mac:     export DEEPSEEK_API_KEY=sk-xxx

# 你也可以在这里直接填（仅测试用）：
# os.environ["DEEPSEEK_API_KEY"] = "sk-7318f5dfec0a4ac5828f245395f864bb"

from engine.llm_client import LLMClient, LLMConfig
from engine.core.types import Message
from engine.message.message_list import MessageList
from engine.state.states import AgentState
from engine.state.machine import StateMachine
from engine.tool.base import FunctionTool, ToolParameter
from engine.tool.registry import ToolRegistry
from engine.loop.fc_loop import FCLoop
from engine.agent.agent import Agent, AgentConfig
from engine.session.manager import SessionManager
from engine.storage.file_store import FileMessageStore


async def demo_simple():
    """示例1：直接调 LLM"""
    print("=" * 50)
    print("示例1：直接调 LLM（不涉及状态机）")
    print("=" * 50)

    client = LLMClient()
    messages = [
        {"role": "system", "content": "你是 Jarvis，一个 AI 编程助手，请用中文回复。"},
        {"role": "user", "content": "你好！介绍一下你自己。"},
    ]

    response = await client.chat_completion(messages)
    content = response["choices"][0]["message"]["content"]
    print(f"回复：{content}\n")


async def demo_with_tools():
    """示例2：带工具调用"""
    print("=" * 50)
    print("示例2：带工具调用（天气查询）")
    print("=" * 50)

    # 1. 注册工具
    registry = ToolRegistry()
    registry.clear()

    async def get_weather(city: str):
        """模拟天气查询"""
        weathers = {"北京": "晴，25°C", "上海": "多云，28°C", "深圳": "雨，30°C"}
        return weathers.get(city, f"未知城市：{city}")

    registry.register(
        FunctionTool(
            name="get_weather",
            description="查询城市天气",
            parameters=[
                ToolParameter(name="city", type="string", description="城市名", required=True),
            ],
            fn=get_weather,
        )
    )

    # 2. 创建 LLM + FC Loop
    llm_client = LLMClient()
    loop = FCLoop(llm_client, registry)

    # 3. 手动执行状态驱动循环
    messages = MessageList()
    messages.add_system("你是 Jarvis，用中文回复。调用工具时如实返回结果。")
    messages.add_user("查询北京的天气")

    sm = StateMachine(AgentState.THINKING)

    # 主循环（按流程图：Agent 控制循环，FCLoop 执行单步）
    while not sm.is_terminal():
        tools = registry.get_openai_tools()
        await loop.step(sm, messages, tools)

        if sm.state == AgentState.COMPLETED:
            break
        elif sm.state == AgentState.FAILED:
            print(f"失败：{sm.context.error_message}")
            break

    final = sm.context.get_data("final_answer", "")
    print(f"最终回复：{final}\n")


async def demo_agent():
    """示例3：用 Agent 完整编排"""
    print("=" * 50)
    print("示例3：Agent 完整编排（自动多步推理）")
    print("=" * 50)

    # 1. 注册几个工具
    registry = ToolRegistry()
    registry.clear()

    async def calculator(expression: str):
        """计算数学表达式"""
        try:
            result = eval(expression, {"__builtins__": {}}, {})
            return f"{expression} = {result}"
        except Exception as e:
            return f"计算错误：{e}"

    registry.register(
        FunctionTool(
            name="calculator",
            description="计算数学表达式",
            parameters=[
                ToolParameter(name="expression", type="string",
                              description="数学表达式，如 '2+3*4'", required=True),
            ],
            fn=calculator,
        )
    )

    # 2. 创建 Agent
    llm_client = LLMClient()
    agent = Agent(
        llm_client=llm_client,
        registry=registry,
        config=AgentConfig(max_steps=5, max_retries=2),
    )

    # 3. 运行
    result = await agent.run(
        user_input="计算 12345 + 67890 等于多少？",
        metadata={"source": "demo"},
    )

    print(f"成功：{result['success']}")
    print(f"回复：{result['final_answer']}")
    print(f"状态：{result['state_summary']['current_state']}")
    print(f"步骤：{result['state_summary']['step_count']}")


if __name__ == "__main__":
    print("Jarvis V3 引擎 - 快速上手指南")
    print()

    # 检查 API Key
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        print("⚠️  未设置 DEEPSEEK_API_KEY 环境变量")
        print(f"   请在运行前执行：export DEEPSEEK_API_KEY='sk-xxx'")
        print(f"   或取消上面 os.environ 的注释直接填入 Key")
        print()

    # 选择要运行的示例
    asyncio.run(demo_simple())
    # asyncio.run(demo_with_tools())
    # asyncio.run(demo_agent())
