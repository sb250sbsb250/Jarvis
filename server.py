"""Jarvis V3 - DAG 智能助手服务器"""
import os, shutil, uuid, logging, json, hashlib, time, yaml, glob, asyncio, traceback
from typing import AsyncGenerator, Optional, List, Dict, Any, Callable
from pathlib import Path

import jinja2
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv

# ───────────── Jarvis 核心引擎 ─────────────
from engine import (
    AgentLoop,
)
from engine.conversation import ConversationSession
from engine.llm_client import LLMClient, LLMConfig
from engine.skill.registry import SkillRegistry
from tools import register_all_tools
from engine.tool.registry import ToolRegistry
from engine.session.manager import SessionManager
from engine.core.approval import ApprovalGate

# ───────────── 日志 ─────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ───────────── 配置 ─────────────
load_dotenv()
DATA_DIR = Path("data")
SESSION_DIR = DATA_DIR / "sessions"
STATIC_DIR = Path("frontend")
WORKSPACE_DIR = Path("workspace")

for d in [DATA_DIR, SESSION_DIR, STATIC_DIR, WORKSPACE_DIR]:
    d.mkdir(parents=True, exist_ok=True)

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
MODEL = os.getenv("MODEL", "deepseek-v4-pro")
MOONSHOT_API_KEY = os.getenv("MOONSHOT_API_KEY", "")

# ───────────── FastAPI 应用 ─────────────
app = FastAPI(title="Jarvis V3")


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    model: Optional[str] = None
    search_enabled: bool = False


# ====================================================================
# 工具注册
# ====================================================================
tool_registry = ToolRegistry()
register_all_tools(tool_registry)
tool_names = tool_registry.list_tools()
logger.info(f"✅ 工具注册完成: {len(tool_names)} 个")
for name in sorted(tool_names):
    logger.info(f"   🔧 {name}")

# ====================================================================
# 核心组件
# ====================================================================
llm_client = LLMClient(
    config=LLMConfig(
        api_key=DEEPSEEK_API_KEY,
        base_url=DEEPSEEK_BASE_URL,
        model=MODEL,
    )
)

# ── 多模型客户端注册表（根据模型预设创建） ──
_model_clients: Dict[str, LLMClient] = {}
for _mname in LLMClient.get_supported_models():
    _mpreset = LLMClient.get_model_preset(_mname)
    _mkey = os.getenv(_mpreset.get("api_key_env", ""), DEEPSEEK_API_KEY)
    _model_clients[_mname] = LLMClient(
        config=LLMConfig(
            api_key=_mkey,
            base_url=_mpreset.get("base_url", DEEPSEEK_BASE_URL),
            model=_mpreset.get("model", "deepseek-chat"),
        )
    )
logger.info(f"📦 已加载 {len(_model_clients)} 个模型客户端: {', '.join(_model_clients.keys())}")

skill_registry = SkillRegistry()
from skills import get_all_skills
for skill in get_all_skills():
    skill_registry.register(skill)

session_manager = SessionManager()  # JsonFileStore 持久化（目录: ./sessions）  # 纯内存模式，不走 JsonFileStore

# ── 连贯对话会话池 ──
_conv_sessions: Dict[str, ConversationSession] = {}
_running_task: Optional[asyncio.Task] = None  # 当前正在执行的任务（用于打断）
_active_approval_gates: Dict[str, ApprovalGate] = {}  # session_id -> ApprovalGate（手动模式用）


def _get_or_create_conv_session(session_id: str, model_name: str = "deepseek-v4-pro") -> ConversationSession:
    """获取或创建该会话的 ConversationSession（管理历史 + 调用 AgentLoop）"""
    if session_id not in _conv_sessions:
        _client = _model_clients.get(model_name, llm_client)

        def _make_loop(c=_client, sid=session_id):
            loop = AgentLoop(
                llm_client=c,
                tool_registry=tool_registry,
                max_rounds=200,
            )
            # 注册 approval gate 到全局（供手动审批 API 访问）
            if hasattr(loop, '_approval_gate') and loop._approval_gate:
                _active_approval_gates[sid] = loop._approval_gate
            return loop

        _conv_sessions[session_id] = ConversationSession(
            loop_factory=_make_loop,
            session_id=session_id,
        )
        logger.info(f"🧵 创建会话: {session_id[:12]}... (模型: {model_name})")
    return _conv_sessions[session_id]


# ====================================================================
# 工具执行脚本
# ====================================================================
def run_tool_locally(tool_name: str, **kwargs) -> Dict[str, Any]:
    """在服务器进程内直接执行工具（不经过 LLM）。v3: 使用 registry.execute() 原子路由。"""
    import asyncio
    call_id = f"local_{uuid.uuid4().hex[:8]}"
    try:
        result = asyncio.get_event_loop().run_until_complete(
            tool_registry.execute(tool_name, call_id, **kwargs)
        ) if asyncio.get_event_loop().is_running() else asyncio.run(
            tool_registry.execute(tool_name, call_id, **kwargs)
        )
    except RuntimeError:
        result = asyncio.run(tool_registry.execute(tool_name, call_id, **kwargs))
    if result.error:
        raise RuntimeError(f"工具 {tool_name} 执行失败: {result.error}")
    return {"result": result.content}


def parse_tool_call(tool_use: Any) -> Dict[str, Any]:
    """统一解析工具调用格式。"""
    if hasattr(tool_use, "name") and hasattr(tool_use, "arguments"):
        return {"name": tool_use.name, "arguments": tool_use.arguments}
    if isinstance(tool_use, dict):
        return {"name": tool_use.get("name", ""), "arguments": tool_use.get("arguments", {})}
    return {"name": str(tool_use), "arguments": {}}


def _format_speed_info(elapsed: float, total_tokens: int) -> str:
    return f"{elapsed:.1f}s" + (f", {total_tokens} tokens" if total_tokens else "")


async def _kimi_web_search(query: str) -> Optional[str]:
    """使用 Kimi (Moonshot) API 进行智能搜索"""
    api_key = os.environ.get("MOONSHOT_API_KEY", "")
    if not api_key or "在这里填" in api_key:
        logger.warning("⚠️ MOONSHOT_API_KEY 未配置，跳过 Kimi 搜索")
        return None

    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=api_key, base_url="https://api.moonshot.cn/v1")
        resp = await client.chat.completions.create(
            model="moonshot-v1-128k",
            messages=[{"role": "user", "content": query}],
            tools=[{"type": "builtin_function", "function": {"name": "$web_search"}}],
            temperature=0.3,
            max_tokens=4096,
        )
        content = resp.choices[0].message.content or ""
        return content
    except ImportError:
        logger.warning("⚠️ openai 库未安装，无法使用 Kimi 搜索")
        return None
    except Exception as e:
        logger.warning(f"⚠️ Kimi 搜索调用失败: {e}")
        return None


# ====================================================================
# SSE 事件流
# ====================================================================
async def event_generator(
    user_input: str,
    session_id: Optional[str] = None,
    model_name: Optional[str] = None,
    search_enabled: bool = False,
) -> AsyncGenerator[str, None]:
    """生成 SSE 事件流。"""
    session = await session_manager.get_or_create_session(session_id) if session_id else await session_manager.create_session()
    history = session.messages

    # 发第一条消息前先返回 session_id（前端需要知道新创建的会话 id）
    yield f"data: {json.dumps({'type': 'session', 'session_id': session.session_id})}\n\n"

    # ── 智能搜索：调用 Kimi 搜索并注入上下文 ──
    if search_enabled:
        yield f"data: {json.dumps({'type': 'info', 'content': '🔍 正在搜索网络信息...'})}\n\n"
        try:
            kimi_result = await _kimi_web_search(user_input)
            if kimi_result:
                history.append({
                    "role": "system",
                    "content": f"以下是通过 Kimi 搜索引擎获取的参考资料，请基于这些信息回答用户问题：\n\n{kimi_result}",
                })
                logger.info(f"✅ Kimi 搜索完成，注入 {len(kimi_result)} 字上下文")
            else:
                logger.warning("⚠️ Kimi 搜索未返回结果")
        except Exception as e:
            logger.warning(f"⚠️ Kimi 搜索失败: {e}")

    try:
        start_time = time.time()

        # 先发一条分析中的信息
        yield f"data: {json.dumps({'type': 'info', 'content': '🧠 正在分析任务...'})}\n\n"

        # ── 定义 SSE 事件回调 ──
        sse_queue: List[str] = []

        async def on_event(event_type: str, data: Dict):
            sse_data = {"type": event_type}
            if event_type == "tool_call":
                sse_data["name"] = data.get("tool", "")
                sse_data["status"] = "running"
                sse_data["args"] = data.get("args", {})
                sse_data["result"] = ""
                logger.info(f"⚡ 工具调用: {data.get('tool', '?')}")
            elif event_type == "tool_result":
                sse_data["name"] = data.get("tool", "")
                sse_data["status"] = "done"
                sse_data["args"] = data.get("args", {})
                sse_data["result"] = str(data.get("result", ""))[:3000]
                logger.debug(f"✅ 工具完成: {data.get('tool', '?')}")
            elif event_type == "tool_error":
                sse_data["name"] = data.get("tool", "")
                sse_data["status"] = "error"
                sse_data["args"] = data.get("args", {})
                sse_data["result"] = str(data.get("error", ""))[:3000]
                logger.warning(f"❌ 工具失败: {data.get('tool', '?')}: {data.get('error', '')[:100]}")
            elif event_type == "round_start":
                sse_data["content"] = f"第 {data.get('round', '?')} 轮执行中..."
            elif event_type == "planning":
                sse_data["content"] = data.get("content", "分析任务中...")
            elif event_type == "done":
                sse_data["content"] = data.get("content", f"执行完成，共 {data.get('rounds', 0)} 轮")
            elif event_type == "approval":
                # Claude Code 审批事件 — 直接透传所有字段
                sse_data.update(data)
                auto = data.get("auto_approved", True)
                logger.info(f"🔓 审批{'(自动)' if auto else '(等待)'}: {data.get('tool', '?')}")
            elif event_type == "todo_update":
                # Claude Code Todo 更新 — 直接透传
                sse_data.update(data)
                logger.debug(f"📋 Todo 更新: {len(data.get('todos', []))} 项")

            sse_queue.append(json.dumps(sse_data, ensure_ascii=False))

        # ── 通过 ConversationSession 执行（自动管理对话历史） ──
        conv_session = _get_or_create_conv_session(session.session_id, model_name or "deepseek-v4-pro")
        if not conv_session._messages:
            # 如果 Session 有持久化历史，导入
            if history and len(history) > 0:
                conv_session.import_history(history)
                # 恢复压缩状态
                if session.summary:
                    conv_session.compressed_summary = session.summary
                    conv_session.compressed_until = session.metadata.get("compressed_until", 0)
                logger.info(f"🧵 导入 {len(history)} 条历史, compressed_until={conv_session.compressed_until}")

        global _running_task
        process_task = asyncio.create_task(
            conv_session.chat(
                task=user_input,
                on_event=on_event,
                model=model_name,
            )
        )
        _running_task = process_task

        try:
            # 轮询 sse_queue 直到任务完成
            while True:
                while sse_queue:
                    payload = sse_queue.pop(0)
                    yield f"data: {payload}\n\n"
                if process_task.done():
                    await asyncio.sleep(0.05)
                    while sse_queue:
                        payload = sse_queue.pop(0)
                        yield f"data: {payload}\n\n"
                    break
                await asyncio.sleep(0.05)

            result = process_task.result()

        except asyncio.CancelledError:
            if not process_task.done():
                process_task.cancel()
                try:
                    await process_task
                except asyncio.CancelledError:
                    pass
            return
        finally:
            if _running_task is process_task:
                _running_task = None

        elapsed = time.time() - start_time
        answer = result.get("content", "处理完成。")
        logger.info(f"✅ 执行完成 ({_format_speed_info(elapsed, 0)})")

        # ── 将对话同步回 Session（持久化）──
        # 只存 user/assistant/tool 消息，system/压缩摘要在内存中管理
        if conv_session._messages:
            display = [m for m in conv_session._messages if m.get("role") != "system"]
            history.clear()
            history.extend(display)
        else:
            history.append({"role": "assistant", "content": answer})

        # 压缩状态持久化到 Session
        session.summary = conv_session.compressed_summary
        session.metadata["compressed_until"] = conv_session.compressed_until

        # 自动生成标题（从首条用户消息提取）
        if not session.title:
            for m in session.messages:
                if m.get("role") == "user" and m.get("content"):
                    title_text = m["content"].strip().replace("\n", " ")[:40]
                    if len(m["content"]) > 40:
                        title_text += "..."
                    session.title = title_text
                    break

        await session_manager.save_session(session)

    except Exception as e:
        elapsed = time.time() - start_time
        tb = traceback.format_exc()
        logger.error(f"❌ 执行失败: {e}\n{tb}")

        try:
            yield f"data: {json.dumps({'type': 'error', 'content': str(e) or '执行失败'})}\n\n"
        except Exception:
            yield f"data: {json.dumps({'type': 'error', 'content': '执行失败'})}\n\n"

    finally:
        yield "data: [DONE]\n\n"


# ====================================================================
# API 路由
# ====================================================================
@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest):
    return StreamingResponse(
        event_generator(req.message.strip(), session_id=req.session_id, model_name=req.model, search_enabled=req.search_enabled),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ───────────── Session API ─────────────
@app.get("/api/sessions")
async def list_sessions():
    sessions = await session_manager.list_sessions()
    result = []
    for s in sessions:
        session_id = s.get("session_id", s.get("id", ""))
        result.append({
            "id": session_id,
            "session_id": session_id,
            "title": s.get("title", f"对话 {session_id[:8]}"),
            "created_at": s.get("created_at", ""),
            "updated_at": s.get("updated_at", ""),
            "message_count": s.get("messages", 0),
        })
    return result


@app.post("/api/sessions")
async def create_session():
    session = await session_manager.create_session()
    return {"id": session.session_id, "session_id": session.session_id}


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    session = await session_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    # 过滤掉 system 消息，只返回用户可见的对话
    display_msgs = [m for m in session.messages if m.get("role") != "system"]
    return {
        "id": session_id,
        "messages": display_msgs,
        "summary": session.summary,
    }


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    await session_manager.delete_session(session_id)
    return {"status": "deleted"}


class UpdateSessionRequest(BaseModel):
    title: Optional[str] = None


@app.patch("/api/sessions/{session_id}")
async def update_session(session_id: str, body: UpdateSessionRequest):
    session = await session_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if body.title:
        session.title = body.title
    return {"id": session_id, "title": session.title}


@app.post("/api/chat/interrupt")
async def interrupt_chat():
    """打断当前正在执行的对话。"""
    global _running_task
    if _running_task and not _running_task.done():
        _running_task.cancel()
        logger.info("⏹️ 收到打断请求，正在取消当前任务")
        return {"status": "cancelled"}
    return {"status": "no_task"}


class ApprovalRequest(BaseModel):
    session_id: str
    call_id: str
    approved: bool


@app.post("/api/approval/respond")
async def approval_respond(body: ApprovalRequest):
    """响应审批请求（手动模式下前端调用）。"""
    gate = _active_approval_gates.get(body.session_id)
    if not gate:
        raise HTTPException(status_code=404, detail="No pending approval for this session")

    if body.approved:
        ok = gate.approve(body.call_id)
    else:
        ok = gate.deny(body.call_id)

    if ok:
        logger.info(f"{'✅ 批准' if body.approved else '❌ 拒绝'}: call_id={body.call_id[:12]}")
        return {"status": "ok", "approved": body.approved}
    else:
        raise HTTPException(status_code=404, detail=f"No pending approval with call_id={body.call_id[:12]}")


@app.get("/api/config/keys")
async def get_config_keys():
    def _mask_key(key: str) -> str:
        if not key or len(key) < 8:
            return ""
        return key[:6] + "****" + key[-4:]

    def _is_placeholder(key: str) -> bool:
        return "在这里填" in key if key else False

    return {
        "providers": [
            {
                "id": "deepseek",
                "name": "DeepSeek",
                "configured": bool(DEEPSEEK_API_KEY) and not _is_placeholder(DEEPSEEK_API_KEY),
                "masked": _mask_key(DEEPSEEK_API_KEY),
            },
            {
                "id": "moonshot",
                "name": "Moonshot (Kimi 搜索)",
                "configured": bool(MOONSHOT_API_KEY) and not _is_placeholder(MOONSHOT_API_KEY),
                "masked": _mask_key(MOONSHOT_API_KEY),
            },
        ]
    }


class SaveKeyRequest(BaseModel):
    provider_id: str
    api_key: str


@app.post("/api/config/keys")
async def save_api_key(body: SaveKeyRequest):
    global DEEPSEEK_API_KEY, MOONSHOT_API_KEY
    if body.provider_id == "deepseek":
        DEEPSEEK_API_KEY = body.api_key
        os.environ["DEEPSEEK_API_KEY"] = body.api_key
        # 同时更新 LLM 客户端
        for _mname, _mclient in _model_clients.items():
            _mclient.config.api_key = body.api_key
            _mclient._client.api_key = body.api_key
    elif body.provider_id == "moonshot":
        MOONSHOT_API_KEY = body.api_key
        os.environ["MOONSHOT_API_KEY"] = body.api_key
    logger.info(f"🔑 API Key 已更新: {body.provider_id}")
    return {"status": "ok"}


@app.get("/api/status")
async def status():
    return {"status": "running", "uptime": time.time()}


# ───────────── 静态文件 ─────────────


# ───────────── 静态文件 ─────────────
@app.get("/")
async def serve_index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ====================================================================
# 启动
# ====================================================================
if __name__ == "__main__":
    import uvicorn
    print("=" * 56)
    print("  Jarvis V3 (DAG 架构) 启动中...")
    print(f"  {'访问':>9}: http://localhost:8000")
    print("=" * 56)
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
