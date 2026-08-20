import { ScissorOutlined } from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { Button, Select, Space, Tooltip } from "antd";
import { useState } from "react";

import { metaApi } from "../../api/endpoints";
import { TemplateCropModal } from "../perception/TemplateCropModal";

interface Props {
  value?: string;
  onChange: (name: string | undefined) => void;
  disabled?: boolean;
}

export function TemplateField({ value, onChange, disabled }: Props) {
  const [cropOpen, setCropOpen] = useState(false);
  const templatesQuery = useQuery({
    queryKey: ["templates"],
    queryFn: metaApi.templates,
  });

  return (
    <Space.Compact block>
      <Select
        size="small"
        showSearch
        allowClear
        placeholder="选择模板"
        value={value || undefined}
        onChange={(v) => onChange(v)}
        style={{ flex: 1 }}
        disabled={disabled}
        loading={templatesQuery.isLoading}
        optionLabelProp="value"
        options={(templatesQuery.data ?? []).map((t) => ({
          value: t.name,
          label: (
            <Space>
              <img
                src={metaApi.templateImageUrl(t.name)}
                alt={t.name}
                style={{ height: 28, maxWidth: 56, objectFit: "contain" }}
              />
              <span>{t.name}</span>
            </Space>
          ),
        }))}
      />
      <Tooltip title="从截图裁剪新模板">
        <Button
          size="small"
          icon={<ScissorOutlined />}
          disabled={disabled}
          onClick={() => setCropOpen(true)}
        />
      </Tooltip>
      {cropOpen && (
        <TemplateCropModal
          onSaved={(name) => {
            onChange(name);
            setCropOpen(false);
          }}
          onCancel={() => setCropOpen(false)}
        />
      )}
    </Space.Compact>
  );
}
