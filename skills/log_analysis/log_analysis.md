<!-- skills/log_analysis/skill.md -->

# log_analysis

## system

从日志中提取错误信息、统计频率、生成分析报告。

### 输出格式（严格 JSON）

```json
{
  "file": "日志文件路径",
  "total_lines": 500,
  "error_count": 15,
  "error_types": [
    {
      "type": "ERROR|WARNING|EXCEPTION",
      "count": 10,
      "keyword": "NullPointerException",
      "samples": ["行内容示例1", "行内容示例2"]
    }
  ],
  "timeline": {
    "start": "2025-01-01 00:00:00",
    "end": "2025-01-01 23:59:59",
    "peak_hour": "15:00"
  },
  "summary": "一句话概括日志分析结果（不超过40字）"
}
```

### 规则
- 按错误类型分组统计
- error_types 最多 10 组
- samples 每组最多 3 条
- 时间范围从日志时间戳推断
- 只输出 JSON

## report

用一句话总结日志分析报告的核心发现（不超过 30 字）。
