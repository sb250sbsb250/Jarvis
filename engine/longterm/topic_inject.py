"""
Topic 注入 — 记忆上下文注入 + 反馈闭环

功能:
  1. Injector: 将相关记忆注入到 LLM 上下文
  2. 反馈处理：用户说"不对"、"不是这样"时，自动降权相关记忆
  3. 记忆合并：两条相似记忆自动合并

使用:
    injector = Injector(topic_store)
    context = injector.prepare_injection(user_input)
    # → context = "[Relevant Memories]\\n• ..."

    # 处理负反馈
    was_feedback, remaining = injector.handle_feedback(user_input)
    if was_feedback:
        # 降权相关记忆
        ...
"""

import json
import logging
from typing import Optional, List, Tuple, Dict

from .topic_store import TopicStore
from .topic_search import build_injection_block, search_topics

logger = logging.getLogger(__name__)

# 负反馈关键词（用户表示"不对"）
_FEEDBACK_PATTERNS = [
    "不对", "不是", "错了", "不是这样", "不对啊",
    "你记错了", "错了", "不对", "不对的",
    "不是这个", "你没记住", "我说的是",
    "no", "not", "wrong", "incorrect",
]


class Injector:
    """
    记忆注入器 — 负责记忆的注入、反馈处理、合并

    使用示例:
        store = TopicStore("data/topics.db")
        injector = Injector(store)

        # Phase 1: 准备注入
        memory_block = injector.prepare_injection("帮我写一个Python脚本")
        if memory_block:
            messages.insert(0, {"role": "system", "content": memory_block})

        # Phase 2: 处理反馈
        if injector.handle_feedback(user_input)[0]:
            injector.apply_feedback(user_input)
    """

    def __init__(self, store: TopicStore, max_injection_tokens: int = 800):
        self.store = store
        self.max_injection_tokens = max_injection_tokens
        self._last_injected: List[str] = []  # 上次注入的 topic ids

    def prepare_injection(self, user_input: str) -> str:
        """
        准备记忆注入块

        根据用户输入检索相关记忆，构建注入文本。

        Returns:
            注入文本（空字符串 = 无相关记忆）
        """
        block = build_injection_block(
            store=self.store,
            query=user_input,
            top_k=5,
            max_tokens=self.max_injection_tokens,
        )
        # 通过搜索获取实际注入的 topic IDs（避免从文本解析的复杂性）
        try:
            results = search_topics(self.store, user_input, top_k=5, min_confidence=0.15)
            self._last_injected = [r["topic"]["id"] for r in results]
        except Exception:
            self._last_injected = []
        return block

    @staticmethod
    def handle_feedback(user_input: str) -> Tuple[bool, str]:
        """
        检测用户输入是否包含负反馈

        Returns:
            (is_feedback, remaining_text)
            is_feedback: 是否包含负反馈
            remaining_text: 去除反馈关键词后的文本
        """
        text = user_input.strip()
        for pattern in _FEEDBACK_PATTERNS:
            if pattern in text:
                # 去除反馈词
                remaining = text.replace(pattern, "").strip()
                # 去除开头的标点
                remaining = remaining.lstrip("，。！？,!.?")
                return True, remaining or text

        return False, text

    def apply_feedback(self, feedback_text: str):
        """
        根据反馈降权相关记忆

        当用户说"不对"时，找到与当前对话相关的记忆并降权。
        """
        # 找到反馈文本涉及的记忆
        results = search_topics(
            self.store, feedback_text, top_k=5, min_confidence=0.05
        )
        for r in results:
            topic = r["topic"]
            tid = topic["id"]
            current_conf = topic.get("confidence", 1.0)
            # 降权 30%
            new_conf = max(0.05, current_conf * 0.7)
            self.store.update_topic(tid, confidence=new_conf)
            logger.info(
                f"Feedback applied to {tid}: {current_conf:.2f} → {new_conf:.2f}"
            )

    def merge_similar_topics(self, threshold: float = 0.8) -> int:
        """
        合并相似 Topic（做梦整合）

        检查所有 Topic 两两之间的相似度，高于 threshold 的合并。

        Returns:
            合并的数量
        """
        all_topics = self.store.get_all_topics(limit=500)
        import itertools

        merged = 0
        for a, b in itertools.combinations(all_topics, 2):
            # 用标题和摘要的文本相似度
            text_a = f"{a['title']} {a.get('one_liner', '')}"
            text_b = f"{b['title']} {b.get('one_liner', '')}"
            sim = self._text_similarity(text_a, text_b)

            if sim >= threshold:
                # 合并：保留置信度更高的那个
                if a["confidence"] >= b["confidence"]:
                    keep, remove = a, b
                else:
                    keep, remove = b, a

                # 扩展 keep 的 summary
                keep_summary = keep.get("summary", "")
                remove_summary = remove.get("summary", "")
                if remove_summary and remove_summary not in keep_summary:
                    new_summary = keep_summary + "\n" + remove_summary
                    # 合并 tags
                    keep_tags = list(
                        set(keep.get("tags", []) + remove.get("tags", []))
                    )
                    self.store.update_topic(
                        keep["id"],
                        summary=new_summary[:500],
                        tags=keep_tags,
                        confidence=min(1.0, keep["confidence"] + 0.1),
                    )
                self.store.delete_topic(remove["id"])
                merged += 1

        if merged > 0:
            logger.info(f"Merged {merged} similar topics")

        return merged

    @staticmethod
    def _extract_topic_ids(block: str) -> List[str]:
        """从注入块中提取 Topic ID（当前简化版）"""
        _ = block
        return []

    @staticmethod
    def _text_similarity(a: str, b: str) -> float:
        """简单文本相似度（基于公共子串/Jaccard）"""
        if not a or not b:
            return 0.0
        set_a = set(a)
        set_b = set(b)
        if not set_a or not set_b:
            return 0.0
        intersection = set_a & set_b
        union = set_a | set_b
        return len(intersection) / len(union)


def get_injector(store: Optional[TopicStore] = None,
                 db_path: str = "data/topics.db") -> Injector:
    """快捷获取 Injector 实例"""
    if store is None:
        store = TopicStore(db_path)
    return Injector(store)
