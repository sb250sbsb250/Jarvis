"""Jarvis V3 - DAG 智能助手服务器"""
import os, shutil, uuid, logging, json, hashlib, time, yaml, glob, asyncio, traceback
from typing import AsyncGenerator, Optional, List, Dict, Any, Callable
from pathlib import Path
from datetime import datetime

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
from engine.llm_client import LLMClient, LLMConfig
from engine.skill.router import SkillRouter
from engine.skill.registry import SkillRegistry
from tools import register_all_tools
from engine.tool.registry import ToolRegistry
from engine.session.manager import SessionManager

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
MODEL = os.getenv("MODEL", "deepseek-chat")

# ───────────── FastAPI 应用 ─────────────
app = FastAPI(title="Jarvis V3")


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    model: Optional[str] = None


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

skill_registry = SkillRegistry()
from skills import get_all_skills
for skill in get_all_skills():
    skill_registry.register(skill)

agent_loop = AgentLoop(
    llm_client=llm_client,
    tool_registry=tool_registry,
    max_rounds=30,
)

skill_router = SkillRouter(
    llm_client=llm_client,
    skill_registry=skill_registry,
    tool_registry=tool_registry,
)

session_manager = SessionManager()  # 纯内存模式，不走 JsonFileStore


# ====================================================================
# 工具执行脚本
# ====================================================================
def run_tool_locally(tool_name: str, **kwargs) -> Dict[str, Any]:
    """在服务器进程内直接执行工具（不经过 LLM）。"""
    tool_cls = tool_registry.get(tool_name)
    if not tool_cls:
        raise ValueError(f"未知工具: {tool_name}")
    tool = tool_cls()
    result = tool.execute(**kwargs)
    if result.is_error():
        raise RuntimeError(f"工具 {tool_name} 执行失败: {result.error_message}")
    return {"result": result.content}


def parse_tool_call(tool_use: Any) -> Dict[str, Any]:
    """统一解析工具调用格式。"""
    if hasattr(tool_use, "name") and hasattr(tool_use, "arguments"):
        return {"name": tool_use.name, "arguments": tool_use.arguments}
    if isinstance(tool_use, dict):
        return {"name": tool_use.get("name", ""), "arguments": tool_use.get("arguments", {})}
    return {"name": str(tool_use), "arguments": {}}


def _get_base_system_prompt(knowledge: Optional[str] = None) -> str:
    """生成基础系统提示词。"""
    now = datetime.now().strftime("%Y-%m-%d %A %H:%M")
    cwd = Path.cwd().as_posix()

    parts = [
        f"你是 Jarvis V3，一个全能智能助手。",
        f"当前时间: {now}",
        f"当前工作目录(workspace): {cwd}",
    ]

    if knowledge:
        parts.append(f"\n## 项目知识\n{knowledge}")

    parts.append(
        """

## 能力
1. 📁 文件操作 — 创建、编辑、读取文件
2. 🐍 代码执行 — 运行 Python/Shell 脚本
3. 🌐 网页访问 — 抓取网页内容
4. 📊 数据分析 — Excel/CSV 处理
5. 📄 PDF/Word 处理

## 工具使用原则
- 先理解任务，再选择工具
- 工具用完立即返回结果
- 复杂任务拆解成工具调用序列
- 使用中文回答用户

## 执行原则
⚡ 效率要求：
- 一次只做一件事
- 做完必要操作后立即输出结果
- 不要重复读取同一个文件
- 不要做多余的检查
"""
    )

    return "\n".join(parts)


def _build_env_info() -> str:
    """生成运行环境信息。"""
    import sys, platform
    parts = [
        f"## 运行环境",
        f"- 操作系统: {platform.system()} {platform.release()}",
        f"- Python: {sys.version}",
        f"- 工作目录: {Path.cwd()}",
    ]
    return "\n".join(parts)


def _format_speed_info(elapsed: float, total_tokens: int) -> str:
    return f"{elapsed:.1f}s" + (f", {total_tokens} tokens" if total_tokens else "")


# ====================================================================
# SSE 事件流
# ====================================================================
async def event_generator(
    user_input: str,
    session_id: Optional[str] = None,
    model_name: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    """生成 SSE 事件流。"""
    session = await session_manager.get_or_create_session(session_id) if session_id else await session_manager.create_session()
    history = session.messages

    # 发第一条消息前先返回 session_id（前端需要知道新创建的会话 id）
    yield f"data: {json.dumps({'type': 'session', 'session_id': session.session_id})}\n\n"

    # 保存用户消息
    history.add_user(user_input)

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
                sse_data["result"] = str(data.get("result", ""))[:300]
                logger.debug(f"✅ 工具完成: {data.get('tool', '?')}")
            elif event_type == "tool_error":
                sse_data["name"] = data.get("tool", "")
                sse_data["status"] = "error"
                sse_data["args"] = data.get("args", {})
                sse_data["result"] = str(data.get("error", ""))[:300]
                logger.warning(f"❌ 工具失败: {data.get('tool', '?')}: {data.get('error', '')[:100]}")
            elif event_type == "round_start":
                sse_data["content"] = f"第 {data.get('round', '?')} 轮执行中..."
            elif event_type == "planning":
                sse_data["content"] = data.get("content", "分析任务中...")
            elif event_type == "done":
                sse_data["content"] = data.get("content", f"执行完成，共 {data.get('rounds', 0)} 轮")

            sse_queue.append(json.dumps(sse_data, ensure_ascii=False))

        # ── 执行过程，逐条 yield SSE 事件 ──
        # 先启动 skill_router.process 作为后台任务
        process_task = asyncio.create_task(
            skill_router.process(
                user_input=user_input,
                history=history,
                session_id=session_id,
                on_event=on_event,
            )
        )

        # 轮询 sse_queue 直到任务完成
        while True:
            done_flag = False
            # 取出所有已积累的事件
            while sse_queue:
                payload = sse_queue.pop(0)
                yield f"data: {payload}\n\n"
            # 检查任务是否完成
            if process_task.done():
                # 再取一次防止遗漏
                while sse_queue:
                    payload = sse_queue.pop(0)
                    yield f"data: {payload}\n\n"
                break
            await asyncio.sleep(0.05)

        result = process_task.result()

        elapsed = time.time() - start_time
        answer = result.content if result else "处理完成。"
        logger.info(f"✅ 执行完成 ({_format_speed_info(elapsed, 0)})")

        # 保存助手回复
        history.add_assistant(answer)
        await session_manager.save_session(session)

        yield f"data: {json.dumps({'type': 'chunk', 'content': answer})}\n\n"

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
@app.get("/api/chat/stream")
async def chat_stream(
    message: str = Query(""),
    session_id: Optional[str] = Query(None),
    model: Optional[str] = Query(None),
):
    return StreamingResponse(
        event_generator(message.strip(), session_id=session_id, model_name=model),
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
            "title": s.get("title", f"对话 {session_id[:8]}"),
            "created_at": s.get("created_at", ""),
            "updated_at": s.get("updated_at", ""),
            "message_count": s.get("messages", 0),
        })
    return result


@app.post("/api/sessions")
async def create_session():
    session = await session_manager.create_session()
    return {"id": session.session_id}


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    session = await session_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    messages = getattr(session, "messages", [])
    if hasattr(messages, "get_for_llm"):
        messages = messages.get_for_llm()
    return {
        "id": session_id,
        "messages": messages,
    }


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    await session_manager.delete_session(session_id)
    return {"status": "deleted"}


@app.get("/api/config/keys")
async def get_config_keys():
    return {"has_key": bool(DEEPSEEK_API_KEY)}


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
    print(f"  {'访问':>9}: http://localhost:8001")
    print("=" * 56)
    uvicorn.run(app, host="0.0.0.0", port=8001, log_level="info")
