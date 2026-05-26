"""
dump_source.py — 分别导出 engine/ 和 tools/ 到独立的 txt 文件

用法：
    python dump_source.py              # 导出到 engine_dump_0.txt, engine_dump_1.txt ... 等
"""

import os
import sys
from pathlib import Path
import re

# ── 配置 ──
PROJECT_ROOT = Path(__file__).resolve().parent
INCLUDE_DIRS = ["engine", "tools"]
EXCLUDE_DIRS = {"__pycache__", ".git", "node_modules", "venv", ".venv"}
EXCLUDE_FILES = {"__pycache__"}
PY_EXT = {".py"}

# 每个文件最大字符数 (10万字)
MAX_CHARS_PER_FILE = 100_000


def collect_files(root: Path, target_dir: str) -> list[Path]:
    """收集指定目录下所有 .py 文件"""
    files = []
    target = root / target_dir
    if not target.is_dir():
        print(f"⚠️  目录不存在: {target}")
        return files
    
    for f in sorted(target.rglob("*")):
        if f.suffix not in PY_EXT:
            continue
        if any(p.name in EXCLUDE_DIRS for p in f.relative_to(root).parents):
            continue
        if f.name in EXCLUDE_FILES:
            continue
        files.append(f)
    return files


def remove_comments(source: str) -> str:
    """移除 Python 代码中的注释（保留字符串内容）"""
    # 匹配单行注释、多行注释以及字符串
    pattern = r'''
        (?P<string>           # 字符串组
            (?:''' + '"""' + r'''[^\\]*(?:\\.[^\\]*)*''' + '"""' + r''') |   # 三双引号字符串
            (?:\'\'\'[^\\]*(?:\\.[^\\]*)*\'\'\') |   # 三单引号字符串
            (?:"[^"\\\n]*(?:\\.[^"\\\n]*)*") |       # 双引号字符串
            (?:'[^'\\\n]*(?:\\.[^'\\\n]*)*')         # 单引号字符串
        )
        |
        (?P<comment>          # 注释组
            \#[^\n]*          # 单行注释
        )
    '''
    
    def replacer(match):
        if match.group('string'):
            return match.group('string')
        elif match.group('comment'):
            return ''
        return match.group(0)

    cleaned = re.sub(pattern, replacer, source, flags=re.VERBOSE | re.DOTALL)
    
    # 清理因删除注释产生的多余空行，但保留基本的代码结构
    lines = cleaned.splitlines()
    filtered_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped:
            filtered_lines.append(line)
        else:
            # 保留少量空行以维持基本可读性，或者完全移除空行
            # 这里选择保留非连续的空行逻辑较复杂，简单起见，如果只想要纯代码，可以过滤掉纯空行
            # 但为了保持函数结构，通常保留空行更好。
            # 根据“删去注释”的严格指令，我们通常也清理掉因为删注释留下的孤立空行
            if filtered_lines and filtered_lines[-1] != "":
                filtered_lines.append("")
            elif not filtered_lines:
                pass # 忽略开头的空行
    
    # 重新组合并去除首尾空白
    result = "\n".join(filtered_lines).strip()
    return result


def write_dump_split(files: list[Path], output_base: str, root: Path, dir_name: str) -> None:
    """
    写入导出文件，如果超过 MAX_CHARS_PER_FILE 则分割成多个文件。
    output_base: 基础路径，例如 "engine_dump.txt"
    """
    total_files_processed = 0
    total_chars_written = 0
    current_file_index = 0
    
    # 初始化第一个输出文件
    def get_output_path(index):
        if index == 0:
            return f"{output_base}"
        else:
            name, ext = os.path.splitext(output_base)
            return f"{name}_{index}{ext}"

    current_output_path = get_output_path(current_file_index)
    out = open(current_output_path, "w", encoding="utf-8")
    current_file_chars = 0
    
    # 用于统计总行数（近似）
    total_lines_count = 0

    try:
        for fpath in files:
            rel_path = fpath.relative_to(root).as_posix()
            try:
                content = fpath.read_text(encoding="utf-8")
                # 移除注释
                clean_content = remove_comments(content)
                
                if not clean_content:
                    continue
                
                # 构建文件头
                header = f"{'=' * 80}\n# FILE: {rel_path}\n# LINES: {clean_content.count(chr(10)) + 1}\n{'=' * 80}\n\n"
                footer = "\n\n"
                
                block = header + clean_content + footer
                block_len = len(block)
                
                # 检查是否需要切分文件
                # 如果当前文件已有内容，且加入新块后超过限制，则关闭当前文件，开启新文件
                if current_file_chars > 0 and (current_file_chars + block_len) > MAX_CHARS_PER_FILE:
                    out.close()
                    print(f"  ✂️  分割点: {current_output_path} ({current_file_chars} chars)")
                    
                    current_file_index += 1
                    current_output_path = get_output_path(current_file_index)
                    out = open(current_output_path, "w", encoding="utf-8")
                    current_file_chars = 0

                out.write(block)
                current_file_chars += block_len
                total_chars_written += block_len
                total_files_processed += 1
                total_lines_count += clean_content.count("\n") + 1

            except Exception as e:
                error_block = f"{'=' * 80}\n# FILE: {rel_path}\n# ERROR: {e}\n{'=' * 80}\n\n"
                # 错误信息通常很短，直接写入，如果不放心也可以做同样的切分检查，但一般不需要
                if current_file_chars > 0 and (current_file_chars + len(error_block)) > MAX_CHARS_PER_FILE:
                    out.close()
                    current_file_index += 1
                    current_output_path = get_output_path(current_file_index)
                    out = open(current_output_path, "w", encoding="utf-8")
                    current_file_chars = 0
                
                out.write(error_block)
                current_file_chars += len(error_block)
                total_files_processed += 1

    finally:
        if out:
            out.close()

    print(f"\n✅ [{dir_name}] 导出完成:")
    if current_file_index == 0:
        print(f"  输出文件: {current_output_path}")
    else:
        print(f"  输出文件系列: {get_output_path(0)} ... {get_output_path(current_file_index)}")
        
    print(f"  处理源文件数: {total_files_processed}")
    print(f"  总字符数:   {total_chars_written}")
    print(f"  生成文件数: {current_file_index + 1}")


def main():
    print(f"📁 扫描目录: {PROJECT_ROOT}")
    print(f"📏 单文件最大字符限制: {MAX_CHARS_PER_FILE}")

    for dir_name in INCLUDE_DIRS:
        files = collect_files(PROJECT_ROOT, dir_name)
        
        if not files:
            print(f"❌ [{dir_name}] 没有找到任何 .py 文件")
            continue

        # 生成输出文件名基础，例如 engine_dump.txt, tools_dump.txt
        output_filename = f"{dir_name}_dump.txt"
        output_path = PROJECT_ROOT / output_filename

        print(f"\n📄 [{dir_name}] 找到 {len(files)} 个 .py 文件:")
        for f in files:
            rel = f.relative_to(PROJECT_ROOT).as_posix()
            print(f"   - {rel}")

        write_dump_split(files, str(output_path), PROJECT_ROOT, dir_name)


if __name__ == "__main__":
    main()
