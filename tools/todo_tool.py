"""
任务追踪工具（原子工具版）— Claude Code 风格

原子工具:
  todo_write  — LLM 写入/更新任务列表
  todo_list   — 查询当前任务状态

用法:
  LLM 在复杂任务中调用 todo_write 维护进度，
  前端通过 SSE 接收 todo_update 事件实时展示。
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from engine.tool.base import (
    BaseTool, ToolDefinition, ToolParameter, ToolResult,
    CATEGORY_SYSTEM,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════
#  全局 Todo 状态管理器（模块级单例）
# ═══════════════════════════════════════

@dataclass
class TodoItem:
    """单个待办项"""
    id: str
    content: str
    status: str = "pending"    # pending | in_progress | completed | cancelled
    priority: str = "medium"   # low | medium | high
    notes: str = ""

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "content": self.content,
            "status": self.status,
            "priority": self.priority,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "TodoItem":
        return cls(
            id=data.get("id", ""),
            content=data.get("content", ""),
            status=data.get("status", "pending"),
            priority=data.get("priority", "medium"),
            notes=data.get("notes", ""),
        )


class TodoManager:
    """
    全局 Todo 状态管理器

    生命周期与一次 agent_loop.run() 对齐。
    """

    _STATUS_ICONS = {
        "completed": "✅",
        "in_progress": "🔄",
        "pending": "⬜",
        "cancelled": "❌",
    }

    def __init__(self):
        self._items: List[TodoItem] = []
        self._next_id: int = 1

    def write(self, todos: List[Dict]) -> List[TodoItem]:
        """
        覆盖式写入任务列表。

        LLM 每次调用 todo_write 传入完整的任务列表。
        已有项通过 id 匹配更新，新项分配新 id。
        """
        # 构建已有项索引
        existing_by_id = {item.id: item for item in self._items}
        new_items = []

        for todo_data in todos:
            item_id = todo_data.get("id", "")

            if item_id and item_id in existing_by_id:
                # 更新已有项
                existing = existing_by_id[item_id]
                existing.content = todo_data.get("content", existing.content)
                existing.status = todo_data.get("status", existing.status)
                existing.priority = todo_data.get("priority", existing.priority)
                existing.notes = todo_data.get("notes", existing.notes)
                new_items.append(existing)
            else:
                # 新建项
                new_id = item_id or f"t{self._next_id}"
                self._next_id = max(self._next_id, self._parse_id_num(new_id) + 1)
                item = TodoItem(
                    id=new_id,
                    content=todo_data.get("content", ""),
                    status=todo_data.get("status", "pending"),
                    priority=todo_data.get("priority", "medium"),
                    notes=todo_data.get("notes", ""),
                )
                new_items.append(item)

        self._items = new_items
        logger.info(f"📋 Todo: 更新 {len(self._items)} 项任务")
        return self._items

    def list_all(self) -> List[TodoItem]:
        """返回当前所有任务"""
        return list(self._items)

    def get_stats(self) -> Dict:
        """统计信息"""
        total = len(self._items)
        completed = sum(1 for i in self._items if i.status == "completed")
        in_progress = sum(1 for i in self._items if i.status == "in_progress")
        pending = sum(1 for i in self._items if i.status == "pending")
        cancelled = sum(1 for i in self._items if i.status == "cancelled")
        return {
            "total": total,
            "completed": completed,
            "in_progress": in_progress,
            "pending": pending,
            "cancelled": cancelled,
        }

    def format_display(self) -> str:
        """
        Claude Code 风格的格式化输出。

        示例:
          ## Tasks [2/5]
          ✅ 读取源文件
          🔄 分析依赖关系
          ⬜ 修改配置文件
          ⬜ 运行测试
          ⬜ 提交代码
        """
        if not self._items:
            return "（无任务）"

        stats = self.get_stats()
        lines = [f"## Tasks [{stats['completed']}/{stats['total']}]"]

        for item in self._items:
            icon = self._STATUS_ICONS.get(item.status, "⬜")
            lines.append(f"{icon} {item.content}")

        return "\n".join(lines)

    def to_dict_list(self) -> List[Dict]:
        """导出为 dict 列表（用于 SSE 推送）"""
        return [item.to_dict() for item in self._items]

    def reset(self) -> None:
        """重置（新 run 开始时调用）"""
        self._items.clear()
        self._next_id = 1

    @staticmethod
    def _parse_id_num(item_id: str) -> int:
        """从 id 字符串中提取数字部分"""
        import re
        match = re.search(r'(\d+)', item_id)
        return int(match.group(1)) if match else 0


# ═══════════════════════════════════════
#  全局 Todo 状态管理器
# ═══════════════════════════════════════

# 全局默认管理器（向后兼容）
_global_todo_manager = TodoManager()

# 活跃管理器（由 ConversationSession 在每轮 run 前设置）
_active_manager: Optional[TodoManager] = None


def set_active_manager(manager: Optional[TodoManager]) -> None:
    """设置当前活跃的 TodoManager（由 AgentLoop.run() 调用）"""
    global _active_manager
    _active_manager = manager


def get_active_manager() -> TodoManager:
    """获取当前活跃的 TodoManager（活跃管理器 > 全局默认）"""
    return _active_manager if _active_manager is not None else _global_todo_manager


def get_todo_manager() -> TodoManager:
    """获取全局默认 TodoManager（向后兼容）"""
    return _global_todo_manager


# ═══════════════════════════════════════
#  TodoTool — BaseTool 子类
# ═══════════════════════════════════════

class TodoTool(BaseTool):
    """任务追踪工具集"""

    def __init__(self):
        # 延迟获取：每次 execute 时动态获取活跃管理器
        self._handlers = {
            "todo_write": self._handle_write,
            "todo_list": self._handle_list,
        }

    @property
    def _manager(self) -> TodoManager:
        """动态获取当前活跃的 TodoManager"""
        return get_active_manager()

    @property
    def name(self) -> str:
        return "todo"

    @property
    def category(self) -> str:
        return CATEGORY_SYSTEM

    @property
    def tools(self) -> List[ToolDefinition]:
        return [
            ToolDefinition(
                name="todo_write",
                description="""写入或更新完整的任务列表（覆盖式更新）。
用于追踪多步骤任务的进度，让用户和系统都能看到当前进展。

使用场景：
- 复杂任务（5 步以上）开始时创建任务清单
- 每完成一个步骤后更新对应任务状态
- 让用户清晰了解当前进度

每次调用时传入所有任务的完整状态（不是增量更新）。
已有任务通过 id 字段匹配更新，新任务自动分配新 id。""",
                parameters=[
                    ToolParameter(
                        "todos", "array",
                        "任务列表数组，每项包含: content(任务描述), status(状态), priority(可选,优先级), id(可选,更新已有任务时传)。status: pending=待处理/in_progress=进行中/completed=已完成/cancelled=已取消。priority: low/medium/high",
                        required=True,
                    ),
                ],
                examples=[
                    'todo_write(todos=[{"content":"读取文件","status":"completed","priority":"high"},{"content":"修改配置","status":"in_progress","priority":"medium"},{"content":"运行测试","status":"pending","priority":"high"}])',
                    'todo_write(todos=[{"id":"t1","content":"读取文件","status":"completed"},{"content":"新增功能","status":"in_progress"}])  # t1更新,t2新任务',
                ],
                constraints=[
                    "每次调用传入完整列表，不是增量更新（缺失的任务会被删除）",
                    "status 只能是 pending/in_progress/completed/cancelled 之一",
                    "id 字段可选，用于更新已有任务；不传 id 会创建新任务",
                    "复杂任务推荐在开始时创建完整清单，逐步更新状态",
                ],
            ),
            ToolDefinition(
                name="todo_list",
                description="""查询当前任务追踪列表中的所有任务及其完成进度。

使用场景：
- 随时查看当前任务进度
- 确认还有哪些任务未完成""",
                parameters=[],
                is_read=True,
                examples=["todo_list()"],
            ),
        ]

    async def execute(self, call_id: str, tool_name: str, **kwargs) -> ToolResult:
        handler = self._handlers.get(tool_name)
        if not handler:
            return ToolResult.fail(call_id, tool_name, f"未知工具: {tool_name}")
        try:
            return await handler(call_id, **kwargs)
        except Exception as e:
            return ToolResult.fail(call_id, tool_name, str(e))

    async def _handle_write(self, call_id: str, todos: List[Dict] = None, **kwargs) -> ToolResult:
        if todos is None:
            todos = kwargs.get("todos", [])

        if not isinstance(todos, list):
            return ToolResult.fail(call_id, "todo_write", "todos 参数必须是数组")

        items = self._manager.write(todos)
        display = self._manager.format_display()

        # metadata 中携带 todo_update + todo_stats，由 agent_loop 触发 SSE
        return ToolResult.ok(call_id, "todo_write", {
            "status": "updated",
            "total": len(items),
            "stats": self._manager.get_stats(),
            "display": display,
        }, todo_update=self._manager.to_dict_list(), todo_stats=self._manager.get_stats())

    async def _handle_list(self, call_id: str, **kwargs) -> ToolResult:
        items = self._manager.list_all()
        display = self._manager.format_display()
        return ToolResult.ok(call_id, "todo_list", {
            "total": len(items),
            "stats": self._manager.get_stats(),
            "display": display,
        })
