/** 任务级设置：Meta / defaults / watchdogs / popups —— 本项目相对 MaaFW 的独有 QA 层。 */
import { DeleteOutlined, PlusOutlined } from "@ant-design/icons";
import {
  Alert,
  Button,
  Collapse,
  Divider,
  Input,
  InputNumber,
  Select,
  Space,
  Switch,
  Tag,
  Typography,
} from "antd";

import { useEditorStore } from "../../store/editorStore";
import type { Popup, Recognition, TaskDefaults, Watchdog } from "../../types/task";
import { Field, RecognitionForm } from "../inspector/RecognitionForm";
import { ActionForm } from "../inspector/ActionForm";

const SEVERITIES = ["info", "warning", "error", "critical"] as const;

export function TaskSettingsPanel() {
  const doc = useEditorStore((s) => s.doc);
  const setDocField = useEditorStore((s) => s.setDocField);
  if (!doc) return null;
  const nodeNames = Object.keys(doc.nodes ?? {});
  const watchdogs = doc.watchdogs ?? [];
  const popups = doc.popups ?? [];
  const defaults = doc.defaults;

  const setWatchdogs = (list: Watchdog[]) =>
    setDocField("watchdogs", list.length ? list : undefined);
  const setPopups = (list: Popup[]) =>
    setDocField("popups", list.length ? list : undefined);

  return (
    <div style={{ padding: 12, overflow: "auto", height: "100%" }}>
      <Divider orientation="left" plain style={{ margin: "0 0 8px" }}>
        任务元信息
      </Divider>
      <Space direction="vertical" style={{ width: "100%" }} size={8}>
        <Field label="entry（入口节点）">
          <Select
            size="small"
            style={{ width: "100%" }}
            showSearch
            value={doc.entry}
            options={nodeNames.map((n) => ({ value: n }))}
            onChange={(v) => setDocField("entry", v)}
          />
        </Field>
        <Field label="on_finding（bug-skip 全局兜底节点）">
          <Select
            size="small"
            style={{ width: "100%" }}
            allowClear
            showSearch
            value={doc.on_finding}
            options={nodeNames.map((n) => ({ value: n }))}
            onChange={(v) => setDocField("on_finding", v ?? undefined)}
          />
        </Field>
        <Space wrap>
          <Field label="max_steps（步数预算）">
            <InputNumber
              size="small"
              min={1}
              value={doc.max_steps}
              onChange={(v) => setDocField("max_steps", v ?? undefined)}
            />
          </Field>
          <Field label="back_fallback（BACK 兜底）">
            <Select
              size="small"
              style={{ width: 120 }}
              allowClear
              placeholder="跟随 config"
              value={doc.back_fallback}
              options={[
                { value: true, label: "开" },
                { value: false, label: "关" },
              ]}
              onChange={(v) => setDocField("back_fallback", v ?? undefined)}
            />
          </Field>
        </Space>
        {doc.includes && doc.includes.length > 0 && (
          <Field label="includes（共享片段，只读；到片段文件中修改）">
            <Space direction="vertical" size={2}>
              {doc.includes.map((i) => (
                <Tag key={i}>{i}</Tag>
              ))}
              <Space>
                <span style={{ fontSize: 12, color: "#667085" }}>on_conflict:</span>
                <Select
                  size="small"
                  style={{ width: 130 }}
                  value={doc.on_conflict ?? "strict"}
                  options={[{ value: "strict" }, { value: "overwrite" }]}
                  onChange={(v) => setDocField("on_conflict", v)}
                />
              </Space>
            </Space>
          </Field>
        )}
      </Space>

      <Divider orientation="left" plain style={{ margin: "16px 0 8px" }}>
        defaults（节点时序默认值）
      </Divider>
      <Space direction="vertical" style={{ width: "100%" }} size={6}>
        {(
          [
            ["timeout_ms", "识别轮询总预算"],
            ["poll_interval_ms", "轮询间隔"],
            ["post_delay_ms", "动作后等待"],
          ] as const
        ).map(([key, hint]) => (
          <Field key={key} label={`${key}（${hint}）`}>
            <InputNumber
              size="small"
              style={{ width: "100%" }}
              min={0}
              value={defaults?.[key]}
              onChange={(v) => {
                const next: TaskDefaults = { ...defaults };
                if (v == null) delete next[key];
                else next[key] = v;
                setDocField("defaults", Object.keys(next).length ? next : undefined);
              }}
            />
          </Field>
        ))}
        <Typography.Text type="secondary" style={{ fontSize: 11 }}>
          优先级：节点字段 &gt; defaults &gt; 引擎默认。节点写 null 可退回引擎默认。
        </Typography.Text>
      </Space>

      <Divider orientation="left" plain style={{ margin: "16px 0 8px" }}>
        watchdogs（负向断言，{watchdogs.length} 条）
      </Divider>
      <Collapse
        size="small"
        items={watchdogs.map((w, i) => {
          const detail = w.message || w.expected || w.template || "";
          const labelText = detail ? `${w.type} ${detail}` : w.type;
          return {
            key: String(i),
            label: (
              <div style={{ display: "flex", alignItems: "center", gap: 4, minWidth: 0 }}>
                <Tag
                  color={
                    ({ info: "blue", warning: "orange", error: "red", critical: "magenta" } as Record<string, string>)[
                      w.severity ?? "error"
                    ]
                  }
                >
                  {w.severity ?? "error"}
                </Tag>
                <Typography.Text
                  style={{ fontSize: 12, minWidth: 0, flex: 1 }}
                  ellipsis={{ tooltip: labelText }}
                >
                  {labelText}
                </Typography.Text>
              </div>
            ),
            extra: (
              <Button
                size="small"
                type="text"
                danger
                icon={<DeleteOutlined />}
                onClick={(e) => {
                  e.stopPropagation();
                  setWatchdogs(watchdogs.filter((_, j) => j !== i));
                }}
              />
            ),
            children: (
              <WatchdogEditor
                value={w}
                nodeNames={nodeNames}
                onChange={(next) =>
                  setWatchdogs(watchdogs.map((old, j) => (j === i ? next : old)))
                }
              />
            ),
          };
        })}
      />
      <Button
        size="small"
        icon={<PlusOutlined />}
        style={{ marginTop: 8 }}
        onClick={() =>
          setWatchdogs([
            ...watchdogs,
            { type: "ocr", expected: "", severity: "error", message: "" } as Watchdog,
          ])
        }
      >
        添加 watchdog
      </Button>

      <Divider orientation="left" plain style={{ margin: "16px 0 8px" }}>
        popups（良性弹窗白名单，{popups.length} 条）
      </Divider>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 8 }}
        message="仅在识别停滞时扫描；被消除的弹窗不记 finding"
      />
      <Collapse
        size="small"
        items={popups.map((p, i) => ({
          key: String(i),
          label: (
            <Typography.Text
              style={{ fontSize: 12, display: "block", minWidth: 0 }}
              ellipsis={{ tooltip: p.name || `弹窗 #${i + 1}` }}
            >
              {p.name || `弹窗 #${i + 1}`}
            </Typography.Text>
          ),
          extra: (
            <Button
              size="small"
              type="text"
              danger
              icon={<DeleteOutlined />}
              onClick={(e) => {
                e.stopPropagation();
                setPopups(popups.filter((_, j) => j !== i));
              }}
            />
          ),
          children: (
            <PopupEditor
              value={p}
              onChange={(next) => setPopups(popups.map((old, j) => (j === i ? next : old)))}
            />
          ),
        }))}
      />
      <Button
        size="small"
        icon={<PlusOutlined />}
        style={{ marginTop: 8 }}
        onClick={() =>
          setPopups([
            ...popups,
            {
              name: "",
              recognition: { type: "template", template: "" },
              action: { type: "click", target: "recognized" },
            },
          ])
        }
      >
        添加弹窗
      </Button>
    </div>
  );
}

function WatchdogEditor({
  value,
  nodeNames,
  onChange,
}: {
  value: Watchdog;
  nodeNames: string[];
  onChange: (w: Watchdog) => void;
}) {
  const patch = (p: Partial<Watchdog>) => {
    const next = { ...value };
    for (const [k, v] of Object.entries(p)) {
      if (v === undefined) delete (next as Record<string, unknown>)[k];
      else (next as Record<string, unknown>)[k] = v;
    }
    onChange(next);
  };
  return (
    <Space direction="vertical" style={{ width: "100%" }} size={6}>
      <RecognitionForm
        value={value as unknown as Recognition}
        onChange={(rec) => onChange({ ...rec, severity: value.severity, message: value.message, skip_to: value.skip_to, fail_task: value.fail_task } as Watchdog)}
        allowAlways={false}
      />
      <Space wrap>
        <Field label="severity">
          <Select
            size="small"
            style={{ width: 110 }}
            value={value.severity ?? "error"}
            options={SEVERITIES.map((s) => ({ value: s }))}
            onChange={(v) => patch({ severity: v })}
          />
        </Field>
        <Field label="fail_task（命中即中止）">
          <Switch
            size="small"
            checked={value.fail_task === true}
            onChange={(v) => patch({ fail_task: v || undefined })}
          />
        </Field>
      </Space>
      <Field label="message（人读描述）">
        <Input
          size="small"
          value={value.message ?? ""}
          onChange={(e) => patch({ message: e.target.value || undefined })}
        />
      </Field>
      <Field label="skip_to（命中后记 finding 并跳转继续测；优先级最高）">
        <Select
          size="small"
          style={{ width: "100%" }}
          allowClear
          showSearch
          value={value.skip_to}
          options={nodeNames.map((n) => ({ value: n }))}
          onChange={(v) => patch({ skip_to: v ?? undefined })}
        />
      </Field>
    </Space>
  );
}

function PopupEditor({ value, onChange }: { value: Popup; onChange: (p: Popup) => void }) {
  const patch = (p: Partial<Popup>) => {
    const next = { ...value };
    for (const [k, v] of Object.entries(p)) {
      if (v === undefined) delete (next as Record<string, unknown>)[k];
      else (next as Record<string, unknown>)[k] = v;
    }
    onChange(next);
  };
  return (
    <Space direction="vertical" style={{ width: "100%" }} size={6}>
      <Field label="name（日志标签）">
        <Input
          size="small"
          value={value.name ?? ""}
          onChange={(e) => patch({ name: e.target.value || undefined })}
        />
      </Field>
      <Field label="recognition（检测弹窗）">
        <RecognitionForm
          value={value.recognition}
          onChange={(rec) => patch({ recognition: rec })}
          allowAlways={false}
        />
      </Field>
      <Field label="confirm（同帧二次门控，可选；不通过 = 视为弹窗不存在）">
        <Space direction="vertical" style={{ width: "100%" }} size={4}>
          <Switch
            size="small"
            checked={value.confirm != null}
            onChange={(on) =>
              patch({ confirm: on ? { type: "ocr", expected: "" } : undefined })
            }
          />
          {value.confirm && (
            <RecognitionForm
              value={value.confirm}
              onChange={(rec) => patch({ confirm: rec })}
              allowAlways={false}
            />
          )}
        </Space>
      </Field>
      <Field label="action（消除动作，仅 click / key / gesture）">
        <PopupActionForm value={value} onChange={onChange} />
      </Field>
    </Space>
  );
}

function PopupActionForm({ value, onChange }: { value: Popup; onChange: (p: Popup) => void }) {
  const act = value.action;
  const allowed = act.type === "click" || act.type === "key" || act.type === "gesture";
  return (
    <Space direction="vertical" style={{ width: "100%" }} size={4}>
      {!allowed && (
        <Alert type="error" showIcon message={`弹窗动作不允许 ${act.type}（仅 click/key/gesture）`} />
      )}
      <ActionForm value={act} onChange={(a) => onChange({ ...value, action: a })} />
    </Space>
  );
}
