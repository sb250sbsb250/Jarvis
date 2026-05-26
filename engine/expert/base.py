"""
专家基类 — 领域专家的抽象定义

ExpertAgent 是领域专家的基类，子类只需定义四个属性:
  - domain: 领域元数据
  - tools:  工具类列表（3-5 个）
  - system_prompt: 领域深度定制的系统提示词
  - can_handle: 是否适合处理某条用户输入

架构说明:
  ExpertAgent 是纯配置类（无状态），不持有 llm_client 或 tool_registry。
  执行时由 ExpertOrchestrator 注入依赖并构建执行图。
"""

from typing import List, Type, Optional


class ExpertDomain:
    """领域定义元数据"""

    def __init__(
        self,
        name: str,
        display_name: str,
        description: str,
        icon: str = "🤖",
        priority: int = 50,
    ):
        self.name = name
        self.display_name = display_name
        self.description = description
        self.icon = icon
        self.priority = priority

    def __repr__(self) -> str:
        return f"{self.icon} {self.display_name}"


class ExpertAgent:
    """
    领域专家基类

    子类只需实现 domain / tools / system_prompt / can_handle 四个方法。
    can_handle 返回 0.0-1.0 的置信度，用于 ExpertRegistry 的路由选择。
    """

    @property
    def domain(self) -> ExpertDomain:
        """领域定义（子类必须实现）"""
        raise NotImplementedError

    @property
    def tools(self) -> List[Type]:
        """该专家需要的工具类列表（子类必须实现）"""
        raise NotImplementedError

    @property
    def system_prompt(self) -> str:
        """专属系统提示词（子类必须实现）"""
        raise NotImplementedError

    def can_handle(self, user_input: str) -> float:
        """
        判断是否能处理该用户输入。

        返回 0.0-1.0 的置信度。基类提供简单关键词匹配，
        子类可覆盖为语义匹配或 ML 分类器。
        """
        _ = user_input
        return 0.5  # 默认中等置信度
