# Jarvis V3 — 自主 Agent 引擎

> 一个基于 LLM + 工具循环的自主智能助手框架。  
> 核心模式：**AgentLoop** — 让 LLM 自主决策、调用工具、完成任务。

---

## 项目结构

```
jarvis-v3/
├── engine/                  # 核心引擎
│   ├── agent_loop.py        # 自主 Agent 循环（主循环）
│   ├── llm_client.py        # LLM 客户端（DeepSeek/OpenAI）
│   ├── checkpoint.py        # 检查点系统（中断恢复）
│   ├── tracer.py            # 调用追踪
│   ├── core/                # 核心类型
│   │   ├── types.py         # 消息/工具调用/结果类型
│   │   ├── errors.py        # 错误定义
│   │   ├── logging_config.py# 日志配置
│   │   └── token_estimator.py # Token 估算
│   ├── skill/               # Skill 系统（经验模块）
│   │   ├── base.py          # Skill 基类
│   │   ├── registry.py      # Skill 注册中心
│   │   ├── router.py        # Skill 路由器
│   │   ├── loader.py        # 标准 Skill 加载器（YAML+MD）
│   │   └── task_agent.py    # 任务型 Agent
│   ├── tool/                # 工具系统
│   │   ├── base.py          # 工具基类
│   │   ├── registry.py      # 工具注册中心（懒加载）
│   │   ├── executor.py      # 工具执行器（超时+重试）
│   │   ├── mcp.py           # MCP 协议适配器
│   │   └── policy.py        # 权限策略
│   ├── session/             # 会话管理
│   │   ├── session.py       # 会话实体
│   │   └── manager.py       # 会话管理器
│   ├── memory/              # 记忆系统
│   │   └── working_memory.py# 工作记忆（防重复操作）
│   ├── message/             # 消息系统
│   │   ├── message_list.py  # 消息列表
│   │   └── validator.py     # 消息验证
│   ├── plan/                # 计划系统
│   │   └── tracker.py       # 计划追踪
│   ├── longterm/            # 长期记忆
│   │   ├── topic_store.py   # 主题存储
│   │   ├── topic_search.py  # 主题搜索
│   │   ├── topic_inject.py  # 主题注入
│   │   └── topic_compress.py# 主题压缩
│   ├── lint/                # 代码检查
│   │   └── runner.py        # Lint 运行器
│   ├── storage/             # 持久化存储
│   │   ├── store.py         # 存储接口
│   │   └── state_store.py   # 状态存储
│   ├── debug/               # 调试工具
│   │   └── save_context.py  # 上下文保存
│   └── prompt/              # 提示词
│       └── complexity.py    # 复杂度分析
├── tools/                   # 工具实现（9个统一工具）
│   ├── __init__.py          # 工具注册入口
│   ├── file_tool.py         # 文件操作
│   ├── excel_tool.py        # Excel 操作
│   ├── code_tool_v4.py      # 代码操作
│   ├── shell_tool.py        # Shell 命令
│   ├── web_tool.py          # 网络操作
│   ├── git_tool.py          # Git 操作
│   ├── system_tool.py       # 系统信息
│   ├── image_tool.py        # 图片处理
│   └── office_tool.py       # Office 文档
├── skills/                  # Skill 定义（YAML+MD）
│   └── __init__.py          # 自动发现所有 Skill
├── frontend/                # 前端界面
├── tests/                   # 测试
│   ├── test_unit/           # 单元测试
│   ├── test_integration/    # 集成测试
│   └── test_e2e/            # 端到端测试
├── scripts/                 # 辅助脚本
├── server.py                # FastAPI 服务器入口
├── AGENTS.md                # Agent 行为指南
├── IDENTITY.md              # 身份定义
├── SOUL.md                  # 核心人格
├── TOOLS.md                 # 工具本地配置
├── HEARTBEAT.md             # 心跳检查清单
└── USER.md                  # 用户信息
```

---

## 核心架构

### AgentLoop — 自主 Agent 循环

```
while not done:
    1. LLM 分析任务 → 决定下一步
    2. 调用工具 → 获取结果
    3. 更新工作记忆 → 追加到消息列表
    4. 检查完成条件 → 继续或结束
```

**增强特性：**
- **工作记忆（WorkingMemory）** — 追踪已读取/已写入/已失败的操作，防止重复
- **自我反思（SelfReflection）** — 连续 3 次失败后强制反思，换方案
- **Token 预算管理** — 超 80% 上下文自动压缩旧轮次
- **检查点（Checkpoint）** — 每 10 轮自动保存，支持中断恢复
- **死循环检测** — 自动检测并跳出
- **自动 Lint** — 代码修改后自动检查语法

### Skill 系统 — 经验模块

Skill 是经过验证的执行经验，固化为可复用的模块。

```
skills/code_review/
├── skill.yaml   ← 元数据（名称、描述、标签、触发条件）
└── skill.md     ← System Prompt（按 ## 分段）
```

**特性：**
- 纯配置驱动，无需写代码
- 关键词/语义匹配自动路由
- 经验等级（使用越多越优先）
- 自动降级（失败时回退）
- 可组合（Skill 可调用其他 Skill）

### 工具系统 — 懒加载注册

```
注册时：只存类引用，不实例化
获取 Schema：临时实例化，用完丢弃
执行时：首次实例化并缓存
```

**9 个统一工具：**

| 工具 | 功能 |
|------|------|
| `file` | 文件 list/read/write/append/rename/diff |
| `excel` | Excel connect/read/write/save/close |
| `code` | 代码 search/read/edit/diff/rollback |
| `shell` | Shell 命令执行 + 输出恢复 |
| `web` | 网页 fetch / 搜索 |
| `git` | Git status/commit/push |
| `system` | 系统 info/time/cwd |
| `image` | 图片 read/ocr |
| `office` | PDF/Word 读写 |

### MCP 协议支持

支持通过 MCP（Model Context Protocol）自动发现和调用外部工具：
- **stdio** — 子进程通信（如 `mcp-server-filesystem`）
- **SSE** — HTTP SSE 通信（远程 MCP 服务）

---

## 快速开始

### 环境要求

- Python 3.10+
- DeepSeek API Key（或 OpenAI 兼容 API）

### 安装

```bash
# 克隆项目
git clone <repo-url>
cd jarvis-v3

# 创建虚拟环境
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
.venv\Scripts\activate     # Windows

# 安装依赖
pip install -r requirements.txt
```

### 配置

```bash
# 复制环境变量模板
cp .env.example .env

# 编辑 .env，填入 API Key
DEEPSEEK_API_KEY=your_api_key_here
DEEPSEEK_BASE_URL=https://api.deepseek.com
MODEL=deepseek-chat
```

### 启动

```bash
# 启动 Web 服务器
python server.py

# 访问 http://localhost:8000
```

---

## 核心概念

### 工作记忆（WorkingMemory）

LLM 的"便签本"，追踪当前任务中已进行的操作，防止重复读取和重复尝试已失败的方法。

```
已读取: app.py (第1-50行), config.py (全部)
已写入: output.xlsx (3个sheet)
最近错误: excel → 文件不存在
尝试过的方案: 方案A: 失败(文件过大)
```

### 检查点（Checkpoint）

每 N 轮自动保存执行状态，支持中断恢复。

```python
cp = Checkpoint(task_id)
cp.save(round_idx, messages, tool_calls_log, working_memory)
# 下次启动自动恢复
state = cp.load()
```

### 会话管理

支持多会话、持久化、断点续聊。

```python
manager = SessionManager()
session = await manager.create_session()
session.add_user_message("你好")
session.add_assistant_message("你好！有什么可以帮助你的？")
await manager.save_session(session)
```

### 工具执行器

带超时控制、权限策略、自动重试（指数退避）。

```python
executor = ToolExecutor(registry)
result = await executor.execute_one(call, timeout=30.0)
# 并行执行
results = await executor.execute_parallel(calls)
```

---

## 配置参考

### 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `DEEPSEEK_API_KEY` | DeepSeek API Key | — |
| `DEEPSEEK_BASE_URL` | API 地址 | `https://api.deepseek.com` |
| `MODEL` | 模型名称 | `deepseek-chat` |
| `DASHSCOPE_API_KEY` | 通义千问 Fallback | — |
| `MOONSHOT_API_KEY` | Kimi Fallback | — |
| `DEEPSEEK_API_KEY_2` | DeepSeek 备用 Key | — |

### AgentLoop 参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `max_rounds` | 最大执行轮数 | 200 |
| `auto_lint` | 自动代码检查 | True |
| `enable_checkpoint` | 启用检查点 | True |
| `CHECKPOINT_INTERVAL` | 检查点间隔 | 10 轮 |
| `REFLECTION_THRESHOLD` | 反思触发阈值 | 3 次失败 |
| `MAX_TOOL_RESULT_CHARS` | 工具结果截断长度 | 8000 |

---

## 开发指南

### 添加新工具

```python
from engine.tool.base import BaseTool, ToolParameter, ToolResult

class MyTool(BaseTool):
    @property
    def name(self) -> str:
        return "my_tool"
    
    @property
    def description(self) -> str:
        return "我的自定义工具"
    
    @property
    def parameters(self) -> list:
        return [
            ToolParameter("input", "string", "输入内容", required=True),
        ]
    
    async def execute(self, call_id: str, **kwargs) -> ToolResult:
        result = do_something(kwargs["input"])
        return ToolResult.success(call_id, self.name, result)

# 注册
registry.register(MyTool)
```

### 添加新 Skill

创建目录 `skills/my_skill/`，包含两个文件：

**skill.yaml:**
```yaml
name: my_skill
display_name: 我的技能
description: 处理特定任务的技能
icon: 🛠️
tags: ["任务", "自动化"]
when_to_use: 当用户需要处理特定任务时
tools: ["file", "shell"]
```

**skill.md:**
```markdown
## system
你是处理特定任务的专家。

## examples
用户: 帮我处理这个任务
助手: 好的，我来处理。

## constraints
- 不要删除原始文件
- 操作前先备份
```

---

## 架构设计原则

1. **纯懒加载** — 工具注册时只存类引用，不实例化，真正执行时才创建
2. **单例注册中心** — ToolRegistry 全局单例，线程安全
3. **配置驱动** — Skill 系统纯配置驱动，无需写代码
4. **可恢复** — 检查点系统支持任意中断恢复
5. **防重复** — 工作记忆追踪所有操作，防止重复读取和重复失败
6. **自动降级** — 连续失败自动反思换方案，API 失败自动 Fallback
7. **MCP 优先** — 支持通过 MCP 协议接入外部工具生态

---

## 许可证

MIT License
