"""
长期记忆 - 向量检索 + 记忆存储
"""

import logging
from typing import Optional, List, Dict, Any, Callable
from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

logger = logging.getLogger(__name__)


@dataclass
class MemoryItem:
    """记忆项"""
    id: str = field(default_factory=lambda: f"mem_{uuid4().hex[:8]}")
    content: str = ""
    embedding: Optional[List[float]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)
    last_accessed_at: datetime = field(default_factory=datetime.now)

    def touch(self) -> None:
        self.last_accessed_at = datetime.now()

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "content": self.content,
            "metadata": self.metadata,
            "created_at": self.created_at.isoformat(),
            "last_accessed_at": self.last_accessed_at.isoformat(),
        }


class VectorStore:
    """简单的向量存储（内存实现，可替换为 FAISS/Chroma）"""

    def __init__(self, dimension: int = 384):
        self.dimension = dimension
        self._items: Dict[str, MemoryItem] = {}

    def add(self, item: MemoryItem) -> None:
        """添加记忆"""
        self._items[item.id] = item

    def get(self, item_id: str) -> Optional[MemoryItem]:
        """获取记忆"""
        item = self._items.get(item_id)
        if item:
            item.touch()
        return item

    def delete(self, item_id: str) -> bool:
        """删除记忆"""
        if item_id in self._items:
            del self._items[item_id]
            return True
        return False

    def search(
        self,
        query_embedding: List[float],
        top_k: int = 5,
        threshold: float = 0.5,
    ) -> List[tuple[MemoryItem, float]]:
        """
        搜索相似记忆

        Args:
            query_embedding: 查询向量
            top_k: 返回数量
            threshold: 相似度阈值

        Returns:
            (记忆项, 相似度分数) 列表
        """
        results = []

        for item in self._items.values():
            if item.embedding:
                similarity = self._cosine_similarity(query_embedding, item.embedding)
                if similarity >= threshold:
                    results.append((item, similarity))

        # 按相似度排序
        results.sort(key=lambda x: x[1], reverse=True)

        return results[:top_k]

    def _cosine_similarity(self, a: List[float], b: List[float]) -> float:
        """计算余弦相似度"""
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5

        if norm_a == 0 or norm_b == 0:
            return 0.0

        return dot / (norm_a * norm_b)

    def clear(self) -> None:
        """清空所有记忆"""
        self._items.clear()

    def count(self) -> int:
        return len(self._items)


class DefaultEmbedder:
    """
    默认嵌入器 — 基于字符哈希的轻量嵌入

    不使用外部依赖，纯 Python 实现。
    每个 Token 映射到固定维度的哈希向量，适合做初步的语义检索。
    生产环境建议替换为 SentenceTransformer / OpenAI Embeddings。
    """

    def __init__(self, dimension: int = 384):
        self.dimension = dimension

    async def embed(self, text: str) -> List[float]:
        """将文本转为向量"""
        vec = [0.0] * self.dimension
        if not text:
            return vec

        # 对每个字符，用 hash 确定它在向量中的位置和贡献
        for i, ch in enumerate(text):
            idx = (hash(ch) % (self.dimension - 2)) + 1
            # 位置编码 + 字符值，使"你好"和"好你"的向量不同
            pos_factor = 1.0 / (1.0 + i * 0.05)
            val = (ord(ch) % 100) / 100.0 * pos_factor
            vec[idx] += val
            vec[0] += val * 0.1  # bias 项

        # L2 归一化
        norm = sum(x * x for x in vec) ** 0.5
        if norm > 0:
            vec = [x / norm for x in vec]

        return vec


class LongTermMemory:
    """
    长期记忆管理器

    负责：
    - 存储重要对话为长期记忆
    - 根据查询检索相关记忆
    - 记忆的自动维护（清理、合并）
    """

    def __init__(
        self,
        embedder: Optional[Any] = None,
        vector_store: Optional[VectorStore] = None,
        max_memories: int = 1000,
    ):
        """
        Args:
            embedder: 嵌入生成器（需要实现 embed(text) -> List[float]）
                      None 时自动使用 DefaultEmbedder（不依赖外部库的哈希嵌入）
            vector_store: 向量存储实例
            max_memories: 最大记忆数量
        """
        self.embedder = embedder or DefaultEmbedder(dimension=384)
        self.vector_store = vector_store or VectorStore()
        self.max_memories = max_memories

    async def remember(
        self,
        content: str,
        metadata: Optional[Dict] = None,
        importance: float = 0.5,
    ) -> Optional[MemoryItem]:
        """
        存储记忆

        Args:
            content: 记忆内容
            metadata: 元数据
            importance: 重要性（0-1），决定是否存储
        """
        # 根据重要性决定是否存储
        if importance < 0.3:
            return None

        # 生成嵌入
        embedding = None
        if self.embedder:
            try:
                embedding = await self.embedder.embed(content)
            except Exception as e:
                logger.error(f"Failed to generate embedding: {e}")

        # 创建记忆项
        item = MemoryItem(
            content=content,
            embedding=embedding,
            metadata=metadata or {},
        )

        # 检查是否需要清理
        if self.vector_store.count() >= self.max_memories:
            self._cleanup()

        self.vector_store.add(item)
        logger.debug(f"Stored memory: {item.id[:8]}...")

        return item

    async def recall(
        self,
        query: str,
        top_k: int = 5,
        threshold: float = 0.5,
    ) -> List[MemoryItem]:
        """
        检索相关记忆

        Args:
            query: 查询文本
            top_k: 返回数量
            threshold: 相似度阈值
        """
        if not self.embedder:
            # 不会触发：__init__ 中会自动创建 DefaultEmbedder
            return []

        try:
            # 生成查询嵌入
            query_embedding = await self.embedder.embed(query)

            # 搜索相似记忆
            results = self.vector_store.search(
                query_embedding,
                top_k=top_k,
                threshold=threshold,
            )

            # 返回记忆项
            memories = [item for item, _ in results]

            logger.debug(f"Recalled {len(memories)} memories for query: {query[:50]}...")
            return memories

        except Exception as e:
            logger.error(f"Failed to recall memories: {e}")
            return []

    async def forget(self, memory_id: str) -> bool:
        """删除记忆"""
        return self.vector_store.delete(memory_id)

    async def forget_old(self, days: int = 30) -> int:
        """删除超过 days 天未被访问的记忆"""
        now = datetime.now()
        to_delete = []
        for mem_id, item in list(self.vector_store._items.items()):
            if (now - item.last_accessed_at).days >= days:
                to_delete.append(mem_id)
        for mem_id in to_delete:
            self.vector_store.delete(mem_id)
        count = len(to_delete)
        if count:
            logger.info(f"Forgot {count} old memories (not accessed in {days}+ days)")
        return count

    async def clear_all(self) -> None:
        """清空所有记忆"""
        self.vector_store.clear()
        logger.info("Cleared all memories")

    async def save(self, filepath: str) -> None:
        """保存记忆到文件"""
        import json
        data = {
            "memories": [
                {
                    "id": mem.id,
                    "content": mem.content,
                    "metadata": mem.metadata,
                    "created_at": mem.created_at.isoformat(),
                    "last_accessed_at": mem.last_accessed_at.isoformat(),
                }
                for mem in self.vector_store._items.values()
            ]
        }
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)
        logger.info(f"Saved {len(data['memories'])} memories to {filepath}")

    async def load(self, filepath: str) -> None:
        """从文件加载记忆"""
        import json
        try:
            with open(filepath, "r") as f:
                data = json.load(f)

            for mem_data in data.get("memories", []):
                item = MemoryItem(
                    id=mem_data["id"],
                    content=mem_data["content"],
                    metadata=mem_data.get("metadata", {}),
                    created_at=datetime.fromisoformat(mem_data["created_at"]),
                    last_accessed_at=datetime.fromisoformat(mem_data["last_accessed_at"]),
                )
                self.vector_store.add(item)

            logger.info(f"Loaded {len(data.get('memories', []))} memories from {filepath}")
        except Exception as e:
            logger.error(f"Failed to load memories: {e}")

    def _cleanup(self) -> None:
        """清理记忆（删除最旧的）"""
        # 按最后访问时间排序，删除最旧的
        items = list(self.vector_store._items.items())
        items.sort(key=lambda x: x[1].last_accessed_at)

        # 删除最旧的 10%
        to_remove = items[:int(len(items) * 0.1)]
        for item_id, _ in to_remove:
            self.vector_store.delete(item_id)

        logger.debug(f"Cleaned up {len(to_remove)} memories")
