/** 动作动态表单：type 驱动；custom 名称走后端动态列表；llm 按 agent 展示。 */
import { useQuery } from "@tanstack/react-query";
import {
  Alert,
  Checkbox,
  Collapse,
  Input,
  InputNumber,
  Segmented,
  Select,
  Space,
  Typography,
} from "antd";
import { useState } from "react";

import { metaApi } from "../../api/endpoints";
import type { ActionType, TaskAction } from "../../types/task";
import { CustomParamsForm } from "./CustomParamsForm";
import { Field } from "./RecognitionForm";

const TYPE_LABELS: Record<string, string> = {
  click: "click（点击）",
  drag: "drag（拖动）",
  input_text: "input_text（输入文本）",
  wait: "wait（等待）",
  key: "key（按键）",
  gesture: "gesture（多指手势）",
  none: "none（只识别不动作）",
  agent: "agent（挂起交接给外部智能体）",
  custom: "custom（自定义 Python handler）",
};

const REPEATABLE = new Set(["click", "drag", "input_text", "key", "gesture"]);

function migrate(act: TaskAction, newType: ActionType): TaskAction {
  const next: TaskAction = { type: newType };
  for (const [k, v] of Object.entries(act)) {
    if (k.startsWith("_")) (next as Record<string, unknown>)[k] = v;
  }
  if (newType === "click") next.target = "recognized";
  if (newType === "agent" && act.text) next.text = act.text;
  if (newType === "custom" && act.name) {
    next.name = act.name;
    next.params = act.params;
  }
  return next;
}

interface Props {
  value: TaskAction;
  onChange: (act: TaskAction) => void;
  disabled?: boolean;
}

export function ActionForm({ value, onChange, disabled }: Props) {
  const act = value;
  const displayType: ActionType = act.type === "llm" ? "agent" : act.type;
  const customActionsQuery = useQuery({
    queryKey: ["custom-actions"],
    queryFn: metaApi.customActions,
    staleTime: 300_000,
  });
  const [paramsError, setParamsError] = useState<string | null>(null);
  const [paramsMode, setParamsMode] = useState<"form" | "json">("form");

  // custom 参数 schema：后端从 handler 源码静态提取。拿不到（404 / 提取失败）
  // 就退回现状的裸 JSON 编辑，不弹错——这只是编辑体验增强，不是必需品。
  const customName = displayType === "custom" ? act.name : undefined;
  const schemaQuery = useQuery({
    queryKey: ["custom-action-schema", customName],
    queryFn: () => metaApi.customActionSchema(customName as string),
    enabled: !!customName,
    staleTime: 300_000,
    retry: false,
  });
  const schemaParams = schemaQuery.data?.params ?? [];

  const patch = (p: Partial<TaskAction>) => {
    const next = { ...act };
    for (const [k, v] of Object.entries(p)) {
      if (v === undefined) delete (next as Record<string, unknown>)[k];
      else (next as Record<string, unknown>)[k] = v;
    }
    onChange(next);
  };

  const patchParams = (p: Record<string, unknown>) => {
    const params = { ...(act.params ?? {}) };
    for (const [k, v] of Object.entries(p)) {
      if (v === undefined) delete params[k];
      else params[k] = v;
    }
    patch({ params: Object.keys(params).length ? params : undefined });
  };

  const numParam = (key: string): number | undefined => {
    const v = act.params?.[key];
    return typeof v === "number" ? v : undefined;
  };

  return (
    <Space direction="vertical" style={{ width: "100%" }} size={6}>
      <Select
        size="small"
        style={{ width: "100%" }}
        value={displayType}
        onChange={(t) => onChange(migrate(act, t))}
        options={Object.entries(TYPE_LABELS).map(([v, label]) => ({ value: v, label }))}
        disabled={disabled}
      />
      {act.type === "llm" && (
        <Alert type="info" showIcon message="llm 是 agent 的废弃别名（保存时保留原值）" />
      )}

      {displayType === "click" && (
        <>
          <Checkbox
            checked={act.target === "recognized"}
            disabled={disabled}
            onChange={(e) => {
              if (e.target.checked) {
                patch({ target: "recognized" });
                patchParams({ x: undefined, y: undefined });
              } else {
                patch({ target: undefined });
              }
            }}
          >
            点击识别命中中心（target: recognized，推荐）
          </Checkbox>
          {act.target !== "recognized" && (
            <Space>
              <Field label="x">
                <InputNumber size="small" value={numParam("x")} disabled={disabled}
                  onChange={(v) => patchParams({ x: v ?? undefined })} />
              </Field>
              <Field label="y">
                <InputNumber size="small" value={numParam("y")} disabled={disabled}
                  onChange={(v) => patchParams({ y: v ?? undefined })} />
              </Field>
            </Space>
          )}
        </>
      )}

      {displayType === "drag" && (
        <Space wrap>
          {(["x1", "y1", "x2", "y2", "duration_ms"] as const).map((k) => (
            <Field key={k} label={k}>
              <InputNumber size="small" style={{ width: 90 }} value={numParam(k)}
                disabled={disabled} onChange={(v) => patchParams({ [k]: v ?? undefined })} />
            </Field>
          ))}
        </Space>
      )}

      {displayType === "input_text" && (
        <Field label="text">
          <Input size="small" value={(act.params?.text as string) ?? ""}
            disabled={disabled}
            onChange={(e) => patchParams({ text: e.target.value })} />
        </Field>
      )}

      {displayType === "wait" && (
        <Field label="duration_ms">
          <InputNumber size="small" min={0} value={numParam("duration_ms")}
            disabled={disabled}
            onChange={(v) => patchParams({ duration_ms: v ?? undefined })} />
        </Field>
      )}

      {displayType === "key" && (
        <Field label="keycode（4=BACK, 3=HOME, 82=MENU）">
          <InputNumber size="small" value={numParam("keycode")} disabled={disabled}
            onChange={(v) => patchParams({ keycode: v ?? undefined })} />
        </Field>
      )}

      {displayType === "gesture" && (
        <Field label="params（frames 或 pinch，JSON）">
          <ParamsJsonEditor value={act.params} disabled={disabled}
            onChange={(p) => patch({ params: p })} onError={setParamsError} />
        </Field>
      )}

      {displayType === "agent" && (
        <Field label="text（交接指令，必填）">
          <Input.TextArea rows={3} value={act.text ?? ""} disabled={disabled}
            onChange={(e) => patch({ text: e.target.value })} />
        </Field>
      )}

      {displayType === "custom" && (
        <>
          <Field label="name（已注册的 custom action）">
            <Select
              size="small"
              style={{ width: "100%" }}
              showSearch
              value={act.name}
              disabled={disabled}
              loading={customActionsQuery.isLoading}
              onChange={(v) => patch({ name: v })}
              options={(customActionsQuery.data ?? []).map((n) => ({ value: n }))}
            />
          </Field>
          {schemaParams.length > 0 && (
            <Segmented
              size="small"
              value={paramsMode}
              onChange={(v) => {
                setParamsMode(v as "form" | "json");
                setParamsError(null);
              }}
              options={[
                { label: "表单", value: "form" },
                { label: "JSON", value: "json" },
              ]}
            />
          )}
          {schemaParams.length > 0 && paramsMode === "form" ? (
            <CustomParamsForm
              schema={schemaParams}
              params={act.params}
              disabled={disabled}
              onChange={(p) => patch({ params: p })}
            />
          ) : (
            <Field label="params（透传给 handler，JSON）">
              <ParamsJsonEditor
                key={`custom-json-${act.name ?? ""}`}
                value={act.params}
                disabled={disabled}
                onChange={(p) => patch({ params: p })}
                onError={setParamsError}
              />
            </Field>
          )}
        </>
      )}

      {paramsError && <Alert type="error" showIcon message={paramsError} />}

      {REPEATABLE.has(displayType) && (
        <Collapse
          size="small"
          ghost
          items={[
            {
              key: "repeat",
              label: (
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  连发参数（QTE 连点，不重跑识别）
                  {act.params?.repeat ? ` — ×${act.params.repeat}` : ""}
                </Typography.Text>
              ),
              children: (
                <Space wrap>
                  <Field label="repeat（≥1）">
                    <InputNumber size="small" min={1} value={numParam("repeat")}
                      disabled={disabled}
                      onChange={(v) => patchParams({ repeat: v ?? undefined })} />
                  </Field>
                  <Field label="repeat_delay_ms">
                    <InputNumber size="small" min={0} value={numParam("repeat_delay_ms")}
                      disabled={disabled}
                      onChange={(v) => patchParams({ repeat_delay_ms: v ?? undefined })} />
                  </Field>
                  <Field label="repeat_wait_freezes_ms">
                    <InputNumber size="small" min={0}
                      value={numParam("repeat_wait_freezes_ms")} disabled={disabled}
                      onChange={(v) =>
                        patchParams({ repeat_wait_freezes_ms: v ?? undefined })
                      } />
                  </Field>
                </Space>
              ),
            },
          ]}
        />
      )}
    </Space>
  );
}

function ParamsJsonEditor({
  value,
  onChange,
  onError,
  disabled,
}: {
  value: Record<string, unknown> | undefined;
  onChange: (v: Record<string, unknown> | undefined) => void;
  onError: (msg: string | null) => void;
  disabled?: boolean;
}) {
  const [text, setText] = useState(() =>
    value ? JSON.stringify(value, null, 2) : "",
  );
  return (
    <Input.TextArea
      rows={4}
      value={text}
      disabled={disabled}
      style={{ fontFamily: "monospace", fontSize: 12 }}
      onChange={(e) => {
        const t = e.target.value;
        setText(t);
        if (!t.trim()) {
          onChange(undefined);
          onError(null);
          return;
        }
        try {
          const parsed = JSON.parse(t);
          if (typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)) {
            onChange(parsed as Record<string, unknown>);
            onError(null);
          } else {
            onError("params 必须是 JSON 对象");
          }
        } catch (err) {
          onError(`JSON 解析失败: ${(err as Error).message}`);
        }
      }}
    />
  );
}
