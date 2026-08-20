import { AimOutlined } from "@ant-design/icons";
import { Button, InputNumber, Space, Tooltip } from "antd";
import { useState } from "react";

import type { Roi } from "../../types/task";
import { RoiPickerModal } from "../perception/RoiPickerModal";

interface Props {
  value?: Roi;
  onChange: (roi: Roi | undefined) => void;
  /** 试识别叠加：ocr（读文字）或 template（试匹配某模板） */
  probe?: { kind: "ocr" } | { kind: "template"; template?: string; threshold?: number };
  disabled?: boolean;
}

export function RoiField({ value, onChange, probe, disabled }: Props) {
  const [pickerOpen, setPickerOpen] = useState(false);
  const set = (i: number, v: number | null) => {
    const roi: Roi = [...(value ?? [0, 0, 0, 0])] as Roi;
    roi[i] = v ?? 0;
    onChange(roi);
  };
  return (
    <Space.Compact block>
      {[0, 1, 2, 3].map((i) => (
        <InputNumber
          key={i}
          size="small"
          placeholder={["x1", "y1", "x2", "y2"][i]}
          value={value?.[i]}
          onChange={(v) => set(i, v)}
          style={{ width: "22%" }}
          controls={false}
          disabled={disabled}
        />
      ))}
      <Tooltip title="在截图上框选">
        <Button
          size="small"
          icon={<AimOutlined />}
          disabled={disabled}
          onClick={() => setPickerOpen(true)}
        />
      </Tooltip>
      {value && (
        <Button size="small" disabled={disabled} onClick={() => onChange(undefined)}>
          清除
        </Button>
      )}
      {pickerOpen && (
        <RoiPickerModal
          initial={value}
          probe={probe}
          onOk={(roi) => {
            onChange(roi);
            setPickerOpen(false);
          }}
          onCancel={() => setPickerOpen(false)}
        />
      )}
    </Space.Compact>
  );
}
