"""
DAG 执行图架构 - Jarvis V3 核心引擎

采用 Node + Edge 的 DAG 执行模型替代传统状态机。
"""

from .node import (
    Node, NodeInput, NodeOutput,
    ToolNode, LLMNode, RouterNode, ParallelNode,
    HumanInLoopNode, MapNode, ToolDispatchNode,
    ListFilesNode, FileProcessorNode, FileRenameNode,
    CodeSearchNode, CodeEditorNode,
)
from .edge import Edge, ConditionalEdge
from .graph import WorkflowGraph
from .context import ExecutionContext, NodeTrace, ExecutionContextConfig
from .tracer import AgentTracer, TraceSpan, tracer as global_tracer
from .executor import GraphExecutor, HumanInterruptError
from .builder import AgentGraphBuilder
from .compiler import GraphCompiler, CompiledGraph, compile_graph
from .planner import DAGPlanner, DAGFactory, describe_tools
