"""
Topic 存储 - SQLite 双层记忆系统

架构:
  Topic 层 (SQLite)          原文层 (JSON 文件)
  ┌──────────────────┐       ┌──────────────────┐
  │ 标题 (title)     │       │ 完整对话原文      │
  │ 一句话摘要       │  ←──→ │ 角色 (user/ai)   │
  │ 完整摘要         │       │ 时间戳            │
  │ 标签 (tags)      │       │ 会话 ID           │
  │ 弱关联           │       └──────────────────┘
  │ 置信度           │
  │ embedding 向量   │ ←── 可选，降级后 BM25 纯文本检索
  └──────────────────┘

特点:
  - 零外部依赖（SQLite 是 Python 内置）
  - embedding 失败时自动降级到 BM25 文本搜索
  - 支持弱关联传播（Topic A ↔ Topic B 的相关度提升）
  - 支持置信度衰减（长期未访问的记忆自动降权）
"""

import json
import logging
import os
import sqlite3
import math
import hashlib
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple
from collections import Counter

logger = logging.getLogger(__name__)

# ── BM25 ──

class BM25Index:
    """
    纯 Python BM25 索引（零外部依赖）

    用于 embedding 不可用时的文本检索降级方案。
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self._doc_count = 0
        self._avg_doc_len = 0.0
        self._doc_lens: List[int] = []
        self._doc_texts: List[str] = []
        self._term_df: Dict[str, int] = {}  # term → doc frequency
        self._term_cf: Dict[str, int] = {}  # term → collection frequency
        self._dirty = True

    def add_document(self, text: str):
        """添加一篇文档到索引"""
        tokens = self._tokenize(text)
        self._doc_texts.append(text)
        self._doc_lens.append(len(tokens))
        doc_terms = set(tokens)
        for term in doc_terms:
            self._term_df[term] = self._term_df.get(term, 0) + 1
        for term in tokens:
            self._term_cf[term] = self._term_cf.get(term, 0) + 1
        self._dirty = True

    def remove_document(self, index: int):
        """移除一篇文档"""
        if 0 <= index < len(self._doc_texts):
            tokens = self._tokenize(self._doc_texts[index])
            doc_terms = set(tokens)
            for term in doc_terms:
                if term in self._term_df:
                    self._term_df[term] -= 1
                    if self._term_df[term] <= 0:
                        del self._term_df[term]
            for term in tokens:
                if term in self._term_cf:
                    self._term_cf[term] -= 1
                    if self._term_cf[term] <= 0:
                        del self._term_cf[term]
            del self._doc_texts[index]
            del self._doc_lens[index]
            self._dirty = True

    def rebuild(self, texts: List[str]):
        """重建索引"""
        self._doc_texts = []
        self._doc_lens = []
        self._term_df = {}
        self._term_cf = {}
        for text in texts:
            self.add_document(text)
        self._finalize()

    def _finalize(self):
        """准备搜索（更新统计信息）"""
        self._doc_count = len(self._doc_texts)
        if self._doc_count > 0:
            self._avg_doc_len = sum(self._doc_lens) / self._doc_count
        self._dirty = False

    def search(self, query: str, top_k: int = 10) -> List[Tuple[int, float]]:
        """搜索，返回 [(doc_index, score), ...]"""
        if self._dirty:
            self._finalize()
        if self._doc_count == 0:
            return []

        query_terms = self._tokenize(query)
        if not query_terms:
            return []

        scores = [0.0] * self._doc_count
        for term in set(query_terms):
            if term not in self._term_df:
                continue
            df = self._term_df[term]
            idf = math.log((self._doc_count - df + 0.5) / (df + 0.5) + 1.0)
            for i, doc_len in enumerate(self._doc_lens):
                # Term frequency in this document
                tf = self._tokenize(self._doc_texts[i]).count(term)
                if tf == 0:
                    continue
                score = idf * (tf * (self.k1 + 1)) / (
                    tf + self.k1 * (1 - self.b + self.b * doc_len / self._avg_doc_len)
                )
                scores[i] += score

        # 排序取 top_k
        scored_indices = sorted(
            range(len(scores)), key=lambda i: scores[i], reverse=True
        )
        return [(i, scores[i]) for i in scored_indices[:top_k] if scores[i] > 0]

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        """分词（支持中文和英文）"""
        if not text:
            return []
        text = text.lower()
        tokens = []
        # 中文：单字
        for ch in text:
            if '\u4e00' <= ch <= '\u9fff':
                tokens.append(ch)
        # 英文：按非字母字符分割
        import re
        en_tokens = re.findall(r'[a-z0-9_]+', text)
        tokens.extend(en_tokens)
        # 过滤短词
        return [t for t in tokens if len(t) >= 1]


# ── 嵌入器 ──

class EmbeddingProvider:
    """
    嵌入提供者 — 尝试使用真实嵌入，失败则降级到 BM25

    不抛出异常，所有失败静默降级。
    """

    def __init__(self):
        self._real_embedder = None
        self._try_init()

    def _try_init(self):
        """尝试初始化真实嵌入器"""
        for module_name, class_name, method in [
            ("sentence_transformers", "SentenceTransformer", None),
        ]:
            try:
                import importlib
                mod = importlib.import_module(module_name)
                logger.info(f"Found embedding module: {module_name}")
                # 暂时不加载模型（太大），仅在需要时加载
                self._real_embedder = module_name
                return
            except ImportError:
                continue
        logger.info("No embedding module found, will use BM25 fallback")

    def is_available(self) -> bool:
        return self._real_embedder is not None

    def embed(self, text: str) -> Optional[List[float]]:
        """生成嵌入，失败返回 None"""
        if not self._real_embedder:
            return None
        try:
            if self._real_embedder == "sentence_transformers":
                import sentence_transformers
                # 懒加载模型
                model = sentence_transformers.SentenceTransformer(
                    "paraphrase-multilingual-MiniLM-L12-v2"
                )
                vec = model.encode(text).tolist()
                return vec
        except Exception as e:
            logger.warning(f"Embedding failed, fallback to BM25: {e}")
            self._real_embedder = None
        return None


# ── Topic Store ──

class TopicStore:
    """
    Topic 存储 — SQLite 双层记忆

    Schema:
      CREATE TABLE topics (
        id          TEXT PRIMARY KEY,
        title       TEXT NOT NULL,
        one_liner   TEXT,          -- 一句话摘要
        summary     TEXT,          -- 完整摘要
        tags        TEXT,          -- JSON 数组
        source      TEXT,          -- 来源（session_id）
        confidence  REAL DEFAULT 1.0,
        access_count INTEGER DEFAULT 0,
        created_at  TEXT,
        updated_at  TEXT,
        embedding   BLOB           -- JSON 浮点数组
      );

      CREATE TABLE topic_links (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id   TEXT NOT NULL,
        target_id   TEXT NOT NULL,
        strength    REAL DEFAULT 0.5,  -- 关联强度
        link_type   TEXT DEFAULT 'related',  -- related / temporal / causal
        created_at  TEXT
      );

      CREATE INDEX idx_topics_tags ON topics(tags);
      CREATE INDEX idx_topics_confidence ON topics(confidence);
      CREATE INDEX idx_topic_links_source ON topic_links(source_id);
    """

    def __init__(self, db_path: str = "data/topics.db"):
        self.db_path = db_path
        self._embedder = EmbeddingProvider()
        self._bm25 = BM25Index()
        self._bm25_loaded = False
        self._init_db()

    def _init_db(self):
        """初始化数据库和表"""
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS topics (
                    id          TEXT PRIMARY KEY,
                    title       TEXT NOT NULL,
                    one_liner   TEXT,
                    summary     TEXT,
                    tags        TEXT,
                    source      TEXT,
                    confidence  REAL DEFAULT 1.0,
                    access_count INTEGER DEFAULT 0,
                    created_at  TEXT,
                    updated_at  TEXT,
                    embedding   BLOB
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS topic_links (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id   TEXT NOT NULL,
                    target_id   TEXT NOT NULL,
                    strength    REAL DEFAULT 0.5,
                    link_type   TEXT DEFAULT 'related',
                    created_at  TEXT,
                    FOREIGN KEY (source_id) REFERENCES topics(id),
                    FOREIGN KEY (target_id) REFERENCES topics(id)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_topics_tags ON topics(tags)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_topics_confidence ON topics(confidence)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_topic_links_source ON topic_links(source_id)
            """)
            conn.commit()
        finally:
            conn.close()

    # ── CRUD ──

    def create_topic(
        self,
        title: str,
        one_liner: str = "",
        summary: str = "",
        tags: Optional[List[str]] = None,
        source: str = "",
        confidence: float = 1.0,
    ) -> str:
        """创建一条 Topic 记忆"""
        import uuid
        topic_id = f"t_{uuid.uuid4().hex[:12]}"
        now = datetime.utcnow().isoformat()
        tags_json = json.dumps(tags or [], ensure_ascii=False)

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """INSERT INTO topics
                   (id, title, one_liner, summary, tags, source,
                    confidence, access_count, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
                (topic_id, title, one_liner, summary, tags_json, source,
                 confidence, now, now),
            )
            conn.commit()
        finally:
            conn.close()

        # 同步到 BM25 索引
        self._sync_to_bm25()

        logger.info(f"Created topic: {topic_id} — {title[:40]}")
        return topic_id

    def get_topic(self, topic_id: str) -> Optional[Dict]:
        """获取一条 Topic"""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM topics WHERE id = ?", (topic_id,)
            ).fetchone()
            if row is None:
                return None
            return self._row_to_dict(row)
        finally:
            conn.close()

    def update_topic(self, topic_id: str, **kwargs) -> bool:
        """更新 Topic（支持 embedding 更新）"""
        allowed = {"title", "one_liner", "summary", "tags", "confidence", "embedding"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return False

        now = datetime.utcnow().isoformat()
        updates["updated_at"] = now

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [topic_id]

        conn = sqlite3.connect(self.db_path)
        try:
            if "tags" in updates and isinstance(updates["tags"], list):
                idx = list(updates.keys()).index("tags")
                values[idx] = json.dumps(updates["tags"], ensure_ascii=False)
            if "embedding" in updates and isinstance(updates["embedding"], list):
                idx = list(updates.keys()).index("embedding")
                values[idx] = json.dumps(updates["embedding"], ensure_ascii=False)
            conn.execute(
                f"UPDATE topics SET {set_clause} WHERE id = ?", values
            )
            conn.commit()
            # embedding 更新后清除缓存，确保下次检索重新加载
            if "embedding" in updates and hasattr(self, '_embedding_cache'):
                self._embedding_cache.discard(topic_id)
            return conn.total_changes > 0
        finally:
            conn.close()

    def record_access(self, topic_id: str):
        """记录访问，增加 access_count 并提升置信度"""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """UPDATE topics
                   SET access_count = access_count + 1,
                       confidence = MIN(1.0, confidence + 0.05),
                       updated_at = ?
                   WHERE id = ?""",
                (datetime.utcnow().isoformat(), topic_id),
            )
            conn.commit()
        finally:
            conn.close()

    def delete_topic(self, topic_id: str) -> bool:
        """删除 Topic（同时删除关联的链接）"""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("DELETE FROM topic_links WHERE source_id = ? OR target_id = ?",
                         (topic_id, topic_id))
            conn.execute("DELETE FROM topics WHERE id = ?", (topic_id,))
            conn.commit()
            return conn.total_changes > 0
        finally:
            conn.close()

    def get_all_topics(self, limit: int = 100, offset: int = 0) -> List[Dict]:
        """获取所有 Topic"""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM topics ORDER BY confidence DESC, created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            conn.close()

    def count(self) -> int:
        """Topic 总数"""
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute("SELECT COUNT(*) as cnt FROM topics").fetchone()
            return row[0] if row else 0
        finally:
            conn.close()

    # ── 关联（弱关联传播）──

    def add_link(self, source_id: str, target_id: str,
                 strength: float = 0.5,
                 link_type: str = "related") -> bool:
        """在两个 Topic 之间建立关联"""
        if source_id == target_id:
            return False
        conn = sqlite3.connect(self.db_path)
        try:
            # 检查是否已存在
            existing = conn.execute(
                """SELECT id FROM topic_links
                   WHERE (source_id = ? AND target_id = ?)
                      OR (source_id = ? AND target_id = ?)""",
                (source_id, target_id, target_id, source_id),
            ).fetchone()
            if existing:
                # 更新强度
                conn.execute(
                    "UPDATE topic_links SET strength = ?, updated_at = ? WHERE id = ?",
                    (strength, datetime.utcnow().isoformat(), existing[0]),
                )
            else:
                conn.execute(
                    """INSERT INTO topic_links
                       (source_id, target_id, strength, link_type, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (source_id, target_id, strength, link_type,
                     datetime.utcnow().isoformat()),
                )
            conn.commit()
            return True
        finally:
            conn.close()

    def get_related_topics(self, topic_id: str, max_depth: int = 1) -> List[Tuple[Dict, float]]:
        """
        获取关联的 Topic（支持弱关联传播）

        Args:
            topic_id: 源 Topic ID
            max_depth: 传播深度（1 = 直接关联，2 = 间接关联）

        Returns:
            [(topic_dict, strength), ...]
        """
        visited = {topic_id}
        results = []

        def _walk(current_id: str, depth: int, decay: float):
            if depth > max_depth:
                return
            conn = sqlite3.connect(self.db_path)
            try:
                conn.row_factory = sqlite3.Row
                links = conn.execute(
                    """SELECT source_id, target_id, strength FROM topic_links
                       WHERE source_id = ? OR target_id = ?""",
                    (current_id, current_id),
                ).fetchall()
                for link in links:
                    neighbor = link["target_id"] if link["source_id"] == current_id else link["source_id"]
                    if neighbor in visited:
                        continue
                    visited.add(neighbor)
                    topic = self.get_topic(neighbor)
                    if topic:
                        propagated_strength = link["strength"] * decay
                        results.append((topic, propagated_strength))
                        _walk(neighbor, depth + 1, decay * 0.5)
            finally:
                conn.close()

        _walk(topic_id, 1, 1.0)
        results.sort(key=lambda x: x[1], reverse=True)
        return results

    # ── 检索 ──

    def search(self, query: str, top_k: int = 10,
               min_confidence: float = 0.1) -> List[Tuple[Dict, float]]:
        """
        检索相关 Topic（BM25 + 向量混合）

        策略:
          1. 尝试 embedding 向量检索（如果有）
          2. 降级到 BM25 全文检索
          3. RRF 融合（当两者都有结果时）
        """
        all_topics = self.get_all_topics(limit=1000)
        if not all_topics:
            return []

        texts = [f"{t['title']} {t['one_liner'] or ''} {t['summary'] or ''}"
                 for t in all_topics]

        # BM25 检索
        self._load_bm25(texts)
        bm25_results = self._bm25.search(query, top_k=top_k * 2)

        # 向量检索（如果有嵌入）
        vector_results = []
        if self._embedder.is_available():
            vector_results = self._search_vector(query, all_topics, top_k=top_k * 2)

        # RRF 融合
        if vector_results and bm25_results:
            merged = self._rrf_fusion(
                [(all_topics[i], s) for i, s in bm25_results],
                vector_results,
                k=60,
            )
        elif bm25_results:
            merged = [(all_topics[i], s) for i, s in bm25_results]
        else:
            merged = vector_results if vector_results else []

        # 过滤低置信度 + 排序
        filtered = [
            (t, s) for t, s in merged
            if t["confidence"] >= min_confidence
        ]
        return filtered[:top_k]

    def _search_vector(self, query: str, topics: List[Dict],
                       top_k: int) -> List[Tuple[Dict, float]]:
        """向量检索（需要嵌入）"""
        try:
            query_vec = self._embedder.embed(query)
            if query_vec is None:
                return []

            scored = []
            for topic in topics:
                emb = topic.get("embedding")
                if emb:
                    # 余弦相似度
                    sim = self._cosine_similarity(query_vec, emb)
                    scored.append((topic, sim))
            scored.sort(key=lambda x: x[1], reverse=True)
            return scored[:top_k]
        except Exception as e:
            logger.warning(f"Vector search failed: {e}")
            return []

    @staticmethod
    def _cosine_similarity(a: List[float], b: List[float]) -> float:
        if not a or not b:
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(x * x for x in b) ** 0.5
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)

    @staticmethod
    def _rrf_fusion(
        list_a: List[Tuple[Any, float]],
        list_b: List[Tuple[Any, float]],
        k: int = 60,
    ) -> List[Tuple[Any, float]]:
        """Reciprocal Rank Fusion"""
        rank_scores: Dict[int, float] = {}
        items = {}

        for rank, (item, _) in enumerate(list_a):
            idx = id(item)
            rank_scores[idx] = rank_scores.get(idx, 0) + 1.0 / (k + rank + 1)
            items[idx] = item
        for rank, (item, _) in enumerate(list_b):
            idx = id(item)
            rank_scores[idx] = rank_scores.get(idx, 0) + 1.0 / (k + rank + 1)
            items[idx] = item

        sorted_items = sorted(rank_scores.items(), key=lambda x: x[1], reverse=True)
        return [(items[idx], score) for idx, score in sorted_items]

    def _load_bm25(self, texts: List[str]):
        """加载 BM25 索引"""
        if self._bm25_loaded:
            return
        self._bm25.rebuild(texts)
        self._bm25_loaded = True

    def _sync_to_bm25(self):
        """重新同步 BM25 索引（增量添加后标记需要刷新）"""
        self._bm25_loaded = False

    # ── 维护 ──

    def decay_confidence(self, factor: float = 0.95):
        """
        衰减所有 Topic 的置信度（长期未访问的记忆逐渐降权）

        调用时机：定期（每天/每次启动）
        """
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE topics SET confidence = confidence * ? WHERE confidence > 0.1",
                (factor,),
            )
            conn.commit()
        finally:
            conn.close()

    def prune_topics(self, min_confidence: float = 0.05, keep_min: int = 50):
        """
        清理低置信度 Topic，保留至少 keep_min 个

        Returns:
            删除的数量
        """
        conn = sqlite3.connect(self.db_path)
        try:
            total = conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
            if total <= keep_min:
                return 0
            to_delete = conn.execute(
                """SELECT id FROM topics
                   WHERE confidence < ?
                   ORDER BY confidence ASC
                   LIMIT ?""",
                (min_confidence, total - keep_min),
            ).fetchall()
            ids = [row[0] for row in to_delete]
            for tid in ids:
                conn.execute("DELETE FROM topic_links WHERE source_id = ? OR target_id = ?",
                             (tid, tid))
                conn.execute("DELETE FROM topics WHERE id = ?", (tid,))
            conn.commit()
            return len(ids)
        finally:
            conn.close()

    def get_all_links(self) -> List[Dict]:
        """获取所有关联"""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM topic_links ORDER BY strength DESC"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ── 内部工具 ──

    @staticmethod
    def _row_to_dict(row) -> Dict:
        d = dict(row)
        if "tags" in d and d["tags"]:
            try:
                d["tags"] = json.loads(d["tags"])
            except (json.JSONDecodeError, TypeError):
                d["tags"] = []
        else:
            d["tags"] = []
        if "embedding" in d and d["embedding"]:
            try:
                d["embedding"] = json.loads(d["embedding"])
            except (json.JSONDecodeError, TypeError):
                d["embedding"] = None
        return d
