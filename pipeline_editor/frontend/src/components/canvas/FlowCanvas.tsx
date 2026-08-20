import {
  Background,
  Controls,
  MiniMap,
  ReactFlow,
  useReactFlow,
  type Connection,
  type Edge,
  type NodeChange,
} from "@xyflow/react";
import { useCallback, useEffect, useMemo } from "react";

import type { NodeSize } from "../../graph/autoLayout";
import { localLint } from "../../graph/localLint";
import { taskToGraph, TASK_LEVEL_NODE_ID, type TaskFlowNode } from "../../graph/taskToGraph";
import { useEditorStore } from "../../store/editorStore";
import { useRunStore } from "../../store/runStore";
import { useUiStore } from "../../store/uiStore";
import { TaskNode } from "./TaskNode";
import { edgeTypes } from "./edges";

const nodeTypes = { taskNode: TaskNode };

interface Props {
  steps?: Record<string, string>;
  readonly?: boolean;
}

export function FlowCanvas({ steps, readonly }: Props) {
  const doc = useEditorStore((s) => s.doc);
  const layout = useEditorStore((s) => s.layout);
  const moveNode = useEditorStore((s) => s.moveNode);
  const connectNext = useEditorStore((s) => s.connectNext);
  const setOnTimeout = useEditorStore((s) => s.setOnTimeout);
  const removeNext = useEditorStore((s) => s.removeNext);
  const relayoutWithSizes = useEditorStore((s) => s.relayoutWithSizes);

  const selectedNode = useUiStore((s) => s.selectedNode);
  const select = useUiStore((s) => s.select);
  const setRightTab = useUiStore((s) => s.setRightTab);
  const relayoutTick = useUiStore((s) => s.relayoutTick);
  const showJumpEdges = useUiStore((s) => s.showJumpEdges);
  const showNodeDetails = useUiStore((s) => s.showNodeDetails);
  const errorNode = useUiStore((s) => s.errorNode);
  const errorMessage = useUiStore((s) => s.errorMessage);
  const followRun = useUiStore((s) => s.followRun);

  const runStatus = useRunStore((s) => s.status);
  const runCurrent = useRunStore((s) => s.currentNode);
  const runVisited = useRunStore((s) => s.visited);
  const running = runStatus === "running";
  const locked = readonly || running;

  const { setCenter, getNode, getNodes, fitView } = useReactFlow();

  const lintWarnings = useMemo(() => (doc ? localLint(doc) : []), [doc]);

  const { nodes, edges } = useMemo(() => {
    if (!doc) return { nodes: [] as TaskFlowNode[], edges: [] as Edge[] };
    return taskToGraph(doc, {
      layout,
      steps,
      selected: selectedNode,
      lintWarnings,
      errorNode,
      errorMessage,
      showJumpEdges,
      showNodeDetails,
      runCurrent: running || runStatus ? runCurrent : null,
      runVisited,
    });
  }, [
    doc, layout, steps, selectedNode, lintWarnings, errorNode, errorMessage,
    showJumpEdges, showNodeDetails, runCurrent, runVisited, running, runStatus,
  ]);

  // 运行跟随：当前节点变化时平滑居中
  useEffect(() => {
    if (!running || !followRun || !runCurrent) return;
    const node = getNode(runCurrent);
    if (node) {
      setCenter(node.position.x + 120, node.position.y + 48, {
        zoom: 1,
        duration: 400,
      });
    }
  }, [runCurrent, running, followRun, getNode, setCenter]);

  /**
   * 工具条「自动布局」：uiStore 只自增 relayoutTick，真正的重排在这里做——
   * 实测尺寸（`node.measured`）只有 ReactFlow context 内部拿得到。
   * 虚拟节点 `__task__` 不在 doc.nodes 里，必须排除（autoLayout 也会再兜一层）。
   */
  useEffect(() => {
    if (relayoutTick === 0) return; // 初始值，不是用户点的
    const sizes: Record<string, NodeSize> = {};
    for (const n of getNodes()) {
      if (n.id === TASK_LEVEL_NODE_ID) continue;
      const w = n.measured?.width;
      const h = n.measured?.height;
      if (typeof w === "number" && typeof h === "number" && w > 0 && h > 0) {
        sizes[n.id] = { width: w, height: h };
      }
    }
    relayoutWithSizes(sizes);
    // 重排后画面多半跑出视口，等新坐标渲染完再 fitView（纯视图操作，不落 store）
    const timer = window.setTimeout(() => fitView({ duration: 300, padding: 0.12 }), 50);
    return () => window.clearTimeout(timer);
  }, [relayoutTick, getNodes, relayoutWithSizes, fitView]);

  const onNodesChange = useCallback(
    (changes: NodeChange<TaskFlowNode>[]) => {
      for (const change of changes) {
        if (change.type === "position" && change.position && !change.dragging) {
          moveNode(change.id, change.position);
        } else if (change.type === "position" && change.position) {
          // 拖动中也更新（视觉跟手），结束时的最终位置同样落 store
          moveNode(change.id, change.position);
        } else if (change.type === "select") {
          if (change.selected) select(change.id === TASK_LEVEL_NODE_ID ? null : change.id);
        }
      }
    },
    [moveNode, select],
  );

  /** 双击 = 选中 + 明确切到属性面板（比单击更强的「我要编辑这个」意图，运行中也照切） */
  const onNodeDoubleClick = useCallback(
    (_e: React.MouseEvent, node: TaskFlowNode) => {
      if (node.id === TASK_LEVEL_NODE_ID) return;
      select(node.id);
      setRightTab("node");
    },
    [select, setRightTab],
  );

  const onConnect = useCallback(
    (conn: Connection) => {
      if (locked || !conn.source || !conn.target) return;
      if (conn.source === TASK_LEVEL_NODE_ID || conn.target === TASK_LEVEL_NODE_ID) return;
      if (conn.sourceHandle === "timeout") setOnTimeout(conn.source, conn.target);
      else connectNext(conn.source, conn.target);
    },
    [locked, connectNext, setOnTimeout],
  );

  const onEdgesDelete = useCallback(
    (deleted: Edge[]) => {
      if (locked) return;
      for (const edge of deleted) {
        if (edge.type === "nextEdge") {
          const order = (edge.data as { order?: number } | undefined)?.order;
          removeNext(edge.source, edge.target, order);
        } else if (edge.type === "timeoutEdge") {
          setOnTimeout(edge.source, null);
        }
      }
    },
    [locked, removeNext, setOnTimeout],
  );

  return (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      nodeTypes={nodeTypes}
      edgeTypes={edgeTypes}
      onNodesChange={onNodesChange}
      onNodeDoubleClick={onNodeDoubleClick}
      onConnect={onConnect}
      onEdgesDelete={onEdgesDelete}
      onPaneClick={() => select(null)}
      nodesDraggable={!readonly}
      nodesConnectable={!locked}
      elementsSelectable
      deleteKeyCode={locked ? null : ["Delete", "Backspace"]}
      // 节点删除走属性面板的删除按钮（防误删），画布 Delete 只删边
      onNodesDelete={() => {}}
      onBeforeDelete={async ({ nodes: n, edges: e }) => ({ nodes: [], edges: e })}
      fitView
      minZoom={0.1}
      proOptions={{ hideAttribution: true }}
    >
      <Background gap={20} />
      <Controls showInteractive={false} />
      <MiniMap pannable zoomable style={{ width: 160, height: 110 }} />
    </ReactFlow>
  );
}
