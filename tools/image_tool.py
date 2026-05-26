"""
图片工具 — 生成/分析图片
"""

import logging
from typing import List

from engine.tool.base import BaseTool, ToolParameter
from engine.core.types import ToolResult

logger = logging.getLogger("jarvis.tools.image")


class ImageGenerateTool(BaseTool):

    def __init__(self, **kwargs):
        pass

    """生成图片"""

    @property
    def name(self) -> str:
        return "image_generate"

    @property
    def description(self) -> str:
        return "根据描述生成图片"

    @property
    def parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(name="prompt", type="string", description="图片描述", required=True),
            ToolParameter(name="size", type="string", description="尺寸（如 1024x1024）", required=False, default="1024x1024"),
        ]

    async def execute(self, call_id, **kwargs) -> ToolResult:
        prompt = kwargs.get("prompt", "")
        size = kwargs.get("size", "1024x1024")
        try:
            result = {"prompt": prompt, "size": size, "note": "图片生成需要配置 API（如 DALL-E 或 Stable Diffusion）"}
            return ToolResult.success(call_id, self.name, result)
        except Exception as e:
            return ToolResult.error(call_id, self.name, str(e))


