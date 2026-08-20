import { PlusOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  App as AntApp,
  Button,
  Flex,
  Input,
  Modal,
  Popconfirm,
  Table,
  Tag,
  Typography,
} from "antd";
import dayjs from "dayjs";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { tasksApi } from "../api/endpoints";
import type { TaskListItem } from "../types/api";

export function TaskListPage() {
  const { message } = AntApp.useApp();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { data, isLoading } = useQuery({ queryKey: ["tasks"], queryFn: tasksApi.list });
  const [newOpen, setNewOpen] = useState(false);
  const [newName, setNewName] = useState("");

  const createMutation = useMutation({
    mutationFn: async (name: string) => {
      return tasksApi.save(name, {
        entry: "开始",
        nodes: {
          开始: {
            recognition: { type: "always" },
            action: { type: "none" },
            next: [],
          },
        },
      });
    },
    onSuccess: (result, name) => {
      if (result.ok) {
        queryClient.invalidateQueries({ queryKey: ["tasks"] });
        setNewOpen(false);
        navigate(`/tasks/${encodeURIComponent(name)}`);
      } else {
        message.error(result.error?.message ?? "创建失败");
      }
    },
    onError: (e: Error) => message.error(e.message),
  });

  const deleteMutation = useMutation({
    mutationFn: ({ name, force }: { name: string; force: boolean }) =>
      tasksApi.delete(name, force),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["tasks"] }),
    onError: (e: Error) => message.error(e.message),
  });

  const columns = [
    {
      title: "任务名",
      dataIndex: "name",
      render: (name: string) => (
        <Link to={`/tasks/${encodeURIComponent(name)}`}>{name}</Link>
      ),
      sorter: (a: TaskListItem, b: TaskListItem) => a.name.localeCompare(b.name),
    },
    { title: "入口节点", dataIndex: "entry" },
    { title: "节点数", dataIndex: "node_count", width: 90 },
    {
      title: "includes",
      dataIndex: "includes",
      render: (inc: string[] | null) =>
        inc?.map((i) => <Tag key={i}>{i}</Tag>) ?? "—",
    },
    {
      title: "修改时间",
      dataIndex: "mtime",
      width: 170,
      render: (m: number | null) =>
        m ? dayjs(m * 1000).format("YYYY-MM-DD HH:mm:ss") : "—",
      sorter: (a: TaskListItem, b: TaskListItem) => (a.mtime ?? 0) - (b.mtime ?? 0),
      defaultSortOrder: "descend" as const,
    },
    {
      title: "操作",
      width: 100,
      render: (_: unknown, record: TaskListItem) => (
        <Popconfirm
          title={`删除任务 ${record.name}？`}
          onConfirm={() => deleteMutation.mutate({ name: record.name, force: false })}
        >
          <Button type="link" danger size="small">
            删除
          </Button>
        </Popconfirm>
      ),
    },
  ];

  return (
    <div style={{ padding: 24, height: "100%", overflow: "auto" }}>
      <Flex justify="space-between" align="center" style={{ marginBottom: 16 }}>
        <Typography.Title level={4} style={{ margin: 0 }}>
          任务列表
        </Typography.Title>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setNewOpen(true)}>
          新建任务
        </Button>
      </Flex>
      <Table
        rowKey="name"
        size="small"
        loading={isLoading}
        dataSource={data ?? []}
        columns={columns}
        pagination={false}
      />
      <Modal
        title="新建任务"
        open={newOpen}
        onOk={() => newName && createMutation.mutate(newName)}
        confirmLoading={createMutation.isPending}
        onCancel={() => setNewOpen(false)}
        okButtonProps={{ disabled: !newName }}
      >
        <Input
          placeholder="任务名（文件名，如 shop_smoke）"
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          onPressEnter={() => newName && createMutation.mutate(newName)}
        />
      </Modal>
    </div>
  );
}
