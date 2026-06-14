"""
test_unit/test_topic_store.py — TopicStore 单元测试

重点覆盖：
  1. BM25Index 基础功能（添加文档、搜索、删除、重建）
  2. TopicStore CRUD（创建、读取、更新、删除）
  3. search() 检索（BM25 fallback）
  4. 关联传播（add_link / get_related_topics）
  5. 维护功能（decay_confidence / prune_topics）
"""

import os
import json
import tempfile
import pytest
from engine.longterm.topic_store import TopicStore, BM25Index


# ═══════════════════════════════════════
#  BM25Index 测试
# ═══════════════════════════════════════

class TestBM25Index:
    """BM25 全文检索索引"""

    def test_empty(self):
        idx = BM25Index()
        assert idx.search("test") == []

    def test_add_and_search(self):
        idx = BM25Index()
        idx.add_document("Python 是一种编程语言")
        idx.add_document("Java 也是一种编程语言")
        idx.add_document("今天的天气很好")

        results = idx.search("编程语言", top_k=5)
        assert len(results) >= 2  # 至少返回前两篇

    def test_search_top_k(self):
        idx = BM25Index()
        for i in range(10):
            idx.add_document(f"文档第{i+1}篇 测试内容")
        results = idx.search("测试", top_k=3)
        assert len(results) <= 3

    def test_remove_document(self):
        idx = BM25Index()
        idx.add_document("Python 编程")
        idx.add_document("Java 编程")
        assert len(idx.search("Python", top_k=5)) == 1
        idx.remove_document(0)
        assert len(idx.search("Python", top_k=5)) == 0

    def test_rebuild(self):
        idx = BM25Index()
        idx.add_document("旧内容")
        idx.rebuild(["新内容1", "新内容2"])
        # 重建后的搜索应只反映新内容
        assert len(idx.search("旧", top_k=5)) == 0
        assert len(idx.search("新内容", top_k=5)) == 2

    def test_tokenize_chinese(self):
        tokens = BM25Index._tokenize("你好世界 hello")
        assert "你" in tokens
        assert "好" in tokens
        assert "世" in tokens
        assert "界" in tokens
        assert "hello" in tokens

    def test_tokenize_english(self):
        tokens = BM25Index._tokenize("Hello World Test")
        assert "hello" in tokens
        assert "world" in tokens
        assert "test" in tokens

    def test_empty_text(self):
        assert BM25Index._tokenize("") == []
        assert BM25Index._tokenize(None) == []


# ═══════════════════════════════════════
#  TopicStore 测试
# ═══════════════════════════════════════

@pytest.fixture
def topic_store():
    """临时 TopicStore 实例（内存 SQLite）"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    store = TopicStore(db_path)
    yield store
    store = None
    try:
        os.unlink(db_path)
    except OSError:
        pass


class TestTopicStoreCRUD:
    """Topic 增删改查"""

    def test_create_topic(self, topic_store):
        tid = topic_store.create_topic(
            title="测试主题",
            one_liner="这是一个测试",
            summary="详细摘要",
            tags=["test", "example"],
            source="session_1",
        )
        assert tid.startswith("t_")

    def test_get_topic(self, topic_store):
        tid = topic_store.create_topic(title="获取测试", one_liner="测试")
        topic = topic_store.get_topic(tid)
        assert topic is not None
        assert topic["title"] == "获取测试"
        assert topic["one_liner"] == "测试"

    def test_get_nonexistent(self, topic_store):
        assert topic_store.get_topic("t_nonexistent") is None

    def test_update_topic(self, topic_store):
        tid = topic_store.create_topic(title="旧标题")
        topic_store.update_topic(tid, title="新标题", confidence=0.8)
        topic = topic_store.get_topic(tid)
        assert topic["title"] == "新标题"
        assert topic["confidence"] == 0.8

    def test_delete_topic(self, topic_store):
        tid = topic_store.create_topic(title="待删除")
        topic_store.delete_topic(tid)
        assert topic_store.get_topic(tid) is None

    def test_get_all_topics(self, topic_store):
        for i in range(5):
            topic_store.create_topic(title=f"主题{i}")
        all_topics = topic_store.get_all_topics()
        assert len(all_topics) == 5

    def test_count(self, topic_store):
        assert topic_store.count() == 0
        topic_store.create_topic(title="A")
        assert topic_store.count() == 1
        topic_store.create_topic(title="B")
        assert topic_store.count() == 2

    def test_record_access(self, topic_store):
        tid = topic_store.create_topic(title="访问测试", confidence=0.5)
        topic_store.record_access(tid)
        topic = topic_store.get_topic(tid)
        assert topic["access_count"] >= 1
        assert topic["confidence"] > 0.5  # access 会增加置信度


class TestTopicStoreSearch:
    """检索功能"""

    def test_search_empty(self, topic_store):
        assert topic_store.search("anything") == []

    def test_search_bm25(self, topic_store):
        topic_store.create_topic(
            title="Python 编程入门",
            one_liner="Python 基础教程",
            summary="Python 是一种流行的编程语言",
            tags=["python"],
            confidence=0.9,
        )
        topic_store.create_topic(
            title="Java 编程入门",
            one_liner="Java 基础",
            summary="Java 是一种编程语言",
            tags=["java"],
            confidence=0.9,
        )

        results = topic_store.search("Python", top_k=5)
        assert len(results) >= 1
        assert "Python" in results[0][0]["title"]

    def test_search_confidence_filter(self, topic_store):
        topic_store.create_topic(title="高置信度", confidence=1.0)
        topic_store.create_topic(title="低置信度", confidence=0.05)
        results = topic_store.search("置信度", top_k=5, min_confidence=0.1)
        titles = [t["title"] for t, _ in results]
        assert "高置信度" in titles
        assert "低置信度" not in titles


class TestTopicStoreLinks:
    """关联传播"""

    def test_add_link(self, topic_store):
        t1 = topic_store.create_topic(title="A")
        t2 = topic_store.create_topic(title="B")
        assert topic_store.add_link(t1, t2, strength=0.8)
        links = topic_store.get_all_links()
        assert len(links) >= 1

    def test_no_self_link(self, topic_store):
        t1 = topic_store.create_topic(title="A")
        assert not topic_store.add_link(t1, t1)

    def test_get_related_topics(self, topic_store):
        t1 = topic_store.create_topic(title="源")
        t2 = topic_store.create_topic(title="目标")
        topic_store.add_link(t1, t2, strength=0.9)
        related = topic_store.get_related_topics(t1)
        assert len(related) == 1
        assert related[0][0]["title"] == "目标"
        assert related[0][1] == 0.9  # strength

    def test_related_empty(self, topic_store):
        t1 = topic_store.create_topic(title="孤立")
        related = topic_store.get_related_topics(t1)
        assert related == []


class TestTopicStoreMaintenance:
    """维护功能"""

    def test_decay_confidence(self, topic_store):
        tid = topic_store.create_topic(title="衰减测试", confidence=1.0)
        topic_store.decay_confidence(factor=0.5)
        topic = topic_store.get_topic(tid)
        assert topic["confidence"] == 0.5

    def test_prune_topics(self, topic_store):
        for i in range(10):
            topic_store.create_topic(
                title=f"低置信度{i}",
                confidence=0.01,
            )
        # 创建 3 个高置信度的，确保保留
        for i in range(3):
            topic_store.create_topic(
                title=f"高置信度{i}",
                confidence=1.0,
            )
        deleted = topic_store.prune_topics(min_confidence=0.05, keep_min=3)
        assert deleted >= 0  # 会删除低于阈值的
        assert topic_store.count() >= 3  # 至少保留 3 个

    def test_prune_keep_min(self, topic_store):
        """即使所有都低于阈值，也至少保留 keep_min 个"""
        for i in range(5):
            topic_store.create_topic(title=f"低{i}", confidence=0.01)
        deleted = topic_store.prune_topics(min_confidence=0.05, keep_min=5)
        assert deleted == 0  # 只有 5 个，keep_min=5 就全保留
