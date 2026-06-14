import os
import pandas as pd
import re
from pathlib import Path

# 目标文件夹
folder = r"C:\Users\xinzh\Downloads\RObot\2026_06_05_北京擎朗科技有限公司-2024年12期月报"

# 收集所有 Excel 文件
files = list(Path(folder).glob("*.xls*"))
print(f"找到 {len(files)} 个文件")

all_data = []

# 标准表头
STANDARD_HEADERS = [
    "科目编码", "科目名称", "核算维度编码", "核算维度名称",
    "(期初余额)借方", "(期初余额)贷方", "(本期发生)借方", "(本期发生)贷方",
    "(本年累计)借方", "(本年累计)贷方", "(期末余额)借方", "(期末余额)贷方"
]


def extract_company_name(filename):
    """
    从文件名正则提取公司名
    支持多种命名格式：
    - 2024年12期_北京擎朗科技有限公司_TB.xlsx
    - 2024年12期-北京擎朗科技有限公司-.xlsx
    - 北京擎朗科技有限公司_2024年12期.xlsx
    - 202412_上海擎朗智能科技有限公司_TB.xls
    - 2024-12_广州分公司_TB.xlsx
    - 报表_北京擎朗科技有限公司_2024年12期.xlsx
    """
    name = Path(filename).stem

    # 模式1: 匹配 _公司名 或 -公司名（公司名后跟 _ 或 - 或结尾）
    match = re.search(
        r'[_-]([\u4e00-\u9fa5()（）]+(?:有限|股份|集团|科技|实业|投资|控股|发展)?(?:责任)?公司[\u4e00-\u9fa5()（）]*?)(?=[_-]|$)',
        name)
    if match:
        return match.group(1)

    # 模式2: 匹配开头的公司名（公司名后跟 _ 或 -）
    match = re.search(
        r'^([\u4e00-\u9fa5()（）]+(?:有限|股份|集团|科技|实业|投资|控股|发展)?(?:责任)?公司[\u4e00-\u9fa5()（）]*?)[_-]',
        name)
    if match:
        return match.group(1)

    # 模式3: 按 _ 或 - 分割，取包含"公司"的部分
    parts = re.split(r'[_-]', name)
    for part in parts:
        if "公司" in part and len(part) >= 4:
            part = part.strip()
            if part:
                return part

    # 模式4: 整个文件名中匹配包含"公司"的最长片段
    match = re.search(r'([\u4e00-\u9fa5()（）]{2,}公司[\u4e00-\u9fa5()（）]*)', name)
    if match:
        return match.group(1)

    # 如果都匹配不到，返回文件名
    return name


def find_header_row(df_raw):
    """查找表头行，返回行索引，找不到返回None"""
    for i in range(min(20, len(df_raw))):
        row = df_raw.iloc[i].astype(str).str.strip().tolist()
        if "科目编码" in row:
            return i
    return None


def mark_leaf_nodes(df):
    """
    标记末级科目，同时识别辅助级
    规则：
    1. 如果核算维度编码有值 → "辅助级"
    2. 在判断末级时，排除辅助级科目
    3. 末级判断：当前科目的编码不是任何其他非辅助级科目编码的前缀
    """
    # 先标记辅助级
    df["科目级别"] = df.apply(
        lambda row: "辅助级" if pd.notna(row.get("核算维度编码")) and str(
            row["核算维度编码"]).strip() != "" else "待判断",
        axis=1
    )

    # 获取所有非辅助级的科目编码
    non_aux_codes = df[df["科目级别"] != "辅助级"]["科目编码"].dropna().astype(str).str.strip().tolist()
    unique_non_aux = sorted(set(non_aux_codes))

    # 判断非辅助级科目是否为末级
    def is_leaf(code):
        code_with_dot = code + "."
        for other in unique_non_aux:
            if other == code:
                continue
            if other.startswith(code_with_dot):
                return False
        return True

    # 更新科目级别
    for idx in df[df["科目级别"] == "待判断"].index:
        code = str(df.loc[idx, "科目编码"]).strip()
        if is_leaf(code):
            df.loc[idx, "科目级别"] = "末级"
        else:
            df.loc[idx, "科目级别"] = "非末级"

    return df


for f in files:
    print(f"处理: {f.name}")
    try:
        # 从文件名提取公司名
        company_name = extract_company_name(f.name)
        print(f"  公司名: {company_name}")

        is_xls = f.suffix.lower() == '.xls'

        # 读取整个 TB sheet（不设表头）
        df_raw = pd.read_excel(
            f,
            sheet_name="TB",
            header=None,
            dtype=object,
            engine='xlrd' if is_xls else 'openpyxl'
        )

        # 查找表头行
        header_row = find_header_row(df_raw)

        if header_row is None:
            print(f"  ⚠️ 未找到表头行，跳过")
            continue

        print(f"  表头行: 第{header_row + 1}行")

        # 重新读取，跳过表头行及之前的行
        skip_rows = header_row + 1
        df_raw_data = pd.read_excel(
            f,
            sheet_name="TB",
            header=None,
            skiprows=skip_rows,
            dtype=object,
            engine='xlrd' if is_xls else 'openpyxl'
        )

        # 删除全空行
        df_raw_data = df_raw_data.dropna(how='all')

        if df_raw_data.empty:
            print(f"  ⚠️ 数据为空，跳过")
            continue

        # 列数处理
        num_cols = len(STANDARD_HEADERS)
        if df_raw_data.shape[1] < num_cols:
            actual_headers = STANDARD_HEADERS[:df_raw_data.shape[1]]
        else:
            df_raw_data = df_raw_data.iloc[:, :num_cols]
            actual_headers = STANDARD_HEADERS

        df_raw_data.columns = actual_headers

        # 只保留标准表头列
        available_cols = [col for col in STANDARD_HEADERS if col in df_raw_data.columns]
        if len(available_cols) < 3:
            print(f"  ⚠️ 列匹配过少（{len(available_cols)}列），跳过")
            continue

        df = df_raw_data[available_cols].copy()

        # 清理科目编码
        df["科目编码"] = df["科目编码"].astype(str).str.strip()

        # 清理核算维度编码
        if "核算维度编码" in df.columns:
            df["核算维度编码"] = df["核算维度编码"].astype(str).str.strip()
            df["核算维度编码"] = df["核算维度编码"].replace(["", "nan", "None"], None)

        # 过滤无效行
        df = df[df["科目编码"] != ""]
        df = df[df["科目编码"] != "nan"]
        df = df.dropna(subset=["科目编码"])

        # 过滤汇总行
        code_col = df["科目编码"]
        df = df[~code_col.str.contains("合计|总计|小计|合 计", na=False)]

        # 过滤表头重复行
        df = df[~code_col.str.contains("科目编码", na=False)]

        # 过滤纯说明行
        df = df[code_col.str.contains(r'[\d.]', na=False)]

        # 标记末级科目和辅助级
        df = mark_leaf_nodes(df)

        # 金额列转换
        amount_cols = [
            "(期初余额)借方", "(期初余额)贷方",
            "(本期发生)借方", "(本期发生)贷方",
            "(本年累计)借方", "(本年累计)贷方",
            "(期末余额)借方", "(期末余额)贷方"
        ]
        for col in amount_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)

        # 添加公司名列和源文件列
        df.insert(0, "公司名", company_name)
        df.insert(1, "源文件", f.name)

        all_data.append(df)

        # 统计
        aux_count = (df["科目级别"] == "辅助级").sum()
        leaf_count = (df["科目级别"] == "末级").sum()
        non_leaf_count = (df["科目级别"] == "非末级").sum()

        print(f"  ✅ 读取 {len(df)} 行数据")
        print(f"     辅助级: {aux_count} | 末级: {leaf_count} | 非末级: {non_leaf_count}")

    except Exception as e:
        print(f"  ❌ 错误: {e}")
        import traceback

        traceback.print_exc()

# 合并所有数据
if all_data:
    merged = pd.concat(all_data, ignore_index=True)

    # 调整列顺序
    column_order = [
        "公司名", "源文件", "科目编码", "科目名称",
        "核算维度编码", "核算维度名称", "科目级别",
        "(期初余额)借方", "(期初余额)贷方",
        "(本期发生)借方", "(本期发生)贷方",
        "(本年累计)借方", "(本年累计)贷方",
        "(期末余额)借方", "(期末余额)贷方"
    ]
    existing_columns = [col for col in column_order if col in merged.columns]
    merged = merged[existing_columns]

    output_path = os.path.join(folder, "TB合并结果.xlsx")
    merged.to_excel(output_path, index=False)

    print(f"\n{'=' * 50}")
    print(f"✅ 完成！")
    print(f"   文件数: {len(all_data)}")
    print(f"   总行数: {len(merged)}")
    print(f"   辅助级: {(merged['科目级别'] == '辅助级').sum()} 个")
    print(f"   末级: {(merged['科目级别'] == '末级').sum()} 个")
    print(f"   非末级: {(merged['科目级别'] == '非末级').sum()} 个")

    # 按公司统计
    print(f"\n公司统计:")
    for company, count in merged.groupby("公司名").size().items():
        print(f"   {company}: {count} 行")

    print(f"\n   列名: {list(merged.columns)}")
    print(f"   输出: {output_path}")

    # 显示示例
    print(f"\n示例数据（前10行）:")
    print(merged.head(10)[["公司名", "科目编码", "科目名称", "核算维度编码", "科目级别"]].to_string(index=False))

else:
    print("\n❌ 没有成功读取任何数据")

# 测试公司名提取
print(f"\n{'=' * 50}")
print("公司名提取测试:")
test_names = [
    "2024年12期_北京擎朗科技有限公司_TB.xlsx",
    "202412_上海擎朗智能科技有限公司_TB.xls",
    "北京擎朗科技有限公司_2024年12期_TB.xlsx",
    "2024-12_广州分公司_TB.xlsx",
    "报表_深圳市腾讯计算机系统有限公司_2024.xlsx",
    "2023年期_阿里巴巴(中国)有限公司_TB.xlsx",
]
for name in test_names:
    company = extract_company_name(name)
    print(f"  {name}")
    print(f"    -> {company}")