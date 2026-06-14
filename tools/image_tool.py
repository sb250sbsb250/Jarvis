"""
图片工具（原子工具版）

原子工具:
  image_read  — 读取图片信息（尺寸、格式、base64）
  image_ocr   — 识别图片中的文字
"""

import os
import base64
import logging
from typing import List

from engine.tool.base import (
    BaseTool, ToolDefinition, ToolParameter, ToolResult,
    CATEGORY_DATA,
)

logger = logging.getLogger(__name__)


class ImageTool(BaseTool):
    """图片工具集"""

    def __init__(self):
        self.api_key = os.environ.get("QWEN_API_KEY", os.environ.get("DASHSCOPE_API_KEY", ""))
        self._handlers = {
            "image_read": self._handle_read,
            "image_ocr": self._handle_ocr,
        }
        for t in self.tools:
            t.handler = self._handlers.get(t.name)

    @property
    def name(self) -> str:
        return "image"

    @property
    def category(self) -> str:
        return CATEGORY_DATA

    @property
    def tools(self) -> List[ToolDefinition]:
        return [
            ToolDefinition(
                name="image_read",
                description="""读取图片文件的基本信息：尺寸、格式、base64 编码。

使用场景：
- 获取图片的分辨率和格式信息
- 获取图片的 base64 编码（用于发送到 LLM 多模态模型）
- 检查图片文件大小

支持格式：jpg/jpeg, png, bmp, gif, webp

图片小于 5MB 时会自动生成 base64 编码。""",
                parameters=[
                    ToolParameter("path", "string", "图片文件路径", required=True),
                ],
                is_read=True,
                examples=[
                    'image_read(path="photo.png")',
                    'image_read(path="screenshot.jpg")',
                ],
                constraints=[
                    "仅支持常见图片格式（jpg/png/bmp/gif/webp）",
                    "大于 5MB 的图片不会生成 base64 编码",
                    "需要安装 Pillow 库：pip install Pillow",
                ],
            ),
            ToolDefinition(
                name="image_ocr",
                description="""使用通义千问（Qwen）VL 模型识别图片中的文字。

使用场景：
- 从发票/收据中提取文字
- 从截图/照片中提取文本内容
- 识别扫描文档中的文字

需要配置 QWEN_API_KEY 或 DASHSCOPE_API_KEY 环境变量。""",
                parameters=[
                    ToolParameter("path", "string", "图片文件路径", required=True),
                    ToolParameter("prompt", "string", "识别提示词，如 '提取发票金额和日期' 或 '识别图中所有文字'，默认自动提取文字", required=False),
                ],
                is_read=True,
                examples=[
                    'image_ocr(path="invoice.jpg", prompt="提取发票金额")',
                    'image_ocr(path="whiteboard.jpg", prompt="提取白板上的所有文字")',
                ],
                constraints=[
                    "需要配置 QWEN_API_KEY 或 DASHSCOPE_API_KEY 环境变量",
                    "需要使用 dashscope 库：pip install dashscope",
                    "图片过大会影响识别速度和准确性",
                    "仅支持中文和英文文字识别",
                ],
            ),
        ]

    async def execute(self, call_id: str, tool_name: str, **kwargs) -> ToolResult:
        handler = self._handlers.get(tool_name)
        if not handler:
            return ToolResult.fail(call_id, tool_name, f"未知工具: {tool_name}")
        try:
            path = kwargs.get("path", "")
            if not path or not os.path.exists(path):
                return ToolResult.fail(call_id, tool_name, f"文件不存在: {path}")
            return await handler(call_id, **kwargs)
        except Exception as e:
            return ToolResult.fail(call_id, tool_name, str(e))

    async def _handle_read(self, call_id: str, path: str) -> ToolResult:
        ext = os.path.splitext(path)[1].lower()
        result = {
            "path": path,
            "size_bytes": os.path.getsize(path),
            "format": ext,
        }

        try:
            from PIL import Image
            with Image.open(path) as img:
                result["width"] = img.width
                result["height"] = img.height
                result["mode"] = img.mode
        except ImportError:
            pass
        except Exception:
            pass

        if result["size_bytes"] < 5 * 1024 * 1024:
            with open(path, "rb") as f:
                result["base64"] = base64.b64encode(f.read()).decode("utf-8")
            mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                    ".bmp": "image/bmp", ".webp": "image/webp", ".gif": "image/gif"}
            result["mime_type"] = mime.get(ext, "application/octet-stream")

        return ToolResult.ok(call_id, "image_read", result)

    async def _handle_ocr(self, call_id: str, path: str, prompt: str = "请提取图中的所有文字") -> ToolResult:
        if not self.api_key:
            return ToolResult.fail(call_id, "image_ocr",
                "需要配置 QWEN_API_KEY 或 DASHSCOPE_API_KEY")

        try:
            import dashscope
            from dashscope import MultiModalConversation

            dashscope.api_key = self.api_key
            response = MultiModalConversation.call(
                model="qwen-vl-max",
                messages=[{
                    "role": "user",
                    "content": [
                        {"image": f"file://{os.path.abspath(path)}"},
                        {"text": prompt},
                    ],
                }],
                max_tokens=4096,
            )

            if response.status_code == 200:
                text = response.output.choices[0].message.content[0].get("text", "")
                return ToolResult.ok(call_id, "image_ocr", {
                    "path": path,
                    "text": text,
                })

            return ToolResult.fail(call_id, "image_ocr", f"OCR 失败: {response.message}")

        except ImportError:
            return ToolResult.fail(call_id, "image_ocr", "需要 dashscope: pip install dashscope")
        except Exception as e:
            return ToolResult.fail(call_id, "image_ocr", str(e))
