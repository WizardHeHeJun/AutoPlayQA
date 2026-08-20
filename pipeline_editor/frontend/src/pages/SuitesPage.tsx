/** 套件页：列表 + 表单编辑（cases 排序、landing 识别、full_boot_cases 约束）+ 运行。 */
import {
  ArrowDownOutlined,
  ArrowUpOutlined,
  DeleteOutlined,
  PlusOutlined,
  SaveOutlined,
} from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  App as AntApp,
  Button,
  Empty,
  Input,
  InputNumber,
  List,
  Modal,
  Popconfirm,
  Select,
  Space,
  Spin,
  Switch,
  Typography,
} from "antd";
import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import { suitesApi, tasksApi } from "../api/endpoints";
import { EventsSocket } from "../api/events";
import { Field, RecognitionForm } from "../components/inspector/RecognitionForm";
import { RunPanel } from "../components/run/RunPanel";
import type { FileVersion } from "../types/api";
import type { SuiteDoc } from "../types/suite";
import { advanceVersion, isVersionConflict } from "../utils/fileVersion";
import type { OwnWriteRecord } from "../utils/ownWrite";
import { canonicalJson, isOwnWrite, withinOwnWriteWindow } from "../utils/ownWrite";

const NEW_SUITE: SuiteDoc = {
  name: "",
  cases: [],
  resume_after: "",
  case_entry: "",
  landing: null,
  on_case_failure: "restart_retry",
  max_retries: 1,
};

export function SuitesPage() {
  const { message } = AntApp.useApp();
  const { name } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const suitesQuery = useQuery({ queryKey: ["suites"], queryFn: suitesApi.list });
  const tasksQuery = useQuery({ queryKey: ["tasks"], queryFn: tasksApi.list });

  const [doc, setDoc] = useState<SuiteDoc | null>(null);
  // 新建（尚未保存）的套件草稿：阻止下方 effect 用路由状态清掉它
  const [isDraft, setIsDraft] = useState(false);
  const [newOpen, setNewOpen] = useState(false);
  const [newName, setNewName] = useState("");
  /** 外部（AI/MCP/手改文件）修改与本地未保存改动冲突时的横幅 */
  const [externalChange, setExternalChange] = useState(false);
  /** 最近一次载入/保存的内容指纹，用来判断表单是否被本地改过 */
  const baselineRef = useRef<string | null>(null);
  /** 自己最近一次写盘的记录（时刻 + 指纹），区分「自己保存的」与「AI 改的」 */
  const lastOwnWriteRef = useRef<OwnWriteRecord | null>(null);
  /** 乐观并发基线：最近一次载入/保存看到的文件版本（详见 utils/fileVersion.ts） */
  const baseVersionRef = useRef<FileVersion>(null);
  // 事件回调里要读最新的表单状态，但订阅不该随每次输入重建 → 走 ref
  const docRef = useRef<SuiteDoc | null>(null);
  const isDraftRef = useRef(false);
  useEffect(() => {
    docRef.current = doc;
    isDraftRef.current = isDraft;
  }, [doc, isDraft]);

  const detailQuery = useQuery({
    queryKey: ["suite", name],
    queryFn: () => suitesApi.get(name!),
    enabled: !!name,
  });

  useEffect(() => {
    if (isDraft) return;
    if (detailQuery.data) {
      setDoc(structuredClone(detailQuery.data.raw));
      baselineRef.current = canonicalJson(detailQuery.data.raw);
      baseVersionRef.current = advanceVersion(detailQuery.data.mtime_ns);
      setExternalChange(false);
    } else if (!name) {
      setDoc(null);
      baselineRef.current = null;
      baseVersionRef.current = null;
      setExternalChange(false);
    }
  }, [detailQuery.data, name, isDraft]);

  const saveMutation = useMutation({
    // 存到另一个名字（新建/改名）就是另一个文件，基线不适用 → 不带版本
    mutationFn: (suite: SuiteDoc) =>
      suitesApi.save(suite.name, suite,
                     suite.name === name ? baseVersionRef.current : null),
    onSuccess: (r, suite) => {
      if (r.ok) {
        message.success("套件已保存");
        setIsDraft(false);
        setExternalChange(false);
        baselineRef.current = canonicalJson(suite);
        baseVersionRef.current = advanceVersion(r.mtime_ns);
        // 记下写盘指纹：watcher 事件回来时用它区分自写与外部写入
        lastOwnWriteRef.current = { at: Date.now(), fingerprint: canonicalJson(suite) };
        queryClient.invalidateQueries({ queryKey: ["suites"] });
        queryClient.invalidateQueries({ queryKey: ["suite", suite.name] });
        if (name !== suite.name) navigate(`/suites/${encodeURIComponent(suite.name)}`);
      } else {
        message.error(r.error ?? "保存失败");
      }
    },
    onError: (e: Error) => {
      if (isVersionConflict(e)) {
        // 磁盘上有更新的修改：表单原样保留，交给冲突横幅决策
        setExternalChange(true);
        message.error("保存已被拒绝：磁盘上有更新的修改（AI/MCP 或手改）", 4);
      } else {
        message.error(e.message);
      }
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (n: string) => suitesApi.delete(n),
    onSuccess: () => {
      // fingerprint=null：删除自己的文件，窗口内的 deleted 事件一律认自写
      lastOwnWriteRef.current = { at: Date.now(), fingerprint: null };
      queryClient.invalidateQueries({ queryKey: ["suites"] });
      navigate("/suites");
    },
    onError: (e: Error) => message.error(e.message),
  });

  /** 本地表单相对最近一次载入/保存有改动（草稿一律算脏） */
  const isSuiteDirty = useCallback((): boolean => {
    if (isDraftRef.current) return true;
    const current = docRef.current;
    if (!current || baselineRef.current === null) return false;
    return canonicalJson(current) !== baselineRef.current;
  }, []);

  /** 重新拉取当前套件并覆盖表单（丢弃本地改动） */
  const reloadSuite = useCallback(async (): Promise<void> => {
    if (!name) return;
    setExternalChange(false);
    try {
      const detail = await queryClient.fetchQuery({
        queryKey: ["suite", name],
        queryFn: () => suitesApi.get(name),
      });
      setDoc(structuredClone(detail.raw));
      baselineRef.current = canonicalJson(detail.raw);
      baseVersionRef.current = advanceVersion(detail.mtime_ns);
    } catch (e) {
      message.error((e as Error).message);
    }
    queryClient.invalidateQueries({ queryKey: ["suites"] });
  }, [name, queryClient, message]);

  /**
   * 冲突横幅的「保持本地」：基线推到磁盘当前版本（用户已知情，下次保存是有意
   * 覆盖），否则保存会一直 409。绕开 react-query 缓存，免得把表单重载掉。
   */
  const keepLocalSuite = useCallback(async (): Promise<void> => {
    if (name) {
      try {
        baseVersionRef.current = advanceVersion((await suitesApi.get(name)).mtime_ns);
      } catch {
        baseVersionRef.current = null; // 拉不到版本：放弃乐观锁，退回直接覆盖
      }
    }
    setExternalChange(false);
  }, [name]);

  // 全局事件订阅：AI（内嵌 MCP）或外部进程改了套件文件 → 刷新列表 / 重载 / 冲突横幅
  useEffect(() => {
    let cancelled = false;

    const refreshList = (): void => {
      queryClient.invalidateQueries({ queryKey: ["suites"] });
    };

    const applyExternal = async (deleted: boolean, toast: string): Promise<void> => {
      if (cancelled) return;
      refreshList();
      if (deleted) {
        message.warning("当前套件文件已被外部删除（本地内容保留，保存可重建）", 4);
        return;
      }
      if (isSuiteDirty()) {
        setExternalChange(true);
      } else {
        await reloadSuite();
        if (!cancelled) message.info(toast, 3);
      }
    };

    const handleSuiteChanged = async (evName: string | undefined,
                                      deleted: boolean): Promise<void> => {
      // 草稿中或改的不是当前套件：只刷列表，绝不碰表单（草稿被清的老 bug）
      if (isDraftRef.current || !name || evName !== name) {
        refreshList();
        return;
      }
      const record = lastOwnWriteRef.current;
      const now = Date.now();
      if (withinOwnWriteWindow(record, now)) {
        let diskFingerprint: string | null = null;
        if (!deleted) {
          try {
            diskFingerprint = canonicalJson((await suitesApi.get(name)).raw);
          } catch {
            /* 拉不到就当外部写入处理，绝不静默吞掉 */
          }
        }
        if (cancelled) return;
        if (isOwnWrite(record, diskFingerprint, now)) {
          refreshList(); // 自己写的：只刷列表（用例数等），不动表单
          return;
        }
        lastOwnWriteRef.current = null;
      }
      await applyExternal(deleted, "套件已被外部修改（AI/MCP），已自动刷新");
    };

    const sock = new EventsSocket(
      (ev) => {
        if (ev.type !== "suite_changed") return;
        void handleSuiteChanged(ev.name, ev.change === "deleted");
      },
      {
        // 事件补不齐（后端缓冲淘汰/重启）：当作可能有外部改动，做一次全量刷新
        onResync: () => {
          if (isDraftRef.current || !name) {
            refreshList();
            return;
          }
          void applyExternal(false, "与后端事件流失联过久，已重新拉取套件");
        },
      },
    );
    sock.connect();
    return () => {
      cancelled = true;
      sock.close();
    };
  }, [name, queryClient, message, isSuiteDirty, reloadSuite]);

  const patch = (p: Partial<SuiteDoc>) => setDoc((d) => (d ? { ...d, ...p } : d));
  const taskNames = (tasksQuery.data ?? []).map((t) => t.name);

  return (
    <div style={{ display: "flex", height: "100%" }}>
      {/* 左：套件列表 */}
      <div style={{ width: 240, borderRight: "1px solid #f0f0f0", background: "#fff" }}>
        <List
          size="small"
          header={
            <Space style={{ width: "100%", justifyContent: "space-between" }}>
              <Typography.Text strong>套件</Typography.Text>
              <Button
                size="small"
                icon={<PlusOutlined />}
                onClick={() => setNewOpen(true)}
              />
            </Space>
          }
          loading={suitesQuery.isLoading}
          dataSource={suitesQuery.data ?? []}
          renderItem={(s) => (
            <List.Item
              style={{
                cursor: "pointer",
                paddingInline: 12,
                background: name === s.name ? "#e6f4ff" : undefined,
              }}
              onClick={() => {
                setIsDraft(false);
                navigate(`/suites/${encodeURIComponent(s.name)}`);
              }}
            >
              <Typography.Text>{s.name}</Typography.Text>
              <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                {s.cases?.length ?? 0} 用例
              </Typography.Text>
            </List.Item>
          )}
        />
      </div>

      {/* 中：编辑表单 */}
      <div style={{ flex: 1, overflow: "auto", padding: 16 }}>
        {externalChange && (
          <Alert
            type="warning"
            showIcon
            banner
            style={{ marginBottom: 12 }}
            message="套件文件已被外部修改（AI/MCP 或手改），与你的未保存修改冲突"
            action={
              <Space>
                <Button size="small" danger onClick={() => void reloadSuite()}>
                  重载（丢弃本地修改）
                </Button>
                <Button size="small" onClick={() => void keepLocalSuite()}>
                  保持本地（下次保存将覆盖外部修改）
                </Button>
              </Space>
            }
          />
        )}
        {!doc ? (
          detailQuery.isLoading ? (
            <Spin />
          ) : (
            <Empty description="选择或新建一个套件" style={{ marginTop: 64 }} />
          )
        ) : (
          <Space direction="vertical" style={{ width: "100%", maxWidth: 640 }} size={12}>
            <Space style={{ width: "100%", justifyContent: "space-between" }}>
              <Typography.Title level={5} style={{ margin: 0 }}>
                {doc.name || "（未命名）"}
              </Typography.Title>
              <Space>
                <Button
                  type="primary"
                  size="small"
                  icon={<SaveOutlined />}
                  loading={saveMutation.isPending}
                  disabled={!doc.name}
                  onClick={() => saveMutation.mutate(doc)}
                >
                  保存
                </Button>
                {name && (
                  <Popconfirm
                    title={`删除套件 ${name}？`}
                    onConfirm={() => deleteMutation.mutate(name)}
                  >
                    <Button size="small" danger icon={<DeleteOutlined />} />
                  </Popconfirm>
                )}
              </Space>
            </Space>
            {detailQuery.data?.error && (
              <Alert type="warning" showIcon message={detailQuery.data.error} />
            )}

            <Field label="cases（用例顺序执行；从任务列表添加）">
              <Space direction="vertical" style={{ width: "100%" }} size={4}>
                {doc.cases.map((c, i) => (
                  <Space.Compact key={`${i}:${c}`} block>
                    <Button size="small" style={{ pointerEvents: "none", width: 32 }}>
                      {i + 1}
                    </Button>
                    <Input size="small" readOnly value={c} style={{ flex: 1 }} />
                    <Button size="small" icon={<ArrowUpOutlined />} disabled={i === 0}
                      onClick={() => {
                        const cases = [...doc.cases];
                        [cases[i - 1], cases[i]] = [cases[i], cases[i - 1]];
                        patch({ cases });
                      }} />
                    <Button size="small" icon={<ArrowDownOutlined />}
                      disabled={i === doc.cases.length - 1}
                      onClick={() => {
                        const cases = [...doc.cases];
                        [cases[i + 1], cases[i]] = [cases[i], cases[i + 1]];
                        patch({ cases });
                      }} />
                    <Button size="small" danger icon={<DeleteOutlined />}
                      onClick={() =>
                        patch({
                          cases: doc.cases.filter((_, j) => j !== i),
                          full_boot_cases: doc.full_boot_cases?.filter((f) => f !== c),
                        })
                      } />
                  </Space.Compact>
                ))}
                <Select
                  size="small"
                  showSearch
                  placeholder="添加用例…"
                  value={null}
                  style={{ width: "100%" }}
                  options={taskNames
                    .filter((t) => !doc.cases.includes(t))
                    .map((t) => ({ value: t }))}
                  onChange={(v) => v && patch({ cases: [...doc.cases, v] })}
                />
              </Space>
            </Field>

            <Space wrap>
              <Field label="resume_after（跳过开机链的续跑节点，必填）">
                <Input size="small" style={{ width: 220 }} value={doc.resume_after}
                  onChange={(e) => patch({ resume_after: e.target.value })} />
              </Field>
              <Field label="case_entry（用例正文入口节点，必填）">
                <Input size="small" style={{ width: 220 }} value={doc.case_entry}
                  onChange={(e) => patch({ case_entry: e.target.value })} />
              </Field>
            </Space>

            <Space wrap>
              <Field label="on_case_failure">
                <Select size="small" style={{ width: 180 }}
                  value={doc.on_case_failure ?? "restart_retry"}
                  options={[
                    { value: "restart_retry", label: "restart_retry（重启重试）" },
                    { value: "restart_continue", label: "restart_continue（重启继续）" },
                    { value: "abort", label: "abort（中止）" },
                  ]}
                  onChange={(v) => patch({ on_case_failure: v })} />
              </Field>
              <Field label="max_retries">
                <InputNumber size="small" min={0} value={doc.max_retries}
                  onChange={(v) => patch({ max_retries: v ?? undefined })} />
              </Field>
            </Space>

            <Field label="full_boot_cases（必须强制冷启动的用例 ⊆ cases）">
              <Select
                size="small"
                mode="multiple"
                style={{ width: "100%" }}
                value={doc.full_boot_cases ?? []}
                options={doc.cases.map((c) => ({ value: c }))}
                onChange={(v) => patch({ full_boot_cases: v.length ? v : undefined })}
              />
            </Field>

            <Field label="landing（用例间落地画面识别；关闭 = 显式 null 禁用）">
              <Space direction="vertical" style={{ width: "100%" }} size={4}>
                <Switch
                  size="small"
                  checked={doc.landing !== null}
                  checkedChildren="启用"
                  unCheckedChildren="禁用(null)"
                  onChange={(on) =>
                    patch({
                      landing: on
                        ? { type: "ocr", expected: "" }
                        : null,
                    })
                  }
                />
                {doc.landing !== null && (
                  <RecognitionForm
                    value={doc.landing}
                    onChange={(rec) => patch({ landing: rec })}
                    allowAlways={false}
                  />
                )}
              </Space>
            </Field>
          </Space>
        )}
      </div>

      {/* 右：运行面板 */}
      {name && (
        <div style={{ width: 340, borderLeft: "1px solid #f0f0f0", background: "#fff" }}>
          <RunPanel taskName={name} kind="suite" />
        </div>
      )}

      <Modal
        title="新建套件"
        open={newOpen}
        onOk={() => {
          if (!newName) return;
          setIsDraft(true);
          setDoc({ ...structuredClone(NEW_SUITE), name: newName });
          setNewOpen(false);
          setNewName("");
          navigate("/suites");
        }}
        onCancel={() => setNewOpen(false)}
      >
        <Input
          placeholder="套件名"
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          autoFocus
        />
      </Modal>
    </div>
  );
}
