<!-- skills/git_workflow/skill.md -->

# git_workflow

## system

分析 Git 操作需求。根据用户输入推断需要执行的 Git 命令序列。

### 输出格式（严格 JSON）

```json
{
  "repo_path": "Git 仓库路径",
  "steps": [
    {"command": "status", "args": ""},
    {"command": "add", "args": "."},
    {"command": "commit", "args": "-m '提交信息'"},
    {"command": "push", "args": ""}
  ]
}
```

### 规则
- steps 按执行顺序排列
- command 不含 git 前缀（如 status/add/commit/push/pull/merge）
- 根据用户意图推断完整的命令序列
- 只输出 JSON

## report

总结 Git 操作结果。用一句话说明操作完成情况和当前分支状态（不超过 30 字）。
