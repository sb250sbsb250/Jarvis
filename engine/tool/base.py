"""
工具基类 - 所有工具必须继承
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field

from ..core.types import ToolResult


@dataclass
class ToolParameter:
    """工具参数定义"""
    name: str
    type: str
    description: str
    required: bool = False
    default: Optional[Any] = None
    enum: Optional[List[Any]] = None

    def to_openai_schema(self) -> Dict:
        schema = {"type": self.type, "description": self.description}
        if self.enum:
            schema["enum"] = self.enum
        if self.default is not None:
            schema["default"] = self.default
        return schema


@dataclass
class ToolSchema:
    """工具完整 Schema"""
    name: str
    description: str
    parameters: List[ToolParameter] = field(default_factory=list)

    def to_openai_tool(self) -> Dict:
        properties = {}
        required = []
        for param in self.parameters:
            properties[param.name] = param.to_openai_schema()
            if param.required:
                required.append(param.name)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                }
            }
        }


class BaseTool(ABC):
    """工具基类"""

    @property
    @abstractmethod
    def name(self) -> str:
        """工具名称"""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """工具描述"""
        ...

    @property
    @abstractmethod
    def parameters(self) -> List[ToolParameter]:
        """参数列表"""
        ...

    @property
    def is_read(self) -> bool:
        """是否为只读工具（不修改文件/状态）。默认 False，子类按需覆盖。"""
        return False

    @property
    def is_write(self) -> bool:
        """是否为写入工具（会修改文件/状态）。默认 False，子类按需覆盖。"""
        return False
    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters=self.parameters,
        )

    @abstractmethod
    async def execute(self, call_id: str, **kwargs) -> ToolResult:
        """执行工具，call_id 由框架传入"""
        ...

    def validate_args(self, args: Dict[str, Any]) -> tuple[bool, Optional[str]]:
        """验证参数，返回 (是否通过, 错误信息)"""
        missing = []
        type_errors = []

        for param in self.parameters:
            if param.required and param.name not in args:
                missing.append(param.name)
                continue

            if param.name in args:
                value = args[param.name]

                if param.type == "string" and not isinstance(value, str):
                    type_errors.append(
                        f"参数 '{param.name}' 必须是字符串，当前是 {type(value).__name__}"
                    )
                elif param.type == "number" and not isinstance(value, (int, float)):
                    type_errors.append(
                        f"参数 '{param.name}' 必须是数字，当前是 {type(value).__name__}"
                    )
                elif param.type == "boolean" and not isinstance(value, bool):
                    type_errors.append(
                        f"参数 '{param.name}' 必须是布尔值，当前是 {type(value).__name__}"
                    )

                if param.enum and value not in param.enum:
                    type_errors.append(
                        f"参数 '{param.name}' 必须是以下之一: {param.enum}"
                    )

        if missing:
            return False, (
                f"缺少必需参数: {', '.join(missing)}。\n"
                f"请提供这些参数后重试。示例: {self._example_args(missing)}\n"
                f"如果你不需要使用此工具，请直接给用户文字回答，"
                f"无需重复调用工具。"
            )

        if type_errors:
            return False, "; ".join(type_errors)

        return True, None

    def _example_args(self, missing: List[str]) -> str:
        """为缺失参数生成调用示例"""
        examples = []
        for name in missing:
            for p in self.parameters:
                if p.name == name:
                    if p.type == "string":
                        examples.append(f'{name}="示例值"')
                    elif p.type == "number":
                        examples.append(f'{name}=0')
                    elif p.type == "boolean":
                        examples.append(f'{name}=true')
                    elif p.enum:
                        examples.append(f'{name}="{p.enum[0]}"')
                    else:
                        examples.append(f'{name}=...')
                    break
        return "{" + ", ".join(examples) + "}"
