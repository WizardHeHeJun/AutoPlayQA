/** 工具页：录制会话→草稿 / 任务健康度 / 交接固化 / 回放缓存。
 * 从 AutoPlayQA CLI（task health / handoffs / cache）与断链函数
 * action_log_to_draft 迁移而来的编辑辅助层。 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  App as AntApp,
  Button,
  Card,
  Descriptions,
  Drawer,
  Image,
  Input,
  InputNumber,
  List,
  Modal,
  Popconfirm,
  Progress,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
} from "antd";
import dayjs from "dayjs";
import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import { tasksApi, toolsApi, type HandoffNode, type SessionInfo } from "../api/endpoints";
import type { TaskDoc } from "../types/task";

export function ToolsPage() {
  return (
    <div style={{ padding: 16, height: "100%", overflow: "auto" }}>
      <Tabs
        items={[
          { key: "sessions", label: "录制会话 → 草稿", children: <SessionsTab /> },
          { key: "health", label: "任务健康度", children: <HealthTab /> },
          { key: "handoffs", label: "交接固化", children: <HandoffsTab /> },
          { key: "cache", label: "回放缓存", children: <CacheTab /> },
        ]}
      />
    </div>
  );
}

/* ---------- 会话 → 草稿 ---------- */

function SessionsTab() {
  const { message } = AntApp.useApp();
  const navigate = useNavigate();
  const sessionsQuery = useQuery({ queryKey: ["sessions"], queryFn: () => toolsApi.sessions() });
  const [preview, setPreview] = useState<SessionInfo | null>(null);
  const [draftFor, setDraftFor] = useState<SessionInfo | null>(null);

  return (
    <>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="录制来自 MCP record_actions_start/stop（agent 自己点的每一步，带锚点元素）。转草稿后锚点步骤自动变成识别驱动节点；盲点坐标会标红，保存前补锚点与 QA 断言。"
      />
      <Table
        rowKey="dir"
        size="small"
        loading={sessionsQuery.isLoading}
        dataSource={sessionsQuery.data ?? []}
        locale={{ emptyText: "暂无录制会话（用 MCP record_actions_start 录一段）" }}
        columns={[
          {
            title: "时间",
            dataIndex: "started_at",
            width: 150,
            render: (t: string | null) => (t ? dayjs(t).format("MM-DD HH:mm:ss") : "—"),
          },
          {
            title: "类型",
            width: 90,
            render: (_: unknown, s: SessionInfo) => (
              <Tag color={s.context.kind === "handoff" ? "purple" : "blue"}>
                {s.context.kind ?? "?"}
              </Tag>
            ),
          },
          {
            title: "任务 / 节点",
            render: (_: unknown, s: SessionInfo) =>
              s.context.task ? `${s.context.task} @ ${s.context.node ?? "?"}` : "—",
          },
          { title: "设备", dataIndex: "device_id", width: 150 },
          {
            title: "步数（锚点/总）",
            width: 130,
            render: (_: unknown, s: SessionInfo) => (
              <span>
                <Typography.Text type={s.anchored_steps > 0 ? "success" : "warning"}>
                  {s.anchored_steps}
                </Typography.Text>
                /{s.step_count}
              </span>
            ),
          },
          {
            title: "操作",
            width: 180,
            render: (_: unknown, s: SessionInfo) => (
              <Space>
                <Button size="small" onClick={() => setPreview(s)}>
                  预览
                </Button>
                <Button size="small" type="primary" onClick={() => setDraftFor(s)}>
                  生成草稿
                </Button>
              </Space>
            ),
          },
        ]}
        pagination={{ pageSize: 15, size: "small" }}
      />
      {preview && <SessionPreview session={preview} onClose={() => setPreview(null)} />}
      {draftFor && (
        <DraftModal
          sessionDir={draftFor.dir}
          onDone={(name) => {
            setDraftFor(null);
            navigate(`/tasks/${encodeURIComponent(name)}`);
          }}
          onCancel={() => setDraftFor(null)}
        />
      )}
    </>
  );
}

function SessionPreview({ session, onClose }: { session: SessionInfo; onClose: () => void }) {
  const detailQuery = useQuery({
    queryKey: ["session", session.dir],
    queryFn: () => toolsApi.session(session.dir),
  });
  const steps = (detailQuery.data?.steps as
    | {
        index: number;
        tool: string;
        action: { type: string; params?: Record<string, unknown> };
        element: { source: string; text: string } | null;
        screenshot: string | null;
      }[]
    | undefined) ?? [];
  return (
    <Drawer open title={`会话 ${session.dir}`} width={520} onClose={onClose}>
      <List
        loading={detailQuery.isLoading}
        dataSource={steps}
        renderItem={(s) => (
          <List.Item style={{ alignItems: "flex-start" }}>
            <Space direction="vertical" size={4} style={{ width: "100%" }}>
              <Space size={6}>
                <Tag>{s.index}</Tag>
                <Typography.Text code style={{ fontSize: 12 }}>
                  {s.action.type}
                  {s.action.params ? ` ${JSON.stringify(s.action.params)}` : ""}
                </Typography.Text>
              </Space>
              {s.element?.text ? (
                <Tag color="green">
                  锚点[{s.element.source}]: {s.element.text}
                </Tag>
              ) : (
                s.action.type === "click" && <Tag color="red">盲点坐标（草稿会标记待补锚点）</Tag>
              )}
              {s.screenshot && (
                <Image
                  src={toolsApi.frameUrl(session.dir, s.screenshot)}
                  width={140}
                  style={{ borderRadius: 4 }}
                />
              )}
            </Space>
          </List.Item>
        )}
      />
    </Drawer>
  );
}

export function DraftModal({
  sessionDir,
  onDone,
  onCancel,
}: {
  sessionDir: string;
  onDone: (taskName: string) => void;
  onCancel: () => void;
}) {
  const { message } = AntApp.useApp();
  const [taskName, setTaskName] = useState("");
  const [prefix, setPrefix] = useState("step");

  const mutation = useMutation({
    mutationFn: async () => {
      const r = await toolsApi.toDraft(sessionDir, prefix);
      if (!r.ok || !r.draft) throw new Error(r.error ?? "草稿转换失败");
      const save = await tasksApi.save(taskName, r.draft as TaskDoc);
      if (!save.ok) throw new Error(save.error?.message ?? "保存失败");
      return { blind: r.blind_clicks ?? 0, nodes: r.node_count ?? 0 };
    },
    onSuccess: ({ blind, nodes }) => {
      message.success(
        `草稿已保存（${nodes} 节点${blind ? `，${blind} 个盲点坐标待补锚点` : ""}），已在编辑器打开`,
      );
      onDone(taskName);
    },
    onError: (e: Error) => message.error(e.message),
  });

  return (
    <Modal
      open
      title={`生成草稿任务（来自 ${sessionDir}）`}
      onCancel={onCancel}
      onOk={() => mutation.mutate()}
      okButtonProps={{ disabled: !taskName, loading: mutation.isPending }}
      okText="转换并保存"
    >
      <Space direction="vertical" style={{ width: "100%" }}>
        <Input
          placeholder="新任务名（如 shop_draft）"
          value={taskName}
          onChange={(e) => setTaskName(e.target.value)}
          autoFocus
        />
        <Input
          addonBefore="节点名前缀"
          value={prefix}
          onChange={(e) => setPrefix(e.target.value || "step")}
        />
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          转换是确定性的（无 LLM）：锚点步骤 → ui_text/ocr 识别 + target:recognized；
          盲点步骤保留坐标并打 TODO 标记。保存后请在编辑器里补 QA 断言
          （watchdogs / finding / on_timeout）。
        </Typography.Text>
      </Space>
    </Modal>
  );
}

/* ---------- 健康度 ---------- */

function HealthTab() {
  const [task, setTask] = useState<string | undefined>();
  const [days, setDays] = useState<number | null>(14);
  const tasksQuery = useQuery({ queryKey: ["tasks"], queryFn: tasksApi.list });
  const healthQuery = useQuery({
    queryKey: ["health", task ?? null, days],
    queryFn: () => toolsApi.health({ task, days: days ?? undefined }),
  });

  const rows = useMemo(() => {
    const out: {
      key: string;
      task: string;
      node: string;
      runs_seen: number;
      direct_hits: number;
      timeout_recoveries: number;
      popup_assisted_hits: number;
      drift_count: number;
      fallback_rate: number;
    }[] = [];
    for (const [taskName, data] of Object.entries(healthQuery.data ?? {})) {
      for (const [node, stats] of Object.entries(data.nodes)) {
        out.push({
          key: `${taskName}/${node}`,
          task: taskName,
          node,
          runs_seen: stats.runs_seen,
          direct_hits: stats.direct_hits,
          timeout_recoveries: stats.timeout_recoveries,
          popup_assisted_hits: stats.popup_assisted_hits,
          drift_count: stats.drift_count,
          fallback_rate: stats.fallback_rate ?? 0,
        });
      }
    }
    return out.sort((a, b) => b.fallback_rate - a.fallback_rate);
  }, [healthQuery.data]);

  return (
    <Space direction="vertical" style={{ width: "100%" }}>
      <Space>
        <Select
          allowClear
          showSearch
          placeholder="全部任务"
          style={{ width: 220 }}
          value={task}
          onChange={setTask}
          options={(tasksQuery.data ?? []).map((t) => ({ value: t.name }))}
        />
        <InputNumber
          addonBefore="最近"
          addonAfter="天"
          min={1}
          value={days}
          onChange={setDays}
          style={{ width: 160 }}
        />
      </Space>
      <Alert
        type="info"
        showIcon
        message="fallback_rate =（超时恢复 + 弹窗协助）/ 总命中——高 = 锚点腐化嫌疑（anchor rot），去编辑器换更稳的锚点或补 on_timeout"
      />
      <Table
        size="small"
        loading={healthQuery.isLoading}
        dataSource={rows}
        locale={{ emptyText: "所选范围内没有带 node_stats 的运行报告" }}
        pagination={{ pageSize: 20, size: "small" }}
        columns={[
          { title: "任务", dataIndex: "task", width: 160 },
          { title: "节点", dataIndex: "node", ellipsis: true },
          { title: "见于运行", dataIndex: "runs_seen", width: 90 },
          { title: "直接命中", dataIndex: "direct_hits", width: 90 },
          { title: "超时恢复", dataIndex: "timeout_recoveries", width: 90 },
          { title: "弹窗协助", dataIndex: "popup_assisted_hits", width: 90 },
          { title: "漂移", dataIndex: "drift_count", width: 70 },
          {
            title: "fallback 率",
            dataIndex: "fallback_rate",
            width: 140,
            render: (v: number) => (
              <Progress
                percent={Math.round(v * 100)}
                size="small"
                status={v > 0.3 ? "exception" : v > 0 ? "active" : "success"}
              />
            ),
          },
        ]}
      />
    </Space>
  );
}

/* ---------- 交接固化 ---------- */

function HandoffsTab() {
  const { message } = AntApp.useApp();
  const navigate = useNavigate();
  const [days, setDays] = useState<number | null>(30);
  const handoffsQuery = useQuery({
    queryKey: ["handoffs", days],
    queryFn: () => toolsApi.handoffs({ days: days ?? undefined }),
  });
  const [draftDir, setDraftDir] = useState<string | null>(null);

  const rows = useMemo(() => {
    const out: ({ key: string; task: string; node: string } & HandoffNode)[] = [];
    for (const [task, nodes] of Object.entries(handoffsQuery.data ?? {})) {
      for (const [node, stats] of Object.entries(nodes)) {
        out.push({ key: `${task}/${node}`, task, node, ...stats });
      }
    }
    return out.sort(
      (a, b) => Number(b.solidify_candidate) - Number(a.solidify_candidate)
        || b.sessions - a.sessions,
    );
  }, [handoffsQuery.data]);

  const pickSession = async (task: string, node: string) => {
    const sessions = await toolsApi.sessions({ kind: "handoff", task, node });
    if (sessions.length === 0) {
      message.warning("找不到该节点的交接会话录制");
      return;
    }
    setDraftDir(sessions[0].dir);
  };

  return (
    <Space direction="vertical" style={{ width: "100%" }}>
      <Alert
        type="info"
        showIcon
        message="agent 交接节点的会话统计：同一节点 ≥3 次交接且 80% 走同一套操作 = 可固化（把 agent 节点改写成确定性节点）。点「生成草稿」取该节点最近一次录制转成识别驱动节点链。"
      />
      <InputNumber addonBefore="最近" addonAfter="天" min={1} value={days}
        onChange={setDays} style={{ width: 160 }} />
      <Table
        size="small"
        loading={handoffsQuery.isLoading}
        dataSource={rows}
        locale={{ emptyText: "暂无交接会话（agent_required 交接时用 record_actions_start kind=handoff 录制）" }}
        pagination={false}
        columns={[
          { title: "任务", dataIndex: "task", width: 160 },
          { title: "节点", dataIndex: "node", ellipsis: true },
          { title: "会话数", dataIndex: "sessions", width: 80 },
          { title: "签名数", dataIndex: "signatures", width: 80 },
          {
            title: "主导操作占比",
            dataIndex: "dominant_ratio",
            width: 130,
            render: (v: number) => `${Math.round(v * 100)}%`,
          },
          {
            title: "主导签名",
            dataIndex: "dominant_signature",
            ellipsis: true,
            render: (sig: [string, string | null][]) =>
              sig.map(([t, text]) => (text ? `${t}('${text}')` : t)).join(" → "),
          },
          {
            title: "可固化",
            dataIndex: "solidify_candidate",
            width: 90,
            render: (v: boolean) => (v ? <Tag color="green">候选</Tag> : "—"),
          },
          {
            title: "操作",
            width: 110,
            render: (_: unknown, r: { task: string; node: string }) => (
              <Button size="small" onClick={() => pickSession(r.task, r.node)}>
                生成草稿
              </Button>
            ),
          },
        ]}
      />
      {draftDir && (
        <DraftModal
          sessionDir={draftDir}
          onDone={(name) => {
            setDraftDir(null);
            navigate(`/tasks/${encodeURIComponent(name)}`);
          }}
          onCancel={() => setDraftDir(null)}
        />
      )}
    </Space>
  );
}

/* ---------- 回放缓存 ---------- */

function CacheTab() {
  const { message } = AntApp.useApp();
  const queryClient = useQueryClient();
  const cacheQuery = useQuery({
    queryKey: ["replay-cache"],
    queryFn: toolsApi.replayCache,
  });
  const clearMutation = useMutation({
    mutationFn: toolsApi.clearReplayCache,
    onSuccess: (r) => {
      message.success(r.note ?? `已清除 ${r.cleared} 条锚点缓存`);
      queryClient.invalidateQueries({ queryKey: ["replay-cache"] });
    },
    onError: (e: Error) => message.error(e.message),
  });

  return (
    <Card style={{ maxWidth: 560 }} loading={cacheQuery.isLoading}>
      <Descriptions column={1} size="small" title="回放缓存（OCR ROI 快路径）">
        <Descriptions.Item label="状态">
          {cacheQuery.data?.enabled ? <Tag color="green">启用</Tag> : <Tag>禁用</Tag>}
        </Descriptions.Item>
        <Descriptions.Item label="缓存条数">{cacheQuery.data?.size ?? 0}</Descriptions.Item>
        <Descriptions.Item label="文件">{cacheQuery.data?.path ?? "—"}</Descriptions.Item>
      </Descriptions>
      <Popconfirm
        title="清除全部锚点缓存？"
        description="下次回放将全屏重识别；缓存重建前不会上报 anchor_drift。UI 有意改版后建议清除，避免一波误报。"
        onConfirm={() => clearMutation.mutate()}
      >
        <Button danger loading={clearMutation.isPending}
          disabled={!cacheQuery.data?.enabled}>
          清除缓存
        </Button>
      </Popconfirm>
    </Card>
  );
}
