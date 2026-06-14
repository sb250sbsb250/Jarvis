"""
网络工具（原子工具版）

原子工具:
  web_fetch    — 抓取网页内容
  web_search   — 搜索信息
  kimi_search  — Kimi AI 智能搜索（需要 MOONSHOT_API_KEY）
"""

import os
import re
import logging
from typing import List
from urllib.parse import quote

from engine.tool.base import (
    BaseTool, ToolDefinition, ToolParameter, ToolResult,
    CATEGORY_NETWORK,
)

logger = logging.getLogger(__name__)


class WebTool(BaseTool):
    """网络工具集"""

    def __init__(self):
        self._handlers = {
            "web_fetch": self._handle_fetch,
            "web_search": self._handle_search,
            "kimi_search": self._handle_kimi_search,
        }
        for t in self.tools:
            t.handler = self._handlers.get(t.name)

    @property
    def name(self) -> str:
        return "web"

    @property
    def category(self) -> str:
        return CATEGORY_NETWORK

    @property
    def tools(self) -> List[ToolDefinition]:
        return [
            ToolDefinition(
                name="web_fetch",
                description="""抓取网页内容并提取纯文本（自动去除 HTML 标签和脚本）。

工作流程：web_search 找到 URL → web_fetch 获取详细内容。

使用场景：
- 获取网页文章的正文内容
- 抓取 API 文档
- 获取网页上的结构化信息

不适用场景：
- 搜索信息（先用 web_search 或 kimi_search）
- 需要登录的页面
- 动态渲染的 SPA 页面（JavaScript 生成的内容）""",
                parameters=[
                    ToolParameter("url", "string", "网页的完整 URL（包含 http:// 或 https://）", required=True),
                    ToolParameter("max_chars", "number", "最大字符数，默认 5000。长文章可增加到 10000", required=False),
                ],
                is_read=True,
                examples=[
                    'web_fetch(url="https://example.com")',
                    'web_fetch(url="https://docs.python.org/3/", max_chars=10000)  # 获取更多内容',
                ],
                constraints=[
                    "需要网络连接，超时 15 秒",
                    "无法执行 JavaScript，SPA 页面可能抓取不到动态内容",
                    "返回纯文本（HTML 标签已去除），不是原始 HTML",
                    "如果返回内容不完整，增加 max_chars 参数",
                ],
            ),
            ToolDefinition(
                name="web_search",
                description="""搜索网络信息，返回结果标题、链接和摘要片段。
再用 web_fetch 获取具体页面的详细内容。

使用场景：
- 搜索技术问题和解决方案
- 查找文档和教程
- 获取最新的新闻和信息

不适用场景：
- 需要详细总结和分析 → 用 kimi_search（AI 直接总结）""",
                parameters=[
                    ToolParameter("query", "string", "搜索关键词（越具体越好，建议用中文关键词）", required=True),
                    ToolParameter("count", "number", "返回结果数量，默认 5，最大 10", required=False),
                ],
                is_read=True,
                examples=[
                    'web_search(query="Python 异步编程最佳实践")',
                    'web_search(query="2024年AI发展趋势", count=10)  # 获取更多结果',
                ],
                constraints=[
                    "需要网络连接",
                    "返回的是搜索结果摘要，详细内容需用 web_fetch 获取",
                    "搜索结果可能不完整，如果没找到需要的信息请调整关键词重试",
                ],
            ),
            ToolDefinition(
                name="kimi_search",
                description="""使用 Kimi（Moonshot）AI 智能搜索。
直接返回搜索总结和分析结果，比普通搜索更快更准。

使用场景：
- 需要快速了解一个主题的全面信息
- 需要搜索结果的综合分析而不是原始链接
- web_search 结果不理想时的替代方案

需要配置 MOONSHOT_API_KEY 环境变量。""",
                parameters=[
                    ToolParameter("query", "string", "搜索关键词或问题描述", required=True),
                ],
                is_read=True,
                examples=[
                    'kimi_search(query="2024年人工智能发展趋势")',
                    'kimi_search(query="Python asyncio 和线程池的对比")',
                ],
                constraints=[
                    "需要配置 MOONSHOT_API_KEY 环境变量",
                    "如果未配置 API Key，会返回错误提示",
                    "每次搜索会消耗 API 额度",
                ],
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

    async def _handle_fetch(self, call_id: str, url: str, max_chars: int = 5000) -> ToolResult:
        if not url:
            return ToolResult.fail(call_id, "web_fetch", "需要 url 参数")

        try:
            import httpx
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                resp = await client.get(url, headers=headers)
                resp.encoding = resp.charset or "utf-8"
                text = resp.text

            # 提取纯文本
            text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r'<[^>]+>', '', text)
            text = re.sub(r'\s+', ' ', text).strip()

            if len(text) > max_chars:
                text = text[:max_chars] + f"\n... (共 {len(text)} 字符，已截断)"

            return ToolResult.ok(call_id, "web_fetch", {
                "url": url,
                "content": text,
                "size": len(text),
            })

        except ImportError:
            return ToolResult.fail(call_id, "web_fetch", "需要 httpx: pip install httpx")
        except Exception as e:
            return ToolResult.fail(call_id, "web_fetch", str(e))

    async def _handle_search(self, call_id: str, query: str, count: int = 5) -> ToolResult:
        if not query:
            return ToolResult.fail(call_id, "web_search", "需要 query 参数")

        try:
            import httpx
            encoded = quote(query)
            url = f"https://www.bing.com/search?q={encoded}&count={count}"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                resp = await client.get(url, headers=headers)
                resp.encoding = "utf-8"

            results = []
            for m in re.finditer(
                r'<h2><a[^>]*href="([^"]+)"[^>]*>(.*?)</a></h2>',
                resp.text, re.DOTALL
            ):
                link = m.group(1)
                title = re.sub(r'<[^>]+>', '', m.group(2)).strip()
                if link and title and not link.startswith("https://www.bing.com/"):
                    results.append({"title": title, "url": link})
                    if len(results) >= count:
                        break

            return ToolResult.ok(call_id, "web_search", {
                "query": query,
                "count": len(results),
                "results": results,
            })

        except ImportError:
            return ToolResult.fail(call_id, "web_search", "需要 httpx: pip install httpx")
        except Exception as e:
            return ToolResult.fail(call_id, "web_search", str(e))

    async def _handle_kimi_search(self, call_id: str, query: str) -> ToolResult:
        """使用 Kimi (Moonshot) API 进行智能搜索"""
        if not query:
            return ToolResult.fail(call_id, "kimi_search", "需要 query 参数")

        api_key = os.environ.get("MOONSHOT_API_KEY", "")
        if not api_key or "在这里填" in api_key:
            return ToolResult.fail(call_id, "kimi_search", "未配置 MOONSHOT_API_KEY")

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
            return ToolResult.ok(call_id, "kimi_search", {
                "query": query,
                "content": content,
            })
        except ImportError:
            return ToolResult.fail(call_id, "kimi_search", "需要 openai 库: pip install openai")
        except Exception as e:
            return ToolResult.fail(call_id, "kimi_search", str(e))
