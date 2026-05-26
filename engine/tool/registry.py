"""
工具注册中心 - 纯懒加载模式
"""
import logging
from typing import Dict, Optional, List, Type, Any, Callable
from threading import Lock

from .base import BaseTool, ToolSchema

logger = logging.getLogger("ToolRegistry")


class ToolRegistry:
    """
    工具注册中心 - 纯懒加载

    设计原则：
    1. 注册时只存类 + 初始化参数，不实例化
    2. 获取 Schema 时，临时实例化获取元数据后即可丢弃
    3. 真正执行时，实例化并缓存
    4. 线程安全
    """

    _instance: Optional["ToolRegistry"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        # 工具类注册表: {name: (tool_class, init_kwargs)}
        self._registry: Dict[str, tuple] = {}

        # 实例缓存: {name: tool_instance}
        self._cache: Dict[str, BaseTool] = {}

        # Schema 缓存: {name: ToolSchema}
        self._schema_cache: Dict[str, ToolSchema] = {}

        # 线程锁
        self._lock = Lock()

        self._initialized = True
        logger.debug("ToolRegistry 初始化（纯懒加载模式）")

    # ========== 注册（只存类，不实例化） ==========

    def register(self, tool_class: Type[BaseTool], **init_kwargs) -> "ToolRegistry":
        """
        注册工具类（懒加载）

        Args:
            tool_class: 工具类（BaseTool 子类）
            **init_kwargs: 初始化参数

        Returns:
            self
        """
        with self._lock:
            try:
                temp = tool_class(**init_kwargs)
                name = temp.name
            except Exception as e:
                logger.error(f"获取工具名称失败: {tool_class.__name__}: {e}")
                return self

            if name in self._registry:
                logger.warning(f"工具 '{name}' 已注册，将被覆盖")

            self._registry[name] = (tool_class, dict(init_kwargs))
            self._cache.pop(name, None)
            self._schema_cache.pop(name, None)

            logger.debug(f"注册工具类: {name} (懒加载)")
        return self

    def register_many(self, tool_classes: List[Type[BaseTool]]) -> "ToolRegistry":
        """批量注册工具类"""
        for tc in tool_classes:
            self.register(tc)
        return self

    def register_with_config(
        self,
        tool_classes: Dict[Type[BaseTool], Dict[str, Any]]
    ) -> "ToolRegistry":
        """注册工具类并带配置"""
        for tc, kwargs in tool_classes.items():
            self.register(tc, **kwargs)
        return self

    # ========== 获取 Schema（轻量，临时实例化） ==========

    def get_schema(self, name: str) -> Optional[ToolSchema]:
        """获取单个工具的 Schema"""
        if name in self._schema_cache:
            return self._schema_cache[name]

        if name in self._cache:
            schema = self._cache[name].get_schema()
            self._schema_cache[name] = schema
            return schema

        if name in self._registry:
            tool_class, init_kwargs = self._registry[name]
            try:
                temp = tool_class(**init_kwargs)
                schema = temp.get_schema()
                self._schema_cache[name] = schema
                return schema
            except Exception as e:
                logger.error(f"获取 Schema 失败: {name}: {e}")
                return None

        return None

    def get_all_schemas(self) -> List[ToolSchema]:
        """获取所有工具的 Schema（用于发送给 LLM）"""
        schemas = []
        for name in list(self._registry.keys()):
            schema = self.get_schema(name)
            if schema:
                schemas.append(schema)
        return schemas

    def get_openai_tools(self) -> List[Dict]:
        """获取 OpenAI 格式的工具列表"""
        return [schema.to_openai_tool() for schema in self.get_all_schemas()]

    # ========== 获取实例（真正使用时才实例化并缓存） ==========

    def get(self, name: str) -> Optional[BaseTool]:
        """获取工具实例（懒加载：首次调用才实例化）"""
        if name in self._cache:
            logger.debug(f"从缓存获取: {name}")
            return self._cache[name]

        if name in self._registry:
            tool_class, init_kwargs = self._registry[name]
            try:
                logger.debug(f"懒加载实例化: {name}")
                tool = tool_class(**init_kwargs)
                self._cache[name] = tool
                self._schema_cache[name] = tool.get_schema()
                return tool
            except Exception as e:
                logger.error(f"实例化失败: {name}: {e}")
                return None

        return None

    # ========== 查询 ==========

    def list_tools(self) -> List[str]:
        """列出所有注册的工具名称"""
        return list(self._registry.keys())

    def has_tool(self, name: str) -> bool:
        return name in self._registry

    def count(self) -> int:
        return len(self._registry)

    def cached_count(self) -> int:
        return len(self._cache)

    def get_status(self) -> Dict:
        return {
            "registered": self.count(),
            "cached": self.cached_count(),
            "lazy_pending": self.count() - self.cached_count(),
            "tool_names": self.list_tools(),
            "cached_names": list(self._cache.keys()),
        }

    # ========== 管理 ==========

    def remove(self, name: str) -> bool:
        with self._lock:
            self._registry.pop(name, None)
            self._cache.pop(name, None)
            self._schema_cache.pop(name, None)
            return name not in self._registry

    def clear_cache(self) -> None:
        with self._lock:
            count = len(self._cache)
            self._cache.clear()
            self._schema_cache.clear()
            logger.debug(f"清除 {count} 个工具实例缓存")

    def clear(self) -> "ToolRegistry":
        with self._lock:
            self._registry.clear()
            self._cache.clear()
            self._schema_cache.clear()
        return self

    def print_summary(self) -> None:
        status = self.get_status()
        logger.info(f"工具注册摘要: {status['registered']} 个注册, {status['cached']} 个已实例化, {status['lazy_pending']} 个待懒加载")
        for name in sorted(self.list_tools()):
            state = "✅ 已缓存" if name in self._cache else "💤 懒加载"
            logger.info(f"  {state} {name}")
