/**
 * custom action 的类型化参数表单（schema 来自后端对 handler 源码的静态提取）。
 *
 * 写入纪律见 `customParams.ts` 顶部：只写用户显式设置的键，默认值只做 placeholder。
 */
import { InfoCircleOutlined } from "@ant-design/icons";
import {
  Alert,
  Button,
  Collapse,
  Input,
  InputNumber,
  Select,
  Space,
  Switch,
  Tooltip,
  Typography,
} from "antd";
import { useState } from "react";

import type { CustomActionParam } from "../../types/api";
import {
  asBool,
  asNumber,
  asRoi,
  asString,
  isSet,
  parseJsonLine,
  placeholderOf,
  setParam,
  stepOf,
  toJsonLine,
  unknownParamKeys,
  type Params,
} from "./customParams";
import { Field } from "./RecognitionForm";
import { RoiField } from "./RoiField";

/** 超过这个数量就把「未设置的参数」折进 Collapse（参数多的 handler 可达数十个）。 */
const FLAT_LIMIT = 8;

interface Props {
  schema: CustomActionParam[];
  params: Params | undefined;
  onChange: (params: Params | undefined) => void;
  disabled?: boolean;
}

export function CustomParamsForm({ schema, params, onChange, disabled }: Props) {
  const write = (key: string, value: unknown) => onChange(setParam(params, key, value));

  const set = schema.filter((p) => isSet(params, p.key) || p.required);
  const unset = schema.filter((p) => !isSet(params, p.key) && !p.required);
  const flat = schema.length <= FLAT_LIMIT;
  const unknown = unknownParamKeys(params, schema);

  const rows = (list: CustomActionParam[]) =>
    list.map((p) => (
      <ParamRow key={p.key} param={p} params={params} onWrite={write} disabled={disabled} />
    ));

  return (
    <Space direction="vertical" style={{ width: "100%" }} size={6}>
      {rows(flat ? schema : set)}
      {!flat && unset.length > 0 && (
        <Collapse
          size="small"
          ghost
          items={[
            {
              key: "unset",
              label: (
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  未设置的参数（{unset.length}，留空即跟随 handler 默认）
                </Typography.Text>
              ),
              children: (
                <Space direction="vertical" style={{ width: "100%" }} size={6}>
                  {rows(unset)}
                </Space>
              ),
            },
          ]}
        />
      )}
      {unknown.length > 0 && (
        <>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            schema 之外的键（原样保留）
          </Typography.Text>
          {unknown.map((key) => (
            <Field key={key} label={key}>
              <Space.Compact block>
                <JsonLineInput
                  value={params?.[key]}
                  disabled={disabled}
                  onChange={(v) => write(key, v)}
                />
                <Button size="small" disabled={disabled} onClick={() => write(key, undefined)}>
                  删除
                </Button>
              </Space.Compact>
            </Field>
          ))}
        </>
      )}
    </Space>
  );
}

function labelOf(param: CustomActionParam): string {
  const hint = param.type === "enum" ? "enum" : param.type;
  const tail =
    param.type === "bool" || param.type === "roi" || param.type === "point" || param.type === "json"
      ? `，${placeholderOf(param)}`
      : "";
  return `${param.key}${param.required ? " *" : ""}（${hint}${tail}）`;
}

/** docstring 说明长，浮层给个上限免得糊满屏；同 TaskNode 的 hint 一样只在有说明时才挂。 */
const DESCRIPTION_TOOLTIP_WIDTH = 360;

/**
 * 字段 label：有 handler docstring 说明就在文本后挂一个 ⓘ 图标并包 Tooltip
 * （悬停图标出说明）；无说明的行不挂任何提示标记。
 */
function ParamLabel({ param }: { param: CustomActionParam }) {
  const text = labelOf(param);
  if (!param.description) return <>{text}</>;
  return (
    <>
      {text}
      <Tooltip
        title={param.description}
        styles={{ root: { maxWidth: DESCRIPTION_TOOLTIP_WIDTH } }}
      >
        <InfoCircleOutlined
          style={{ marginLeft: 4, fontSize: 12, color: "#98a2b3", cursor: "help" }}
        />
      </Tooltip>
    </>
  );
}

function ParamRow({
  param,
  params,
  onWrite,
  disabled,
}: {
  param: CustomActionParam;
  params: Params | undefined;
  onWrite: (key: string, value: unknown) => void;
  disabled?: boolean;
}) {
  const value = params?.[param.key];
  const write = (v: unknown) => onWrite(param.key, v);
  const placeholder = placeholderOf(param);

  return <Field label={<ParamLabel param={param} />}>{control()}</Field>;

  function control() {
    switch (param.type) {
      case "int":
      case "float":
        return (
          <InputNumber
            size="small"
            style={{ width: "100%" }}
            step={stepOf(param)}
            placeholder={placeholder}
            value={asNumber(value)}
            disabled={disabled}
            onChange={(v) => write(v ?? undefined)}
          />
        );
      case "bool":
        return (
          <Space>
            <Switch
              size="small"
              checked={asBool(value) ?? false}
              disabled={disabled}
              onChange={(checked) => write(checked)}
            />
            {isSet(params, param.key) && (
              <Button size="small" type="link" disabled={disabled} onClick={() => write(undefined)}>
                清除
              </Button>
            )}
          </Space>
        );
      case "str":
        return (
          <Input
            size="small"
            allowClear
            placeholder={placeholder}
            value={asString(value) ?? ""}
            disabled={disabled}
            onChange={(e) => write(e.target.value === "" ? undefined : e.target.value)}
          />
        );
      case "enum":
        return (
          <Select
            size="small"
            style={{ width: "100%" }}
            allowClear
            placeholder={placeholder}
            value={asString(value)}
            disabled={disabled}
            options={(param.choices ?? []).map((c) => ({ value: c }))}
            onChange={(v) => write(v ?? undefined)}
          />
        );
      case "roi": {
        const roi = asRoi(value);
        // 值不是 4 个数字（手写成别的形态）时不硬塞进 RoiField，退回 JSON 行免得改坏。
        if (value !== undefined && roi === undefined) {
          return <JsonLineInput value={value} disabled={disabled} onChange={write} />;
        }
        return <RoiField value={roi} disabled={disabled} onChange={(r) => write(r ?? undefined)} />;
      }
      default:
        return <JsonLineInput value={value} disabled={disabled} onChange={write} />;
    }
  }
}

/** 单行 JSON 输入：空串 = 删键；解析失败时保留文本并报错，不写坏值。 */
function JsonLineInput({
  value,
  onChange,
  disabled,
}: {
  value: unknown;
  onChange: (v: unknown) => void;
  disabled?: boolean;
}) {
  const external = toJsonLine(value);
  const [text, setText] = useState(external);
  const [seen, setSeen] = useState(external);
  const [error, setError] = useState<string | null>(null);
  // 外部（撤销 / 切换节点 / JSON 模式改动）改了值就跟着刷新输入框。
  if (external !== seen) {
    setSeen(external);
    setText(external);
    setError(null);
  }

  return (
    <>
      <Input
        size="small"
        style={{ fontFamily: "monospace", fontSize: 12 }}
        placeholder="JSON，如 [1,2] 或 {&quot;a&quot;:1}"
        value={text}
        disabled={disabled}
        status={error ? "error" : undefined}
        onChange={(e) => {
          const t = e.target.value;
          setText(t);
          const parsed = parseJsonLine(t);
          if (parsed.ok) {
            setError(null);
            setSeen(toJsonLine(parsed.value));
            onChange(parsed.value);
          } else {
            setError(parsed.error);
          }
        }}
      />
      {error && <Alert type="error" showIcon message={error} style={{ marginTop: 2 }} />}
    </>
  );
}
