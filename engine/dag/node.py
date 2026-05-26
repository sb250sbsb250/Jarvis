"""
dag/node.py — DAG 执行图节点定义

Node 是最小执行单元，每个 Node 有：
  - name: 唯一标识
  - execute(ctx, inputs) -> outputs: 执行逻辑
  - on_error: 可选的错误回调

内置节点类型：
  - LLMNode: LLM 调用
  - ToolNode: 工具调用
  - RouterNode: 条件路由
  - ParallelNode: 并行分支
  - HumanInLoopNode: 人工审批
"""

from __future__ import annotations

import asyncio
import difflib
import fnmatch
import json
import logging
import os
import shutil
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, AsyncIterator, TYPE_CHECKING

if TYPE_CHECKING:
    from .context import ExecutionContext

logger = logging.getLogger(__name__)


# ── 数据单元 ──

@dataclass
class NodeInput:
    """节点输入"""
    data: Any
    metadata: Dict[str, Any] = field(default_factory=dict)
    source_node: Optional[str] = None
    source_port: str = "default"


@dataclass
class NodeOutput:
    """节点输出"""
    data: Any
    metadata: Dict[str, Any] = field(default_factory=dict)
    error: Optional[Exception] = None
    port: str = "output"

    @classmethod
    def ok(cls, data: Any, port: str = "output", **metadata) -> "NodeOutput":
        return cls(data=data, port=port, metadata=metadata)

    @classmethod
    def fail(cls, error: str, port: str = "output") -> "NodeOutput":
        return cls(data=None, error=Exception(error), port=port)

    @property
    def is_ok(self) -> bool:
        return self.error is None

    @property
    def is_error(self) -> bool:
        return self.error is not None


# ── 节点基类 ──

class Node(ABC):
    """执行图的最小单元"""

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @property
    def node_type(self) -> str:
        return self.__class__.__name__

    @abstractmethod
    async def execute(
        self,
        ctx: "ExecutionContext",
        inputs: Dict[str, NodeInput],
    ) -> Dict[str, NodeOutput]:
        """
        执行节点。

        Args:
            ctx: 执行上下文
            inputs: 按端口名索引的上游输入

        Returns:
            按端口名索引的输出，供下游节点消费
        """
        ...

    async def on_error(self, ctx: "ExecutionContext", error: Exception) -> None:
        """节点执行出错回调（可选覆盖）"""
        logger.error(f"[{self.name}] 节点出错: {error}")

    def __repr__(self) -> str:
        return f"<{self.node_type} '{self.name}'>"


# ── 内置节点类型 ──

class LLMNode(Node):
    """LLM 调用节点"""

    def __init__(
        self,
        name: str,
        system_prompt: str = "",
        temperature: float = 0.7,
        max_tokens: int = 4096,
        model: Optional[str] = None,
        stream: bool = False,
    ):
        self._name = name
        self.system_prompt = system_prompt
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.model = model
        self.stream = stream

    @property
    def name(self) -> str:
        return self._name

    async def execute(
        self,
        ctx: "ExecutionContext",
        inputs: Dict[str, NodeInput],
    ) -> Dict[str, NodeOutput]:
        user_msg = inputs.get("messages", inputs.get("default", inputs.get("input")))

        # 构建 LLM 消息
        messages = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})

        if isinstance(user_msg.data, str):
            messages.append({"role": "user", "content": user_msg.data})
        elif isinstance(user_msg.data, list):
            messages.extend(user_msg.data)
        elif isinstance(user_msg.data, dict) and "messages" in user_msg.data:
            messages.extend(user_msg.data["messages"])
        elif hasattr(user_msg.data, "get_for_llm"):
            messages.extend(user_msg.data.get_for_llm())
        else:
            messages.append({"role": "user", "content": str(user_msg.data)})

        # 获取工具定义（如果有）
        tools_input = inputs.get("tools")
        tools = tools_input.data if tools_input else None

        # 调用 LLM
        try:
            start = asyncio.get_event_loop().time()
            response = await ctx.llm_client.chat_completion(
                messages=messages,
                tools=tools,
                stream=False,
                model=self.model,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )
            elapsed = (asyncio.get_event_loop().time() - start) * 1000

            choice = response.get("choices", [{}])[0]
            msg = choice.get("message", {})
            content = msg.get("content", "")
            tool_calls_raw = msg.get("tool_calls", [])
            usage = response.get("usage", {})

            # 记录追踪信息
            ctx.record(
                "llm_call",
                node=self.name,
                tokens_prompt=usage.get("prompt_tokens", 0),
                tokens_completion=usage.get("completion_tokens", 0),
                duration_ms=round(elapsed, 1),
                has_tools=bool(tools),
                content_preview=content[:100] if content else "",
            )

            return {
                "output": NodeOutput.ok({
                    "content": content,
                    "tool_calls": tool_calls_raw,
                    "usage": usage,
                }),
                "content": NodeOutput.ok(content),
                "tool_calls": NodeOutput.ok(tool_calls_raw),
            }
        except Exception as e:
            logger.exception(f"[{self.name}] LLM 调用失败: {e}")
            return {"output": NodeOutput.fail(str(e))}


class ToolNode(Node):
    """工具调用节点（支持并行）"""

    def __init__(self, name: str, tool_name: str, timeout: float = 30.0):
        self._name = name
        self.tool_name = tool_name
        self.timeout = timeout

    @property
    def name(self) -> str:
        return self._name

    async def execute(
        self,
        ctx: "ExecutionContext",
        inputs: Dict[str, NodeInput],
    ) -> Dict[str, NodeOutput]:
        params_input = inputs.get("params", inputs.get("arguments", inputs.get("default", inputs.get("input"))))
        params = params_input.data if isinstance(params_input.data, dict) else {}

        # 如果输入是 tool_calls 格式，从中提取参数
        if not params and isinstance(params_input.data, list):
            for tc in params_input.data:
                if isinstance(tc, dict) and tc.get("function", {}).get("name") == self.tool_name:
                    try:
                        raw_args = tc.get("function", {}).get("arguments", "{}")
                        if isinstance(raw_args, str):
                            params = json.loads(raw_args)
                        else:
                            params = raw_args
                    except json.JSONDecodeError:
                        params = {}
                    break

        tool = ctx.tool_registry.get(self.tool_name)
        if not tool:
            return {"output": NodeOutput.fail(f"工具 '{self.tool_name}' 未注册")}

        # 参数校验
        is_valid, error_msg = tool.validate_args(params)
        if not is_valid:
            return {"output": NodeOutput.fail(error_msg or "参数无效")}

        # 执行
        try:
            start = asyncio.get_event_loop().time()
            result = await asyncio.wait_for(
                tool.execute(call_id=f"dag_{ctx.request_id}", **params),
                timeout=self.timeout,
            )
            elapsed = (asyncio.get_event_loop().time() - start) * 1000

            ctx.record(
                "tool_call",
                node=self.name,
                tool=self.tool_name,
                duration_ms=round(elapsed, 1),
                success=result.is_success(),
                preview=str(result.content)[:200] if result.content else "",
            )

            if result.is_success():
                return {
                    "output": NodeOutput.ok(result.content),
                    "result": NodeOutput.ok(result),
                }
            else:
                return {"output": NodeOutput.fail(result.error_message or "执行失败")}
        except asyncio.TimeoutError:
            return {"output": NodeOutput.fail(f"工具执行超时 ({self.timeout}s)")}
        except Exception as e:
            logger.exception(f"[{self.name}] 工具异常: {e}")
            return {"output": NodeOutput.fail(str(e))}


class RouterNode(Node):
    """路由节点——根据输入决定走哪条边

    Args:
        name: 节点名
        routes: 路由表 {条件名: 目标节点名}
        default_route: 默认路由键名（没有匹配时使用）
        route_port: 输出端口名（与 ConditionalEdge 的 route_port 对应）
    """

    def __init__(
        self,
        name: str,
        routes: Optional[Dict[str, str]] = None,
        default_route: str = "completed",
        route_port: str = "route",
    ):
        self._name = name
        self.routes = routes or {}
        self.default_route = default_route
        self.route_port = route_port

    @property
    def name(self) -> str:
        return self._name

    @property
    def node_type(self) -> str:
        return "RouterNode"

    async def execute(
        self,
        ctx: "ExecutionContext",
        inputs: Dict[str, NodeInput],
    ) -> Dict[str, NodeOutput]:
        # 收集所有输入数据
        data = None
        tool_calls_data = None

        if "tool_calls" in inputs:
            tc = inputs["tool_calls"].data
            if tc:
                tool_calls_data = tc

        for port_name in ["output", "content", "default", "input"]:
            if port_name in inputs:
                data = inputs[port_name].data
                break

        if data is None:
            data = next(iter(inputs.values()), NodeInput(data=None)).data

        # 构造包含 tool_calls 的决策上下文
        decision_data = data
        if isinstance(data, dict) and tool_calls_data:
            decision_data = {**data, "tool_calls": tool_calls_data}
        elif tool_calls_data and not isinstance(data, dict):
            decision_data = {"content": data, "tool_calls": tool_calls_data}

        target = await self._decide_route(ctx, decision_data)

        ctx.record("route", node=self.name, target=target)

        return {
            self.route_port: NodeOutput.ok(target, port=self.route_port),
            "output": NodeOutput.ok(decision_data),
        }

    async def _decide_route(self, ctx: "ExecutionContext", data: Any) -> str:
        """决定路由策略名（如 "executing"、"completed"、"pos"、"neg"）"""
        # 数值路由
        if isinstance(data, (int, float)):
            if data > 0 and "pos" in self.routes:
                return "pos"
            if data < 0 and "neg" in self.routes:
                return "neg"
            if data == 0 and "zero" in self.routes:
                return "zero"

        # 字符串路由：前缀匹配
        if isinstance(data, str):
            for condition in self.routes:
                if data.strip().lower().startswith(condition.lower()):
                    return condition

        # 字典路由
        if isinstance(data, dict):
            # 优先使用传入的 route_key 同名字段
            for key in [self.route_port, "intent", "decision"]:
                route_val = data.get(key)
                if route_val and str(route_val) in self.routes:
                    return str(route_val)
            # 有 tool_calls → executing
            if data.get("tool_calls"):
                return "executing"

        # 默认路由
        if self.default_route in self.routes:
            return self.default_route
        return next(iter(self.routes.keys()))


class ParallelNode(Node):
    """并行执行多个子节点（内部节点，不直接注册到图）"""

    def __init__(self, name: str, branches: List[Node]):
        self._name = name
        self.branches = branches

    @property
    def name(self) -> str:
        return self._name

    async def execute(
        self,
        ctx: "ExecutionContext",
        inputs: Dict[str, NodeInput],
    ) -> Dict[str, NodeOutput]:
        tasks = [branch.execute(ctx, inputs) for branch in self.branches]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        outputs = {}
        for branch, result in zip(self.branches, results):
            if isinstance(result, Exception):
                outputs[branch.name] = NodeOutput(data=None, error=result)
            else:
                # 取第一个非空端口
                if isinstance(result, dict):
                    for port_name in list(result.keys()):
                        outputs[f"{branch.name}_{port_name}"] = result[port_name]
                        break
                else:
                    outputs[branch.name] = NodeOutput(data=result)

        return {"outputs": NodeOutput.ok(outputs)}


class HumanInLoopNode(Node):
    """需要人工审批的节点"""

    def __init__(self, name: str, require_approval: bool = True):
        self._name = name
        self.require_approval = require_approval
        self.approval_state: Optional[bool] = None

    @property
    def name(self) -> str:
        return self._name

    async def execute(
        self,
        ctx: "ExecutionContext",
        inputs: Dict[str, NodeInput],
    ) -> Dict[str, NodeOutput]:
        data = inputs.get("default", inputs.get("input")).data

        if self.require_approval and self.approval_state is None:
            ctx.pending_approval = {
                "node": self.name,
                "data": data,
                "request_id": ctx.request_id,
            }
            from .executor import HumanInterruptError
            raise HumanInterruptError(f"等待审批: {self.name}")

        if self.approval_state is False:
            return {"output": NodeOutput.fail("用户拒绝")}

        return {"output": NodeOutput.ok(data)}

    def approve(self, approved: bool) -> None:
        self.approval_state = approved


class MapNode(Node):
    """Map 节点——对输入列表中的每个元素执行相同子节点"""

    def __init__(self, name: str, sub_node: Node, map_key: str = "item"):
        self._name = name
        self.sub_node = sub_node
        self.map_key = map_key

    @property
    def name(self) -> str:
        return self._name

    async def execute(
        self,
        ctx: "ExecutionContext",
        inputs: Dict[str, NodeInput],
    ) -> Dict[str, NodeOutput]:
        data = inputs.get("default", inputs.get("input")).data

        if not isinstance(data, list):
            return {"output": NodeOutput.fail(f"MapNode 需要 list 输入，收到 {type(data).__name__}")}

        async def process_item(item: Any) -> Any:
            item_ctx = ctx  # 在单个上下文中执行
            item_inputs = {self.map_key: NodeInput(data=item)}
            result = await self.sub_node.execute(item_ctx, item_inputs)
            return {k: v.data for k, v in result.items()}

        tasks = [process_item(item) for item in data]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        processed = []
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                processed.append({"error": str(r), "index": i})
            else:
                processed.append(r)

        return {"output": NodeOutput.ok(processed)}


class ToolDispatchNode(Node):
    """
    工具分发节点——接收 LLM 的 tool_calls 输出，拆分为独立工具调用。

    它不调用 LLM，而是解析上游 LLM 输出的 tool_calls，
    然后逐个派发给对应的 ToolNode。
    """

    def __init__(self, name: str = "tool_dispatch"):
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def node_type(self) -> str:
        return "ToolDispatchNode"

    async def execute(
        self,
        ctx: "ExecutionContext",
        inputs: Dict[str, NodeInput],
    ) -> Dict[str, NodeOutput]:
        """将 LLM 的 tool_calls 分拆为独立的工具调用"""
        data = inputs.get("default", inputs.get("input"))
        content = data.data

        if isinstance(content, dict):
            tool_calls = content.get("tool_calls", content.get("function_calls", []))
        elif isinstance(content, list):
            tool_calls = content
        else:
            tool_calls = []

        if not tool_calls:
            return {
                "output": NodeOutput.ok({"tool_calls": []}),
                "route": NodeOutput.ok("none"),
            }

        # 为每个 tool_call 创建一个独立输出
        results = {}
        for tc in tool_calls:
            if isinstance(tc, dict):
                func = tc.get("function", tc)
                name = func.get("name", "unknown")
                raw_args = func.get("arguments", "{}")
                if isinstance(raw_args, str):
                    try:
                        args = json.loads(raw_args)
                    except json.JSONDecodeError:
                        args = {}
                else:
                    args = raw_args
            else:
                name = str(tc)
                args = {}

            results[f"tool_{name}"] = {
                "tool_name": name,
                "arguments": args,
                "raw": tc,
            }

        return {
            "output": NodeOutput.ok(results),
            "tool_calls": NodeOutput.ok(results),
        }


# ═══════════════════════════════════════
#  批处理节点
# ═══════════════════════════════════════

class ListFilesNode(Node):
    """列出目录中匹配模式的文件"""

    def __init__(self, name: str = "list_files"):
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def node_type(self) -> str:
        return "ListFilesNode"

    async def execute(
        self,
        ctx: "ExecutionContext",
        inputs: Dict[str, NodeInput],
    ) -> Dict[str, NodeOutput]:
        folder_input = inputs.get("folder_path", inputs.get("default"))
        folder = folder_input.data if folder_input else "."

        patterns_input = inputs.get("file_patterns", inputs.get("patterns"))
        patterns_str = patterns_input.data if patterns_input else ".jpg,.png,.pdf"
        patterns = [p.strip().lower() for p in patterns_str.split(",")]

        if not os.path.isdir(folder):
            return {"output": NodeOutput.fail(f"文件夹不存在: {folder}")}

        files = []
        for f in sorted(os.listdir(folder)):
            full_path = os.path.join(folder, f)
            if not os.path.isfile(full_path):
                continue
            if any(f.lower().endswith(p) for p in patterns) and not f.startswith("_"):
                files.append({
                    "file_name": f,
                    "file_path": full_path,
                    "folder": folder,
                })

        if not files:
            return {"output": NodeOutput.fail(f"没有匹配的文件: {folder}/*{patterns}")}

        return {
            "output": NodeOutput.ok(files),
            "file_list": NodeOutput.ok(files),
            "count": NodeOutput.ok(len(files)),
        }


class FileProcessorNode(Node):
    """对单个文件执行处理（供 MapNode 调度）"""

    def __init__(
        self,
        name: str,
        processor_func: Any,
        timeout: float = 60.0,
    ):
        self._name = name
        self.processor_func = processor_func
        self.timeout = timeout

    @property
    def name(self) -> str:
        return self._name

    @property
    def node_type(self) -> str:
        return "FileProcessorNode"

    async def execute(
        self,
        ctx: "ExecutionContext",
        inputs: Dict[str, NodeInput],
    ) -> Dict[str, NodeOutput]:
        file_info = inputs.get("item", inputs.get("default"))
        if not file_info or not file_info.data:
            return {"output": NodeOutput.fail("缺少文件信息")}

        data = file_info.data
        file_path = data.get("file_path", "") if isinstance(data, dict) else str(data)
        file_name = data.get("file_name", os.path.basename(file_path)) if isinstance(data, dict) else os.path.basename(file_path)

        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(self.processor_func, file_path),
                timeout=self.timeout,
            )
            if result is None:
                return {"output": NodeOutput.ok({
                    "file_name": file_name, "status": "failed"})}

            if isinstance(result, dict):
                result.setdefault("file_name", file_name)
                result.setdefault("file_path", file_path)
                result["status"] = "success"
            return {"output": NodeOutput.ok(result)}

        except asyncio.TimeoutError:
            return {"output": NodeOutput.ok({
                "file_name": file_name, "status": "timeout"})}
        except Exception as e:
            return {"output": NodeOutput.ok({
                "file_name": file_name,
                "status": "error",
                "error": str(e)[:200],
            })}


# ═══════════════════════════════════════
#  代码编辑节点
# ═══════════════════════════════════════

class CodeSearchNode(Node):
    """项目代码搜索节点"""

    def __init__(self, name: str = "code_search", base_dir: str = "."):
        self._name = name
        self.base_dir = base_dir

    @property
    def name(self) -> str:
        return self._name

    @property
    def node_type(self) -> str:
        return "CodeSearchNode"

    async def execute(
        self,
        ctx: "ExecutionContext",
        inputs: Dict[str, NodeInput],
    ) -> Dict[str, NodeOutput]:
        keyword_input = inputs.get("keyword", inputs.get("default"))
        pattern_input = inputs.get("file_pattern", inputs.get("pattern"))

        keyword = keyword_input.data if keyword_input else ""
        file_pattern = pattern_input.data if pattern_input else "*.py"

        if not keyword:
            return {"output": NodeOutput.fail("缺少搜索关键字")}

        results = []
        base = self.base_dir
        for root, dirs, fnames in os.walk(base):
            dirs[:] = [d for d in dirs
                       if d not in ('.venv', '__pycache__', '.git', 'node_modules', '.idea')]
            for fname in fnames:
                if not fnmatch.fnmatch(fname, file_pattern):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                        for i, line in enumerate(f, 1):
                            if keyword in line:
                                rel = os.path.relpath(fpath, base)
                                results.append({
                                    "file": rel,
                                    "line": i,
                                    "content": line.strip()[:120],
                                })
                except Exception:
                    pass

        ctx.record("code_search", node=self.name, keyword=keyword, results=len(results))

        return {
            "output": NodeOutput.ok(results),
            "results": NodeOutput.ok(results),
            "count": NodeOutput.ok(len(results)),
        }


class CodeEditorNode(Node):
    """
    代码编辑器节点 — 合一操作

    支持操作类型:
      - "read": 读取文件
      - "diff": 预览修改（dryrun）
      - "write": 写入修改（带备份）
      - "rollback": 回滚修改
      - "append": 追加内容到文件末尾
    """

    # 类级别共享编辑历史（跨同一节点名称持久化）
    _shared_history: dict = {}

    def __init__(self, name: str = "code_editor", base_dir: str = "."):
        self._name = name
        self.base_dir = base_dir
        self._edit_counter = 0
        if name not in self._shared_history:
            self._shared_history[name] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def node_type(self) -> str:
        return "CodeEditorNode"

    @property
    def _edit_history(self) -> list:
        return self._shared_history[self._name]

    # ── 内部帮助方法 ──

    def _resolve(self, rel_path: str) -> str:
        return os.path.join(self.base_dir, rel_path)

    def _backup(self, path: str) -> str | None:
        bak_path = path + ".bak"
        try:
            shutil.copy2(path, bak_path)
            return bak_path
        except Exception:
            return None

    @staticmethod
    def _generate_diff(old: str, new: str, path: str) -> str:
        old_lines = old.splitlines(keepends=True)
        new_lines = new.splitlines(keepends=True)
        diff = difflib.unified_diff(
            old_lines, new_lines,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
        )
        return "".join(diff)

    # ── 主入口 ──

    async def execute(
        self,
        ctx: "ExecutionContext",
        inputs: Dict[str, NodeInput],
    ) -> Dict[str, NodeOutput]:
        action_input = inputs.get("action", inputs.get("default"))

        if action_input and isinstance(action_input.data, dict):
            action = action_input.data
        else:
            action = {
                "type": inputs.get("type", NodeInput(data="read")).data,
                "path": inputs.get("path", NodeInput(data="")).data,
                "old_text": inputs.get("old_text", NodeInput(data="")).data,
                "new_text": inputs.get("new_text", NodeInput(data="")).data,
            }

        action_type = action.get("type", "read")
        path = action.get("path", "")
        full_path = self._resolve(path)

        if action_type == "read":
            return await self._do_read(ctx, path, full_path, action)
        elif action_type == "diff":
            return await self._do_diff(ctx, path, full_path, action)
        elif action_type == "write":
            return await self._do_write(ctx, path, full_path, action)
        elif action_type == "rollback":
            return await self._do_rollback(ctx, path, full_path, action)
        elif action_type == "append":
            return await self._do_append(ctx, path, full_path, action)
        else:
            return {"output": NodeOutput.fail(f"未知操作: {action_type}")}

    async def _do_read(self, ctx, path, full_path, action):
        if not os.path.exists(full_path):
            return {"output": NodeOutput.fail(f"文件不存在: {path}")}

        offset = action.get("offset", 1)
        limit = action.get("limit", 200)

        with open(full_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        total = len(lines)
        start = max(0, offset - 1)
        end = min(total, start + limit)
        content = "".join(lines[start:end])

        ctx.record("code_read", node=self.name, path=path, lines=end - start)

        return {
            "output": NodeOutput.ok(content),
            "path": NodeOutput.ok(path),
            "total_lines": NodeOutput.ok(total),
            "start_line": NodeOutput.ok(offset),
            "end_line": NodeOutput.ok(end),
        }

    async def _do_diff(self, ctx, path, full_path, action):
        old_text = action.get("old_text", "")
        new_text = action.get("new_text", "")

        if not old_text:
            return {"output": NodeOutput.fail("缺少 old_text")}
        if not os.path.exists(full_path):
            return {"output": NodeOutput.fail(f"文件不存在: {path}")}

        with open(full_path, "r", encoding="utf-8") as f:
            content = f.read()

        count = content.count(old_text)
        if count == 0:
            return {"output": NodeOutput.fail("未找到匹配文本")}
        if count > 1:
            return {"output": NodeOutput.fail(f"匹配到 {count} 处，请提供更精确的定位")}

        new_content = content.replace(old_text, new_text, 1)
        diff = self._generate_diff(content, new_content, path)

        ctx.record("code_diff", node=self.name, path=path)

        return {
            "output": NodeOutput.ok(diff),
            "diff": NodeOutput.ok(diff),
            "path": NodeOutput.ok(path),
        }

    async def _do_write(self, ctx, path, full_path, action):
        old_text = action.get("old_text", "")
        new_text = action.get("new_text", "")

        if not os.path.exists(full_path):
            return {"output": NodeOutput.fail(f"文件不存在: {path}")}

        with open(full_path, "r", encoding="utf-8") as f:
            content = f.read()

        count = content.count(old_text)
        if count == 0:
            return {"output": NodeOutput.fail("未找到匹配文本")}
        if count > 1:
            return {"output": NodeOutput.fail(f"匹配到 {count} 处，请提供更精确的定位")}

        # 备份
        bak = self._backup(full_path)

        # 写入
        new_content = content.replace(old_text, new_text, 1)
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(new_content)

        # 记录
        self._edit_counter += 1
        self._edit_history.append({
            "id": self._edit_counter,
            "path": path,
            "backup": bak,
            "old_text": old_text,
            "new_text": new_text,
            "time": time.time(),
        })

        ctx.record("code_write", node=self.name, path=path, edit_id=self._edit_counter)

        return {
            "output": NodeOutput.ok({
                "path": path,
                "edit_id": self._edit_counter,
                "backup": bak,
            }),
        }

    async def _do_rollback(self, ctx, path, full_path, action):
        edit_id = action.get("edit_id")

        if edit_id:
            entries = [e for e in self._edit_history if e["id"] == edit_id]
        else:
            entries = [self._edit_history[-1]] if self._edit_history else []

        if not entries:
            return {"output": NodeOutput.fail("没有可回滚的编辑")}

        entry = entries[0]
        bak_path = entry.get("backup")

        if bak_path and os.path.exists(bak_path):
            shutil.copy2(bak_path, self._resolve(entry["path"]))
            ctx.record("code_rollback", node=self.name, path=entry["path"], method="backup")
        else:
            # 反向替换
            target_path = self._resolve(entry["path"])
            if os.path.exists(target_path):
                with open(target_path, "r", encoding="utf-8") as f:
                    content = f.read()
                new_content = content.replace(entry["new_text"], entry["old_text"], 1)
                with open(target_path, "w", encoding="utf-8") as f:
                    f.write(new_content)
                ctx.record("code_rollback", node=self.name, path=entry["path"], method="reverse")

        return {
            "output": NodeOutput.ok({"path": entry["path"], "rolled_back": True}),
        }

    async def _do_append(self, ctx, path, full_path, action):
        new_text = action.get("new_text", action.get("content", ""))
        if not new_text:
            return {"output": NodeOutput.fail("缺少追加内容")}

        # 如果文件不存在就创建
        os.makedirs(os.path.dirname(full_path), exist_ok=True)

        with open(full_path, "a", encoding="utf-8") as f:
            f.write(new_text)
            if not new_text.endswith("\n"):
                f.write("\n")

        ctx.record("code_append", node=self.name, path=path)

        return {
            "output": NodeOutput.ok({"path": path, "appended": True}),
        }


class FileRenameNode(Node):
    """批量重命名文件（带试运行模式）"""

    def __init__(self, name: str = "rename_files"):
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def node_type(self) -> str:
        return "FileRenameNode"

    async def execute(
        self,
        ctx: "ExecutionContext",
        inputs: Dict[str, NodeInput],
    ) -> Dict[str, NodeOutput]:
        data_input = inputs.get("rename_list", inputs.get("default"))
        if not data_input:
            return {"output": NodeOutput.fail("缺少重命名列表")}

        rename_list = data_input.data
        if isinstance(rename_list, dict):
            rename_list = rename_list.get("results", [])
        if not isinstance(rename_list, list):
            combined = rename_list.get("combined", {}) if isinstance(rename_list, dict) else {}
            if combined:
                rename_list = combined.get("results", [])
            else:
                rename_list = [rename_list]

        dry_run = ctx.global_data.get("dry_run", False)
        rename_rule = ctx.global_data.get("rename_rule",
                                          "{file_type}_{index:03d}")
        folder = ctx.global_data.get("folder_path", ".")

        renamed = []
        failed = []

        for idx, item in enumerate(rename_list):
            if isinstance(item, dict) and item.get("status") not in ("success", None):
                continue

            old_name = ""
            old_path = ""

            if isinstance(item, dict):
                old_name = item.get("file_name", "")
                old_path = item.get("file_path", "")
                if old_path and not os.path.isabs(old_path):
                    old_path = os.path.join(folder, old_name)
                if not old_path:
                    old_path = os.path.join(folder, old_name)
            elif isinstance(item, str):
                old_name = os.path.basename(item)
                old_path = item

            if not old_name:
                failed.append("缺少文件名")
                continue

            # 在 dry_run 模式下不检查文件是否存在（只是预览）
            if not dry_run and not os.path.exists(old_path):
                failed.append(f"文件不存在: {old_path}")
                continue

            # 构造新文件名
            try:
                ctx_item = {**item} if isinstance(item, dict) else {}
                new_name = rename_rule.format(index=idx, **ctx_item)
            except (KeyError, ValueError):
                new_name = f"renamed_{idx:03d}_{old_name}"

            new_path = os.path.join(folder, new_name)

            if dry_run:
                renamed.append(f"[DRY RUN] {old_name} → {new_name}")
            else:
                try:
                    os.rename(old_path, new_path)
                    renamed.append(f"{old_name} → {new_name}")
                except Exception as e:
                    failed.append(f"{old_name}: {str(e)[:100]}")

        return {
            "output": NodeOutput.ok({
                "renamed": renamed,
                "renamed_count": len(renamed),
                "failed": failed,
                "failed_count": len(failed),
                "dry_run": dry_run,
            }),
        }
