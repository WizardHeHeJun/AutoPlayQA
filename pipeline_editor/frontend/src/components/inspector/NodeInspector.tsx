/** 右侧节点属性面板：改名 / 识别 / 动作 / next / 时序 / finding / wait_still。 */
import { DeleteOutlined, EditOutlined } from "@ant-design/icons";
import {
  Alert,
  App as AntApp,
  Button,
  Collapse,
  Divider,
  Empty,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Space,
  Switch,
  Typography,
} from "antd";
import { useState } from "react";

import { renameImpact } from "../../graph/rename";
import { useEditorStore } from "../../store/editorStore";
import { useUiStore } from "../../store/uiStore";
import type { Finding } from "../../types/task";
import { includeSource } from "../../types/task";
import { ActionForm } from "./ActionForm";
import { Field, RecognitionForm } from "./RecognitionForm";
import { NextListEditor } from "./NextListEditor";

export function NodeInspector() {
  const { message } = AntApp.useApp();
  const doc = useEditorStore((s) => s.doc);
  const updateNode = useEditorStore((s) => s.updateNode);
  const setRecognition = useEditorStore((s) => s.setRecognition);
  const setAction = useEditorStore((s) => s.setAction);
  const deleteNode = useEditorStore((s) => s.deleteNode);
  const renameNode = useEditorStore((s) => s.renameNode);
  const connectNext = useEditorStore((s) => s.connectNext);
  const removeNext = useEditorStore((s) => s.removeNext);
  const reorderNext = useEditorStore((s) => s.reorderNext);
  const setOnTimeout = useEditorStore((s) => s.setOnTimeout);

  const selected = useUiStore((s) => s.selectedNode);
  const select = useUiStore((s) => s.select);

  const [renameOpen, setRenameOpen] = useState(false);
  const [renameValue, setRenameValue] = useState("");

  if (!doc || !selected || !doc.nodes[selected]) {
    return <Empty description="选中一个节点以编辑属性" style={{ marginTop: 48 }} />;
  }
  const name = selected;
  const node = doc.nodes[name];
  const incFrom = includeSource(doc, name);
  const readonly = incFrom !== null;
  const nodeNames = Object.keys(doc.nodes);
  const finding = node.finding;
  const findingObj: Finding | null =
    finding == null
      ? null
      : typeof finding === "string"
        ? { message: finding, severity: "error" }
        : finding;

  const doRename = () => {
    const newName = renameValue.trim();
    if (!newName || newName === name) return setRenameOpen(false);
    if (doc.nodes[newName]) {
      message.error(`节点 "${newName}" 已存在`);
      return;
    }
    const impact = renameImpact(doc, name);
    const refCount =
      impact.nextRefs.length + impact.timeoutRefs.length + impact.watchdogRefs.length;
    renameNode(name, newName);
    select(newName);
    setRenameOpen(false);
    message.success(
      `已改名并级联更新 ${refCount} 处引用` +
        (impact.entry ? "（含 entry）" : "") +
        (impact.onFinding ? "（含 on_finding）" : ""),
    );
  };

  return (
    <div style={{ padding: 12, overflow: "auto", height: "100%" }}>
      <Space style={{ width: "100%", justifyContent: "space-between" }}>
        <Typography.Text strong style={{ fontSize: 14 }} ellipsis={{ tooltip: name }}>
          {name}
        </Typography.Text>
        <Space>
          <Button
            size="small"
            icon={<EditOutlined />}
            disabled={readonly}
            onClick={() => {
              setRenameValue(name);
              setRenameOpen(true);
            }}
          />
          <Popconfirm
            title={`删除节点 "${name}"？引用它的 next/on_timeout 会被清理`}
            onConfirm={() => {
              deleteNode(name);
              select(null);
            }}
            disabled={readonly}
          >
            <Button size="small" danger icon={<DeleteOutlined />} disabled={readonly} />
          </Popconfirm>
        </Space>
      </Space>

      {readonly && (
        <Alert
          style={{ marginTop: 8 }}
          type="info"
          showIcon
          message={`来自 include: ${incFrom}`}
          description="共享片段节点为只读——改它会影响所有引用该片段的任务。请到片段文件中修改。"
        />
      )}

      <Divider orientation="left" plain style={{ margin: "12px 0 8px" }}>
        识别（recognition）
      </Divider>
      <RecognitionForm
        value={node.recognition}
        onChange={(rec) => setRecognition(name, rec)}
        disabled={readonly}
      />

      <Divider orientation="left" plain style={{ margin: "16px 0 8px" }}>
        动作（action）
      </Divider>
      <ActionForm
        value={node.action}
        onChange={(act) => setAction(name, act)}
        disabled={readonly}
      />

      <Divider orientation="left" plain style={{ margin: "16px 0 8px" }}>
        next 候选（顺序 = 识别优先级）
      </Divider>
      <NextListEditor
        value={node.next ?? []}
        nodeNames={nodeNames}
        disabled={readonly}
        onReorder={(from, to) => reorderNext(name, from, to)}
        onRemove={(target, i) => removeNext(name, target, i)}
        onAdd={(target) => connectNext(name, target)}
        onLocate={(target) => select(target)}
      />

      <Divider orientation="left" plain style={{ margin: "16px 0 8px" }}>
        超时兜底 / QA
      </Divider>
      <Space direction="vertical" style={{ width: "100%" }} size={8}>
        <Field label="on_timeout（识别超时的恢复节点）">
          <Select
            size="small"
            style={{ width: "100%" }}
            allowClear
            showSearch
            value={node.on_timeout}
            disabled={readonly}
            options={nodeNames.filter((n) => n !== name).map((n) => ({ value: n }))}
            onChange={(v) => setOnTimeout(name, v ?? null)}
          />
        </Field>

        <Field label="finding（进入此节点即上报 QA 发现）">
          <Space direction="vertical" style={{ width: "100%" }} size={4}>
            <Switch
              size="small"
              checked={findingObj !== null}
              disabled={readonly}
              checkedChildren="上报"
              unCheckedChildren="不上报"
              onChange={(on) =>
                updateNode(name, {
                  finding: on ? { severity: "warning", message: "" } : undefined,
                })
              }
            />
            {findingObj && (
              <Space.Compact block>
                <Select
                  size="small"
                  style={{ width: 110 }}
                  value={findingObj.severity ?? "error"}
                  disabled={readonly}
                  options={["info", "warning", "error", "critical"].map((s) => ({
                    value: s,
                  }))}
                  onChange={(sev) =>
                    updateNode(name, {
                      finding: { ...findingObj, severity: sev as Finding["severity"] },
                    })
                  }
                />
                <Input
                  size="small"
                  placeholder="message（人读描述）"
                  value={findingObj.message}
                  disabled={readonly}
                  onChange={(e) =>
                    updateNode(name, {
                      finding: { ...findingObj, message: e.target.value },
                    })
                  }
                />
              </Space.Compact>
            )}
          </Space>
        </Field>
      </Space>

      <Collapse
        size="small"
        ghost
        style={{ marginTop: 8 }}
        items={[
          {
            key: "timing",
            label: (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                时序调参（留空 = 任务 defaults / 引擎默认）
              </Typography.Text>
            ),
            children: (
              <Space direction="vertical" style={{ width: "100%" }} size={6}>
                {(
                  [
                    ["timeout_ms", "识别轮询总预算（默认 10000）"],
                    ["poll_interval_ms", "轮询间隔（默认 1000）"],
                    ["post_delay_ms", "动作后固定等待（默认 0）"],
                  ] as const
                ).map(([key, hint]) => (
                  <Field key={key} label={`${key}（${hint}）`}>
                    <InputNumber
                      size="small"
                      style={{ width: "100%" }}
                      min={0}
                      value={typeof node[key] === "number" ? (node[key] as number) : undefined}
                      disabled={readonly}
                      onChange={(v) => updateNode(name, { [key]: v ?? undefined })}
                    />
                  </Field>
                ))}
                <Field label="wait_still（画面静止再放行；超时不算失败）">
                  <Space wrap>
                    <Switch
                      size="small"
                      checked={node.wait_still != null}
                      disabled={readonly}
                      onChange={(on) =>
                        updateNode(name, {
                          wait_still: on
                            ? { timeout_ms: 5000, interval_ms: 200, threshold: 0.01 }
                            : undefined,
                        })
                      }
                    />
                    {node.wait_still && (
                      <>
                        {(
                          [
                            ["timeout_ms", 5000],
                            ["interval_ms", 200],
                          ] as const
                        ).map(([k]) => (
                          <InputNumber
                            key={k}
                            size="small"
                            min={0}
                            style={{ width: 90 }}
                            placeholder={k}
                            value={node.wait_still?.[k]}
                            disabled={readonly}
                            onChange={(v) =>
                              updateNode(name, {
                                wait_still: { ...node.wait_still, [k]: v ?? undefined },
                              })
                            }
                          />
                        ))}
                        <InputNumber
                          size="small"
                          min={0}
                          max={1}
                          step={0.005}
                          style={{ width: 90 }}
                          placeholder="threshold"
                          value={node.wait_still?.threshold}
                          disabled={readonly}
                          onChange={(v) =>
                            updateNode(name, {
                              wait_still: {
                                ...node.wait_still,
                                threshold: v ?? undefined,
                              },
                            })
                          }
                        />
                      </>
                    )}
                  </Space>
                </Field>
              </Space>
            ),
          },
        ]}
      />

      <Modal
        title="重命名节点（级联更新所有引用）"
        open={renameOpen}
        onOk={doRename}
        onCancel={() => setRenameOpen(false)}
      >
        <Input
          value={renameValue}
          onChange={(e) => setRenameValue(e.target.value)}
          onPressEnter={doRename}
        />
      </Modal>
    </div>
  );
}
