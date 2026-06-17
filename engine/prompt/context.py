"""
engine/prompt/context.py — 消息上下文构建 + 历史压缩

从 agent_loop.py 提取的 ContextBuilder 类和辅助函数。
职责：构建初始消息列表、压缩历史、环境信息采集。
"""

import json
import logging
import os
import re
import subprocess
from typing import Any, Dict, List, Optional, Set

from .template import render_template

logger = logging.getLogger(__name__)

# ── 常量 ──
KEEP_RECENT_TURNS = 3
COMPRESS_ROUNDS = 2
MAX_CONTEXT_TOKENS = 128_000
TOKEN_SAFETY_MARGIN = 0.80
MAX_TOOL_RESULT_CHARS = 80000

_OLD_INSTRUCTION_PATTERNS = [
    "分析当前情况，决定下一步行动",
    "如果需要更多信息，调用工具获取",
    "如果任务完成或无法继续，直接输出最终答案",
]


# ═══════════════════════════════════════════════════════════════
#  模块级辅助函数
# ═══════════════════════════════════════════════════════════════

def _trim_history_messages(messages: List[Dict], max_tool_chars: int = 40000) -> List[Dict]:
    """截短历史消息中的 tool 结果，智能保留结构。"""
    trimmed = []
    for m in messages:
        m = dict(m)
        if m.get("role") == "tool":
            content = str(m.get("content", ""))
            if len(content) > max_tool_chars:
                try:
                    if content.strip().startswith(("{", "[")):
                        obj = json.loads(content)
                        if isinstance(obj, dict):
                            keys = list(obj.keys())[:5]
                            summary = {k: str(obj[k])[:200] for k in keys}
                            m["content"] = json.dumps(summary, ensure_ascii=False) + "\n... [历史结果已精简]"
                        else:
                            m["content"] = content[:max_tool_chars] + "\n... [历史结果已截短]"
                    else:
                        lines = content.split("\n")
                        keep = 0
                        total = 0
                        for line in lines:
                            total += len(line) + 1
                            if total > max_tool_chars:
                                break
                            keep += 1
                        m["content"] = "\n".join(lines[:keep])
                        if keep < len(lines):
                            m["content"] += f"\n... [已截短, 省略 {len(lines)-keep} 行]"
                except Exception:
                    m["content"] = content[:max_tool_chars] + "\n... [历史结果已截短]"
        trimmed.append(m)
    return trimmed


def _unwrap_task_message(content: str) -> str:
    """将旧版 '任务: xxx\\n\\n指令...' 格式还原为原始用户输入"""
    if not content.startswith("任务: "):
        return content
    inner = content[len("任务: "):]
    for pattern in _OLD_INSTRUCTION_PATTERNS:
        inner = re.sub(r'\s*' + re.escape(pattern) + r'[。，]?\s*', '', inner)
    inner = inner.strip()
    return inner if inner else content


def sanitize_tool_messages(messages: List[Dict]) -> List[Dict]:
    """清洗 tool 消息：移除孤立/无主 tool 消息，并清理缺失响应的 assistant tool_calls"""
    # ── 第一步：收集所有存在的 tool 响应 call_id ──
    responded_call_ids: Set[str] = set()
    for m in messages:
        if m.get("role") == "tool":
            cid = m.get("tool_call_id", "")
            if cid:
                responded_call_ids.add(cid)

    # ── 第二步：收集 assistant 中所有声明的 call_id ──
    declared_call_ids: Set[str] = set()
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                cid = tc.get("id", "")
                if cid:
                    declared_call_ids.add(cid)

    # ── 第三步：清洗 ──
    cleaned = []
    dropped_tool = 0
    stripped_assistant = 0
    for m in messages:
        role = m.get("role")

        if role == "tool":
            # 移除无主 tool 消息（没有对应 assistant.tool_calls）
            call_id = m.get("tool_call_id", "")
            if call_id and call_id in declared_call_ids:
                cleaned.append(m)
            else:
                dropped_tool += 1
                if dropped_tool <= 3:
                    reason = "空 call_id" if not call_id else f"孤立 call_id={call_id!r}"
                    logger.warning(
                        f"丢弃 {reason} tool 消息: "
                        f"content={str(m.get('content', ''))[:80]}"
                    )

        elif role == "assistant" and m.get("tool_calls"):
            # 过滤掉没有对应 tool 响应的 tool_calls
            valid_tcs = [
                tc for tc in m["tool_calls"]
                if tc.get("id", "") in responded_call_ids
            ]
            missing_count = len(m["tool_calls"]) - len(valid_tcs)

            if missing_count > 0:
                if not valid_tcs:
                    # 所有 tool_calls 都没有响应
                    content = m.get("content", "") or ""
                    if content.strip():
                        # 有内容：保留为普通 assistant 消息，去掉 tool_calls
                        stripped_msg = {k: v for k, v in m.items() if k != "tool_calls"}
                        cleaned.append(stripped_msg)
                        stripped_assistant += 1
                        logger.warning(
                            f"assistant 消息 {missing_count} 个 tool_calls 无响应，"
                            f"已剥离 tool_calls（保留 content）"
                        )
                    else:
                        # 无内容：完全丢弃
                        stripped_assistant += 1
                        logger.warning(
                            f"assistant 消息 {missing_count} 个 tool_calls 无响应且无 content，已丢弃"
                        )
                else:
                    # 部分 tool_calls 缺失响应，保留有效的
                    stripped_msg = dict(m)
                    stripped_msg["tool_calls"] = valid_tcs
                    cleaned.append(stripped_msg)
                    stripped_assistant += 1
                    logger.warning(
                        f"assistant 消息有 {missing_count} 个 tool_calls 无响应，"
                        f"已过滤（保留 {len(valid_tcs)} 个）"
                    )
            else:
                cleaned.append(m)
        else:
            cleaned.append(m)

    if dropped_tool:
        logger.warning(f"清洗 tool 消息: 移除了 {dropped_tool} 条孤立消息")
    if stripped_assistant:
        logger.warning(f"清洗 assistant 消息: 处理了 {stripped_assistant} 条残留 tool_calls")
    return cleaned


def _smart_truncate(text: str, max_chars: int) -> str:
    """智能截断文本，优先保留结构。"""
    if len(text) <= max_chars:
        return text
    if text.strip().startswith("{"):
        try:
            obj = json.loads(text)
            summary = {}
            for k, v in list(obj.items())[:10]:
                if isinstance(v, str) and len(v) > 100:
                    summary[k] = v[:100] + "..."
                elif isinstance(v, list):
                    summary[k] = v[:3]
                    if len(v) > 3:
                        summary[k].append(f"... (+{len(v)-3}项)")
                else:
                    summary[k] = v
            result = json.dumps(summary, ensure_ascii=False, indent=2)
            if len(result) <= max_chars:
                return result + f"\n... [截断: 完整数据{len(text)}字符]"
        except (json.JSONDecodeError, Exception):
            pass
    if text.strip().startswith("["):
        try:
            arr = json.loads(text)
            if isinstance(arr, list) and len(arr) > 5:
                snippet = json.dumps(arr[:5], ensure_ascii=False, indent=2)
                return (
                    snippet[:-1]
                    + f",\n  ... (+{len(arr)-5}项)\n]\n"
                    f"[截断: 完整数据{len(text)}字符]"
                )
        except (json.JSONDecodeError, Exception):
            pass
    if "\n" in text:
        lines = text.split("\n")
        head_count = max(int(len(lines) * 0.7), 1)
        tail_count = max(int(len(lines) * 0.1), 1)
        head = "\n".join(lines[:head_count])
        if len(head) <= max_chars:
            tail = "\n".join(lines[-tail_count:])
            return (
                f"{head}\n... [省略 {len(lines)-head_count-tail_count} 行] ...\n{tail}\n"
                f"[截断: 完整数据 {len(lines)} 行, {len(text)} 字符]"
            )
        to_keep = max_chars - 200
        for i in range(head_count - 1, 0, -1):
            chunk = "\n".join(lines[:i])
            if len(chunk) <= to_keep:
                return chunk + f"\n... [截断: 完整数据 {len(lines)} 行, {len(text)} 字符]"
    keep = max_chars - 150
    return text[:keep] + f"\n... [截断: 完整数据 {len(text)} 字符]"


def _estimate_tokens(messages: List[Dict]) -> int:
    """估算当前消息列表的 token 数。"""
    try:
        from ..core.token_estimator import estimate_message_dict
        return sum(estimate_message_dict(m) for m in messages if isinstance(m, dict))
    except Exception:
        return sum(len(str(m.get("content", ""))) // 2 for m in messages if isinstance(m, dict))


# ═══════════════════════════════════════════════════════════════
#  ContextBuilder 类
# ═══════════════════════════════════════════════════════════════

class ContextBuilder:
    """消息上下文构建器 / 历史压缩器"""

    def __init__(self):
        self.compressed_until: int = 0
        self.compressed_summary: str = ""

    # ── 构建初始消息 ──

    async def build_messages(
        self,
        task: str,
        working_dir: str,
        skill: Optional[Any] = None,
        base_system: str = "",
        history: Optional[List[Dict]] = None,
        skip_last_user: bool = True,
        compressed_until: int = 0,
        compressed_summary: str = "",
        mode: str = "coding",
        todo_state: str = "",
    ) -> List[Dict]:
        """构建初始消息列表。返回 messages + 更新 compressed_* 状态。"""
        self.compressed_until = compressed_until
        self.compressed_summary = compressed_summary

        messages: List[Dict] = []
        variables = self._build_template_variables(task, working_dir, skill)
        variables["todo_state"] = todo_state

        if self.compressed_summary:
            variables["compressed_summary"] = f"## 📜 历史操作日志\n{self.compressed_summary}"

        system_prompt = render_template(variables, base_system, mode=mode)
        messages.append({"role": "system", "content": system_prompt})

        if history:
            user_indices = [i for i, m in enumerate(history) if m.get("role") == "user"]
            if skip_last_user:
                user_indices = user_indices[:-1] if user_indices else []

            if user_indices:
                new_history_start = compressed_until if compressed_until else 0
                recent_start = (
                    user_indices[-KEEP_RECENT_TURNS]
                    if len(user_indices) >= KEEP_RECENT_TURNS
                    else user_indices[0]
                )
                old_history = history[new_history_start:recent_start]
                recent_history = history[recent_start:]

                if old_history:
                    summary = await self._llm_summarize(
                        old_history, llm_client=None, context="早期对话",
                    )
                    if summary:
                        old_turn_count = len(user_indices) - min(len(user_indices), KEEP_RECENT_TURNS)
                        if self.compressed_summary:
                            self.compressed_summary = f"{self.compressed_summary}\n{summary}"
                        else:
                            self.compressed_summary = summary
                        self.compressed_until = recent_start

                messages.extend(_trim_history_messages(recent_history))

        messages = sanitize_tool_messages(messages)
        for m in messages:
            if m.get("role") == "user" and isinstance(m.get("content"), str):
                m["content"] = _unwrap_task_message(m["content"])

        messages.append({"role": "user", "content": task})
        return messages

    def _build_template_variables(
        self, task: str, working_dir: str, skill: Optional[Any],
    ) -> Dict[str, str]:
        variables = {
            "task": task,
            "working_dir": working_dir,
            "env_info": self._gather_environment(working_dir),
            "user_profile": self._read_user_profile(working_dir),
            "skill_prompt": "",
            "constraints": "",
            "compressed_summary": "",
            "self_knowledge": "",
            "todo_state": "",
        }
        if skill:
            if hasattr(skill, 'get_config_value'):
                variables["constraints"] = skill.get_config_value("constraints", "")
            sp = skill.get_system_prompt()
            if sp:
                variables["skill_prompt"] = (
                    f"## 当前任务领域：{skill.meta.display_name}\n{sp}"
                )
            # 自我升级技能：注入自我架构知识
            if getattr(skill.meta, 'name', '') == 'self_upgrade':
                try:
                    from ..core.self_analyzer import SelfAnalyzer
                    analyzer = SelfAnalyzer()
                    variables["self_knowledge"] = analyzer.generate_self_description()
                except Exception:
                    pass
        return variables

    # ── Token 预算检查 ──

    def should_compress(self, messages: List[Dict]) -> bool:
        """判断是否需要压缩"""
        if len(messages) < 10:
            return False
        return _estimate_tokens(messages) > MAX_CONTEXT_TOKENS * TOKEN_SAFETY_MARGIN

    # ── 历史压缩 ──

    async def compress(
        self,
        messages: List[Dict],
        llm_client: Any,
        current_round: int,
        key_findings_text: str = "",
    ) -> bool:
        """压缩历史消息，注入摘要和关键发现。"""
        if len(messages) < 6:
            return False

        system_end = 1
        for i, m in enumerate(messages):
            if m.get("role") == "user" and i > 0:
                system_end = i + 1
                break

        assistant_indices = [
            i for i in range(system_end, len(messages))
            if messages[i].get("role") == "assistant"
        ]

        to_drop = []
        dropped = 0
        for idx in assistant_indices[:COMPRESS_ROUNDS * 3]:
            if dropped >= COMPRESS_ROUNDS * 4:
                break
            to_drop.append(idx)
            dropped += 1
            for j in range(idx + 1, len(messages)):
                if messages[j].get("role") == "tool":
                    to_drop.append(j)
                else:
                    break

        if not to_drop:
            return False

        dropped_msgs = [messages[idx] for idx in sorted(set(to_drop))]
        summary = await self._llm_summarize(
            dropped_msgs, llm_client=llm_client,
            context=f"Token超限压缩(第{current_round}轮)",
        )
        if summary:
            summary = f"## 📜 早期对话摘要（已压缩 {len(dropped_msgs)} 条消息）\n{summary}"

        to_drop = sorted(set(to_drop), reverse=True)
        for idx in to_drop:
            if idx < len(messages):
                messages.pop(idx)

        # 清理孤立 tool 消息
        valid_call_ids = set()
        for m in messages:
            for tc in m.get("tool_calls", []):
                valid_call_ids.add(tc.get("id", ""))
        orphan_indices = [i for i, m in enumerate(messages)
                          if m.get("role") == "tool" and m.get("tool_call_id", "") not in valid_call_ids]
        for idx in reversed(orphan_indices):
            messages.pop(idx)

        # 注入摘要
        if summary and system_end < len(messages):
            messages.insert(system_end, {"role": "system", "content": summary})

        # 注入关键发现
        if key_findings_text:
            inject_pos = system_end + (1 if summary else 0)
            if inject_pos < len(messages):
                messages.insert(inject_pos, {"role": "system", "content": key_findings_text})

        logger.info(
            f"🗜️ Token 压缩: 删除了 {len(to_drop)} 条消息 (约{COMPRESS_ROUNDS}轮), "
            f"剩余 {len(messages)} 条"
        )
        return True

    # ── LLM 摘要 ──

    async def _llm_summarize(
        self, messages: List[Dict], llm_client: Any, context: str = "",
    ) -> str:
        """用 LLM 压缩消息为结构化操作日志。"""
        if not messages or len(messages) < 3:
            return ""
        total_chars = sum(len(str(m.get("content", ""))) for m in messages)
        if len(messages) <= 5 and total_chars < 500:
            return self._rule_summarize(messages)

        dialogue_parts = []
        for m in messages:
            role = m.get("role", "?")
            content = str(m.get("content", ""))[:400]
            if m.get("tool_calls"):
                tools = [tc.get("function", {}).get("name", "?") for tc in m.get("tool_calls", [])]
                content += f"【调用了: {', '.join(tools)}】"
            dialogue_parts.append(f"[{role}] {content}")

        prompt = (
            "将以下Agent对话记录压缩为**操作日志**。每条日志包含：做了什么 → 结果如何。\n\n"
            "## 输出格式\n"
            "每行一条记录，格式为：\n"
            "  - R{轮次} {状态} {操作}: {做了什么} → {结果/发现}\n\n"
            "状态图标: ✅成功 ❌失败 ⚠️部分 📖读取 ✏️写入 🔍搜索 ⚡执行 💭思考\n\n"
            "## 压缩要求\n"
            "1. 每条记录一句话，保留关键信息（文件路径、数据量、错误原因）\n"
            "2. 丢弃冗余过程（重试细节、调试信息、重复的相同操作）\n"
            "3. 保留用户意图和Agent的决策转折点\n"
            "4. 保留所有具体的发现（\"发现循环依赖\"比\"分析了代码\"更有用）\n"
            "5. 用中文，控制在150-300字\n\n"
            f"## 对话记录（{context}）\n"
            + "\n".join(dialogue_parts)
            + "\n\n## 操作日志:"
        )

        if llm_client and hasattr(llm_client, 'chat_completion'):
            try:
                resp = await llm_client.chat_completion(
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=500,
                    temperature=0.2,
                )
                result = resp.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                if result and len(result) > 10:
                    logger.info(f"🤖 LLM 压缩完成: {len(messages)}条 → {len(result)}字操作日志")
                    return result
            except Exception as e:
                logger.warning(f"LLM 压缩失败，fallback 规则摘要: {e}")
        else:
            logger.info("无 LLM 客户端，使用规则摘要")

        return self._rule_summarize(messages)

    def _rule_summarize(self, history: List[Dict]) -> str:
        """规则摘要 — LLM 压缩失败时的 fallback。"""
        if not history:
            return ""

        turns: List[List[Dict]] = []
        current_turn: List[Dict] = []
        for m in history:
            if m.get("role") == "user" and current_turn:
                turns.append(current_turn)
                current_turn = []
            current_turn.append(m)
        if current_turn:
            turns.append(current_turn)

        if not turns:
            return ""

        lines = []
        for ti, turn in enumerate(turns, 1):
            user_text = ""
            assistant_text = ""
            tool_names = []
            tool_highlights: List[str] = []

            for m in turn:
                role = m.get("role", "")
                if role == "user":
                    user_text = str(m.get("content", ""))[:150].replace("\n", " ")
                elif role == "assistant":
                    content = str(m.get("content", ""))
                    if content:
                        assistant_text = content[:200].replace("\n", " ")
                    tcs = m.get("tool_calls", [])
                    for tc in tcs:
                        fn = tc.get("function", {})
                        tool_names.append(fn.get("name", "?"))
                elif role == "tool":
                    tool_highlights.extend(self._extract_highlights(str(m.get("content", ""))))

            indicator = ""
            if tool_names:
                names_str = " | ".join(tool_names[:3])
                if len(tool_names) > 3:
                    names_str += f" +{len(tool_names)-3}"
                indicator = f"[{names_str}]"
            else:
                indicator = "[直接回答]"

            line = f"R{ti} {indicator} Q: {user_text or '(系统消息)'}"
            if assistant_text:
                line += f" A: {assistant_text}"
            if tool_highlights:
                line += f" 数据: {'; '.join(tool_highlights[:5])}"
            lines.append(line)

        return "\n".join(lines)

    @staticmethod
    def _extract_highlights(text: str) -> List[str]:
        """从 tool 结果中提取关键数值。"""
        highlights = []
        if not text:
            return highlights
        text = text[:2000]
        patterns = [
            (r'"count"\s*:\s*(\d+)', "count"),
            (r'"total_lines"\s*:\s*(\d+)', "lines"),
            (r'"matches"\s*:\s*(\d+)', "matches"),
            (r'"score"\s*:\s*(\d+)', "score"),
            (r'"files"\s*:\s*(\d+)', "files"),
            (r'"status"\s*:\s*"([^"]+)"', "status"),
            (r'"passed"\s*:\s*(true|false)', "passed"),
            (r'"keyword"\s*:\s*"([^"]+)"', "keyword"),
        ]
        seen = set()
        for pattern, label in patterns:
            for m in re.findall(pattern, text, re.IGNORECASE)[:2]:
                h = f"{label}={m}"
                if h not in seen:
                    seen.add(h)
                    highlights.append(h)
        if not highlights and len(text) > 20:
            clean = text.strip("{} \n\r\t")
            first = clean.split("\n")[0].strip()[:100]
            if first:
                highlights.append(first)
        return highlights[:5]

    # ── 环境信息 ──

    @staticmethod
    def _read_user_profile(working_dir: str) -> str:
        """读取用户档案文件（USER.md, SOUL.md）"""
        profile_specs = [
            ("USER.md", "## 用户档案"),
            ("SOUL.md", "## 行为风格"),
        ]
        parts = []
        for fname, label in profile_specs:
            path = os.path.join(working_dir, fname)
            if os.path.isfile(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        content = f.read().strip()
                    if content:
                        parts.append(f"{label}（{fname}）\n{content[:1500]}")
                except Exception:
                    pass
        return "\n\n".join(parts) if parts else ""

    @staticmethod
    def _gather_environment(working_dir: str) -> str:
        """收集工作环境信息。"""
        parts = []
        try:
            files = os.listdir(working_dir)[:50]
            parts.append(f"- 工作目录: {working_dir}")
            parts.append(f"- 目录文件: {', '.join(files[:30])}")
        except Exception:
            parts.append(f"- 工作目录: {working_dir}")

        project_info = ContextBuilder._detect_project_context(working_dir)
        if project_info:
            parts.append(f"- 项目类型: {project_info.get('type', '?')}")
            deps = project_info.get('dependencies', [])
            if deps:
                parts.append(f"- 依赖: {', '.join(deps[:15])}")
            if project_info.get('framework'):
                parts.append(f"- 框架: {project_info['framework']}")
            if project_info.get('python_version'):
                parts.append(f"- Python: {project_info['python_version']}")

        try:
            key_dirs = []
            for d in ['src', 'tests', 'lib', 'app', 'utils', 'scripts', 'config']:
                if os.path.isdir(os.path.join(working_dir, d)):
                    key_dirs.append(d)
            if key_dirs:
                parts.append(f"- 关键目录: {', '.join(key_dirs)}")
        except Exception:
            pass

        for rule_file in [".jarvis-rules.md", ".jarvis-rules.yaml", ".jarvis-rules.yml", "JARVIS_RULES.md"]:
            rule_path = os.path.join(working_dir, rule_file)
            if os.path.isfile(rule_path):
                try:
                    with open(rule_path, "r", encoding="utf-8") as f:
                        rules_content = f.read().strip()
                    if len(rules_content) > 10:
                        parts.append(f"\n### 项目规则（{rule_file}）\n{rules_content[:2000]}")
                    break
                except Exception:
                    pass

        try:
            r = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=working_dir, capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip():
                parts.append(f"- Git 分支: {r.stdout.strip()}")
            r2 = subprocess.run(
                ["git", "status", "--short"],
                cwd=working_dir, capture_output=True, text=True, timeout=5,
            )
            if r2.returncode == 0 and r2.stdout.strip():
                lines = r2.stdout.strip().split("\n")[:10]
                parts.append(f"- Git 变更: {', '.join(l.strip() for l in lines)}")
        except Exception:
            pass

        return "## 工作环境\n" + "\n".join(parts)

    @staticmethod
    def _detect_project_context(working_dir: str) -> dict:
        """检测项目上下文（类型、依赖、框架）。"""
        info = {}
        for req_file in ['requirements.txt', 'requirements-dev.txt']:
            req_path = os.path.join(working_dir, req_file)
            if os.path.isfile(req_path):
                try:
                    with open(req_path, 'r') as f:
                        deps = [l.strip() for l in f if l.strip() and not l.startswith('#') and not l.startswith('-')]
                    if deps:
                        info['type'] = 'Python'
                        info['dependencies'] = [d.split('==')[0].split('>=')[0].split('<')[0].strip() for d in deps[:20]]
                    break
                except Exception:
                    pass
        pp_path = os.path.join(working_dir, 'pyproject.toml')
        if os.path.isfile(pp_path):
            info['type'] = 'Python'
            try:
                import tomllib
                with open(pp_path, 'rb') as f:
                    pp = tomllib.load(f)
                deps = []
                for section in ['dependencies', 'optional-dependencies']:
                    if section in pp.get('project', {}):
                        raw = pp['project'][section]
                        if isinstance(raw, list):
                            deps.extend([d.split('>=')[0].split('==')[0].split('!=')[0].strip() for d in raw if isinstance(d, str)])
                if deps:
                    info['dependencies'] = deps[:15]
                req_py = pp.get('project', {}).get('requires-python', '')
                if req_py:
                    info['python_version'] = req_py
                if deps:
                    frameworks = ['flask', 'django', 'fastapi', 'pandas', 'numpy', 'requests', 'sqlalchemy', 'click', 'typer', 'httpx', 'scrapy']
                    found = [f for f in frameworks if any(f in d.lower() for d in deps)]
                    if found:
                        info['framework'] = found[0]
            except Exception:
                pass
        pkg_path = os.path.join(working_dir, 'package.json')
        if os.path.isfile(pkg_path):
            info['type'] = 'Node.js'
            try:
                with open(pkg_path, 'r') as f:
                    pkg = json.load(f)
                all_deps = {}
                for key in ['dependencies', 'devDependencies']:
                    all_deps.update(pkg.get(key, {}))
                if all_deps:
                    info['dependencies'] = list(all_deps.keys())[:15]
            except Exception:
                pass
        return info
