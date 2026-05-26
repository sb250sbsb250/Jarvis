"""
长期记忆层 — Jarvis V3 双层记忆系统

├── memory.py           — (保留) VectorStore + DefaultEmbedder + LongTermMemory
├── compactor.py        — (保留) 会话压缩器（滑动窗口/摘要）
├── topic_store.py       — 新增 SQLite 双层记忆（Topic + 关联）
├── topic_search.py      — 新增 BM25+向量混合检索 + 注入块构建
├── topic_compress.py    — 新增 LLM 知识蒸馏 + 对话压缩
└── topic_inject.py      — 新增 记忆注入 + 反馈闭环 + 做梦整合
"""

from .memory import LongTermMemory, MemoryItem, VectorStore, DefaultEmbedder
from .compactor import Compactor, AutoCompactor

# Topic 系统（新）
from .topic_store import TopicStore, BM25Index
from .topic_search import search_topics, build_injection_block, search_simple
from .topic_compress import compress_dialogue, should_compress
from .topic_inject import Injector, get_injector

__all__ = [
    # 旧（保留兼容）
    "LongTermMemory",
    "MemoryItem",
    "VectorStore",
    "DefaultEmbedder",
    "Compactor",
    "AutoCompactor",
    # 新
    "TopicStore",
    "BM25Index",
    "search_topics",
    "build_injection_block",
    "search_simple",
    "compress_dialogue",
    "should_compress",
    "Injector",
    "get_injector",
]
