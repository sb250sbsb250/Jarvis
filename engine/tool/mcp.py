"""
tool/mcp.py — MCP (Model Context Protocol) 工具适配器

支持通过 MCP 协议自动发现和调用外部工具，无需手动注册。

支持的 transport:
  - stdio: 子进程通信 (mcp-server-filesystem 等官方服务)
  - sse: HTTP SSE 通信 (远程 MCP 服务)

用法:
  # 注册一个 MCP stdio 服务
  registry.register(MCPToolAdapter(
      name="mcp_fs",
      transport="stdio",
      command=["npx", "-y", "@modelcontextprotocol/server-filesystem", "."],
      description="文件系统 MCP 服务"
  ))

  # 注册一个 MCP SSE 服务
  registry.register(MCPToolAdapter(
      name="mcp_remote",
      transport="sse",
      url="http://localhost:3001/mcp",
      description="远程 MCP 服务"
  ))
"""

import asyncio
import json
import logging
import subprocess
from typing import Any, Dict, List, Optional

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

from .base import BaseTool, ToolParameter, ToolResult

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════
#  资源泄漏防护：注册 atexit 清理
# ═════════════════════════════════════==
import atexit as _atexit
_mcp_instances: list = []


def _close_all_mcp():
    """关闭所有 MCP 适配器（atexit 回调）"""
    import asyncio
    for inst in _mcp_instances[:]:
        try:
            if inst._process:
                inst._process.terminate()
                inst._process.wait(timeout=3)
        except Exception:
            pass
        try:
            if inst._client:
                loop = asyncio.new_event_loop()
                loop.run_until_complete(inst._client.aclose())
                loop.close()
        except Exception:
            pass
        inst._initialized = False
    _mcp_instances.clear()


_atexit.register(_close_all_mcp)


class MCPToolAdapter(BaseTool):
    """
    MCP 协议适配器 — 自动发现并代理 MCP 服务的所有工具。

    每个 MCPToolAdapter 实例对应一个 MCP 服务（含多个工具）。
    通过 `__getattr__` 或 `get_tool(name)` 获取子工具。
    """

    def __init__(
        self,
        name: str,
        description: str = "MCP 工具服务",
        transport: str = "stdio",
        command: Optional[List[str]] = None,
        url: Optional[str] = None,
        timeout: float = 30.0,
    ):
        self._name = name
        self._desc = description
        self.transport = transport
        self.command = command or []
        self.url = url
        self.timeout = timeout

        self._process: Optional[subprocess.Popen] = None
        self._client: Optional[httpx.AsyncClient] = None
        self._session_id: Optional[str] = None
        self._tools: List[Dict] = []  # 自动发现的工具列表
        self._initialized = False
        self._finalized = False
        self._stderr_task: Optional[asyncio.Task] = None  # stderr 读取任务

        # 注册到全局清理列表（防泄漏）
        _mcp_instances.append(self)
        self._lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        discovered = len(self._tools)
        if discovered:
            return f"{self._desc} ({discovered} 个子工具 via MCP)"
        return self._desc

    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter("tool", "string", "要调用的子工具名称", required=True),
            ToolParameter("args", "object", "子工具的参数", required=True),
        ]

    async def initialize(self) -> bool:
        """初始化 MCP 连接并自动发现工具"""
        if self._initialized:
            return True

        async with self._lock:
            if self._initialized:
                return True

            try:
                if self.transport == "stdio":
                    await self._init_stdio()
                elif self.transport == "sse":
                    await self._init_sse()
                else:
                    logger.error(f"不支持的 transport: {self.transport}")
                    return False

                # 自动发现工具
                await self._discover_tools()
                self._initialized = True
                logger.info(f"MCP [{self._name}] 初始化完成: {len(self._tools)} 个工具")
                return True

            except Exception as e:
                logger.error(f"MCP [{self._name}] 初始化失败: {e}")
                return False

    async def _init_stdio(self):
        """启动 stdio 子进程"""
        if not self.command:
            raise ValueError("stdio transport 需要 command 参数")

        self._process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        logger.info(f"MCP [{self._name}] stdio 进程已启动: {' '.join(self.command)}")

        # 检测进程是否立即退出（启动失败场景）
        await asyncio.sleep(0.1)
        if self._process.poll() is not None:
            stderr_output = self._process.stderr.read() if self._process.stderr else ""
            raise RuntimeError(
                f"MCP 进程启动失败 (exit={self._process.returncode}): {stderr_output[:500]}"
            )

        # 启动 stderr 读取任务（防止缓冲区满阻塞子进程）
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self):
        """异步读取 stderr，防止管道缓冲区满阻塞子进程"""
        if not self._process or not self._process.stderr:
            return
        try:
            while self._process.poll() is None:
                line = await asyncio.get_event_loop().run_in_executor(
                    None, self._process.stderr.readline
                )
                if not line:
                    break
                line = line.strip()
                if line:
                    logger.debug(f"MCP [{self._name}] stderr: {line[:200]}")
        except Exception as e:
            logger.debug(f"MCP [{self._name}] stderr 读取异常: {e}")

    async def _init_sse(self):
        """初始化 SSE 连接"""
        if not HAS_HTTPX:
            raise ImportError("SSE transport 需要 httpx 库: pip install httpx")
        if not self.url:
            raise ValueError("SSE transport 需要 url 参数")

        self._client = httpx.AsyncClient(timeout=self.timeout)
        resp = await self._client.post(f"{self.url}/session", json={})
        if resp.status_code == 200:
            data = resp.json()
            self._session_id = data.get("sessionId")
            logger.info(f"MCP [{self._name}] SSE 会话已创建: {self._session_id}")

    async def _discover_tools(self):
        """调用 MCP list_tools 方法发现所有工具"""
        result = await self._send_request("list_tools", {})
        if result and "tools" in result:
            self._tools = result["tools"]
        else:
            logger.warning(f"MCP [{self._name}] list_tools 返回空")

    async def _send_request(self, method: str, params: Dict) -> Optional[Dict]:
        """发送 MCP JSON-RPC 请求"""
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        }

        if self.transport == "stdio":
            return await self._send_stdio(request)
        elif self.transport == "sse":
            return await self._send_sse(request)
        return None

    async def _send_stdio(self, request: Dict) -> Optional[Dict]:
        """通过 stdio 发送请求"""
        if not self._process or not self._process.stdin:
            return None

        line = json.dumps(request, ensure_ascii=False) + "\n"
        self._process.stdin.write(line)
        self._process.stdin.flush()

        response_line = self._process.stdout.readline() if self._process.stdout else ""
        if response_line:
            try:
                return json.loads(response_line.strip())
            except json.JSONDecodeError as e:
                logger.error(f"MCP stdio 响应解析失败: {e}, 原始: {response_line[:200]}")
        return None

    async def _send_sse(self, request: Dict) -> Optional[Dict]:
        """通过 SSE HTTP 发送请求"""
        if not self._client:
            return None

        try:
            resp = await self._client.post(
                f"{self.url}/message",
                json=request,
                params={"sessionId": self._session_id} if self._session_id else {},
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            logger.error(f"MCP SSE 请求失败: {e}")
        return None

    async def execute(self, call_id: str, **kwargs) -> ToolResult:
        """执行 MCP 工具的某个子工具"""
        tool_name = kwargs.get("tool", "")
        tool_args = kwargs.get("args", {})

        if not tool_name:
            return ToolResult.error(call_id, self._name, "缺少 tool 参数")

        # 确保已初始化
        if not self._initialized:
            ok = await self.initialize()
            if not ok:
                return ToolResult.error(call_id, self._name, "MCP 初始化失败")

        # 检查工具是否存在
        discovered = {t.get("name", "") for t in self._tools}
        if tool_name not in discovered:
            available = ", ".join(sorted(discovered)) if discovered else "(无)"
            return ToolResult.fail(
                call_id, self._name,
                f"子工具 '{tool_name}' 不存在。可用工具: {available}"
            )

        # 调用 MCP tool_call
        result = await self._send_request("tools/call", {
            "name": tool_name,
            "arguments": tool_args,
        })

        if result is None:
            return ToolResult.error(call_id, self._name, "MCP 调用无响应")

        if "error" in result:
            return ToolResult.error(call_id, self._name, str(result["error"]))

        content = result.get("content", [])
        # MCP 返回的 content 是 content item 列表，提取文本
        texts = []
        for item in content if isinstance(content, list) else [content]:
            if isinstance(item, dict):
                texts.append(item.get("text", str(item)))
            else:
                texts.append(str(item))

        return ToolResult.ok(call_id, self._name, {
            "tool": tool_name,
            "result": "\n".join(texts),
        })

    def get_discovered_tools(self) -> List[Dict]:
        """获取自动发现的所有子工具列表"""
        return self._tools.copy()

    def expand_to_tools(self) -> List["MCPSubTool"]:
        """
        将 MCP 服务的子工具展开为独立的 MCPSubTool 实例。

        这样 LLM 直接看到 `mcp_fs_read`、`mcp_fs_write` 等具体工具，
        而不是一个 `mcp_fs(tool="...")` 二级调用。
        """
        sub_tools = []
        for t in self._tools:
            sub_tool = MCPSubTool(
                parent=self,
                sub_name=t.get("name", ""),
                description=t.get("description", ""),
                input_schema=t.get("inputSchema", {}),
            )
            sub_tools.append(sub_tool)
        return sub_tools

    def get_tool_names(self) -> List[str]:
        """获取自动发现的工具名称列表"""
        return [t.get("name", "") for t in self._tools]

    async def aclose(self):
        """异步关闭（在事件循环中使用，推荐）"""
        if self._finalized:
            return
        self._finalized = True

        # 取消 stderr 读取任务
        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
            self._stderr_task = None

        if self._process:
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
            finally:
                self._process = None

        if self._client:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

        self._initialized = False
        # 从全局清理列表移除
        try:
            _mcp_instances.remove(self)
        except ValueError:
            pass
        logger.info(f"MCP [{self._name}] 已关闭")

    def close(self):
        """同步关闭（在非事件循环中使用）"""
        if self._finalized:
            return
        self._finalized = True

        # 取消 stderr 读取任务（尽力而为）
        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
            self._stderr_task = None

        if self._process:
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
            finally:
                self._process = None

        if self._client:
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                if not loop.is_running():
                    loop.run_until_complete(self._client.aclose())
            except RuntimeError:
                # 没有可用的事件循环，直接跳过
                pass
            self._client = None

        self._initialized = False
        # 从全局清理列表移除
        try:
            _mcp_instances.remove(self)
        except ValueError:
            pass
        logger.info(f"MCP [{self._name}] 已关闭")


class MCPSubTool(BaseTool):
    """
    MCP 子工具包装——每个子工具独立注册，LLM 直接可见。

    由 MCPToolAdapter.expand_to_tools() 生成。
    执行时委托给父 adapter 处理。
    """

    def __init__(
        self,
        parent: MCPToolAdapter,
        sub_name: str,
        description: str = "",
        input_schema: Optional[Dict] = None,
    ):
        self._parent = parent
        self._sub_name = sub_name
        self._desc = description
        self._input_schema = input_schema or {}

    @property
    def name(self) -> str:
        return f"{self._parent.name}_{self._sub_name}"

    @property
    def description(self) -> str:
        return self._desc or f"MCP 子工具: {self._sub_name}"

    @property
    def parameters(self) -> List[ToolParameter]:
        """从 MCP inputSchema 转换为 ToolParameter 列表"""
        params = []
        schema = self._input_schema
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        required_list = schema.get("required", []) if isinstance(schema, dict) else []

        for pname, pinfo in properties.items():
            if isinstance(pinfo, dict):
                ptype = pinfo.get("type", "string")
                pdesc = pinfo.get("description", "")
            else:
                ptype, pdesc = "string", ""
            params.append(ToolParameter(
                name=pname,
                type=ptype,
                description=pdesc,
                required=pname in required_list,
            ))
        return params

    async def execute(self, call_id: str, **kwargs) -> ToolResult:
        """委托 parent 执行"""
        # 先确保 parent 已初始化
        if not self._parent._initialized:
            ok = await self._parent.initialize()
            if not ok:
                return ToolResult.error(call_id, self.name, "MCP 初始化失败")

        result = await self._parent._send_request("tools/call", {
            "name": self._sub_name,
            "arguments": kwargs,
        })

        if result is None:
            return ToolResult.error(call_id, self.name, "MCP 调用无响应")

        if "error" in result:
            return ToolResult.error(call_id, self.name, str(result["error"]))

        content = result.get("content", [])
        texts = []
        for item in content if isinstance(content, list) else [content]:
            if isinstance(item, dict):
                texts.append(item.get("text", str(item)))
            else:
                texts.append(str(item))

        return ToolResult.ok(call_id, self.name, {
            "tool": self._sub_name,
            "result": "\n".join(texts),
        })

    def close(self):
        """委托到 parent 关闭"""
        self._parent.close()
