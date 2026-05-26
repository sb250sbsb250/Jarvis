"""
Topic 检索 — BM25 + 向量混合检索 + 上下文注入

提供:
  1. search_topics: 检索相关 Topic（混合搜索 + 关联传播）
  2. build_injection_block: 构建注入到 LLM 上下文的记忆块
  3. simple_kw_search: 纯关键词搜索（轻量级 fallback）
"""

import logging
from typing import Optional, List, Dict, Any

from .topic_store import TopicStore

logger = logging.getLogger(__name__)


def search_topics(
    store: TopicStore,
    query: str,
    top_k: int = 5,
    propagate_links: bool = True,
    min_confidence: float = 0.1,
) -> List[Dict]:
    """
    检索相关 Topic（混合检索 + 弱关联传播）

    流程:
      1. BM25 + 向量混合检索，召回 top_k 个 Topic
      2. 对每个 Topic 展开弱关联传播（最多 depth=1）
      3. RRF 融合 + 置信度过滤

    Returns:
        [{"topic": {...}, "score": float, "source": "search|propagation"}, ...]
    """
    results = []

    # Phase 1: 混合检索
    search_results = store.search(query, top_k=top_k, min_confidence=min_confidence)
    seen_ids = set()
    for topic, score in search_results:
        seen_ids.add(topic["id"])
        results.append({
            "topic": topic,
            "score": round(score, 4),
            "source": "search",
        })

    # Phase 2: 弱关联传播
    if propagate_links:
        for topic, _ in search_results[:3]:
            related = store.get_related_topics(topic["id"], max_depth=1)
            for rel_topic, strength in related:
                if rel_topic["id"] not in seen_ids:
                    seen_ids.add(rel_topic["id"])
                    results.append({
                        "topic": rel_topic,
                        "score": round(strength, 4),
                        "source": "propagation",
                    })

    # 排序（搜索优先，同分搜索 > 传播）
    results.sort(key=lambda x: (x["score"], x["source"] == "search"), reverse=True)
    return results[:top_k]


def build_injection_block(
    store: TopicStore,
    query: str,
    top_k: int = 5,
    max_tokens: int = 800,
) -> str:
    """
    构建注入到 LLM 上下文的记忆块

    用于在 LLM 处理用户输入前注入相关记忆。

    输出格式:
      [Relevant Memories]
      • [置信度] 标题: 一句话摘要
      • ...

    Args:
        store: TopicStore 实例
        query: 用户输入或查询
        top_k: 最多注入多少条
        max_tokens: 注入块的最大 token 估算

    Returns:
        格式化的记忆注入文本（空字符串 = 无相关记忆）
    """
    results = search_topics(store, query, top_k=top_k, min_confidence=0.15)

    if not results:
        return ""

    lines = ["[Relevant Memories]"]
    used_chars = len(lines[0])

    for r in results:
        topic = r["topic"]
        title = topic.get("title", "")
        one_liner = topic.get("one_liner", "")
        confidence = topic.get("confidence", 1.0)
        source = r["source"]

        icon = "🔄" if source == "propagation" else "📌"
        tag_str = ""
        if topic.get("tags"):
            tags = topic["tags"][:3]
            tag_str = f" [{', '.join(tags)}]"

        line = f"{icon} [{confidence:.0%}] {title}: {one_liner}{tag_str}"
        if len(line) > 200:
            line = line[:197] + "..."

        # 估算 token（中文 ≈ 2 char/token，英文 ≈ 4 char/token）
        est_tokens = sum(
            2 if '\u4e00' <= c <= '\u9fff' else 1
            for c in line
        ) // 2 + 1
        new_total = used_chars + est_tokens + 1

        if new_total > max_tokens:
            break

        lines.append(line)
        used_chars = new_total

        # 更新访问计数
        store.record_access(topic["id"])

    if len(lines) <= 1:
        return ""

    return "\n".join(lines)


def search_simple(
    store: TopicStore,
    keyword: str,
    top_k: int = 10,
) -> List[Dict]:
    """
    纯关键词搜索（轻量级，不使用 BM25/向量）

    用于"搜一下我记得..."这种用户明确要求搜索记忆的场景。
    """
    all_topics = store.get_all_topics(limit=1000)
    if not all_topics:
        return []

    kw = keyword.lower()
    scored = []
    for topic in all_topics:
        text = f"{topic['title']} {topic.get('one_liner', '')} {topic.get('summary', '')} {json.dumps(topic.get('tags', []))}".lower()
        count = text.count(kw)
        if count > 0:
            scored.append((topic, count))

    scored.sort(key=lambda x: x[1], reverse=True)
    return [t for t, _ in scored[:top_k]]


# 需要 json 用于 search_simple
import json
