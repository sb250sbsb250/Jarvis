"""
计划/提醒工具 — 简单的调度功能
"""

import logging
import json
import os
from typing import List
from datetime import datetime

from engine.tool.base import BaseTool, ToolParameter
from engine.core.types import ToolResult

logger = logging.getLogger("jarvis.tools.schedule")

_SCHEDULE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "schedules.json")


def _load_schedules():
    try:
        if os.path.exists(_SCHEDULE_FILE):
            with open(_SCHEDULE_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return []


def _save_schedules(schedules):
    os.makedirs(os.path.dirname(_SCHEDULE_FILE), exist_ok=True)
    with open(_SCHEDULE_FILE, "w") as f:
        json.dump(schedules, f, ensure_ascii=False, indent=2)


class ScheduleAddTool(BaseTool):

    def __init__(self, **kwargs):
        pass

    """添加提醒"""

    @property
    def name(self) -> str:
        return "schedule_add"

    @property
    def description(self) -> str:
        return "添加提醒事项"

    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(name="title", type="string", description="提醒标题", required=True),
            ToolParameter(name="time", type="string", description="提醒时间（如 2024-01-01 14:00）", required=True),
            ToolParameter(name="note", type="string", description="备注", required=False, default=""),
        ]

    async def execute(self, call_id, **kwargs) -> ToolResult:
        title = kwargs.get("title", "")
        time = kwargs.get("time", "")
        note = kwargs.get("note", "")
        schedules = _load_schedules()
        item = {"id": len(schedules) + 1, "title": title, "time": time, "note": note, "created": datetime.now().isoformat()}
        schedules.append(item)
        _save_schedules(schedules)
        return ToolResult.success(call_id, self.name, {"id": item["id"], "title": title, "time": time})


class ScheduleListTool(BaseTool):


    @property
    def name(self) -> str:
        return "schedule_list"

    @property
    def description(self) -> str:
        return "列出所有提醒事项"

    @property
    def parameters(self) -> List[ToolParameter]:
        return []

    async def execute(self, call_id, **kwargs) -> ToolResult:
        schedules = _load_schedules()
        return ToolResult.success(call_id, self.name, {"schedules": schedules, "count": len(schedules)})


