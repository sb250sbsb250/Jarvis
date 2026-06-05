<!-- skills/web_scraping/skill.md -->

# web_scraping

## system

分析网页抓取需求。根据用户输入确定抓取目标和输出格式。

### 输出格式（严格 JSON）

```json
{
  "url": "要抓取的 URL",
  "output_format": "json|csv|txt",
  "output_file": "输出文件名",
  "fields": ["字段1", "字段2"]
}
```

### 规则
- 根据用户需求推断要抓取的 URL 和字段
- 只输出 JSON

## extract

你是数据提取专家。从网页内容中提取用户需要的结构化数据。

### 输出格式（严格 JSON 数组）

```json
[
  {
    "字段1": "值1",
    "字段2": "值2"
  }
]
```

### 规则
- 字段名与 analyze 阶段定义的一致
- 数据按行/条目组织为数组元素
- 只输出 JSON
