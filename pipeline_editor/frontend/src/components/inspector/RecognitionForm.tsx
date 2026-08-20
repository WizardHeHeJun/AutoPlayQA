/** 识别动态表单：type 驱动字段集，and/or 递归渲染子识别（≤2 层）。 */
import { DeleteOutlined, PlusOutlined } from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import {
  Alert,
  AutoComplete,
  Button,
  Checkbox,
  Input,
  InputNumber,
  Select,
  Space,
  Typography,
} from "antd";

import { metaApi } from "../../api/endpoints";
import type { Recognition, RecognitionType } from "../../types/task";
import { RoiField } from "./RoiField";
import { TemplateField } from "./TemplateField";

const ALL_TYPES: RecognitionType[] = [
  "always", "ui_text", "ocr", "blank_screen", "template", "feature", "yolo", "and", "or",
];

const TYPE_LABELS: Record<RecognitionType, string> = {
  always: "always（无条件命中）",
  ui_text: "ui_text（控件文本，慢 ~4.3s）",
  ocr: "ocr（本地 OCR）",
  blank_screen: "blank_screen（黑/白屏）",
  template: "template（模板匹配）",
  feature: "feature（ORB 特征）",
  yolo: "yolo（目标检测）",
  and: "and（组合：全部命中）",
  or: "or（组合：任一命中）",
};

const THRESHOLD_HINT: Partial<Record<RecognitionType, string>> = {
  ui_text: "文本相似度，默认 0.65",
  ocr: "文本相似度，默认 0.65",
  blank_screen: "灰度标准差上限，默认 8.0",
  template: "相关性下限，默认 0.8",
};

/** type 切换时保留兼容字段，丢弃不兼容字段。 */
function migrate(rec: Recognition, newType: RecognitionType): Recognition {
  const keep: Recognition = { type: newType };
  const carry = (keys: (keyof Recognition & string)[]) => {
    for (const k of keys) if (rec[k] !== undefined) (keep as Record<string, unknown>)[k] = rec[k];
  };
  if (newType === "ui_text" || newType === "ocr") carry(["expected", "threshold", "roi"]);
  else if (newType === "template") carry(["template", "threshold", "roi", "scales", "grayscale"]);
  else if (newType === "feature") carry(["template", "roi", "min_matches", "ratio"]);
  else if (newType === "yolo") carry(["roi", "label", "model", "conf"]);
  else if (newType === "blank_screen") carry(["roi"]);
  else if (newType === "and") keep.all_of = rec.any_of ?? rec.all_of ?? [];
  else if (newType === "or") keep.any_of = rec.all_of ?? rec.any_of ?? [];
  // _comment 等 `_` 键透传
  for (const [k, v] of Object.entries(rec)) {
    if (k.startsWith("_")) (keep as Record<string, unknown>)[k] = v;
  }
  return keep;
}

interface Props {
  value: Recognition;
  onChange: (rec: Recognition) => void;
  /** 组合嵌套深度：0 = 节点顶层 */
  depth?: number;
  /** 是否允许 always（watchdog/popup/子识别里禁止） */
  allowAlways?: boolean;
  disabled?: boolean;
}

export function RecognitionForm({
  value,
  onChange,
  depth = 0,
  allowAlways = true,
  disabled,
}: Props) {
  const rec = value;
  const patch = (p: Partial<Recognition>) => {
    const next = { ...rec };
    for (const [k, v] of Object.entries(p)) {
      if (v === undefined) delete (next as Record<string, unknown>)[k];
      else (next as Record<string, unknown>)[k] = v;
    }
    onChange(next);
  };

  const isYolo = rec.type === "yolo";
  const yoloQuery = useQuery({
    queryKey: ["yolo-classes", rec.model ?? null],
    queryFn: () => metaApi.yoloClasses(rec.model),
    enabled: isYolo,
    retry: false,
  });

  const typeOptions = ALL_TYPES.filter((t) => {
    if (t === "always" && !allowAlways) return false;
    if ((t === "and" || t === "or") && depth >= 2) return false;
    return true;
  }).map((t) => ({ value: t, label: TYPE_LABELS[t] }));

  const listKey = rec.type === "and" ? "all_of" : "any_of";
  const subs: Recognition[] = rec.type === "and" || rec.type === "or"
    ? ((rec[listKey] as Recognition[] | undefined) ?? [])
    : [];
  const uiTextSubCount = subs.filter((s) => s.type === "ui_text").length;

  return (
    <Space direction="vertical" style={{ width: "100%" }} size={6}>
      <Select
        size="small"
        style={{ width: "100%" }}
        value={rec.type}
        onChange={(t) => onChange(migrate(rec, t))}
        options={typeOptions}
        disabled={disabled}
      />

      {(rec.type === "ui_text" || rec.type === "ocr") && (
        <Field label="expected（待匹配文本）">
          <Input
            size="small"
            value={rec.expected ?? ""}
            onChange={(e) => patch({ expected: e.target.value })}
            disabled={disabled}
          />
        </Field>
      )}

      {(rec.type === "template" || rec.type === "feature") && (
        <Field label="template（模板）">
          <TemplateField
            value={rec.template}
            onChange={(t) => patch({ template: t })}
            disabled={disabled}
          />
        </Field>
      )}

      {THRESHOLD_HINT[rec.type] && (
        <Field label={`threshold（${THRESHOLD_HINT[rec.type]}）`}>
          <InputNumber
            size="small"
            style={{ width: "100%" }}
            step={rec.type === "blank_screen" ? 0.5 : 0.05}
            value={rec.threshold}
            onChange={(v) => patch({ threshold: v ?? undefined })}
            disabled={disabled}
          />
        </Field>
      )}

      {rec.type === "template" && (
        <>
          <Field label="scales（多尺度，逗号分隔，如 0.8,0.9,1,1.1）">
            <Input
              size="small"
              value={rec.scales?.join(",") ?? ""}
              onChange={(e) => {
                const text = e.target.value.trim();
                if (!text) return patch({ scales: undefined });
                const nums = text
                  .split(/[,，\s]+/)
                  .map(Number)
                  .filter((n) => !Number.isNaN(n) && n > 0);
                patch({ scales: nums.length ? nums : undefined });
              }}
              disabled={disabled}
            />
          </Field>
          <Checkbox
            checked={rec.grayscale === true}
            onChange={(e) => patch({ grayscale: e.target.checked || undefined })}
            disabled={disabled}
          >
            grayscale（按亮度匹配）
          </Checkbox>
        </>
      )}

      {rec.type === "feature" && (
        <Space>
          <Field label="min_matches（默认 4）">
            <InputNumber
              size="small"
              min={1}
              value={rec.min_matches}
              onChange={(v) => patch({ min_matches: v ?? undefined })}
              disabled={disabled}
            />
          </Field>
          <Field label="ratio（(0,1]，默认 0.75）">
            <InputNumber
              size="small"
              min={0.05}
              max={1}
              step={0.05}
              value={rec.ratio}
              onChange={(v) => patch({ ratio: v ?? undefined })}
              disabled={disabled}
            />
          </Field>
        </Space>
      )}

      {isYolo && (
        <>
          <Field label="label（类别过滤，留空 = 任意类）">
            <AutoComplete
              size="small"
              style={{ width: "100%" }}
              value={rec.label ?? ""}
              onChange={(v) => patch({ label: v || undefined })}
              options={Object.values(yoloQuery.data?.classes ?? {}).map((c) => ({
                value: c,
              }))}
              disabled={disabled}
            />
          </Field>
          <Space>
            <Field label="model（命名模型）">
              <AutoComplete
                size="small"
                style={{ width: 140 }}
                value={rec.model ?? ""}
                onChange={(v) => patch({ model: v || undefined })}
                options={(yoloQuery.data?.models ?? []).map((m) => ({ value: m }))}
                disabled={disabled}
              />
            </Field>
            <Field label="conf（默认 0.25）">
              <InputNumber
                size="small"
                min={0}
                max={1}
                step={0.05}
                value={rec.conf}
                onChange={(v) => patch({ conf: v ?? undefined })}
                disabled={disabled}
              />
            </Field>
          </Space>
        </>
      )}

      {rec.type !== "always" && rec.type !== "and" && rec.type !== "or" && (
        <Field label="roi（搜索范围 [x1,y1,x2,y2]，留空 = 全屏）">
          <RoiField
            value={rec.roi}
            onChange={(roi) => patch({ roi })}
            disabled={disabled}
            probe={
              rec.type === "template" || rec.type === "feature"
                ? { kind: "template", template: rec.template, threshold: rec.threshold }
                : { kind: "ocr" }
            }
          />
        </Field>
      )}

      {(rec.type === "and" || rec.type === "or") && (
        <>
          {uiTextSubCount >= 2 && (
            <Alert
              type="warning"
              showIcon
              message={`${uiTextSubCount} 个 ui_text 子识别 —— 每个都触发一次 uiautomator dump（真机约 4.3s/次）`}
            />
          )}
          {subs.map((sub, i) => (
            <div
              key={i}
              style={{
                border: "1px solid #f0f0f0",
                borderRadius: 6,
                padding: 8,
                position: "relative",
              }}
            >
              <Space
                style={{ width: "100%", justifyContent: "space-between", marginBottom: 4 }}
              >
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  {listKey}[{i}]
                </Typography.Text>
                <Button
                  size="small"
                  type="text"
                  danger
                  icon={<DeleteOutlined />}
                  disabled={disabled}
                  onClick={() => {
                    const next = subs.filter((_, j) => j !== i);
                    patch({ [listKey]: next } as Partial<Recognition>);
                  }}
                />
              </Space>
              <RecognitionForm
                value={sub}
                onChange={(s) => {
                  const next = subs.map((old, j) => (j === i ? s : old));
                  patch({ [listKey]: next } as Partial<Recognition>);
                }}
                depth={depth + 1}
                allowAlways={false}
                disabled={disabled}
              />
            </div>
          ))}
          <Button
            size="small"
            icon={<PlusOutlined />}
            disabled={disabled}
            onClick={() =>
              patch({
                [listKey]: [...subs, { type: "ocr", expected: "" }],
              } as Partial<Recognition>)
            }
          >
            添加子识别
          </Button>
          {rec.type === "and" && subs.length > 1 && (
            <Field label="box_index（点击哪个子识别的命中框，默认 0）">
              <InputNumber
                size="small"
                min={0}
                max={subs.length - 1}
                value={rec.box_index}
                onChange={(v) => patch({ box_index: v ?? undefined })}
                disabled={disabled}
              />
            </Field>
          )}
        </>
      )}
    </Space>
  );
}

export function Field({
  label,
  children,
}: {
  /** ReactNode 而非 string：带说明的字段会把 label 包进 Tooltip（见 CustomParamsForm）。 */
  label: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        {label}
      </Typography.Text>
      <div style={{ marginTop: 2 }}>{children}</div>
    </div>
  );
}
