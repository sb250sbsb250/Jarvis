"""
engine/checkpoint.py — Agent 检查点系统

每 N 轮自动保存状态，支持中断恢复。

用法：
    cp = Checkpoint(task_id)
    # 每轮结束后
    cp.save(round_idx, messages, tool_calls_log)
    # 启动时
    if cp.exists():
        state = cp.load()
        # 从断点恢复
"""

import json
import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 检查点存储目录
CHECKPOINT_DIR = Path.home() / ".jarvis" / "checkpoints"


class Checkpoint:
    """Agent 检查点 — 保存/恢复执行状态"""

    def __init__(self, task_id: str, save_dir: Optional[str] = None):
        """
        Args:
            task_id: 任务唯一标识（用于文件命名）
            save_dir: 检查点存储目录，默认 ~/.jarvis/checkpoints/
        """
        # 用 task_id 的 hash 做文件名，避免文件名过长
        task_hash = hashlib.md5(task_id.encode()).hexdigest()[:12]
        self._name = f"{task_hash}"
        self._dir = Path(save_dir or CHECKPOINT_DIR)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / f"{self._name}.json"
        self._last_save_time: float = 0.0

    @property
    def path(self) -> str:
        return str(self._path)

    def exists(self) -> bool:
        """检查是否存在有效的检查点"""
        if not self._path.exists():
            return False
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return data.get("valid", False)
        except Exception:
            return False

    def save(
        self,
        round_idx: int,
        messages: List[Dict],
        tool_calls_log: List,
        findings: List[str],
        edited_files: Optional[List[str]] = None,
    ) -> None:
        """
        保存检查点。

        Args:
            round_idx: 当前轮次
            messages: 消息列表（序列化用）
            tool_calls_log: 工具调用日志
            findings: 发现列表
        """
        # 序列化 messages（移除不可 JSON 序列化的内容）
        safe_messages = []
        for m in messages:
            safe = {}
            for k, v in m.items():
                if isinstance(v, (str, int, float, bool, list, dict, type(None))):
                    safe[k] = v
                else:
                    safe[k] = str(v)
            safe_messages.append(safe)

        data = {
            "valid": True,
            "saved_at": time.time(),
            "round": round_idx,
            "tool_calls_count": len(tool_calls_log),
            "messages": safe_messages,
            "tool_calls_log": [
                r.to_dict() if hasattr(r, "to_dict") else r
                for r in tool_calls_log
            ],
            "findings": findings,
        }


        if edited_files is not None:
            data["edited_files"] = edited_files

        self._path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        self._last_save_time = time.time()
        logger.info(
            f"💾 检查点已保存: 第{round_idx}轮, "
            f"{len(safe_messages)}条消息, "
            f"{self._fmt_size(self._path.stat().st_size)}"
        )

    def load(self) -> Optional[Dict]:
        """加载检查点"""
        if not self._path.exists():
            return None
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            logger.info(
                f"📂 检查点已加载: 第{data.get('round', '?')}轮, "
                f"{len(data.get('messages', []))}条消息"
            )
            return data
        except Exception as e:
            logger.warning(f"检查点加载失败: {e}")
            return None

    def cleanup(self, remove_file: bool = True) -> None:
        """清理检查点"""
        if remove_file and self._path.exists():
            self._path.unlink()
            logger.debug(f"检查点已删除: {self._path}")

    def time_since_save(self) -> float:
        """距离上次保存的秒数"""
        if self._last_save_time == 0 and self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._last_save_time = data.get("saved_at", 0)
            except Exception:
                pass
        if self._last_save_time == 0:
            return float("inf")
        return time.time() - self._last_save_time

    @staticmethod
    def _fmt_size(size: int) -> str:
        if size < 1024:
            return f"{size}B"
        elif size < 1024 * 1024:
            return f"{size/1024:.0f}KB"
        else:
            return f"{size/1024/1024:.1f}MB"

    @staticmethod
    def list_checkpoints() -> List[Dict]:
        """列出所有检查点"""
        cp_dir = Path(CHECKPOINT_DIR)
        if not cp_dir.exists():
            return []
        checkpoints = []
        for f in sorted(cp_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if data.get("valid"):
                    checkpoints.append({
                        "file": f.name,
                        "saved_at": time.strftime(
                            "%Y-%m-%d %H:%M:%S",
                            time.localtime(data.get("saved_at", 0))
                        ),
                        "round": data.get("round", 0),
                        "messages": len(data.get("messages", [])),
                        "tool_calls": data.get("tool_calls_count", 0),
                        "size": f.stat().st_size,
                    })
            except Exception:
                pass
        return checkpoints
