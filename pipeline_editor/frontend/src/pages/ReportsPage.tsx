/** 报告页：历史 run 报告列表 + report.html 内嵌 + findings 侧栏。 */
import { useQuery } from "@tanstack/react-query";
import { Empty, Layout, List, Space, Spin, Table, Tag, Typography } from "antd";
import dayjs from "dayjs";
import { useState } from "react";

import { reportsApi } from "../api/endpoints";
import type { ReportListItem } from "../types/api";

const SEV_COLORS: Record<string, string> = {
  info: "blue",
  warning: "orange",
  error: "red",
  critical: "magenta",
};

export function ReportsPage() {
  const [selected, setSelected] = useState<ReportListItem | null>(null);
  const listQuery = useQuery({
    queryKey: ["reports"],
    queryFn: () => reportsApi.list({ limit: 200 }),
  });

  const detailQuery = useQuery({
    queryKey: ["report", selected?.date, selected?.device, selected?.run_id],
    queryFn: () => reportsApi.get(selected!.date, selected!.device, selected!.run_id),
    enabled: !!selected,
  });

  const findings =
    (detailQuery.data?.findings as
      | { severity?: string; message?: string; node?: string; kind?: string }[]
      | undefined) ?? [];

  return (
    <Layout style={{ height: "100%" }}>
      <Layout.Sider width={480} theme="light" style={{ overflow: "auto", padding: 12 }}>
        <Typography.Title level={5}>历史报告</Typography.Title>
        <Table
          rowKey={(r) => `${r.date}/${r.device}/${r.run_id}`}
          size="small"
          loading={listQuery.isLoading}
          dataSource={listQuery.data ?? []}
          pagination={{ pageSize: 20, size: "small" }}
          onRow={(record) => ({
            onClick: () => setSelected(record),
            style: {
              cursor: "pointer",
              background:
                selected?.run_id === record.run_id ? "#e6f4ff" : undefined,
            },
          })}
          columns={[
            {
              title: "时间",
              dataIndex: "mtime",
              width: 120,
              render: (m: number) => dayjs(m * 1000).format("MM-DD HH:mm"),
            },
            { title: "任务", dataIndex: "task", ellipsis: true },
            {
              title: "状态",
              dataIndex: "status",
              width: 90,
              render: (s: string | null) =>
                s ? (
                  <Tag color={s === "completed" ? "green" : "red"}>{s}</Tag>
                ) : (
                  "—"
                ),
            },
            {
              title: "findings",
              dataIndex: "finding_count",
              width: 80,
              render: (n: number | undefined, r: ReportListItem) => (
                <Space size={2}>
                  {Object.entries(r.severity_counts ?? {}).map(([sev, count]) => (
                    <Tag key={sev} color={SEV_COLORS[sev]} style={{ marginInlineEnd: 0 }}>
                      {count}
                    </Tag>
                  ))}
                  {!n && "0"}
                </Space>
              ),
            },
          ]}
        />
      </Layout.Sider>
      <Layout.Content style={{ display: "flex", minWidth: 0 }}>
        {!selected ? (
          <Empty description="选择一份报告查看" style={{ margin: "auto" }} />
        ) : (
          <>
            <div style={{ flex: 1, minWidth: 0 }}>
              {selected.has_html ? (
                <iframe
                  title="report"
                  src={reportsApi.htmlUrl(selected.date, selected.device, selected.run_id)}
                  style={{ width: "100%", height: "100%", border: "none" }}
                />
              ) : (
                <pre
                  style={{
                    margin: 0,
                    padding: 16,
                    overflow: "auto",
                    height: "100%",
                    fontSize: 12,
                  }}
                >
                  {detailQuery.data
                    ? JSON.stringify(detailQuery.data, null, 2)
                    : "加载中…"}
                </pre>
              )}
            </div>
            <div
              style={{
                width: 300,
                borderLeft: "1px solid #f0f0f0",
                overflow: "auto",
                padding: 12,
                background: "#fff",
              }}
            >
              <Typography.Text strong>
                Findings（{findings.length}）
              </Typography.Text>
              {detailQuery.isLoading && <Spin size="small" style={{ marginLeft: 8 }} />}
              <List
                size="small"
                dataSource={findings}
                renderItem={(f) => (
                  <List.Item style={{ alignItems: "flex-start", padding: "6px 0" }}>
                    <Space direction="vertical" size={2}>
                      <Space size={4}>
                        <Tag color={SEV_COLORS[f.severity ?? "error"]}>
                          {f.severity ?? "error"}
                        </Tag>
                        {f.node && <Tag>{f.node}</Tag>}
                      </Space>
                      <Typography.Text style={{ fontSize: 12 }}>
                        {f.message ?? f.kind ?? ""}
                      </Typography.Text>
                    </Space>
                  </List.Item>
                )}
              />
            </div>
          </>
        )}
      </Layout.Content>
    </Layout>
  );
}
