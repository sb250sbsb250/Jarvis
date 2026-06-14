<!-- skills/image_batch/skill.md -->

# image_batch

## system

分析图片批处理需求。根据用户输入确定需要执行的操作和参数。

### 输出格式（严格 JSON）

```json
{
  "action": "compress|convert|resize|rename",
  "folder": "图片目录路径",
  "pattern": "*.jpg,*.png",
  "quality": 85,
  "target_format": "jpg|png|webp",
  "max_width": 0
}
```

### 规则
- action: compress（压缩）、convert（格式转换）、resize（缩放）、rename（重命名）
- quality: 1-100，仅 compress 时有效
- max_width: 仅 resize 时有效，0 表示不限制
- 只输出 JSON

## report

总结图片处理结果。用一句话说明处理了多少文件、执行了什么操作（不超过 20 字）。
