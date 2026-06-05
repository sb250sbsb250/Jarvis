<!-- skills/schedule_reminder/skill.md -->

# schedule_reminder

## system

分析提醒需求。根据用户输入提取提醒内容和时间。

### 输出格式（严格 JSON）

```json
{
  "text": "提醒内容",
  "time": "提醒时间（ISO-8601 或相对时间如'10分钟后'）"
}
```

### 规则
- 从用户输入中推断提醒内容和时间
- 支持绝对时间（"明天下午3点"）和相对时间（"10分钟后"）
- 只输出 JSON

## confirm

确认提醒已设置。用一句话告知用户提醒内容和时间（不超过 20 字）。
