"""
engine/core/self_analyzer.py — Jarvis V3 自我代码分析器

为 self_upgrade 技能提供自我架构知识，使 Agent 能理解自身的代码结构，
从而在升级时准确评估远程变更的影响范围。
"""

import os
import logging
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class SelfAnalyzer:
    """Jarvis V3 自我代码分析器"""

    # 核心模块映射 — 先验知识，作为分析的起点
    CORE_MODULES: Dict[str, str] = {
        "server.py": "FastAPI 服务器入口，API 路由定义，SSE 事件流，组件组装",
        "engine/__init__.py": "引擎包导出",
        "engine/agent_loop.py": "AgentLoop 主循环，任务编排核心（LLM 调用 → 工具执行 → 结果处理）",
        "engine/llm_client.py": "LLMClient 封装 DeepSeek/OpenAI API，含 Fallback 机制和模型路由",
        "engine/conversation.py": "ConversationSession 连贯对话管理，跨轮次消息历史维护",
        "engine/checkpoint.py": "Checkpoint 检查点保存/恢复，防止中断丢失进度",
        "engine/prompt/template.py": "System Prompt 模板 (_BASE_TEMPLATE)，render_template 模板渲染",
        "engine/prompt/context.py": "ContextBuilder 消息上下文构建 + 历史压缩",
        "engine/prompt/complexity.py": "ComplexityRouter 复杂度路由器，根据任务自动选择模型/参数",
        "engine/prompt/modes.py": "ModeConfig 工作模式配置中心（coding/workbuddy/video）",
        "engine/tool/registry.py": "ToolRegistry 工具注册中心，扁平化调度",
        "engine/tool/base.py": "BaseTool / ToolDefinition / ToolParameter / ToolResult 工具基类",
        "engine/tool/executor.py": "ToolExecutor 工具执行器，超时和策略控制",
        "engine/tool/parser.py": "工具参数解析，JSON 修正",
        "engine/tool/policy.py": "ToolPolicy 工具审批策略",
        "engine/skill/registry.py": "SkillRegistry 技能注册中心",
        "engine/skill/base.py": "Skill / SkillMeta 技能基类和元数据",
        "engine/skill/loader.py": "StandardSkill 标准技能加载器（YAML + MD），SkillPromptParser",
        "engine/skill/matcher.py": "match_skill 技能匹配 + get_filtered_tools 工具过滤",
        "engine/core/guard.py": "GuardState 守卫系统，死循环检测/空回复修复/错误分级",
        "engine/core/approval.py": "ApprovalGate 工具审批门控",
        "engine/core/file_guard.py": "FileEditGuard 文件编辑守卫",
        "engine/core/types.py": "ToolCallRecord / ToolResult 核心类型定义",
        "engine/core/self_analyzer.py": "SelfAnalyzer 自我代码分析器（本文件）",
        "engine/longterm/topic_inject.py": "长期记忆注入器",
        "engine/longterm/topic_compress.py": "长期记忆压缩器",
        "engine/lint/runner.py": "LintRunner 代码质量检查",
        "engine/session/manager.py": "SessionManager 会话持久化管理",
        "engine/tracer.py": "调用链追踪",
        "tools/__init__.py": "工具注册入口，register_all_tools()，STANDARD_TOOL_NAMES",
        "tools/file_tool.py": "文件操作工具 (file_list/read/glob/write/append/rename/diff)",
        "tools/code_tool.py": "代码编辑工具 (code_read/diff/write/rollback/append/create)",
        "tools/code_graph_tool.py": "代码图谱工具 (code_graph_related/symbol/callers/callees/impact/folder/stats)",
        "tools/excel_tool.py": "Excel 操作工具 (excel_open/close/list_sheets/read_sheet/write...)",
        "tools/shell_tool.py": "Shell 命令执行 (shell_run)",
        "tools/web_tool.py": "网络工具 (web_fetch/search)",
        "tools/git_tool.py": "Git 工具 (git_status/commit/push/pull/fetch/diff/log/branch_list/stash/stash_pop)",
        "tools/pdf_tool.py": "PDF 处理工具 (pdf_read/split/concat)",
        "tools/word_tool.py": "Word 文档工具 (word_read/write)",
        "tools/image_tool.py": "图片工具 (image_read/ocr)",
        "tools/todo_tool.py": "任务追踪工具 (todo_write/list)",
        "tools/system_tool.py": "系统信息工具 (system_info/time/cwd)",
        "tools/pentest_tool.py": "渗透测试工具 (pentest_run)",
        "frontend/index.html": "Vue.js SPA 入口页面",
        "frontend/app.js": "Vue 3 主应用逻辑",
        "frontend/api.js": "前端 API 层（HTTP 请求封装）",
        "frontend/style.css": "前端样式",
        "skills/__init__.py": "技能发现入口，扫描 skills/ 子目录加载标准技能",
    }

    # 高风险文件 — 变更需要仔细审查
    HIGH_RISK_FILES = {
        "server.py", "engine/agent_loop.py", "engine/llm_client.py",
        "engine/conversation.py", "engine/prompt/template.py",
    }

    # 配置文件 — 不应被自动覆盖
    CONFIG_FILES = {
        ".env", ".env.bak", "requirements.txt",
    }

    def __init__(self, project_root: Optional[str] = None):
        if project_root:
            self._root = Path(project_root)
        else:
            # 自动推断项目根目录：从本文件向上 3 层
            self._root = Path(__file__).resolve().parent.parent.parent

    def analyze_current_architecture(self) -> Dict:
        """
        分析当前代码架构，返回结构化报告。

        Returns:
            {
                "root": str,
                "modules": {path: description},
                "tool_count": int,
                "skill_count": int,
                "directory_structure": [str],
            }
        """
        report = {
            "root": str(self._root),
            "modules": {},
            "tool_count": 0,
            "skill_count": 0,
            "directory_structure": [],
        }

        # 扫描目录结构（仅扫描前两层）
        try:
            for item in sorted(self._root.iterdir()):
                name = item.name
                if name.startswith(".") or name.startswith("_"):
                    continue
                if item.is_dir():
                    report["directory_structure"].append(f"{name}/")
                    for sub in sorted(item.iterdir())[:20]:
                        if not sub.name.startswith(".") and not sub.name.startswith("__"):
                            prefix = "  ├── " if sub != sorted(item.iterdir())[-1] else "  └── "
                            suffix = "/" if sub.is_dir() else ""
                            report["directory_structure"].append(f"{prefix}{sub.name}{suffix}")
                else:
                    report["directory_structure"].append(name)
        except Exception as e:
            report["directory_structure"] = [f"扫描失败: {e}"]

        # 验证核心模块是否存在并补充描述
        for path, desc in self.CORE_MODULES.items():
            full_path = self._root / path
            report["modules"][path] = {
                "exists": full_path.exists(),
                "description": desc,
            }

        # 统计工具数量
        try:
            tools_init = self._root / "tools" / "__init__.py"
            if tools_init.exists():
                content = tools_init.read_text(encoding="utf-8")
                # 统计 STANDARD_TOOL_NAMES 中的条目
                import re
                names_block = re.search(
                    r"STANDARD_TOOL_NAMES\s*=\s*\[(.*?)\]",
                    content, re.DOTALL
                )
                if names_block:
                    count = names_block.group(1).count('"')
                    report["tool_count"] = count // 2
        except Exception:
            pass

        # 统计技能数量
        try:
            skills_dir = self._root / "skills"
            if skills_dir.exists():
                count = sum(
                    1 for d in skills_dir.iterdir()
                    if d.is_dir() and not d.name.startswith("_") and not d.name.startswith(".")
                )
                report["skill_count"] = count
        except Exception:
            pass

        return report

    def generate_self_description(self) -> str:
        """
        生成 Agent 自我描述文本，用于注入到系统提示中。
        使 Agent 能理解自身的代码结构和模块职责。
        """
        report = self.analyze_current_architecture()

        lines = [
            "## 🧠 自我架构知识",
            "",
            f"我是 Jarvis V3，项目根目录: `{report['root']}`",
            f"当前注册了 {report['tool_count']} 个原子工具，{report['skill_count']} 个技能。",
            "",
            "### 核心模块",
            "",
        ]

        # 按目录分组输出模块信息
        groups: Dict[str, List] = {}
        for path, info in report["modules"].items():
            if not info["exists"]:
                continue
            parts = path.split("/")
            group = parts[0] if len(parts) == 1 else "/".join(parts[:2])
            if group not in groups:
                groups[group] = []
            groups[group].append((path, info["description"]))

        for group in sorted(groups.keys()):
            lines.append(f"**{group}/**")
            for path, desc in groups[group]:
                lines.append(f"- `{path}` — {desc}")
            lines.append("")

        # 高风险文件提示
        lines.append("### 高风险文件（升级时需仔细审查）")
        lines.append("")
        for f in sorted(self.HIGH_RISK_FILES):
            full_path = self._root / f
            if full_path.exists():
                lines.append(f"- `{f}`")
        lines.append("")

        # 配置文件提示
        lines.append("### 配置文件（不应自动覆盖）")
        lines.append("")
        for f in sorted(self.CONFIG_FILES):
            full_path = self._root / f
            if full_path.exists():
                lines.append(f"- `{f}`")

        return "\n".join(lines)


def analyze_self(project_root: Optional[str] = None) -> str:
    """便捷函数：生成自我描述文本"""
    analyzer = SelfAnalyzer(project_root)
    return analyzer.generate_self_description()
