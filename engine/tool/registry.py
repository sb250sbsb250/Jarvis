"""
工具注册中心 — v3.0 原子工具扁平化

设计：
  - 注册的是工具大类（BaseTool 子类）
  - 导出的是扁平化原子工具列表（供 LLM 调用）
  - 执行时按原子工具名路由到对应大类的 handler
"""

import json
import logging
import re
import time
from typing import Dict, List, Optional, Type, Any

from .base import BaseTool, ToolDefinition

logger = logging.getLogger("ToolRegistry")


class ToolRegistry:
    """
    工具注册中心

    注册大类 → 导出原子工具 → 按名执行
    """

    _instance: Optional["ToolRegistry"] = None
    _singleton_enabled: bool = True  # 类变量控制，测试时可禁用

    def __new__(cls):
        if cls._singleton_enabled and cls._instance is not None:
            return cls._instance
        instance = super().__new__(cls)
        if cls._singleton_enabled:
            cls._instance = instance
            instance._initialized = False
        else:
            instance._initialized = False
        return instance

    @classmethod
    def disable_singleton(cls):
        """测试用：禁用单例模式，允许创建独立实例"""
        cls._singleton_enabled = False
        cls._instance = None

    @classmethod
    def enable_singleton(cls):
        """恢复单例模式"""
        cls._singleton_enabled = True
        cls._instance = None

    def __init__(self):
        if self._initialized:
            return

        # 大类注册表: {namespace: (tool_class, init_kwargs)}
        self._categories: Dict[str, tuple] = {}

        # 大类实例缓存: {namespace: instance}
        self._instances: Dict[str, BaseTool] = {}

        # 扁平化原子工具: {atomic_name: (namespace, ToolDefinition)}
        self._atomic_tools: Dict[str, tuple] = {}

        # OpenAI 格式缓存
        self._openai_cache: Optional[List[Dict]] = None

        # 结果缓存
        self._result_cache: Dict[str, tuple] = {}
        self._result_cache_ttl: float = 300.0

        self._lock = __import__('threading').Lock()

        self._initialized = True
        logger.debug("ToolRegistry v3 初始化（原子工具扁平化）")

    @classmethod
    def reset(cls):
        if cls._instance:
            cls._instance.clear()
        cls._instance = None

    # ── 注册（懒加载）──

    def register(self, tool_class: Type[BaseTool], **init_kwargs) -> "ToolRegistry":
        """注册工具大类"""
        with self._lock:
            try:
                temp = tool_class(**init_kwargs)
                ns = temp.name
            except Exception as e:
                logger.error(f"实例化失败 {tool_class.__name__}: {e}")
                return self

            self._categories[ns] = (tool_class, dict(init_kwargs))
            self._instances.pop(ns, None)
            self._atomic_tools.clear()
            self._openai_cache = None

            # 校验工具名规范
            if not re.match(r'^[a-z][a-z0-9_]*$', ns):
                logger.warning(f"大类名 '{ns}' 非 snake_case")

            logger.debug(f"注册大类: {ns}")
        return self

    def register_many(self, tool_classes: List[Type[BaseTool]]) -> "ToolRegistry":
        for tc in tool_classes:
            self.register(tc)
        return self

    def register_with_config(self, config: Dict[Type[BaseTool], Dict]) -> "ToolRegistry":
        for tc, kwargs in config.items():
            self.register(tc, **kwargs)
        return self

    # ── 获取实例（懒加载）──

    def _get_instance(self, namespace: str) -> Optional[BaseTool]:
        if namespace in self._instances:
            return self._instances[namespace]

        if namespace in self._categories:
            cls, kwargs = self._categories[namespace]
            try:
                inst = cls(**kwargs)
                self._instances[namespace] = inst
                return inst
            except Exception as e:
                logger.error(f"实例化失败 {namespace}: {e}")
        return None

    # ── 扁平化原子工具 ──

    def _build_flattened_index(self):
        """构建扁平化原子工具索引"""
        if self._atomic_tools:
            return

        for ns in self._categories:
            inst = self._get_instance(ns)
            if not inst:
                continue

            tool_defs = inst.tools if callable(inst.tools) else inst.tools
            for td in tool_defs:
                if td.name in self._atomic_tools:
                    prev_ns = self._atomic_tools[td.name][0]
                    logger.warning(f"原子工具 '{td.name}' 冲突: {prev_ns} vs {ns}")
                self._atomic_tools[td.name] = (ns, td)

    # ── 查询 ──

    def list_tools(self) -> List[str]:
        """列出所有原子工具名"""
        self._build_flattened_index()
        return list(self._atomic_tools.keys())

    def list_categories(self) -> List[str]:
        """列出所有大类名"""
        return list(self._categories.keys())

    def get_all_definitions(self) -> List[ToolDefinition]:
        """获取所有原子工具定义"""
        self._build_flattened_index()
        return [td for _, td in sorted(self._atomic_tools.values(), key=lambda x: x[0])]

    def get_openai_tools(self) -> List[Dict]:
        """获取 OpenAI 格式的工具列表（带缓存）"""
        if self._openai_cache:
            return self._openai_cache

        defs = self.get_all_definitions()
        result = [td.to_openai_format() for td in defs]
        self._openai_cache = result
        return result

    def get(self, atomic_name: str) -> Optional[BaseTool]:
        """【向后兼容】通过原子工具名获取所属大类的实例"""
        self._build_flattened_index()
        entry = self._atomic_tools.get(atomic_name)
        if entry:
            ns = entry[0]
            return self._get_instance(ns)
        return None

    def get_tool_def(self, atomic_name: str) -> Optional[ToolDefinition]:
        """获取原子工具定义"""
        self._build_flattened_index()
        entry = self._atomic_tools.get(atomic_name)
        return entry[1] if entry else None

    def is_read_tool(self, atomic_name: str) -> bool:
        """判断原子工具是否只读"""
        td = self.get_tool_def(atomic_name)
        return td.is_read if td else False

    def has_tool(self, atomic_name: str) -> bool:
        self._build_flattened_index()
        return atomic_name in self._atomic_tools

    def count_atomic(self) -> int:
        self._build_flattened_index()
        return len(self._atomic_tools)

    def count_categories(self) -> int:
        return len(self._categories)

    # ── 执行 ──

    async def execute(self, atomic_name: str, call_id: str, **kwargs) -> Any:
        """执行原子工具

        Args:
            atomic_name: 原子工具名（如 "excel_read_sheet"）
            call_id: 调用 ID
            **kwargs: 工具参数
        Returns:
            ToolResult
        """
        self._build_flattened_index()
        entry = self._atomic_tools.get(atomic_name)
        if not entry:
            raise ValueError(f"未知工具: {atomic_name}")

        ns, td = entry
        inst = self._get_instance(ns)
        if not inst:
            raise ValueError(f"无法实例化工具类: {ns}")

        return await inst.execute(call_id, atomic_name, **kwargs)

    # ── 状态 ──

    def get_status(self) -> Dict:
        return {
            "categories": self.count_categories(),
            "atomic_tools": self.count_atomic(),
            "names": self.list_tools(),
            "category_list": self.list_categories(),
        }

    # ── 结果缓存 ──

    def get_cached_result(self, tool_name: str, args: Dict) -> Optional[Any]:
        key = f"{tool_name}:{json.dumps(args, sort_keys=True)}"
        entry = self._result_cache.get(key)
        if not entry:
            return None
        cached_at, result = entry
        if time.monotonic() - cached_at > self._result_cache_ttl:
            self._result_cache.pop(key, None)
            return None
        return result

    def cache_result(self, tool_name: str, args: Dict, result: Any) -> None:
        key = f"{tool_name}:{json.dumps(args, sort_keys=True)}"
        self._result_cache[key] = (time.monotonic(), result)
        if len(self._result_cache) > 1000:
            sorted_items = sorted(
                self._result_cache.items(),
                key=lambda x: x[1][0],
            )
            for k, _ in sorted_items[:len(sorted_items) // 2]:
                self._result_cache.pop(k, None)

    # ── 校验 ──

    def validate_all(self) -> List[str]:
        """检查所有工具的一致性"""
        issues = []
        self._build_flattened_index()

        seen_names = {}
        for atomic_name, (ns, td) in self._atomic_tools.items():
            # 命名重复检查
            if atomic_name in seen_names:
                issues.append(f"原子工具名冲突: '{atomic_name}' (大类 {seen_names[atomic_name]} vs {ns})")
            seen_names[atomic_name] = ns

            # 规范检查
            if not re.match(r'^[a-z][a-z0-9_]*$', atomic_name):
                issues.append(f"'{atomic_name}': 名称非 snake_case")

        return issues

    # ── 管理 ──

    def remove(self, ns: str) -> bool:
        with self._lock:
            self._categories.pop(ns, None)
            self._instances.pop(ns, None)
            self._atomic_tools.clear()
            self._openai_cache = None
        return ns not in self._categories

    def clear_cache(self):
        with self._lock:
            self._instances.clear()
            self._atomic_tools.clear()
            self._openai_cache = None

    def clear(self):
        with self._lock:
            self._categories.clear()
            self._instances.clear()
            self._atomic_tools.clear()
            self._openai_cache = None

    def print_summary(self) -> None:
        status = self.get_status()
        logger.info(
            f"ToolRegistry v3: {status['categories']} 个大类, "
            f"{status['atomic_tools']} 个原子工具"
        )
        for ns in sorted(self.list_categories()):
            inst = self._get_instance(ns)
            tool_names = inst.get_tool_names() if inst else []
            logger.info(f"  📦 {ns} ({len(tool_names)} 个工具): {', '.join(tool_names)}")
