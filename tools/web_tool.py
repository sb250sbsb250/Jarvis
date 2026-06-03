"""
tools/web_tool.py — 网络工具（合并版）

合并 web_fetch + web_search
"""

import re
import logging
from typing import List
from urllib.parse import quote

from engine.tool.base import BaseTool, ToolParameter, ToolResult

logger = logging.getLogger(__name__)


class WebTool(BaseTool):
    """网络工具 — fetch + search"""

    @property
    def name(self) -> str:
        return "web"

    @property
    def description(self) -> str:
        return (
            "网络操作。action: fetch(抓取网页)/search(搜索)\n"
            "- fetch: url='https://...', max_chars=5000\n"
            "- search: query='关键词', count=5\n"
            "先 search 找信息源，再 fetch 获取详情"
        )

    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter("action", "string", "fetch/search", required=True,
                          enum=["fetch", "search"]),
            ToolParameter("url", "string", "网页URL(fetch用)", required=False),
            ToolParameter("query", "string", "搜索关键词(search用)", required=False),
            ToolParameter("max_chars", "number", "最大字符数(fetch用，默认5000)", required=False),
            ToolParameter("count", "number", "结果数(search用，默认5)", required=False),
        ]

    async def execute(self, call_id: str, **kwargs) -> ToolResult:
        action = kwargs.get("action", "search")

        if action == "fetch":
            return await self._fetch(call_id, kwargs)
        elif action == "search":
            return await self._search(call_id, kwargs)
        else:
            return ToolResult.error(call_id, self.name, f"未知操作: {action}")

    async def _fetch(self, call_id, args):
        url = args.get("url", "")
        max_chars = int(args.get("max_chars", 5000))

        if not url:
            return ToolResult.error(call_id, self.name, "fetch 需要 url")

        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    html = await resp.text()
                    status = resp.status
        except ImportError:
            from urllib.request import urlopen, Request
            req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(req, timeout=15) as resp:
                html = resp.read().decode("utf-8", errors="replace")
                status = resp.status
        except Exception as e:
            return ToolResult.error(call_id, self.name, f"抓取失败: {e}。检查 URL 是否正确")

        # 提取文本
        text = re.sub(r'<(script|style|noscript|iframe|svg)[^>]*>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'&[a-z]+;', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()

        truncated = len(text) > max_chars
        text = text[:max_chars]

        return ToolResult.success(call_id, self.name, {
            "url": url,
            "status": status,
            "size": len(text),
            "content": text,
            "_hint": "内容已截断" if truncated else "如需更多内容，增大 max_chars",
        })

    async def _search(self, call_id, args):
        query = args.get("query", "")
        count = int(args.get("count", 5))

        if not query:
            return ToolResult.error(call_id, self.name, "search 需要 query")

        try:
            import aiohttp
            search_url = f"https://html.duckduckgo.com/html/?q={quote(query)}"
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    search_url,
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    html = await resp.text()
        except ImportError:
            from urllib.request import urlopen, Request
            req = Request(
                f"https://html.duckduckgo.com/html/?q={quote(query)}",
                headers={"User-Agent": "Mozilla/5.0"},
            )
            with urlopen(req, timeout=10) as resp:
                html = resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            return ToolResult.error(call_id, self.name, f"搜索失败: {e}")

        results = []
        for match in re.finditer(
            r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            html, re.DOTALL,
        ):
            url = match.group(1)
            title = re.sub(r'<[^>]+>', '', match.group(2)).strip()
            if url and title and not url.startswith("//duckduckgo.com"):
                results.append({"title": title, "url": url})
                if len(results) >= count:
                    break

        if not results:
            return ToolResult.success(call_id, self.name, {
                "query": query,
                "results": [],
                "_hint": "未找到结果。尝试: 1)简化关键词 2)用英文搜索 3)用 web(action='fetch') 直接访问已知网站",
            })

        return ToolResult.success(call_id, self.name, {
            "query": query,
            "results": results,
            "_hint": f"找到 {len(results)} 条结果。用 web(action='fetch', url='...') 获取详情",
        })
