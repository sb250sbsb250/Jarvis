"""
skill/examples.py — 内置 Skill 示例

每个 Skill 封装一个可复用的 DAG 执行经验。
"""

import logging
from typing import List, Type

from ..dag.graph import WorkflowGraph
from ..dag.node import (
    LLMNode, RouterNode, ToolNode,
    HumanInLoopNode, MapNode, ToolDispatchNode,
    ListFilesNode, FileProcessorNode, CodeSearchNode,
    CodeEditorNode, FileRenameNode,
)
from .base import Skill, SkillMeta

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════
# Skill 1: 代码审查
# ═══════════════════════════════════════

class CodeReviewSkill(Skill):
    """代码审查 Skill — 搜索 + 分析 + 报告"""

    @property
    def meta(self) -> SkillMeta:
        return SkillMeta(
            name="code_review",
            display_name="代码审查",
            description="对指定代码文件进行全面审查，生成改进建议",
            icon="🔍",
            tags=["code", "review", "analysis", "quality"],
        )

    @property
    def required_tools(self) -> List[Type]:
        return []

    @property
    def trigger_keywords(self) -> List[str]:
        return [
            "代码审查", "review", "审查代码", "检查代码",
            "code review", "审查这个文件", "看看这段代码",
            "代码质量", "有什么问题", "改进建议",
        ]

    def build_graph(self, **kwargs) -> WorkflowGraph:
        graph = WorkflowGraph("code_review")

        analyze = LLMNode(
            name="analyze",
            system_prompt="""你是一位资深代码审查专家。请从以下维度审查代码：
1. **安全性**: SQL注入、XSS、权限问题
2. **性能**: 时间复杂度、内存使用、不必要的IO
3. **可维护性**: 命名规范、代码结构、注释质量
4. **最佳实践**: 语言特性使用、设计模式
5. **错误处理**: 异常捕获、边界条件

请给出具体的改进建议，按严重程度排序。""",
        )
        report = LLMNode(
            name="report",
            system_prompt="将审查结果整理为结构化的 Markdown 报告，包含摘要、问题列表、改进建议。",
        )

        graph.add_node(analyze)
        graph.add_node(report)
        graph.add_edge("analyze", "report")
        graph.set_entry("analyze")
        graph.set_exit("report")

        return graph


# ═══════════════════════════════════════
# Skill 2: 代码生成
# ═══════════════════════════════════════

class CodeGenerationSkill(Skill):
    """代码生成 Skill — 需求分析 + 代码生成 + 自检"""

    @property
    def meta(self) -> SkillMeta:
        return SkillMeta(
            name="code_generation",
            display_name="代码生成",
            description="根据需求描述生成高质量代码",
            icon="💻",
            tags=["code", "generation", "create"],
        )

    @property
    def required_tools(self) -> List[Type]:
        return []

    @property
    def trigger_keywords(self) -> List[str]:
        return [
            "写代码", "生成代码", "实现一个", "创建一个",
            "写一个", "帮我写", "编写", "生成函数",
            "生成", "code generation", "write code", "implement",
            "写段代码", "写个脚本", "编写脚本",
        ]

    def build_graph(self, **kwargs) -> WorkflowGraph:
        graph = WorkflowGraph("code_generation")

        plan = LLMNode(
            name="plan",
            system_prompt="分析用户需求，列出代码的关键设计点和技术选型。",
        )
        generate = LLMNode(
            name="generate",
            system_prompt="""根据需求和设计点生成代码。
要求：
- 代码完整可运行
- 包含必要的注释
- 处理边界情况
- 遵循最佳实践""",
        )
        review = LLMNode(
            name="review",
            system_prompt="审查生成的代码，检查是否有遗漏或错误，给出改进意见。",
        )
        final = LLMNode(
            name="final",
            system_prompt="综合审查意见，输出最终版本代码。",
        )

        for node in [plan, generate, review, final]:
            graph.add_node(node)

        graph.add_edge("plan", "generate")
        graph.add_edge("generate", "review")
        graph.add_edge("review", "final")
        graph.set_entry("plan")
        graph.set_exit("final")

        return graph


# ═══════════════════════════════════════
# Skill 3: 错误调试
# ═══════════════════════════════════════

class DebugSkill(Skill):
    """错误调试 Skill — 分析错误 + 定位原因 + 修复建议"""

    @property
    def meta(self) -> SkillMeta:
        return SkillMeta(
            name="debug",
            display_name="错误调试",
            description="分析错误信息，定位根因，给出修复方案",
            icon="🐛",
            tags=["debug", "error", "fix", "troubleshooting"],
        )

    @property
    def required_tools(self) -> List[Type]:
        return []

    @property
    def trigger_keywords(self) -> List[str]:
        return [
            "报错", "出错", "错误", "bug", "调试", "debug",
            "不工作", "失败", "异常", "exception", "error",
            "为什么", "怎么回事", "帮我看看", "修复",
        ]

    def build_graph(self, **kwargs) -> WorkflowGraph:
        graph = WorkflowGraph("debug")

        analyze = LLMNode(
            name="analyze",
            system_prompt="""你是一位资深调试专家。请分析以下错误：
1. 错误类型是什么？
2. 可能的原因有哪些？
3. 如何复现？
4. 最可能的根因是什么？""",
        )
        solution = LLMNode(
            name="solution",
            system_prompt="基于根因分析，给出具体的修复步骤和代码修改建议。",
        )

        graph.add_node(analyze)
        graph.add_node(solution)
        graph.add_edge("analyze", "solution")
        graph.set_entry("analyze")
        graph.set_exit("solution")

        return graph


# ═══════════════════════════════════════
# Skill 4: 文档生成
# ═══════════════════════════════════════

class DocumentationSkill(Skill):
    """文档生成 Skill — 分析代码 + 生成文档"""

    @property
    def meta(self) -> SkillMeta:
        return SkillMeta(
            name="documentation",
            display_name="文档生成",
            description="为代码生成 API 文档或使用说明",
            icon="📝",
            tags=["documentation", "docs", "api", "说明"],
        )

    @property
    def required_tools(self) -> List[Type]:
        return []

    @property
    def trigger_keywords(self) -> List[str]:
        return [
            "文档", "注释", "doc", "documentation",
            "生成文档", "写文档", "api文档", "使用说明",
            "readme", "README",
        ]

    def build_graph(self, **kwargs) -> WorkflowGraph:
        graph = WorkflowGraph("documentation")

        analyze = LLMNode(
            name="analyze",
            system_prompt="分析代码，提取所有公共API、参数、返回值、使用示例。",
        )
        generate = LLMNode(
            name="generate",
            system_prompt="生成结构化的 Markdown 文档，包含概述、API列表、示例代码。",
        )

        graph.add_node(analyze)
        graph.add_node(generate)
        graph.add_edge("analyze", "generate")
        graph.set_entry("analyze")
        graph.set_exit("generate")

        return graph


# ═══════════════════════════════════════
# Skill 5: 项目初始化
# ═══════════════════════════════════════

class ProjectInitSkill(Skill):
    """项目初始化 Skill — 需求分析 + 项目结构 + 初始化"""

    @property
    def meta(self) -> SkillMeta:
        return SkillMeta(
            name="project_init",
            display_name="项目初始化",
            description="根据需求创建项目结构和配置文件",
            icon="🚀",
            tags=["project", "init", "scaffold", "create"],
        )

    @property
    def required_tools(self) -> List[Type]:
        return []

    @property
    def trigger_keywords(self) -> List[str]:
        return [
            "创建项目", "初始化项目", "新建项目",
            "项目结构", "项目模板", "项目骨架",
            "脚手架", "脚手架工具", "create project",
            "init project", "scaffold", "project init",
            "initial project", "项目创建",
            "建一个项目", "搭一个项目", "搭建一个",
        ]

    def build_graph(self, **kwargs) -> WorkflowGraph:
        graph = WorkflowGraph("project_init")

        plan = LLMNode(
            name="plan",
            system_prompt="""分析用户需求，规划项目结构：
1. 推荐的技术栈
2. 目录结构
3. 核心配置文件
4. 依赖管理""",
        )
        generate = LLMNode(
            name="generate",
            system_prompt="""生成完整的项目初始化方案，包括：
1. 目录结构（树形展示）
2. 每个文件的用途说明
3. 核心文件的内容
4. 初始化和运行命令""",
        )

        graph.add_node(plan)
        graph.add_node(generate)
        graph.add_edge("plan", "generate")
        graph.set_entry("plan")
        graph.set_exit("generate")

        return graph


# ═══════════════════════════════════════
# Skill 6: 数据探索
# ═══════════════════════════════════════

class DataExploreSkill(Skill):
    """数据探索 Skill — 读取 → 分析 → 可视化"""

    @property
    def meta(self) -> SkillMeta:
        return SkillMeta(
            name="data_explore",
            display_name="数据探索",
            description="读取数据文件并进行分析和可视化",
            icon="📊",
            tags=["data", "analyze", "chart", "csv"],
        )

    @property
    def required_tools(self) -> List[Type]:
        return []

    @property
    def trigger_keywords(self) -> List[str]:
        return [
            "分析数据", "数据分析", "统计", "图表",
            "data analysis", "analyze data", "chart",
            "csv", "excel", "可视化", "visualization",
            "数据文件", "数据统计", "统计一下",
            "数据探索", "查看数据", "数据表",
        ]

    def build_graph(self, **kwargs) -> WorkflowGraph:
        graph = WorkflowGraph("data_explore")

        understand = LLMNode(
            name="understand",
            system_prompt="分析用户需求和数据，确定分析目标和需要的统计方法。",
        )
        analyze = LLMNode(
            name="analyze",
            system_prompt="""执行数据分析。给出：
1. 数据概览（行数、列数、类型）
2. 关键统计指标（均值、中位数、分布）
3. 发现的模式和异常
4. 建议的可视化方式""",
        )
        report = LLMNode(
            name="report",
            system_prompt="将分析结果整理为结构化的报告，包含数据和结论。",
        )

        graph.add_node(understand)
        graph.add_node(analyze)
        graph.add_node(report)
        graph.add_edge("understand", "analyze")
        graph.add_edge("analyze", "report")
        graph.set_entry("understand")
        graph.set_exit("report")

        return graph


# ═══════════════════════════════════════
# Skill 7: 文档翻译
# ═══════════════════════════════════════

class TranslateSkill(Skill):
    """翻译 Skill — 中英互译"""

    @property
    def meta(self) -> SkillMeta:
        return SkillMeta(
            name="translate",
            display_name="翻译",
            description="中英文互译，支持技术文档和通用文本",
            icon="🌐",
            tags=["translate", "翻译", "中英", "英文"],
        )

    @property
    def required_tools(self) -> List[Type]:
        return []

    @property
    def trigger_keywords(self) -> List[str]:
        return [
            "翻译", "translate", "中文", "英文",
            "翻译成", "翻成", "英译中", "中译英",
        ]

    def build_graph(self, **kwargs) -> WorkflowGraph:
        graph = WorkflowGraph("translate")

        detect = LLMNode(
            name="detect",
            system_prompt="判断输入文本的语言和需要翻译的目标语言。",
        )
        translate = LLMNode(
            name="translate",
            system_prompt="""进行专业翻译，遵循：
1. 准确传达原意
2. 符合目标语言表达习惯
3. 保留技术术语
4. 保持原文格式和语气""",
        )

        graph.add_node(detect)
        graph.add_node(translate)
        graph.add_edge("detect", "translate")
        graph.set_entry("detect")
        graph.set_exit("translate")

        return graph


# ═══════════════════════════════════════
# 全部内置 Skill 列表
# ═══════════════════════════════════════

BUILTIN_SKILLS = [
    CodeReviewSkill(),
    CodeGenerationSkill(),
    DebugSkill(),
    DocumentationSkill(),
    ProjectInitSkill(),
    DataExploreSkill(),
    TranslateSkill(),
]
