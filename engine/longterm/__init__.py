"""
长期记忆层 — Jarvis V3 双层记忆系统

├── topic_store.py       — SQLite 双层记忆（Topic + 关联）
├── topic_search.py      — BM25+向量混合检索 + 注入块构建
├── topic_compress.py    — LLM 知识蒸馏 + 对话压缩
└── topic_inject.py      — 记忆注入 + 反馈闭环 + 做梦整合
"""

from .topic_store import TopicStore, BM25Index
from .topic_search import search_topics, build_injection_block, search_simple
from .topic_compress import compress_dialogue, should_compress
from .topic_inject import Injector, get_injector

__all__ = [
    "TopicStore", "BM25Index",
    "search_topics", "build_injection_block", "search_simple",
    "compress_dialogue", "should_compress",
    "Injector", "get_injector",
]
