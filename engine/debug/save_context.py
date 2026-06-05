"""
engine/debug/save_context.py — 失败现场保存

每次 Agent 执行失败时，自动保存完整的会话上下文到磁盘，
包括 session_id、用户输入、错误信息、消息历史、执行追踪。

用法：
    from engine.debug.save_context import save_failure_context
    save_failure_context(session_id, user_input, error, messages)
"""

import json
import logging
import os
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEBUG_DIR = Path(os.environ.get("JARVIS_DEBUG_DIR", "./debug_sessions"))
# 最大保留文件数
MAX_KEEP = 50


def ensure_dir():
    """确保调试目录存在"""
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)


def save_failure_context(
    session_id: str,
    user_input: str,
    error: Exception,
    messages: Optional[List] = None,
    extra: Optional[Dict] = None,
) -> str:
    """
    失败时自动保存完整上下文。

    Args:
        session_id: 会话 ID
        user_input: 用户输入
        error: 异常对象
        messages: 最近的消息列表（可选）
        extra: 额外上下文（可选）

    Returns:
        保存的文件路径
    """
    ensure_dir()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    short_id = session_id[:8] if session_id else "unknown"
    filename = f"failure_{timestamp}_{short_id}.json"
    filepath = DEBUG_DIR / filename

    # 将消息转为可序列化格式
    serialized_messages = []
    if messages:
        for m in (messages[-30:] if len(messages) > 30 else messages):
            try:
                serialized_messages.append({
                    "role": getattr(m, "role", "?"),
                    "content": str(getattr(m, "content", ""))[:500],
                    "round_id": getattr(m, "_round_id", -1),
                })
            except Exception:
                serialized_messages.append({"role": "?", "content": str(m)[:500]})

    data = {
        "session_id": session_id,
        "timestamp": timestamp,
        "user_input": user_input,
        "error_type": type(error).__name__,
        "error_message": str(error),
        "traceback": traceback.format_exc(),
        "messages": serialized_messages,
    }

    if extra:
        data["extra"] = extra

    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info(f"💾 失败上下文已保存: {filepath}")
        _cleanup_old()
        return str(filepath)
    except Exception as e:
        logger.warning(f"保存失败上下文出错: {e}")
        return ""


def save_execution_record(
    session_id: str,
    user_input: str,
    success: bool,
    skill_name: str = "",
    duration_ms: float = 0,
    extra: Optional[Dict] = None,
) -> str:
    """
    记录执行记录（无论成功还是失败）。

    成功时保存摘要，失败时保存完整上下文。
    """
    ensure_dir()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    short_id = session_id[:8] if session_id else "unknown"
    status = "success" if success else "failure"
    filename = f"{status}_{timestamp}_{short_id}.json"
    filepath = DEBUG_DIR / filename

    data = {
        "session_id": session_id,
        "timestamp": timestamp,
        "user_input": user_input,
        "success": success,
        "skill_name": skill_name,
        "duration_ms": round(duration_ms, 1),
    }

    if extra:
        data["extra"] = extra

    if not success:
        # 失败时加入系统环境信息
        import platform
        data["system"] = {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cwd": os.getcwd(),
        }

    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        _cleanup_old()
        return str(filepath)
    except Exception:
        return ""


def get_latest_failure() -> Optional[Dict]:
    """获取最近一次失败的完整上下文"""
    ensure_dir()
    failures = sorted(DEBUG_DIR.glob("failure_*.json"), reverse=True)
    if not failures:
        return None
    try:
        with open(failures[0], "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def list_records(limit: int = 20) -> List[Dict]:
    """列出最近的执行记录"""
    ensure_dir()
    records = sorted(DEBUG_DIR.glob("*.json"), reverse=True)[:limit]
    results = []
    for r in records:
        try:
            with open(r, "r", encoding="utf-8") as f:
                data = json.load(f)
            results.append({
                "file": r.name,
                "timestamp": data.get("timestamp", ""),
                "success": data.get("success", data.get("error_type") is None),
                "user_input": data.get("user_input", "")[:80],
                "error_type": data.get("error_type", ""),
            })
        except Exception:
            pass
    return results


def _cleanup_old():
    """清理旧文件"""
    ensure_dir()
    files = sorted(DEBUG_DIR.glob("*.json"), reverse=True)
    if len(files) > MAX_KEEP:
        for f in files[MAX_KEEP:]:
            try:
                f.unlink()
            except Exception:
                pass
