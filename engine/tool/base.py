"""
工具基类 — v3.0 原子工具架构

核心设计：
  - 每个大类（ExcelTool）是一个命名空间
  - 每个原子工具（excel_read_sheet）有独立名称、描述、参数
  - LLM 直接调用原子工具名，不需要 action 字段
  - Registry 将原子工具扁平化

分类常量：
  CATEGORY_FILE / CATEGORY_CODE / CATEGORY_DATA
  CATEGORY_SYSTEM / CATEGORY_NETWORK / CATEGORY_SECURITY / CATEGORY_VERSION
"""

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Callable, Awaitable, Union

from ..core.types import ToolResult

# ── 标准分类常量 ──
CATEGORY_FILE = "file"
CATEGORY_CODE = "code"
CATEGORY_DATA = "data"
CATEGORY_SYSTEM = "system"
CATEGORY_NETWORK = "network"
CATEGORY_SECURITY = "security"
CATEGORY_VERSION = "version"


@dataclass
class ToolParameter:
    """工具参数定义"""
    name: str
    type: str        # "string" | "number" | "boolean" | "object" | "array"
    description: str
    required: bool = False
    default: Any = None
    enum: Optional[List[str]] = None


@dataclass
class ToolDefinition:
    """原子工具定义

    LLM 直接看到的工具单位。
    每个原子工具有完整独立的 name/description/parameters。
    """
    name: str
    description: str
    parameters: List[ToolParameter]
    examples: List[str] = field(default_factory=list)
    constraints: List[str] = field(default_factory=list)
    is_read: bool = False   # 是否只读（并行执行优化）

    def to_openai_format(self) -> Dict:
        """转换为 OpenAI function calling 格式"""
        properties = {}
        required = []

        for param in self.parameters:
            prop = {
                "type": param.type,
                "description": param.description,
            }
            if param.enum:
                prop["enum"] = param.enum
            if param.default is not None:
                prop["default"] = param.default
            properties[param.name] = prop
            if param.required:
                required.append(param.name)

        # 构建 description（含示例和约束）
        desc_parts = [self.description]
        if self.examples:
            desc_parts.append("\n示例：")
            for ex in self.examples:
                desc_parts.append(f"  {ex}")
        if self.constraints:
            desc_parts.append("\n注意：")
            for c in self.constraints:
                desc_parts.append(f"  - {c}")

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": "\n".join(desc_parts),
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            },
        }


class BaseTool(ABC):
    """工具大类基类

    子类必须实现：
      name       — 大类名称（命名空间，如 "excel"）
      tools      — 原子工具列表

    可选覆盖：
      category   — 分类
      version    — 版本号
    """

    # ── 类型检查映射表 ──
    TYPE_CHECKS = {
        "string": lambda v: isinstance(v, str),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
        "array": lambda v: isinstance(v, list),
        "object": lambda v: isinstance(v, dict),
    }

    TYPE_NAMES = {
        "string": "字符串",
        "number": "数字",
        "integer": "整数",
        "boolean": "布尔值",
        "array": "数组",
        "object": "对象",
    }

    @property
    @abstractmethod
    def name(self) -> str:
        """大类名称（命名空间，不直接暴露给 LLM）"""
        ...

    @property
    @abstractmethod
    def tools(self) -> List[ToolDefinition]:
        """该大类下所有原子工具的定义"""
        ...

    @property
    def category(self) -> str:
        return "general"

    @property
    def version(self) -> str:
        return "3.0.0"

    # ── 工具查找 ──

    def get_tool_def(self, tool_name: str) -> Optional[ToolDefinition]:
        for td in self.tools:
            if td.name == tool_name:
                return td
        return None

    def get_tool_names(self) -> List[str]:
        return [t.name for t in self.tools]

    def is_read_tool(self, tool_name: str) -> bool:
        td = self.get_tool_def(tool_name)
        return td.is_read if td else False

    # ── 参数校验 ──

    def validate_args(self, tool_name: str, args: Dict) -> tuple[bool, Optional[str]]:
        td = self.get_tool_def(tool_name)
        if not td:
            return False, f"未知工具: {tool_name}"

        missing = []
        type_errors = []

        for param in td.parameters:
            if param.required and param.name not in args:
                missing.append(param.name)
                continue

            if param.name in args:
                value = args[param.name]

                # 使用 TYPE_CHECKS 映射表进行类型检查（支持 string/number/boolean/array/object）
                checker = self.TYPE_CHECKS.get(param.type)
                if checker and not checker(value):
                    expected_name = self.TYPE_NAMES.get(param.type, param.type)
                    type_errors.append(
                        f"参数 '{param.name}' 必须是 {expected_name}，当前是 {type(value).__name__}"
                    )

                if param.enum and value not in param.enum:
                    type_errors.append(
                        f"参数 '{param.name}' 必须是以下之一: {param.enum}"
                    )

        if missing:
            return False, f"缺少必需参数: {', '.join(missing)}"
        if type_errors:
            return False, "; ".join(type_errors)
        return True, None

    # ── 统一调度入口 ──

    async def execute(self, call_id: str, tool_name: str, **kwargs) -> ToolResult:
        """执行原子工具

        子类必须实现自己的 execute() 方法做调度。
        此基类方法仅做参数校验和错误包装。
        """
        td = self.get_tool_def(tool_name)
        if not td:
            return ToolResult.fail(call_id, tool_name, f"未知工具: {tool_name}")

        # 参数校验
        ok, err = self.validate_args(tool_name, kwargs)
        if not ok:
            return ToolResult.fail(call_id, tool_name, err)

        return ToolResult.ok(call_id, tool_name, {"message": f"工具 {tool_name} 的 execute() 未被子类实现"})
