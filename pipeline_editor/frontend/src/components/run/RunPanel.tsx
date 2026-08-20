/** 运行面板：设备选择、启动/停止、实时状态、事件时间线、findings。 */
import {
  CaretRightOutlined,
  ReloadOutlined,
  StopOutlined,
} from "@ant-design/icons";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  Alert,
  App as AntApp,
  Button,
  Descriptions,
  Input,
  List,
  Progress,
  Select,
  Space,
  Switch,
  Tag,
  Timeline,
  Typography,
} from "antd";
import { useEffect, useRef, useState } from "react";

import { metaApi, runsApi } from "../../api/endpoints";
import { RunSocket } from "../../api/ws";
import { useRunStore } from "../../store/runStore";
import { useUiStore } from "../../store/uiStore";

const STATUS_LABELS: Record<string, { color: string; text: string }> = {
  running: { color: "processing", text: "运行中" },
  done: { color: "success", text: "完成" },
  error: { color: "error", text: "失败" },
  agent_required: { color: "warning", text: "等待 agent 交接" },
  stopped: { color: "default", text: "已停止" },
};

interface Props {
  taskName: string;
  kind?: "task" | "suite";
}

export function RunPanel({ taskName, kind = "task" }: Props) {
  const { message, notification } = AntApp.useApp();
  const deviceId = useUiStore((s) => s.deviceId);
  const setDeviceId = useUiStore((s) => s.setDeviceId);
  const followRun = useUiStore((s) => s.followRun);
  const setFollowRun = useUiStore((s) => s.setFollowRun);
  const select = useUiStore((s) => s.select);

  const run = useRunStore();
  const socketRef = useRef<RunSocket | null>(null);
  const [startAfter, setStartAfter] = useState("");
  const notifiedSeq = useRef(0);

  const devicesQuery = useQuery({ queryKey: ["devices"], queryFn: metaApi.devices });

  useEffect(() => () => socketRef.current?.close(), []);

  // findings 通知：从 recent_events 里挑 finding 类事件太深；用 end result 提示
  useEffect(() => {
    if (run.status && run.status !== "running" && run.runId && notifiedSeq.current !== run.lastSeq) {
      notifiedSeq.current = run.lastSeq;
      const findings = (run.result?.findings as unknown[] | undefined)?.length ?? 0;
      const label = STATUS_LABELS[run.status]?.text ?? run.status;
      notification.open({
        type: run.status === "done" ? "success" : run.status === "error" ? "error" : "info",
        message: `运行${label}`,
        description: `${run.name} — ${run.steps} 步${findings ? `，${findings} 条 findings` : ""}`,
      });
    }
  }, [run.status, run.lastSeq, run.runId, run.name, run.steps, run.result, notification]);

  const startMutation = useMutation({
    mutationFn: () =>
      runsApi.start({
        kind,
        name: taskName,
        device_id: deviceId!,
        start_after: startAfter || undefined,
      }),
    onSuccess: (r) => {
      useRunStore.getState().beginRun(r.run_id, kind, taskName, deviceId!);
      socketRef.current?.close();
      socketRef.current = new RunSocket(r.run_id);
      socketRef.current.connect();
    },
    onError: (e: Error) => message.error(e.message),
  });

  const stopMutation = useMutation({
    mutationFn: () => runsApi.stop(run.runId!),
    onSuccess: (r) => {
      if (r.warning) message.warning(r.warning, 6);
      else if (r.note) message.info(r.note, 4);
    },
    onError: (e: Error) => message.error(e.message),
  });

  const running = run.status === "running";
  const statusInfo = run.status ? STATUS_LABELS[run.status] : null;

  return (
    <div style={{ padding: 12, overflow: "auto", height: "100%" }}>
      <Space direction="vertical" style={{ width: "100%" }} size={8}>
        <Space.Compact block>
          <Select
            size="small"
            style={{ flex: 1 }}
            placeholder="选择设备"
            value={deviceId}
            onChange={setDeviceId}
            options={(devicesQuery.data ?? []).map((d) => ({
              value: d.device_id,
              label: `${d.device_id} (${d.model})`,
            }))}
            loading={devicesQuery.isLoading}
            disabled={running}
          />
          <Button
            size="small"
            icon={<ReloadOutlined />}
            onClick={() => devicesQuery.refetch()}
          />
        </Space.Compact>
        {kind === "task" && (
          <Input
            size="small"
            placeholder="start_after（可选：从该节点之后续跑）"
            value={startAfter}
            onChange={(e) => setStartAfter(e.target.value)}
            disabled={running}
          />
        )}
        <Space>
          <Button
            size="small"
            type="primary"
            icon={<CaretRightOutlined />}
            disabled={!deviceId || running}
            loading={startMutation.isPending}
            onClick={() => startMutation.mutate()}
          >
            运行
          </Button>
          <Button
            size="small"
            danger
            icon={<StopOutlined />}
            disabled={!running}
            loading={stopMutation.isPending}
            onClick={() => stopMutation.mutate()}
          >
            停止
          </Button>
          <Switch
            size="small"
            checked={followRun}
            onChange={setFollowRun}
            checkedChildren="跟随"
            unCheckedChildren="跟随"
          />
          {running && !run.wsConnected && <Tag color="orange">重连中…</Tag>}
        </Space>

        {statusInfo && (
          <Descriptions size="small" column={1} bordered>
            <Descriptions.Item label="状态">
              <Tag color={statusInfo.color}>{statusInfo.text}</Tag>
            </Descriptions.Item>
            <Descriptions.Item label="当前节点">
              {run.currentNode ?? "—"}
            </Descriptions.Item>
            <Descriptions.Item label="步数">{run.steps}</Descriptions.Item>
            {run.kind === "suite" && (
              <Descriptions.Item label="用例进度">
                <Space direction="vertical" style={{ width: "100%" }} size={2}>
                  <span>
                    {run.case ?? "—"}（{run.casesDone}/{run.casesTotal}）
                  </span>
                  <Progress
                    size="small"
                    percent={
                      run.casesTotal
                        ? Math.round((run.casesDone / run.casesTotal) * 100)
                        : 0
                    }
                  />
                </Space>
              </Descriptions.Item>
            )}
            {run.error && (
              <Descriptions.Item label="错误">
                <Typography.Text type="danger" style={{ fontSize: 12 }}>
                  {run.error}
                </Typography.Text>
              </Descriptions.Item>
            )}
          </Descriptions>
        )}

        {run.status === "agent_required" && (
          <Alert
            type="warning"
            showIcon
            message="任务已挂起等待 agent 交接"
            description={String(
              (run.result?.handoff as Record<string, unknown> | undefined)?.instruction ??
                "在外部完成交接后，用 start_after 从交接节点续跑。",
            )}
          />
        )}

        <FindingsList findings={(run.result?.findings as FindingItem[] | undefined) ?? []} />

        <EventTimeline onLocate={(node) => select(node)} />
      </Space>
    </div>
  );
}

interface FindingItem {
  severity?: string;
  message?: string;
  node?: string;
  [key: string]: unknown;
}

function FindingsList({ findings }: { findings: FindingItem[] }) {
  if (findings.length === 0) return null;
  return (
    <div>
      <Typography.Text strong style={{ fontSize: 12 }}>
        Findings（{findings.length}）
      </Typography.Text>
      <List
        size="small"
        dataSource={findings}
        renderItem={(f) => (
          <List.Item style={{ padding: "4px 0" }}>
            <Space size={4} align="start">
              <Tag
                color={
                  { info: "blue", warning: "orange", error: "red", critical: "magenta" }[
                    f.severity ?? "error"
                  ]
                }
              >
                {f.severity ?? "error"}
              </Tag>
              <Typography.Text style={{ fontSize: 12 }}>
                {String(f.message ?? f.kind ?? "")}
                {f.node ? ` @${f.node}` : ""}
              </Typography.Text>
            </Space>
          </List.Item>
        )}
      />
    </div>
  );
}

function EventTimeline({ onLocate }: { onLocate: (node: string) => void }) {
  const events = useRunStore((s) => s.events);
  const items = events
    .slice(-200)
    .reverse()
    .map((e) => {
      if (e.type === "node") {
        return {
          key: e.seq,
          color: "green",
          children: (
            <span
              style={{ fontSize: 12, cursor: "pointer" }}
              onClick={() => onLocate(e.node)}
            >
              [{e.steps}] {e.node}
            </span>
          ),
        };
      }
      if (e.type === "suite_progress") {
        const ev = e as Record<string, unknown>;
        if (ev.event === "node") {
          return {
            key: e.seq,
            color: "green",
            children: (
              <span
                style={{ fontSize: 12, cursor: "pointer" }}
                onClick={() => onLocate(String(ev.node))}
              >
                {String(ev.node)}
              </span>
            ),
          };
        }
        return {
          key: e.seq,
          color: "blue",
          children: (
            <span style={{ fontSize: 12 }}>
              {String(ev.event)}: {String(ev.case ?? "")}
            </span>
          ),
        };
      }
      if (e.type === "end") {
        return {
          key: e.seq,
          color: e.status === "done" ? "green" : "red",
          children: <span style={{ fontSize: 12 }}>结束（{e.status}）</span>,
        };
      }
      return { key: e.seq, color: "gray", children: null };
    })
    .filter((i) => i.children !== null);

  if (items.length === 0) return null;
  return (
    <div>
      <Typography.Text strong style={{ fontSize: 12 }}>
        事件流
      </Typography.Text>
      <Timeline style={{ marginTop: 8 }} items={items} />
    </div>
  );
}
