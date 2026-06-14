"""
test_unit/test_skill_loader.py — StandardSkill 加载器单元测试

重点覆盖（Skill 不再构建 DAG，只提供 system prompt）：
  1. SkillPromptParser 解析 skill.md
  2. StandardSkill 元数据加载
  3. get_system_prompt() 提供领域提示
  4. 错误处理
"""

import os
import tempfile
import yaml
import pytest
from pathlib import Path

from engine.skill.loader import StandardSkill, SkillPromptParser
from engine.skill.base import SkillMeta


# ═══════════════════════════════════════
#  SkillPromptParser 测试
# ═══════════════════════════════════════

class TestSkillPromptParser:
    """Markdown 提示词解析器"""

    def test_parse_sections(self):
        """按 ## 标题分段"""
        with tempfile.NamedTemporaryFile(suffix=".md", mode="w", delete=False, encoding="utf-8") as f:
            f.write("## system\n你是系统提示\n\n## think\n思考分析\n\n## output\n输出结果\n")
            f.flush()
            parser = SkillPromptParser(f.name)

            assert "你是系统提示" in parser.get("system")
            assert "思考分析" in parser.get("think")
            assert "输出结果" in parser.get("output")

        os.unlink(f.name)

    def test_no_sections(self):
        """无 ## 标题时统一作为 system"""
        with tempfile.NamedTemporaryFile(suffix=".md", mode="w", delete=False, encoding="utf-8") as f:
            f.write("纯文本提示词，无分段")
            f.flush()
            parser = SkillPromptParser(f.name)
            assert "纯文本提示词" in parser.get("system")

        os.unlink(f.name)

    def test_file_not_exist(self):
        """文件不存在时 system 为空"""
        parser = SkillPromptParser("/tmp/nonexistent.md")
        assert parser.get("system") == ""

    def test_reload(self):
        """reload 支持热更新"""
        with tempfile.NamedTemporaryFile(suffix=".md", mode="w", delete=False, encoding="utf-8") as f:
            f.write("## system\n旧内容")
            f.flush()
            parser = SkillPromptParser(f.name)

            assert "旧内容" in parser.get("system")

            with open(f.name, "w", encoding="utf-8") as f2:
                f2.write("## system\n新内容")
            parser.reload()
            assert "新内容" in parser.get("system")

        os.unlink(f.name)

    def test_get_case_insensitive(self):
        """get 方法大小写不敏感"""
        with tempfile.NamedTemporaryFile(suffix=".md", mode="w", delete=False, encoding="utf-8") as f:
            f.write("## SYSTEM\n大写关键词")
            f.flush()
            parser = SkillPromptParser(f.name)
            assert "大写关键词" in parser.get("system")
            assert "大写关键词" in parser.get("SYSTEM")

        os.unlink(f.name)

    def test_has_section(self):
        """has 检查"""
        with tempfile.NamedTemporaryFile(suffix=".md", mode="w", delete=False, encoding="utf-8") as f:
            f.write("## system\n内容\n\n## output\n内容")
            f.flush()
            parser = SkillPromptParser(f.name)
            assert parser.has("system")
            assert parser.has("output")
            assert not parser.has("nonexistent")

        os.unlink(f.name)

    def test_get_default(self):
        """不存在的段落返回默认值"""
        with tempfile.NamedTemporaryFile(suffix=".md", mode="w", delete=False, encoding="utf-8") as f:
            f.write("## system\n内容")
            f.flush()
            parser = SkillPromptParser(f.name)
            assert parser.get("nonexistent", "默认值") == "默认值"

        os.unlink(f.name)


# ═══════════════════════════════════════
#  StandardSkill 测试
# ═══════════════════════════════════════

@pytest.fixture
def skill_dir():
    """创建临时 Skill 目录"""
    tmp = tempfile.mkdtemp()

    yaml_path = os.path.join(tmp, "skill.yaml")
    with open(yaml_path, "w", encoding="utf-8") as f:
        yaml.dump({
            "name": "test_skill",
            "display_name": "测试技能",
            "description": "用于单元测试",
            "icon": "🧪",
            "tags": ["test", "debug"],
            "triggers": ["测试", "test", "测试技能"],
        }, f, allow_unicode=True)

    md_path = os.path.join(tmp, "skill.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("## system\n你是测试技能的系统提示\n\n## think\n思考分析\n\n## output\n输出结果\n")

    yield tmp
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


class TestStandardSkill:
    """StandardSkill 加载"""

    def test_load_from_dir(self, skill_dir):
        skill = StandardSkill.from_dir(skill_dir)
        assert skill is not None

    def test_meta(self, skill_dir):
        skill = StandardSkill.from_dir(skill_dir)
        meta = skill.meta
        assert meta.name == "test_skill"
        assert meta.display_name == "测试技能"
        assert meta.icon == "🧪"
        assert "test" in meta.tags
        assert "debug" in meta.tags

    def test_trigger_keywords(self, skill_dir):
        skill = StandardSkill.from_dir(skill_dir)
        assert "测试" in skill.trigger_keywords
        assert "test" in skill.trigger_keywords

    def test_get_system_prompt(self, skill_dir):
        """get_system_prompt 返回 system 段"""
        skill = StandardSkill.from_dir(skill_dir)
        prompt = skill.get_system_prompt()
        assert "你是测试技能的系统提示" in prompt

    def test_get_system_prompt_empty(self):
        """无 system 段的 skill 返回空"""
        tmp = tempfile.mkdtemp()
        try:
            yaml_path = os.path.join(tmp, "skill.yaml")
            with open(yaml_path, "w", encoding="utf-8") as f:
                yaml.dump({"name": "no_prompt", "nodes": [{"name": "n", "type": "llm"}]}, f)

            md_path = os.path.join(tmp, "skill.md")
            with open(md_path, "w", encoding="utf-8") as f:
                f.write("")

            skill = StandardSkill.from_dir(tmp)
            prompt = skill.get_system_prompt()
            assert prompt == ""
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_repr(self, skill_dir):
        skill = StandardSkill.from_dir(skill_dir)
        rep = repr(skill)
        assert "StandardSkill" in rep
        assert "test_skill" in rep

    def test_can_handle(self, skill_dir):
        """can_handle 基于 trigger_keywords 匹配"""
        skill = StandardSkill.from_dir(skill_dir)
        score = skill.can_handle("帮我测试这个功能")
        assert score > 0.0

        score = skill.can_handle("完全不相关的内容")
        assert score == 0.0


class TestStandardSkillErrors:
    """错误处理"""

    def test_missing_yaml(self):
        with pytest.raises(FileNotFoundError):
            StandardSkill.from_dir("/tmp/nonexistent_skill_path")

    def test_missing_md_fallback(self):
        """缺少 skill.md 时 get_system_prompt 为空"""
        tmp = tempfile.mkdtemp()
        try:
            yaml_path = os.path.join(tmp, "skill.yaml")
            with open(yaml_path, "w", encoding="utf-8") as f:
                yaml.dump({
                    "name": "no_md",
                }, f)

            skill = StandardSkill.from_dir(tmp)
            assert skill is not None
            prompt = skill.get_system_prompt()
            assert prompt == ""
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
