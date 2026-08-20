/** 编辑器页：工具条 + 节点大纲 + 画布 + 右侧 Tabs（节点/任务/问题/运行）。 */
import {
  ArrowLeftOutlined,
  LayoutOutlined,
  NodeIndexOutlined,
  OrderedListOutlined,
  PlusOutlined,
  RedoOutlined,
  SaveOutlined,
  UndoOutlined,
} from "@ant-design/icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ReactFlowProvider } from "@xyflow/react";
import {
  Alert,
  App as AntApp,
  Badge,
  Button,
  Input,
  List,
  Modal,
  Space,
  Spin,
  Switch,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { tasksApi } from "../api/endpoints";
import { EventsSocket } from "../api/events";
import { FlowCanvas } from "../components/canvas/FlowCanvas";
import { NodeInspector } from "../components/inspector/NodeInspector";
import { RunPanel } from "../components/run/RunPanel";
import { TaskSettingsPanel } from "../components/taskSettings/TaskSettingsPanel";
import { ProblemsPanel } from "../components/validation/ProblemsPanel";
import { serializeForSave } from "../graph/serialize";
import { isDirty, useEditorStore } from "../store/editorStore";
import { useRunStore } from "../store/runStore";
import { useUiStore } from "../store/uiStore";
import type { FileVersion, LintWarning, StructuredError } from "../types/api";
import { advanceVersion, isVersionConflict } from "../utils/fileVersion";
import type { OwnWriteRecord } from "../utils/ownWrite";
import {
  canonicalJson,
  isOwnWrite,
  taskFingerprint,
  withinOwnWriteWindow,
} from "../utils/ownWrite";

export function EditorPage() {
  const { name = "" } = useParams();
  const { message } = AntApp.useApp();
  const queryClient = useQueryClient();

  const doc = useEditorStore((s) => s.doc);
  const layout = useEditorStore((s) => s.layout);
  const layoutDirty = useEditorStore((s) => s.layoutDirty);
  const taskName = useEditorStore((s) => s.taskName);
  const loadTask = useEditorStore((s) => s.loadTask);
  const markSaved = useEditorStore((s) => s.markSaved);
  const markLayoutSaved = useEditorStore((s) => s.markLayoutSaved);
  const addNode = useEditorStore((s) => s.addNode);
  const dirty = useEditorStore(isDirty);

  const selectedNode = useUiStore((s) => s.selectedNode);
  const select = useUiStore((s) => s.select);
  const rightTab = useUiStore((s) => s.rightTab);
  const setRightTab = useUiStore((s) => s.setRightTab);
  const showJumpEdges = useUiStore((s) => s.showJumpEdges);
  const toggleJumpEdges = useUiStore((s) => s.toggleJumpEdges);
  const showNodeDetails = useUiStore((s) => s.showNodeDetails);
  const setShowNodeDetails = useUiStore((s) => s.setShowNodeDetails);
  const requestRelayout = useUiStore((s) => s.requestRelayout);
  const setError = useUiStore((s) => s.setError);

  const runStatus = useRunStore((s) => s.status);
  const running = runStatus === "running";

  const [steps, setSteps] = useState<Record<string, string>>({});
  const [validateError, setValidateError] = useState<StructuredError | null>(null);
  const [lintWarnings, setLintWarnings] = useState<LintWarning[]>([]);
  const [saving, setSaving] = useState(false);
  const [addOpen, setAddOpen] = useState(false);
  const [addName, setAddName] = useState("");
  /** 外部（AI/MCP/手改文件）修改与本地未保存修改冲突时的横幅 */
  const [externalChange, setExternalChange] = useState(false);
  /**
   * 自己最近一次写盘的记录（时刻 + 写盘内容指纹）：收到变更事件时先看时间窗，
   * 窗口内再比磁盘内容——一致才算自写（忽略，不清 undo），不一致就是真外部写入。
   */
  const lastOwnWriteRef = useRef<OwnWriteRecord | null>(null);
  /**
   * 乐观并发基线：最近一次载入/保存/重排看到的文件版本，保存时随请求带上。
   * 磁盘已经比它新 → 后端 409，不覆盖（推进方式见 utils/fileVersion.ts）。
   */
  const baseVersionRef = useRef<FileVersion>(null);

  const detailQuery = useQuery({
    queryKey: ["task", name],
    queryFn: () => tasksApi.get(name),
  });

  // 加载：resolved（合并态）进 store；resolved 为空（任务坏）退回 raw
  useEffect(() => {
    const detail = detailQuery.data;
    if (!detail) return;
    (async () => {
      let savedLayout = null;
      try {
        savedLayout = await tasksApi.getLayout(name);
      } catch {
        /* 后端不可达 → 自动布局 */
      }
      const layoutNodes =
        savedLayout && Object.keys(savedLayout.nodes ?? {}).length > 0
          ? savedLayout.nodes
          : null;
      const docToLoad = detail.resolved ?? detail.raw;
      baseVersionRef.current = advanceVersion(detail.mtime_ns);
      loadTask(name, structuredClone(docToLoad), layoutNodes);
      setSteps(detail.resolved?._steps ?? {});
      setValidateError(detail.error);
      select(null);
      setError(null, null);
      useEditorStore.temporal.getState().clear();
    })();
  }, [detailQuery.data, name, loadTask, select, setError]);

  // 编辑防抖校验（800ms）：后端 resolve_task 干跑 → 错误 + 步号
  const validateTimer = useRef<ReturnType<typeof setTimeout>>(undefined);
  useEffect(() => {
    if (!doc || taskName !== name) return;
    clearTimeout(validateTimer.current);
    validateTimer.current = setTimeout(async () => {
      try {
        const result = await tasksApi.validate(serializeForSave(doc));
        if (result.ok) {
          setValidateError(null);
          setSteps(result.steps ?? {});
          setError(null, null);
        } else {
          setValidateError(result.error ?? null);
          if (result.error?.node) setError(result.error.node, result.error.message);
        }
      } catch {
        /* 后端不在线时静默 */
      }
    }, 800);
    return () => clearTimeout(validateTimer.current);
  }, [doc, taskName, name, setError]);

  // 布局自动保存（1.5s 防抖）
  const layoutTimer = useRef<ReturnType<typeof setTimeout>>(undefined);
  useEffect(() => {
    if (!layoutDirty || taskName !== name) return;
    clearTimeout(layoutTimer.current);
    layoutTimer.current = setTimeout(async () => {
      try {
        await tasksApi.saveLayout(name, { nodes: layout });
        markLayoutSaved();
      } catch {
        /* 静默 */
      }
    }, 1500);
    return () => clearTimeout(layoutTimer.current);
  }, [layout, layoutDirty, taskName, name, markLayoutSaved]);

  // 全局事件订阅：AI（内嵌 MCP）或外部进程改了当前任务 → 自动重载或冲突横幅
  useEffect(() => {
    let cancelled = false;

    /** 确认是外部写入后的落地：干净则重载，脏则冲突横幅 */
    const applyExternal = (toast: string): void => {
      if (cancelled) return;
      if (isDirty(useEditorStore.getState())) {
        setExternalChange(true);
      } else {
        queryClient.invalidateQueries({ queryKey: ["task", name] });
        message.info(toast, 3);
      }
    };

    const handleTaskChanged = async (): Promise<void> => {
      const record = lastOwnWriteRef.current;
      const now = Date.now();
      if (withinOwnWriteWindow(record, now)) {
        // 疑似自写：拉一次磁盘内容比对。刻意绕开 react-query 缓存——走
        // fetchQuery 会顺带刷新缓存，触发载入 effect 重载画布并清 undo
        let diskFingerprint: string | null = null;
        try {
          const detail = await tasksApi.get(name);
          const disk = detail.resolved ?? detail.raw;
          diskFingerprint = disk ? taskFingerprint(disk) : null;
        } catch {
          /* 拉不到就当外部写入处理，绝不静默吞掉 */
        }
        if (cancelled) return;
        if (isOwnWrite(record, diskFingerprint, now)) return; // 确为自写
        lastOwnWriteRef.current = null; // 磁盘已不是我写的那份，后续事件不必再比
      }
      applyExternal("任务已被外部修改（AI/MCP），画布已自动重载");
    };

    const sock = new EventsSocket(
      (ev) => {
        if (ev.type !== "task_changed" || ev.name !== name) return;
        void handleTaskChanged();
      },
      {
        // 事件补不齐（后端缓冲淘汰/重启）：当作可能有外部改动，做一次全量刷新
        onResync: () => applyExternal("与后端事件流失联过久，已重新拉取任务"),
      },
    );
    sock.connect();
    return () => {
      cancelled = true;
      sock.close();
    };
  }, [name, queryClient, message]);

  // dirty 离开守卫
  useEffect(() => {
    const handler = (e: BeforeUnloadEvent) => {
      if (isDirty(useEditorStore.getState())) e.preventDefault();
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, []);

  // Ctrl+Z / Ctrl+Shift+Z / Ctrl+S
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement;
      const inInput =
        target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable;
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z" && !inInput) {
        e.preventDefault();
        const temporal = useEditorStore.temporal.getState();
        if (e.shiftKey) temporal.redo();
        else temporal.undo();
      }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
        e.preventDefault();
        void handleSave();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [doc, name]);

  const handleSave = useCallback(async () => {
    const state = useEditorStore.getState();
    if (!state.doc) return;
    setSaving(true);
    try {
      const payload = serializeForSave(state.doc);
      const result = await tasksApi.save(name, payload, baseVersionRef.current);
      if (result.ok) {
        // 记下写盘内容指纹：watcher 事件回来时用它区分「自己写的」与「AI 写的」
        lastOwnWriteRef.current = { at: Date.now(), fingerprint: canonicalJson(payload) };
        baseVersionRef.current = advanceVersion(result.mtime_ns);
        markSaved();
        setLintWarnings(result.lint_warnings ?? []);
        message.success(
          `已保存（${result.nodes} 节点${
            result.lint_warnings?.length ? `，${result.lint_warnings.length} 条 lint 提示` : ""
          }）`,
        );
        if (result.lint_warnings?.length) setRightTab("problems");
      } else {
        setValidateError(result.error ?? null);
        setLintWarnings(result.lint_warnings ?? []);
        if (result.error?.node) {
          setError(result.error.node, result.error.message);
          select(result.error.node);
        }
        setRightTab("problems");
        message.error(result.error?.message ?? "保存失败");
      }
    } catch (e) {
      if (isVersionConflict(e)) {
        // 磁盘上有更新的修改：本地内容原样保留（仍是脏的），交给冲突横幅决策
        setExternalChange(true);
        message.error("保存已被拒绝：磁盘上有更新的修改（AI/MCP 或手改）", 4);
      } else {
        message.error((e as Error).message);
      }
    } finally {
      setSaving(false);
    }
  }, [name, markSaved, message, setRightTab, setError, select]);

  /**
   * 冲突横幅的「保持本地」：把基线推到磁盘当前版本 —— 用户已知情，下次保存
   * 是有意覆盖。不推的话基线永远过期，保存会一直 409（死循环）。
   * 刻意绕开 react-query 缓存：走 fetchQuery 会刷新缓存触发载入 effect，
   * 把用户想保住的本地修改重载掉。
   */
  const keepLocal = useCallback(async () => {
    try {
      const detail = await tasksApi.get(name);
      baseVersionRef.current = advanceVersion(detail.mtime_ns);
    } catch {
      baseVersionRef.current = null; // 拉不到版本：放弃乐观锁，退回直接覆盖
    }
    setExternalChange(false);
  }, [name]);

  const outline = useMemo(() => {
    if (!doc) return [];
    const sortKey = (label: string | undefined): number[] => {
      if (!label || label === "?") return [Number.MAX_SAFE_INTEGER];
      return label.split(".").map(Number);
    };
    return Object.keys(doc.nodes).sort((a, b) => {
      const ka = sortKey(steps[a]);
      const kb = sortKey(steps[b]);
      for (let i = 0; i < Math.max(ka.length, kb.length); i++) {
        const d = (ka[i] ?? 0) - (kb[i] ?? 0);
        if (d !== 0) return d;
      }
      return a.localeCompare(b);
    });
  }, [doc, steps]);

  const problemCount =
    (validateError ? 1 : 0) + lintWarnings.length;

  if (detailQuery.isLoading || !doc || taskName !== name) {
    return (
      <div style={{ display: "grid", placeItems: "center", height: "100%" }}>
        <Spin tip="加载任务中…">
          <div style={{ minWidth: 160, minHeight: 80 }} />
        </Spin>
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      {/* 工具条 */}
      <Space
        style={{
          padding: "6px 12px",
          borderBottom: "1px solid #f0f0f0",
          background: "#fff",
          flexWrap: "wrap",
        }}
      >
        <Link to="/">
          <Button size="small" icon={<ArrowLeftOutlined />}>
            列表
          </Button>
        </Link>
        <Typography.Text strong>{name}</Typography.Text>
        {dirty && <Tag color="orange">未保存</Tag>}
        {running && <Tag color="green">运行中（画布只读）</Tag>}
        <Button
          size="small"
          type="primary"
          icon={<SaveOutlined />}
          loading={saving}
          disabled={running}
          onClick={handleSave}
        >
          保存
        </Button>
        <Tooltip title="撤销 (Ctrl+Z)">
          <Button
            size="small"
            icon={<UndoOutlined />}
            disabled={running}
            onClick={() => useEditorStore.temporal.getState().undo()}
          />
        </Tooltip>
        <Tooltip title="重做 (Ctrl+Shift+Z)">
          <Button
            size="small"
            icon={<RedoOutlined />}
            disabled={running}
            onClick={() => useEditorStore.temporal.getState().redo()}
          />
        </Tooltip>
        <Button
          size="small"
          icon={<PlusOutlined />}
          disabled={running}
          onClick={() => setAddOpen(true)}
        >
          新建节点
        </Button>
        {/* 实际重排在 FlowCanvas 里做（只有它拿得到节点实测尺寸），这里只发请求 */}
        <Tooltip title="dagre 自动布局（按卡片实际渲染高度排，不会重叠）">
          <Button size="small" icon={<LayoutOutlined />} onClick={requestRelayout}>
            自动布局
          </Button>
        </Tooltip>
        <Tooltip
          title={
            dirty
              ? "有未保存修改，先保存再重排（重排会整文件重写）"
              : "按图重算步号并写回文件（主干 1,2,3…，分支 2.1）"
          }
        >
          <Button
            size="small"
            icon={<OrderedListOutlined />}
            disabled={dirty || running}
            onClick={async () => {
              try {
                const r = await tasksApi.renumber(name);
                // 重排由后端整文件重写，前端算不出指纹：窗口内一律认自写
                // （重载由紧接着的 invalidate 负责，避免和事件处理打架）
                lastOwnWriteRef.current = { at: Date.now(), fingerprint: null };
                baseVersionRef.current = advanceVersion(r.mtime_ns);
                message.success(`已重排 ${r.count} 个节点的步号`);
                queryClient.invalidateQueries({ queryKey: ["task", name] });
              } catch (e) {
                message.error((e as Error).message);
              }
            }}
          >
            重排步号
          </Button>
        </Tooltip>
        <Tooltip title="显示 on_finding / watchdog skip_to 跳转边">
          <Switch
            size="small"
            checked={showJumpEdges}
            onChange={toggleJumpEdges}
            checkedChildren="跳转层"
            unCheckedChildren="跳转层"
          />
        </Tooltip>
        <Tooltip title="节点卡片显示 roi / template / target / 时序等参数行（关掉只看两行摘要）">
          <Switch
            size="small"
            checked={showNodeDetails}
            onChange={setShowNodeDetails}
            checkedChildren="详情"
            unCheckedChildren="详情"
          />
        </Tooltip>
        {validateError && (
          <Alert
            type="error"
            showIcon
            banner
            message={validateError.message}
            style={{ padding: "0 8px", fontSize: 12 }}
          />
        )}
      </Space>

      {externalChange && (
        <Alert
          type="warning"
          showIcon
          banner
          message="任务文件已被外部修改（AI/MCP 或手改），与你的未保存修改冲突"
          action={
            <Space>
              <Button
                size="small"
                danger
                onClick={() => {
                  setExternalChange(false);
                  queryClient.invalidateQueries({ queryKey: ["task", name] });
                }}
              >
                重载（丢弃本地修改）
              </Button>
              <Button size="small" onClick={() => void keepLocal()}>
                保持本地（下次保存将覆盖外部修改）
              </Button>
            </Space>
          }
        />
      )}

      <div style={{ display: "flex", flex: 1, minHeight: 0 }}>
        {/* 左侧大纲 */}
        <div
          style={{
            width: 220,
            borderRight: "1px solid #f0f0f0",
            overflow: "auto",
            background: "#fff",
          }}
        >
          <List
            size="small"
            header={
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                <NodeIndexOutlined /> 节点大纲（按步号）
              </Typography.Text>
            }
            dataSource={outline}
            renderItem={(n) => {
              const label = steps[n] ?? "?";
              const depth = label === "?" ? 0 : label.split(".").length - 1;
              return (
                <List.Item
                  style={{
                    padding: "3px 8px",
                    paddingLeft: 8 + depth * 14,
                    cursor: "pointer",
                    background: selectedNode === n ? "#e6f4ff" : undefined,
                  }}
                  onClick={() => select(n)}
                >
                  <Space size={4}>
                    <span
                      className={
                        label === "?"
                          ? "pe-step-badge unreachable"
                          : label.includes(".")
                            ? "pe-step-badge branch"
                            : "pe-step-badge"
                      }
                    >
                      {label}
                    </span>
                    <Typography.Text style={{ fontSize: 12 }} ellipsis={{ tooltip: n }}>
                      {n}
                    </Typography.Text>
                  </Space>
                </List.Item>
              );
            }}
          />
        </div>

        {/* 画布 */}
        <div style={{ flex: 1, minWidth: 0 }}>
          <ReactFlowProvider>
            <FlowCanvas steps={steps} />
          </ReactFlowProvider>
        </div>

        {/* 右侧面板 */}
        <div
          style={{
            width: 360,
            borderLeft: "1px solid #f0f0f0",
            background: "#fff",
            display: "flex",
            flexDirection: "column",
            minHeight: 0,
          }}
        >
          <Tabs
            className="pe-right-tabs"
            size="small"
            activeKey={rightTab}
            onChange={(k) => setRightTab(k as typeof rightTab)}
            style={{ flex: 1, minHeight: 0 }}
            tabBarStyle={{ margin: 0, paddingInline: 8 }}
            items={[
              {
                key: "node",
                label: "节点",
                children: <NodeInspector />,
                style: { height: "100%" },
              },
              {
                key: "task",
                label: "任务设置",
                children: <TaskSettingsPanel />,
                style: { height: "100%" },
              },
              {
                key: "problems",
                label: (
                  <Badge size="small" count={problemCount} offset={[6, 0]}>
                    问题
                  </Badge>
                ),
                children: (
                  <ProblemsPanel
                    validateError={validateError}
                    lintWarnings={lintWarnings}
                  />
                ),
                style: { height: "100%" },
              },
              {
                key: "run",
                label: "运行",
                children: <RunPanel taskName={name} />,
                style: { height: "100%" },
              },
            ]}
          />
        </div>
      </div>

      <Modal
        title="新建节点"
        open={addOpen}
        onOk={() => {
          const n = addName.trim();
          if (!n) return;
          if (doc.nodes[n]) {
            message.error(`节点 "${n}" 已存在`);
            return;
          }
          addNode(n);
          select(n);
          setAddOpen(false);
          setAddName("");
        }}
        onCancel={() => setAddOpen(false)}
      >
        <Input
          placeholder="节点名"
          value={addName}
          onChange={(e) => setAddName(e.target.value)}
          autoFocus
        />
      </Modal>
    </div>
  );
}
