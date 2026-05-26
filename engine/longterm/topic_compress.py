"""
Topic 压缩 — LLM 知识蒸馏 + 对话压缩

功能:
  1. compress_dialogue: 将一段对话压缩为 Topic（用 LLM 提取）
  2. should_compress: 判断是否达到压缩阈值
  3. extract_topics_from_history: 从历史中批量提取 Topic

与 Compactor 的区别:
  - Compactor: 减少上下文长度（保留最近 N 条）
  - Topic Compress: 提取知识（转化为长期记忆，保留在 SQLite 中）
"""

import json
import logging
from typing import Optional, List, Dict, Any

from .topic_store import TopicStore

logger = logging.getLogger(__name__)


def should_compress(history_length: int, since_last_compress: int) -> bool:
    """
    判断是否应该触发压缩

    Args:
        history_length: 当前历史消息数
        since_last_compress: 距上次压缩的消息数

    Returns:
        True 表示应该触发压缩
    """
    # 至少积累 20 条消息才压缩
    if history_length < 20:
        return False
    # 距上次压缩至少 10 条消息
    if since_last_compress < 10:
        return False
    return True


async def compress_dialogue(
    messages: List[Dict],
    llm_client: Any,
    store: Optional[TopicStore] = None,
    min_importance: float = 0.3,
) -> List[Dict]:
    """
    用 LLM 将对话提炼为 Topic 记忆

    Args:
        messages: 消息列表 [{"role": "...", "content": "..."}, ...]
        llm_client: LLM 客户端（需要 chat_completion 方法）
        store: 可选，自动存储提取的 Topic
        min_importance: 最低重要性，低于此值不存储

    Returns:
        提取的 Topic 列表 [{"title": ..., "one_liner": ..., "summary": ...,
                           "tags": [...], "confidence": float}, ...]

    流程:
      1. 将对话发给 LLM，要求提取关键知识点
      2. 解析 LLM 返回的结构化 JSON
      3. 如果提供了 store，自动写入 SQLite
    """
    if not messages or not llm_client:
        return []

    # 控制输入长度（取最后 30 轮）
    recent = messages[-60:] if len(messages) > 60 else messages

    # 构建对话文本
    dialogue_text = "\n".join(
        f"{'User' if m.get('role') == 'user' else 'Assistant'}: {m.get('content', '')[:300]}"
        for m in recent if m.get('content')
    )

    if not dialogue_text.strip():
        return []

    prompt = f"""Analyze the following dialogue and extract important knowledge points as structured topics.

Return ONLY a JSON array (no markdown, no explanation):
[
  {{
    "title": "Short topic title (max 20 chars)",
    "one_liner": "One sentence summary (max 50 chars)",
    "summary": "Detailed summary (max 200 chars)",
    "tags": ["tag1", "tag2"],
    "importance": 0.8
  }}
]

Guidelines:
- importance: 0.0-1.0 (1.0 = very important, e.g. user preferences, project decisions)
- Only extract if importance >= {min_importance}
- Max 5 topics per call
- Prefer extracting facts over general conversation

Dialogue:
{dialogue_text}

JSON:"""

    try:
        response = await llm_client.chat_completion(
            messages=[{"role": "user", "content": prompt}],
            tools=None,
            stream=False,
        )
        content = (
            response.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        )

        # 清理可能的 markdown 包裹
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1]
            if "```" in content:
                content = content.rsplit("```", 1)[0]

        topics = json.loads(content)
        if isinstance(topics, dict):
            topics = [topics]

        # 过滤
        topics = [
            t for t in topics
            if t.get("importance", 0) >= min_importance
        ]

        # 存储
        if store and topics:
            for t in topics:
                store.create_topic(
                    title=t.get("title", "Untitled"),
                    one_liner=t.get("one_liner", ""),
                    summary=t.get("summary", ""),
                    tags=t.get("tags", []),
                    confidence=t.get("importance", 0.5),
                )
            logger.info(f"Stored {len(topics)} topics from dialogue")

        return topics

    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse LLM topic output: {e}")
        return []
    except Exception as e:
        logger.error(f"Topic compression failed: {e}")
        return []


def format_topics_for_injection(topics: List[Dict]) -> str:
    """
    将 Topic 列表格式化为注入文本

    用于在对话中插入："根据之前的记忆，用户偏好..."
    """
    if not topics:
        return ""

    lines = []
    for i, t in enumerate(topics, 1):
        lines.append(f"{i}. {t.get('title', '')}: {t.get('one_liner', '')}")
    return "\n".join(lines)
