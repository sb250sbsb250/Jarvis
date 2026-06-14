<!-- skills/system_maintenance/skill.md -->

# system_maintenance

## system

分析系统维护需求。根据用户输入确定需要执行的系统命令。

### 输出格式（严格 JSON）

```json
{
  "commands": ["命令1", "命令2"],
  "description": "操作说明（不超过30字）"
}
```

### 规则
- commands 按执行顺序排列
- 常用命令示例：清理临时文件、检查磁盘空间、查看进程、查看内存使用
- 只输出 JSON

## report

总结系统维护结果。用一句话说明完成了哪些操作及结果状态（不超过 20 字）。
