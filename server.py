"""
Jarvis V3 — FastAPI 后端服务（DAG 执行图架构）

采用 Node + Edge 的 DAG 执行模型替代传统状态机。
提供：
  - 静态文件服务（前端）
  - SSE 聊天流 API
  - 会话管理（CRUD）
  - API Key 配置
"""

import asyncio
import json
import os
import time
import uuid
from typing import Any, Optional, AsyncGenerator

from fastapi import FastAPI, HTTPException, Query, Body
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from contextlib import asynccontextmanager

from engine import (
    LLMClient, ToolRegistry, MessageList, Session,
    WorkflowGraph, GraphExecutor, AgentGraphBuilder,
    LLMNode, ToolNode, RouterNode, ToolDispatchNode,
    NodeOutput, NodeInput,
    Edge, ConditionalEdge,
    HumanInterruptError,
)

# ── 加载 .env ──
_env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_path):
    with open(_env_path, "r", encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _val = _line.split("=", 1)
                os.environ.setdefault(_key.strip(), _val.strip())

# ── 日志 ──
import logging
logger = logging.getLogger("jarvis")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(handler)

# ── 全局状态 ──
llm_client: Optional[LLMClient] = None
executor: Optional[GraphExecutor] = None
registry: Optional[ToolRegistry] = None
message_store = None
skill_registry: Optional[Any] = None
skill_router: Optional[Any] = None

# 中断控制（DAG 版本）
_interrupt_flags: set = set()


# ── 生命周期 ──
@asynccontextmanager
async def lifespan(app: FastAPI):
    global llm_client, executor, registry, message_store, skill_registry, skill_router

    logger.info("Jarvis V3 (DAG 架构) 启动中...")

    # 1. LLM 客户端
    llm_client = LLMClient()
    logger.info("✅ LLM Client 初始化完成")

    # 2. 工具注册
    from tools import register_all_tools, print_tool_list
    registry = ToolRegistry()
    register_all_tools(registry)
    status = registry.get_status()
    logger.info(f"✅ 工具注册完成: {status['registered']} 个")
    print_tool_list(registry)

    # 3. DAG 执行器
    executor = GraphExecutor(
        llm_client=llm_client,
        tool_registry=registry,
        max_parallel=5,
        default_node_timeout=60.0,
    )
    logger.info("✅ GraphExecutor (DAG) 初始化完成")

    # 4. Skill 系统
    from engine.skill import SkillRegistry, SkillRouter
    from engine.skill.examples import BUILTIN_SKILLS
    skill_registry = SkillRegistry()
    for skill_cls in BUILTIN_SKILLS:
        skill_registry.register(skill_cls)
    skill_router = SkillRouter(
        llm_client=llm_client,
        skill_registry=skill_registry,
    )
    skill_registry.print_stats()

    # 5. 消息存储
    store_dir = os.path.join(os.path.dirname(__file__), "data", "sessions")
    os.makedirs(store_dir, exist_ok=True)
    from engine.storage.file_store import FileMessageStore
    message_store = FileMessageStore(store_dir)
    logger.info(f"✅ 消息存储初始化完成: {store_dir}")

    yield

    logger.info("Jarvis V3 服务关闭")


# ── FastAPI 应用 ──
app = FastAPI(
    title="Jarvis V3 API",
    description="DAG 执行图架构的智能助手",
    version="3.1.0",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)


# ── 模型 ──
class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None


class SessionCreateResponse(BaseModel):
    session_id: str


class SessionUpdateRequest(BaseModel):
    title: Optional[str] = None


class ApiKeySaveRequest(BaseModel):
    provider_id: str
    api_key: str


# ── SSE 事件 ──
def sse_event(data: dict) -> str:
    """生成 SSE 事件字符串"""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


# ── 构建 Agent DAG ──
def build_agent_graph(
    user_input: str,
    system_prompt: str = "",
    max_steps: int = 30,
) -> WorkflowGraph:
    """
    构建标准 Agent 执行图（DAG）

    流程:
      think → router → [tool_xxx] → think (循环)
                      → complete (出口)
    """
    graph = WorkflowGraph("agent")

    # ── LLM 思考节点 ──
    think = LLMNode(
        name="think",
        system_prompt=system_prompt,
    )

    # ── 路由节点 ──
    router = RouterNode(
        name="router",
        routes={
            "executing": "tool_dispatch",
            "completed": "complete",
        },
    )

    # ── 工具分发节点 ──
    tool_dispatch = ToolDispatchNode("tool_dispatch")

    # ── 工具执行节点（每个已注册工具一个） ──
    tool_names = registry.list_tools() if registry else []
    tool_nodes = {}
    for tn in tool_names:
        tool_node = ToolNode(name=f"tool_{tn}", tool_name=tn)
        tool_nodes[tn] = tool_node

    # ── 完成节点 ──
    complete = LLMNode(name="complete", system_prompt=system_prompt)

    # ── 注册节点 ──
    graph.add_node(think)
    graph.add_node(router)
    graph.add_node(tool_dispatch)
    for tn_name, tn_node in tool_nodes.items():
        graph.add_node(tn_node)
    graph.add_node(complete)

    # ── 边定义 ──
    # think → router
    graph.add_edge("think", "router")

    # router → tool_dispatch（有条件地）
    graph.add_conditional_edge("router", "executing", "tool_dispatch")

    # router → complete（最终回答）
    graph.add_conditional_edge("router", "completed", "complete")

    # tool_dispatch → tool_xxx（根据 LLM 返回的 tool_calls 分发）
    for tn_name in tool_names:
        graph.add_edge("tool_dispatch", f"tool_{tn_name}",
                        condition=lambda outputs, n=tn_name: _check_tool_condition(outputs, n))

    # tool_xxx → think（循环回来继续思考）
    for tn_name in tool_names:
        graph.add_edge(f"tool_{tn_name}", "think")

    # ── 入口/出口 ──
    graph.set_entry("think")
    graph.set_exit("complete")

    # 元数据
    graph._meta = {
        "user_input": user_input,
        "max_steps": max_steps,
        "step_count": 0,
    }

    return graph


def _check_tool_condition(outputs: dict, tool_name: str) -> bool:
    """检查这个工具节点是否应该被分发到"""
    route_val = outputs.get("route")
    if hasattr(route_val, 'data'):
        route_val = route_val.data
    return str(route_val) == tool_name


# ── 聊天 SSE ──
@app.get("/api/chat/stream")
async def chat_stream(
    message: str = Query(..., description="用户输入"),
    session_id: Optional[str] = Query(None, description="会话 ID"),
    mode: Optional[str] = Query(None, description="执行模式: expert/auto/dag"),
):
    """SSE 聊天流（DAG 执行图驱动）

    Args:
        message: 用户输入
        session_id: 会话 ID
        mode: 执行模式
            - None/auto: 自动判断。如果消息匹配领域专家特征，使用专家模式；否则用 DAG
            - expert: 强制使用领域专家模式
            - dag: 强制使用标准 DAG 执行图
    """
    global executor, skill_router

    if not executor:
        raise HTTPException(status_code=503, detail="服务尚未就绪")

    sid = session_id or str(uuid.uuid4())

    async def event_generator() -> AsyncGenerator[str, None]:
        nonlocal sid

        def is_interrupted():
            return sid in _interrupt_flags

        # ⭐ Skill 模式：直接路由到最合适的 Skill
        effective_mode = mode or "auto"
        if effective_mode == "expert" or (
            effective_mode == "auto" and skill_router
        ):
            try:
                if effective_mode == "expert" or True:  # auto 模式下用 Skill 路由
                    # 检查是否有高置信度的 Skill 匹配
                    candidates = skill_router.skill_registry.route(message, top_k=1)
                    if candidates and candidates[0][1] >= 0.6 or effective_mode == "expert":
                        yield sse_event({"type": "info", "content": f"🧠 匹配到 Skill: {candidates[0][0].meta.icon} {candidates[0][0].meta.display_name}..."})

                        # 加载历史
                        history = None
                        if session_id and message_store:
                            try:
                                stored = await message_store.load_session(session_id)
                                if stored:
                                    history = stored.messages
                            except Exception:
                                history = None

                        result = await skill_router.process(
                            user_input=message,
                            history=history,
                            mode="single",
                            enable_tracing=True,
                        )

                        if result.success:
                            content = result.content
                            yield sse_event({
                                "type": "message",
                                "content": content,
                            })
                            yield sse_event({"type": "done"})
                            return
                        else:
                            yield sse_event({
                                "type": "error",
                                "content": f"Skill 模式失败: {result.error}。回退到 DAG 模式...",
                            })
            except Exception as e:
                logger.warning(f"Skill 模式失败({e})，回退到 DAG 模式")
                yield sse_event({"type": "info", "content": f"回退到标准 DAG 模式..."})

        # ── 标准 DAG 模式 ──
        # 1. 加载历史消息
        history = None
        if session_id and message_store:
            try:
                stored = await message_store.load_session(session_id)
                if stored:
                    history = stored.messages
            except Exception:
                history = None

        # 2. 构建消息列表
        msgs = history or MessageList()
        msgs.add_user(message)

        # 3. 初始化 step 计数
        step_count = 0
        max_steps = 30

        # 4. 构建系统提示词
        system_prompt = """你是 Jarvis，一个强大的智能助手。
你有以下能力：
1. 使用工具来完成任务
2. 多步推理
3. 从经验中学习

请根据用户的问题，逐步思考并使用合适的工具。"""

        try:
            # ── DAG 执行循环 ──
            while step_count < max_steps:
                if is_interrupted():
                    yield sse_event({"type": "interrupted", "content": "用户中断"})
                    _interrupt_flags.discard(sid)
                    return

                step_count += 1
                yield sse_event({
                    "type": "thought",
                    "content": f"思考中... (第{step_count}轮)",
                })

                # 构建本轮 DAG
                graph = WorkflowGraph(f"step_{step_count}")

                # 思考节点
                think = LLMNode(name="think", system_prompt=system_prompt)
                graph.add_node(think)

                # 路由节点
                router = RouterNode(name="router", routes={
                    "executing": "tool_dispatch",
                    "completed": "complete",
                })
                graph.add_node(router)

                # 工具分发节点
                dispatch = ToolDispatchNode("tool_dispatch")
                graph.add_node(dispatch)

                # 工具节点
                tool_names = registry.list_tools() if registry else []
                tool_nodes_map = {}
                for tn in tool_names:
                    tnode = ToolNode(name=f"tool_{tn}", tool_name=tn)
                    tool_nodes_map[tn] = tnode
                    graph.add_node(tnode)

                # 完成节点
                complete = LLMNode(name="complete", system_prompt=system_prompt)
                graph.add_node(complete)

                # 边
                graph.add_edge("think", "router")
                graph.add_conditional_edge("router", "executing", "tool_dispatch")
                graph.add_conditional_edge("router", "completed", "complete")

                # dispatch → 具体工具
                for tn in tool_names:
                    graph.add_edge(
                        "tool_dispatch", f"tool_{tn}",
                        condition=lambda outputs, n=tn: _check_tool_condition(outputs, n),
                    )

                # 工具 → 本轮结束（在循环中我们手动处理工具结果）
                graph.set_entry("think")
                graph.set_exit("complete")

                # 准备输入：用户消息 + 工具定义
                llm_messages = msgs.get_for_llm(include_system=False)

                # 保存当前历史
                history_snapshot = msgs.get_all().copy()

                # 执行 DAG（每步一次遍历）
                ctx = await executor.run(
                    graph=graph,
                    initial_input={
                        "messages": llm_messages,
                        "tools": registry.get_openai_tools() if registry else [],
                    },
                    timeout=120.0,
                )

                # 获取结果
                think_output = ctx.get_node_output("think")
                route_value = ctx.get_node_output("router", "route")

                # 检查是否有 tool_calls
                llm_content = ""
                tool_calls = []

                if think_output:
                    if isinstance(think_output, dict):
                        llm_content = think_output.get("content", "")
                        tool_calls = think_output.get("tool_calls", [])
                    elif isinstance(think_output, str):
                        llm_content = think_output
                    elif hasattr(think_output, 'data'):
                        data = think_output.data if hasattr(think_output, 'data') else think_output
                        if isinstance(data, dict):
                            llm_content = data.get("content", "")
                            tool_calls = data.get("tool_calls", [])

                # 添加 assistant 消息
                msgs.add_assistant(llm_content, tool_calls if tool_calls else None)

                if tool_calls:
                    # 执行工具
                    yield sse_event({
                        "type": "thought",
                        "content": f"执行工具... (第{step_count}轮)",
                    })

                    for tc in tool_calls:
                        if is_interrupted():
                            break

                        # 解析工具调用
                        if isinstance(tc, dict):
                            func = tc.get("function", tc)
                            tc_name = func.get("name", "unknown")
                            raw_args = func.get("arguments", "{}")
                            if isinstance(raw_args, str):
                                try:
                                    tc_args = json.loads(raw_args)
                                except json.JSONDecodeError:
                                    tc_args = {}
                            else:
                                tc_args = raw_args
                        else:
                            tc_name = str(tc)
                            tc_args = {}

                        yield sse_event({
                            "type": "step",
                            "name": tc_name,
                            "status": "running",
                            "args": tc_args,
                        })

                        # 执行工具
                        tool = registry.get(tc_name) if registry else None
                        if tool:
                            try:
                                tc_id = tc.get("id", f"call_{uuid.uuid4().hex[:8]}") if isinstance(tc, dict) else f"call_{uuid.uuid4().hex[:8]}"
                                result = await tool.execute(call_id=tc_id, **tc_args)
                                content = str(result.content) if result.is_success() else (result.error_message or "")
                                msgs.add_tool(tc_id, content)
                                yield sse_event({
                                    "type": "step",
                                    "name": tc_name,
                                    "status": "done" if result.is_success() else "error",
                                    "result": content[:500],
                                })
                            except Exception as e:
                                yield sse_event({
                                    "type": "step",
                                    "name": tc_name,
                                    "status": "error",
                                    "result": str(e),
                                })
                                msgs.add_tool(f"call_err", f"错误: {e}")
                        else:
                            yield sse_event({
                                "type": "step",
                                "name": tc_name,
                                "status": "error",
                                "result": f"工具 '{tc_name}' 未注册",
                            })
                            msgs.add_tool(f"call_err", f"错误: 工具 '{tc_name}' 未注册")

                    # 继续循环
                    continue

                else:
                    # 没有 tool_calls = 最终回答
                    yield sse_event({
                        "type": "complete",
                        "content": llm_content,
                    })

                    # 保存消息
                    if message_store and sid:
                        try:
                            session = Session(session_id=sid, messages=msgs)
                            await message_store.save_session(session)
                        except Exception as e:
                            logger.error(f"保存消息失败: {e}")

                    return

            # 超出最大步数
            yield sse_event({
                "type": "complete",
                "content": f"已执行 {max_steps} 轮思考，已达到最大步数限制。如果需要继续，请告诉我。",
            })

        except HumanInterruptError as e:
            yield sse_event({"type": "interrupted", "content": str(e)})
        except Exception as e:
            logger.error(f"聊天出错: {e}", exc_info=True)
            yield sse_event({"type": "error", "content": f"服务器错误: {str(e)}"})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── 会话管理 ──
@app.get("/api/sessions")
async def list_sessions():
    """列出所有会话"""
    if not message_store:
        return {"sessions": []}
    try:
        sessions = await message_store.list_sessions()
        return {"sessions": sessions}
    except Exception as e:
        logger.error(f"列出会话失败: {e}")
        return {"sessions": []}


@app.post("/api/sessions")
async def create_session():
    """创建新会话"""
    sid = str(uuid.uuid4())
    if message_store:
        try:
            msgs = MessageList()
            session = Session(session_id=sid, messages=msgs)
            await message_store.save_session(session)
        except Exception as e:
            logger.error(f"保存空会话失败: {e}")
    return {"session_id": sid}


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    """获取会话详情"""
    if not message_store:
        return {"session_id": session_id, "messages": []}
    try:
        stored = await message_store.load_session(session_id)
        msgs = stored.messages if stored else None
        messages_data = []
        if msgs:
            for m in msgs.get_all():
                messages_data.append({
                    "role": m.role,
                    "content": m.content if hasattr(m, 'content') else (m.text if hasattr(m, 'text') else str(m)),
                    "steps": getattr(m, 'steps', []),
                })
        return {"session_id": session_id, "messages": messages_data}
    except Exception as e:
        logger.error(f"获取会话失败: {e}")
        return {"session_id": session_id, "messages": []}


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    """删除会话"""
    if message_store:
        try:
            await message_store.delete_session(session_id)
        except Exception as e:
            logger.error(f"删除会话失败: {e}")
    return {"success": True}


@app.patch("/api/sessions/{session_id}")
async def update_session(session_id: str, req: SessionUpdateRequest):
    """更新会话"""
    return {"success": True}


# ── Skill 系统 API ──
@app.get("/api/experts")
async def list_experts():
    """列出所有可用的 Skill（兼容旧 API）"""
    global skill_registry
    if not skill_registry:
        return {"experts": []}
    skills = skill_registry.list_all()
    return {
        "experts": [
            {
                "name": s.name,
                "display_name": s.display_name,
                "description": s.description,
                "icon": s.icon,
                "tags": s.tags,
            }
            for s in sorted(skills, key=lambda x: x.name)
        ]
    }


@app.post("/api/chat/route")
async def route_expert(message: str = Body(...)):
    """测试路由：查看用户消息会路由到哪个 Skill"""
    global skill_registry
    if not skill_registry:
        return {"success": False, "error": "Skill 系统未就绪"}
    candidates = skill_registry.route(message, top_k=5)
    return {
        "success": True,
        "candidates": [
            {
                "skill": skill.meta.display_name,
                "icon": skill.meta.icon,
                "confidence": round(conf, 4),
            }
            for skill, conf in candidates
        ]
    }


# ── 中断 ──
@app.post("/api/chat/interrupt")
async def interrupt_chat():
    """中断当前对话"""
    # DAG 版本：设置中断标志，由 SSE 循环检查
    _interrupt_flags.add("__broadcast__")
    return {"success": True}


# ── API Key 配置 ──
@app.get("/api/config/keys")
async def get_api_keys():
    """获取已配置的 API Key 列表"""
    keys = []
    for key_name in ["DEEPSEEK_API_KEY", "OPENAI_API_KEY", "DASHSCOPE_API_KEY"]:
        value = os.environ.get(key_name, "")
        keys.append({
            "id": key_name.lower().replace("_api_key", ""),
            "name": key_name.replace("_API_KEY", "").replace("_", " ").title(),
            "configured": bool(value),
            "masked": f"****{value[-4:]}" if len(value) > 4 else "",
            "newKey": "",
        })
    return {"providers": keys}


@app.post("/api/config/keys")
async def save_api_key(req: ApiKeySaveRequest):
    """保存 API Key"""
    env_key_name = f"{req.provider_id.upper()}_API_KEY"
    os.environ[env_key_name] = req.api_key
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    try:
        lines = []
        found = False
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith(f"{env_key_name}="):
                        lines.append(f"{env_key_name}={req.api_key}\n")
                        found = True
                    else:
                        lines.append(line)
        if not found:
            lines.append(f"{env_key_name}={req.api_key}\n")
        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
        logger.info(f"API Key {env_key_name} 已保存到 .env")
    except Exception as e:
        logger.error(f"保存 .env 失败: {e}")
    return {"success": True, "provider": req.provider_id}


# ── 状态检查 ──
@app.get("/api/status")
async def check_status():
    """检查服务状态"""
    return {
        "status": "ok",
        "version": "3.1.0",
        "architecture": "dag",
        "llm_configured": bool(os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY")),
    }


# ── 静态文件 ──
static_dir = os.path.join(os.path.dirname(__file__), "frontend")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir, html=True), name="frontend")


@app.get("/")
async def serve_index():
    """提供前端页面"""
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path, media_type="text/html")
    raise HTTPException(status_code=404, detail="前端文件未找到")


# ── 启动入口 ──
if __name__ == "__main__":
    import uvicorn

    print("=" * 56)
    print("  Jarvis V3 (DAG 架构) 启动中...")
    print(f"  .env: {'已加载' if os.path.exists(_env_path) else '未找到'}")
    print(f"  DeepSeek: {'✅ 已配置' if os.environ.get('DEEPSEEK_API_KEY') else '❌ 未配置'}")
    print(f"  前端: {'✅ 已就绪' if os.path.exists(static_dir) else '❌ 未找到'}")
    print(f"  访问: http://localhost:8001")
    print(f"  架构: DAG 执行图 (Node+Edge)")
    print("=" * 56)

    uvicorn.run(app, host="0.0.0.0", port=8001, log_level="info")
