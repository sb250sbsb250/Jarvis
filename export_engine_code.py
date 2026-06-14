#!/usr/bin/env python3
"""
读取 engine, tools 和 skills 文件夹下所有 .py, .yaml, .yml, .md 文件（排除 __pycache__），
分别以 100000 字符为上限，输出到 txt 文件。
如果内容超过 100000 字符，则拆分为多个文件。
"""

import os

# 定义要处理的目录配置
# key: 目录名, value: 输出文件的前缀
DIR_CONFIG = {
    "engine": "engine_code",
    "tools": "tool_code",
    "skills": "skill_code"
}

BASE_DIR = os.path.dirname(__file__)
MAX_CHARS = 100000

# 支持的文件扩展名
SUPPORTED_EXTENSIONS = ('.py', '.yaml', '.yml', '.md')


def collect_files(root_dir):
    """收集指定目录下所有支持的文件，排除 __pycache__"""
    files = []
    if not os.path.exists(root_dir):
        return files
        
    for dirpath, dirnames, filenames in os.walk(root_dir):
        # 跳过 __pycache__ 目录
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for f in filenames:
            if f.lower().endswith(SUPPORTED_EXTENSIONS):
                full_path = os.path.join(dirpath, f)
                # 获取相对路径（相对于 root_dir）
                rel_path = os.path.relpath(full_path, root_dir)
                files.append((rel_path, full_path))
    # 按路径排序，保证输出顺序一致
    files.sort(key=lambda x: x[0])
    return files


def format_file_content(rel_path, abs_path):
    """格式化单个文件的内容"""
    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        print(f"警告: 无法读取文件 {abs_path}: {e}")
        return ""

    separator = f"{'=' * 80}\n# FILE: {rel_path}\n{'=' * 80}\n"
    return separator + content + "\n\n"


def process_directory(dir_name, output_prefix):
    """处理单个目录的代码导出"""
    root_dir = os.path.join(BASE_DIR, dir_name)
    files = collect_files(root_dir)
    
    print(f"\n--- 处理目录: {dir_name} ---")
    print(f"找到 {len(files)} 个支持的文件 (.py, .yaml, .yml, .md)")

    if not files:
        print(f"在 {dir_name} 中没有找到任何支持的文件")
        return

    file_index = 1
    current_content = ""
    current_file_count = 0

    for rel_path, abs_path in files:
        formatted = format_file_content(rel_path, abs_path)
        if not formatted:
            continue

        # 如果单个文件就超过上限，单独输出
        if len(formatted) > MAX_CHARS:
            # 先把当前累积的内容写入
            if current_content:
                output_path = os.path.join(BASE_DIR, f"{output_prefix}_part_{file_index}.txt")
                with open(output_path, "w", encoding="utf-8") as f:
                    f.write(current_content)
                print(f"已写入: {output_path} ({len(current_content)} 字符, {current_file_count} 个文件)")
                file_index += 1
                current_content = ""
                current_file_count = 0

            # 超大文件单独写
            output_path = os.path.join(BASE_DIR, f"{output_prefix}_part_{file_index}.txt")
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(formatted)
            print(f"已写入: {output_path} ({len(formatted)} 字符, 1 个文件 - {rel_path})")
            file_index += 1
            continue

        # 如果加上这个文件会超限，先写入当前累积
        if len(current_content) + len(formatted) > MAX_CHARS and current_content:
            output_path = os.path.join(BASE_DIR, f"{output_prefix}_part_{file_index}.txt")
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(current_content)
            print(f"已写入: {output_path} ({len(current_content)} 字符, {current_file_count} 个文件)")
            file_index += 1
            current_content = ""
            current_file_count = 0

        current_content += formatted
        current_file_count += 1

    # 写入最后剩余的内容
    if current_content:
        output_path = os.path.join(BASE_DIR, f"{output_prefix}_part_{file_index}.txt")
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(current_content)
        print(f"已写入: {output_path} ({len(current_content)} 字符, {current_file_count} 个文件)")

    print(f"完成！{dir_name} 共生成 {file_index} 个文件")


def main():
    for dir_name, output_prefix in DIR_CONFIG.items():
        process_directory(dir_name, output_prefix)
    
    print("\n=== 全部处理完成 ===")


if __name__ == "__main__":
    main()
