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


class InsufficientBalanceError(Exception):
    """API 余额不足，不可自动重试"""
    pass


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

    # ── 模型路由表：ResponseMode → 推荐模型 ──
    MODEL_ROUTING = {
        "direct":   "deepseek-chat",
        "concise":  "deepseek-chat",
        "standard": "deepseek-v4-pro",
        "detailed": "deepseek-v4-pro",
    }

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

        # ── 加载备用提供商（Fallback） ──
        self._fallbacks: List[tuple[str, AsyncOpenAI, str]] = []
        self._load_fallback_providers()

    def _load_fallback_providers(self):
        """从环境变量加载备用 LLM 提供商"""
        fallback_configs = [
            ("qwen",    "DASHSCOPE_API_KEY",    "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
            ("kimi",    "MOONSHOT_API_KEY",      "https://api.moonshot.cn/v1",                       "moonshot-v1-128k"),
            ("deepseek2", "DEEPSEEK_API_KEY_2",  "https://api.deepseek.com/v1",                      "deepseek-chat"),
        ]
        for name, env_key, base_url, model in fallback_configs:
            key = os.environ.get(env_key, "")
            if key:
                self._fallbacks.append((
                    name,
                    AsyncOpenAI(api_key=key, base_url=base_url, timeout=self.config.timeout_seconds),
                    model,
                ))
                logger.info(f"🔁 Fallback 提供商已加载: {name} ({model})")

        if self._fallbacks:
            names = [fb[0] for fb in self._fallbacks]
            logger.info(f"🔁 共 {len(self._fallbacks)} 个 Fallback 提供商: {', '.join(names)}")

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

    @classmethod
    def get_model_for_mode(cls, mode: str) -> str:
        """根据复杂度模式获取推荐模型，支持环境变量覆盖"""
        env_key = f"LLM_MODEL_{mode.upper()}"
        env_model = os.environ.get(env_key)
        if env_model:
            return env_model
        return cls.MODEL_ROUTING.get(mode, cls._load_config_from_env().model)

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
            logger.error(f"LLM 调用失败: {e}")

            # ── 永久性错误检查（余额不足、认证失败） → 尝试 Fallback ──
            if LLMClient._is_permanent_error(e):
                provider_name = "deepseek"
                logger.warning(f"⚠️ {provider_name} 永久性错误，尝试 Fallback 提供商...")

                for fb_name, fb_client, fb_model in self._fallbacks:
                    try:
                        logger.info(f"🔄 切换到 Fallback: {fb_name} ({fb_model})")
                        fb_params = dict(params)
                        fb_params["model"] = fb_model
                        fb_params["messages"] = messages  # 用原始消息
                        fb_response = await fb_client.chat.completions.create(**fb_params)
                        result_dict = self._to_dict(fb_response)

                        # 日志
                        usage = result_dict.get("usage", {})
                        logger.info(
                            f"✅ Fallback {fb_name} 成功 "
                            f"(↑{usage.get('prompt_tokens', '?')} "
                            f"↓{usage.get('completion_tokens', '?')})"
                        )
                        return result_dict
                    except Exception as fb_e:
                        fb_elapsed = (time.time() - _llm_start) * 1000
                        logger.warning(
                            f"⚠️ Fallback {fb_name} 也失败 ({fb_elapsed:.0f}ms): {fb_e}"
                        )
                        if LLMClient._is_permanent_error(fb_e):
                            continue  # 也是永久错误，试下一个
                        raise  # 临时错误直接抛

                # 所有 Fallback 都失败了
                raise InsufficientBalanceError(
                    "所有 LLM 提供商均不可用。请至少充值一个账户：\n"
                    "  1. DeepSeek:  https://platform.deepseek.com/\n"
                    "  2. 通义千问:  https://dashscope.aliyuncs.com/\n"
                    "  3. Kimi:      https://kimi.moonshot.cn/"
                )

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

    @staticmethod
    def _is_permanent_error(e: Exception) -> bool:
        """判断是否为永久性错误（不应重试）"""
        error_str = str(e).lower()
        keywords = [
            "402", "insufficient balance", "insufficient_quota",
            "401", "invalid_api_key", "authentication",
            "403", "forbidden",
            "exceeded", "rate limit exceeded",  # 真正的限流可重试，但这里算上更安全
        ]
        # 排除可重试的限流
        if "rate limit" in error_str and "retry after" not in error_str:
            return False
        return any(kw in error_str for kw in keywords)

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
            "model": response.model,
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
