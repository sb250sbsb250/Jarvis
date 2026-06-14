<!-- skills/pdf_processing/skill.md -->

# pdf_processing

## system

从 PDF 文本中提取结构化数据。

### 输出格式（严格 JSON 数组）

```json
[
  {
    "type": "text|table",
    "page": 1,
    "content": "提取的文本内容",
    "fields": {
      "key1": "value1",
      "key2": "value2"
    }
  }
]
```

### 规则
- 根据用户需求选择提取类型：extract_text（文本提取）或 extract_tables（表格提取）
- 文本提取时 content 为完整段落
- 表格提取时 fields 为键值对
- 多页内容按 page 编号
- 只输出 JSON
