"""
网页工具 — 获取网页内容
"""

import logging
from typing import List

from engine.tool.base import BaseTool, ToolParameter
from engine.core.types import ToolResult

logger = logging.getLogger("jarvis.tools.web")


class WebFetchTool(BaseTool):

    def __init__(self, **kwargs):
        self._timeout = kwargs.get("timeout", 10)

    """获取网页内容"""

    @property
    def name(self) -> str:
        return "web_fetch"

    @property
    def description(self) -> str:
        return (
            "获取网页内容并转为 Markdown\n"
            "\n"
            "📖 使用示例：\n"
            "  # 抓取网页内容:\n"
            "  web_fetch(url='https://example.com')\n"
            "  # 限制返回长度:\n"
            "  web_fetch(url='https://example.com', max_chars=3000)\n"
            "  💡 返回纯文本 Markdown，适合 LLM 阅读。\n"
        )

    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(name="url", type="string", description="网页 URL", required=True),
            ToolParameter(name="max_chars", type="number", description="最大字符数", required=False, default=50000),
        ]

    async def execute(self, call_id, **kwargs) -> ToolResult:
        url = kwargs.get("url", "")
        max_chars = kwargs.get("max_chars", 50000)
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    html = await resp.text()
            # 简单提取文本
            import re
            text = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL)
            text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL)
            text = re.sub(r'<[^>]+>', ' ', text)
            text = re.sub(r'\s+', ' ', text).strip()
            text = text[:max_chars]
            return ToolResult.success(call_id, self.name, {
                "url": url,
                "content": text,
                "size": len(text),
                "status": resp.status,
            })
        except ImportError:
            # fallback 到 urllib
            try:
                from urllib.request import urlopen
                with urlopen(url, timeout=15) as resp:
                    html = resp.read().decode("utf-8", errors="replace")
                import re
                text = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL)
                text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL)
                text = re.sub(r'<[^>]+>', ' ', text)
                text = re.sub(r'\s+', ' ', text).strip()
                text = text[:max_chars]
                return ToolResult.success(call_id, self.name, {
                    "url": url, "content": text, "size": len(text),
                })
            except Exception as e2:
                return ToolResult.error(call_id, self.name, str(e2))
        except Exception as e:
            return ToolResult.error(call_id, self.name, str(e))


class WebSearchTool(BaseTool):

    def __init__(self, **kwargs):
        self._max_results = kwargs.get("max_results", 5)

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return (
            "搜索互联网信息\n"
            "\n"
            "📖 使用示例：\n"
            "  # 基本搜索:\n"
            "  web_search(query='Python 异步编程教程')\n"
            "  # 限制返回结果数:\n"
            "  web_search(query='Python 教程', count=3)\n"
            "  # 按时间过滤:\n"
            "  web_search(query='最新 AI 新闻', freshness='day')\n"
            "  💡 返回标题+摘要+URL，需要详细内容再用 web_fetch 抓取。\n"
        )

    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(name="query", type="string", description="搜索关键词", required=True),
            ToolParameter(name="count", type="number", description="返回结果数", required=False, default=5),
        ]

    async def execute(self, call_id, **kwargs) -> ToolResult:
        query = kwargs.get("query", "")
        count = kwargs.get("count", 5)
        try:
            import aiohttp
            search_url = f"https://www.google.com/search?q={query.replace(' ', '+')}"
            headers = {"User-Agent": "Mozilla/5.0"}
            async with aiohttp.ClientSession() as session:
                async with session.get(search_url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    html = await resp.text()
            import re
            results = []
            for match in re.finditer(r'<h3[^>]*>.*?<a[^>]*href="(/url\?q=[^"&]+)', html, re.DOTALL):
                url_match = re.search(r'href="(/url\?q=([^"&]+))', match.group(0))
                if url_match:
                    from urllib.parse import unquote
                    url = unquote(url_match.group(2))
                    title_match = re.search(r'<h3[^>]*>(.*?)</h3>', match.group(0))
                    title = re.sub(r'<[^>]+>', '', title_match.group(1)) if title_match else ""
                    results.append({"title": title, "url": url})
                    if len(results) >= count:
                        break
            if not results:
                results.append({"title": "无结果", "url": ""})
            return ToolResult.success(call_id, self.name, {"query": query, "results": results})
        except Exception as e:
            return ToolResult.error(call_id, self.name, f"搜索失败: {e}")


