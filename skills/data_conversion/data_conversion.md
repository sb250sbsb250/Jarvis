<!-- skills/data_conversion/skill.md -->

# data_conversion

## system

分析格式转换需求。用户输入中指定源格式、目标格式、输入文件和输出文件路径。

### 输出格式（严格 JSON）

```json
{
  "from_format": "csv|json|xml",
  "to_format": "csv|json|xml",
  "input_file": "输入文件路径",
  "output_file": "输出文件路径"
}
```

### 规则
- from_format/to_format 必须是 csv、json、xml 之一
- 根据用户需求推断格式名称
- 只输出 JSON，不要其他文字
- 路径支持绝对路径和相对路径

## report

总结转换结果。用一句话描述格式转换完成情况（不超过 20 字）。
