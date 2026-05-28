"""
LLM 客户端 - 封装 DeepSeek/OpenAI API
"""

import os
import json
import time
import logging
from typing import Any, Dict, List, Optional, AsyncIterator
from dataclasses import dataclass, field
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


@dataclass
class LLMConfig:
    """LLM 配置"""
    api_key: str = ""
    base_url: str = "https://api.deepseek.com/v1"
    model: str = "deepseek-chat"
    max_tokens: int = 4096
    temperature: float = 0.7
    timeout_seconds: float = 60.0


class LLMClient:
    """
    统一的 LLM 客户端

    用法:
        client = LLMClient()
        response = await client.chat_completion(messages, tools)
    """

    def __init__(self, config: Optional[LLMConfig] = None):
        self.config = config or self._load_config_from_env()

        if not self.config.api_key:
            raise ValueError(
                "未配置 API Key。请设置环境变量 DEEPSEEK_API_KEY 或 OPENAI_API_KEY。"
            )

        self._client = AsyncOpenAI(
            api_key=self.config.api_key,
            base_url=self.config.base_url,
            timeout=self.config.timeout_seconds,
        )

    @staticmethod
    def _load_config_from_env() -> LLMConfig:
        """从环境变量加载配置"""
        api_key = os.environ.get("DEEPSEEK_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")
        base_url = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com/v1")
        model = os.environ.get("LLM_MODEL", "deepseek-chat")

        return LLMConfig(
            api_key=api_key,
            base_url=base_url,
            model=model,
        )

    async def chat_completion(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict]] = None,
        stream: bool = False,
        **kwargs,
    ) -> Dict[str, Any]:
        """
        调用 LLM

        Args:
            messages: OpenAI 格式消息列表
            tools: OpenAI 格式工具定义
            stream: 是否流式输出

        Returns:
            OpenAI 格式响应
        """
        # 注意: 使用显式 None 检查确保合法 falsy 值（如 temperature=0.0）不被误吞
        _model = kwargs.pop("model", None)
        _max_tokens = kwargs.pop("max_tokens", None)
        _temperature = kwargs.pop("temperature", None)

        _model = _model if _model is not None else self.config.model
        _max_tokens = _max_tokens if _max_tokens is not None else self.config.max_tokens
        _temperature = _temperature if _temperature is not None else self.config.temperature

        params = {
            "model": _model,
            "messages": messages,
            "max_tokens": _max_tokens,
            "temperature": _temperature,
            "stream": stream,
        }

        if tools:
            params["tools"] = tools
            params["tool_choice"] = "auto"

        # ⭐ LLM 请求日志
        last_msg = messages[-1] if messages else {}
        last_content = str(last_msg.get("content", ""))[:200] if isinstance(last_msg, dict) else str(last_msg)[:200]
        logger.info("─" * 50)
        logger.info(f"🤖 LLM → {_model}")
        # 估算输入 token
        try:
            from .core.token_estimator import estimate_message_dict
            total_est = 0
            for m in messages:
                if isinstance(m, dict):
                    total_est += estimate_message_dict(m)
            logger.info(f"📨 消息: {len(messages)} 条 (~{total_est} tok) | 🔧 工具: {len(tools) if tools else 0} 个")
        except Exception:
            logger.info(f"📨 消息: {len(messages)} 条 | 🔧 工具: {len(tools) if tools else 0} 个")
        logger.info(f"📝 最后一条: [{last_msg.get('role', '?')}] {last_content.replace(chr(10), ' ')[:120]}")

        _llm_start = time.time()
        try:
            response = await self._client.chat.completions.create(**params)

            if stream:
                result = await self._handle_stream_response(response)
                # 流式模式下检测到 tool_calls 被丢弃 → 回退到非流式
                if isinstance(result, dict) and result.get("_stream_fallback"):
                    logger.info("stream tool_calls dropped, retrying with stream=False")
                    params["stream"] = False
                    non_stream_resp = await self._client.chat.completions.create(**params)
                    result_dict = self._to_dict(non_stream_resp)
                else:
                    result_dict = result if isinstance(result, dict) else {"choices": []}

                _llm_elapsed = (time.time() - _llm_start) * 1000
                choice = result_dict.get("choices", [{}])[0] if result_dict.get("choices") else {}
                msg = choice.get("message", {}) if isinstance(choice, dict) else {}
                resp_content = (msg.get("content", "") or "")[:200] if isinstance(msg, dict) else ""
                tc = msg.get("tool_calls", []) if isinstance(msg, dict) else []
                usage = result_dict.get("usage", {})
                logger.info(
                    f"✅ LLM ← ({_llm_elapsed:.0f}ms | "
                    f"↑{usage.get('prompt_tokens', '?')} ↓{usage.get('completion_tokens', '?')} "
                    f"Σ{usage.get('total_tokens', '?')})"
                )
                if tc:
                    for t in tc:
                        fn = t.get("function", {}) if isinstance(t, dict) else {}
                        name = fn.get("name", "?") if isinstance(fn, dict) else "?"
                        args_raw = fn.get("arguments", "") if isinstance(fn, dict) else ""
                        if len(str(args_raw)) > 120:
                            args_raw = str(args_raw)[:120] + "..."
                        logger.info(f"  🛠️  工具调用: {name}({args_raw})")
                if resp_content:
                    logger.info(f"  💬 回复: {resp_content.replace(chr(10), ' ')[:150]}")
                logger.info("─" * 50)

                return result if not isinstance(result, dict) else result_dict

            result_dict = self._to_dict(response)
            _llm_elapsed = (time.time() - _llm_start) * 1000

            # ⭐ LLM 响应日志
            choice = result_dict.get("choices", [{}])[0] if result_dict.get("choices") else {}
            msg = choice.get("message", {}) if isinstance(choice, dict) else {}
            resp_content = (msg.get("content", "") or "")[:200] if isinstance(msg, dict) else ""
            tc = msg.get("tool_calls", []) if isinstance(msg, dict) else []
            usage = result_dict.get("usage", {})
            logger.info(
                f"✅ LLM ← ({_llm_elapsed:.0f}ms | "
                f"↑{usage.get('prompt_tokens', '?')} ↓{usage.get('completion_tokens', '?')} "
                f"Σ{usage.get('total_tokens', '?')})"
            )
            if tc:
                for t in tc:
                    fn = t.get("function", {}) if isinstance(t, dict) else {}
                    name = fn.get("name", "?") if isinstance(fn, dict) else "?"
                    args_raw = fn.get("arguments", "") if isinstance(fn, dict) else ""
                    if len(str(args_raw)) > 120:
                        args_raw = str(args_raw)[:120] + "..."
                    logger.info(f"  🛠️  工具调用: {name}({args_raw})")
            if resp_content:
                logger.info(f"  💬 回复: {resp_content.replace(chr(10), ' ')[:150]}")
            logger.info("─" * 50)

            return result_dict

        except Exception as e:
            _llm_elapsed = (time.time() - _llm_start) * 1000
            logger.error(f"❌ LLM 调用失败 ({_llm_elapsed:.0f}ms): {e}")
            # ⭐ 消息格式错误时打印详细分析
            error_str = str(e)
            if "tool' must be a response" in error_str or "BadRequestError" in error_str:
                from .message.validator import MessageDebugger, aggressive_clean
                MessageDebugger.print_analysis(messages, e)
                # 激进修复：移除孤立的 tool 消息后重试一次
                if "tool' must be a response" in error_str:
                    cleaned = aggressive_clean(messages)
                    if len(cleaned) != len(messages):
                        logger.warning(f"激进修复: 消息数 {len(messages)} → {len(cleaned)}")
                        params["messages"] = cleaned
                        non_stream_resp = await self._client.chat.completions.create(**params)
                        return self._to_dict(non_stream_resp)
            logger.error(f"LLM 调用失败: {e}")
            raise

    async def stream_chunks(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict]] = None,
        **kwargs,
    ) -> AsyncIterator[Dict[str, Any]]:
        """流式输出，逐块返回"""
        _model = kwargs.pop("model", None)
        _max_tokens = kwargs.pop("max_tokens", None)
        _temperature = kwargs.pop("temperature", None)

        # DeepSeek 思考模型要求原样传回 reasoning_content，不做过滤
        params = {
            "model": _model if _model is not None else self.config.model,
            "messages": messages,
            "max_tokens": _max_tokens if _max_tokens is not None else self.config.max_tokens,
            "temperature": _temperature if _temperature is not None else self.config.temperature,
            "stream": True,
        }

        if tools:
            params["tools"] = tools
            params["tool_choice"] = "auto"

        response = await self._client.chat.completions.create(**params)

        async for chunk in response:
            yield self._chunk_to_dict(chunk)

    def _to_dict(self, response: Any) -> Dict:
        """OpenAI 响应 → 字典"""
        choice = response.choices[0] if response.choices else None
        if not choice:
            return {"choices": [{"message": {"content": "", "tool_calls": []}}]}

        msg = choice.message
        tool_calls = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                tool_calls.append({
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                })

        # 保留 reasoning_content（DeepSeek 思考模型要求后续请求必须传回）
        msg_dict = {
            "role": msg.role,
            "content": msg.content or "",
        }
        if tool_calls:
            msg_dict["tool_calls"] = tool_calls
        rc = getattr(msg, "reasoning_content", None)
        if rc:
            msg_dict["reasoning_content"] = rc

        return {
            "choices": [{
                "index": choice.index,
                "message": msg_dict,
                "finish_reason": choice.finish_reason,
            }],
            "usage": {
                "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
                "completion_tokens": response.usage.completion_tokens if response.usage else 0,
                "total_tokens": response.usage.total_tokens if response.usage else 0,
            },
        }

    def _chunk_to_dict(self, chunk: Any) -> Dict:
        """流式块 → 字典"""
        choice = chunk.choices[0] if chunk.choices else None
        if not choice:
            return {"choices": [{"delta": {"content": ""}}]}

        delta = choice.delta
        return {
            "choices": [{
                "index": choice.index,
                "delta": {
                    "role": delta.role,
                    "content": delta.content or "",
                },
                "finish_reason": choice.finish_reason,
            }]
        }

    async def _handle_stream_response(self, response: Any) -> Dict:
        """流式响应 → 合并为完整响应

        注意：流式模式下 tool_calls 增量信息会被丢弃。
        如果检测到原始响应包含工具调用信号，返回特殊标记
        让上层回退到非流式模式。
        """
        content_parts = []
        full_response = None
        detected_tool_call = False

        async for chunk in response:
            if full_response is None:
                full_response = chunk
            for choice in chunk.choices:
                if choice.delta and choice.delta.content:
                    content_parts.append(choice.delta.content)
                # 检测工具调用信号
                if (hasattr(choice.delta, 'tool_calls') and choice.delta.tool_calls):
                    detected_tool_call = True

        if detected_tool_call:
            logger.warning("stream tool_calls detected but dropped, use non-stream instead")
            return {"_stream_fallback": True}

        content = "".join(content_parts)
        return {
            "choices": [{
                "message": {"role": "assistant", "content": content, "tool_calls": []},
                "finish_reason": "stop",
            }]
        }
