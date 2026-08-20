/** 截图裁剪保存为新模板（POST /api/capture-template）。 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App as AntApp, Button, Input, Modal, Select, Space, Spin } from "antd";
import { useEffect, useRef, useState } from "react";

import { metaApi, perceptionApi } from "../../api/endpoints";
import { useUiStore } from "../../store/uiStore";
import type { Roi } from "../../types/task";

interface Props {
  onSaved: (templateName: string) => void;
  onCancel: () => void;
}

export function TemplateCropModal({ onSaved, onCancel }: Props) {
  const { message } = AntApp.useApp();
  const queryClient = useQueryClient();
  const deviceId = useUiStore((s) => s.deviceId);
  const setDeviceId = useUiStore((s) => s.setDeviceId);
  const devicesQuery = useQuery({ queryKey: ["devices"], queryFn: metaApi.devices });

  const [region, setRegion] = useState<Roi | undefined>();
  const [name, setName] = useState("");
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const imgRef = useRef<HTMLImageElement | null>(null);
  const dragRef = useRef<{ x: number; y: number } | null>(null);
  const [scale, setScale] = useState(1);

  const shotMutation = useMutation({
    mutationFn: () => perceptionApi.screenshot(deviceId!),
    onSuccess: (data) => {
      const img = new Image();
      img.onload = () => {
        imgRef.current = img;
        draw(region);
      };
      img.src = `data:image/png;base64,${data.image_base64}`;
    },
  });

  const draw = (r?: Roi) => {
    const canvas = canvasRef.current;
    const img = imgRef.current;
    if (!canvas || !img) return;
    const s = Math.min(520 / img.width, 560 / img.height, 1);
    setScale(s);
    canvas.width = Math.round(img.width * s);
    canvas.height = Math.round(img.height * s);
    const ctx = canvas.getContext("2d")!;
    ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
    if (r) {
      ctx.strokeStyle = "#fa541c";
      ctx.lineWidth = 2;
      ctx.strokeRect(r[0] * s, r[1] * s, (r[2] - r[0]) * s, (r[3] - r[1]) * s);
    }
  };

  useEffect(() => draw(region), [region]);
  useEffect(() => {
    if (deviceId) shotMutation.mutate();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [deviceId]);

  const saveMutation = useMutation({
    mutationFn: () => perceptionApi.captureTemplate(deviceId!, name, region!),
    onSuccess: (r) => {
      queryClient.invalidateQueries({ queryKey: ["templates"] });
      message.success(`模板已保存: ${r.name}`);
      onSaved(r.name);
    },
    onError: (e: Error) => message.error(e.message),
  });

  const toDevice = (e: React.MouseEvent): [number, number] => {
    const rect = canvasRef.current!.getBoundingClientRect();
    return [
      Math.round((e.clientX - rect.left) / scale),
      Math.round((e.clientY - rect.top) / scale),
    ];
  };

  return (
    <Modal
      open
      title="从截图裁剪新模板"
      width={620}
      onCancel={onCancel}
      footer={[
        <Button key="reshot" onClick={() => shotMutation.mutate()} disabled={!deviceId}>
          重新截图
        </Button>,
        <Button
          key="save"
          type="primary"
          disabled={!region || !name || !deviceId}
          loading={saveMutation.isPending}
          onClick={() => saveMutation.mutate()}
        >
          保存模板
        </Button>,
      ]}
    >
      <Space direction="vertical" style={{ width: "100%" }}>
        <Space>
          <Select
            size="small"
            style={{ minWidth: 220 }}
            placeholder="选择设备"
            value={deviceId}
            onChange={setDeviceId}
            options={(devicesQuery.data ?? []).map((d) => ({
              value: d.device_id,
              label: `${d.device_id} (${d.model})`,
            }))}
            loading={devicesQuery.isLoading}
          />
          <Input
            size="small"
            placeholder="模板名（文件 stem）"
            value={name}
            onChange={(e) => setName(e.target.value)}
            style={{ width: 200 }}
          />
        </Space>
        {!deviceId && <Alert type="info" message="选择设备后自动截图" showIcon />}
        {shotMutation.isError && (
          <Alert type="error" message={(shotMutation.error as Error).message} showIcon />
        )}
        <Spin spinning={shotMutation.isPending}>
          <canvas
            ref={canvasRef}
            style={{ cursor: "crosshair", border: "1px solid #d9d9d9", maxWidth: "100%" }}
            onMouseDown={(e) => {
              dragRef.current = { x: toDevice(e)[0], y: toDevice(e)[1] };
            }}
            onMouseMove={(e) => {
              if (!dragRef.current) return;
              const [x, y] = toDevice(e);
              const s = dragRef.current;
              setRegion([
                Math.min(s.x, x),
                Math.min(s.y, y),
                Math.max(s.x, x),
                Math.max(s.y, y),
              ]);
            }}
            onMouseUp={() => {
              dragRef.current = null;
            }}
          />
        </Spin>
      </Space>
    </Modal>
  );
}
