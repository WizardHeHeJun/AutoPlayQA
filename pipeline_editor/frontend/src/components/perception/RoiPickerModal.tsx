/** 截图拉框：全分辨率底图 + 鼠标框选 → 设备像素 ROI。
 * 附带"试识别"：OCR 读框内文字 / 模板试匹配，结果框叠加显示（所见即所得调参）。
 */
import { useMutation, useQuery } from "@tanstack/react-query";
import { Alert, Button, Modal, Select, Space, Spin, Tag, Typography } from "antd";
import { useCallback, useEffect, useRef, useState } from "react";

import { metaApi, perceptionApi } from "../../api/endpoints";
import { useUiStore } from "../../store/uiStore";
import type { Roi } from "../../types/task";

interface Props {
  initial?: Roi;
  probe?: { kind: "ocr" } | { kind: "template"; template?: string; threshold?: number };
  onOk: (roi: Roi) => void;
  onCancel: () => void;
}

interface Overlay {
  bbox: number[];
  label: string;
}

export function RoiPickerModal({ initial, probe, onOk, onCancel }: Props) {
  const deviceId = useUiStore((s) => s.deviceId);
  const setDeviceId = useUiStore((s) => s.setDeviceId);
  const devicesQuery = useQuery({ queryKey: ["devices"], queryFn: metaApi.devices });

  const [roi, setRoi] = useState<Roi | undefined>(initial);
  const [overlays, setOverlays] = useState<Overlay[]>([]);
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
        setOverlays([]);
        draw();
      };
      img.src = `data:image/png;base64,${data.image_base64}`;
    },
  });

  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    const img = imgRef.current;
    if (!canvas || !img) return;
    const maxW = 520;
    const maxH = 560;
    const s = Math.min(maxW / img.width, maxH / img.height, 1);
    setScale(s);
    canvas.width = Math.round(img.width * s);
    canvas.height = Math.round(img.height * s);
    const ctx = canvas.getContext("2d")!;
    ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
    if (roi) {
      ctx.strokeStyle = "#1677ff";
      ctx.lineWidth = 2;
      ctx.strokeRect(roi[0] * s, roi[1] * s, (roi[2] - roi[0]) * s, (roi[3] - roi[1]) * s);
    }
    for (const o of overlays) {
      ctx.strokeStyle = "#52c41a";
      ctx.lineWidth = 1.5;
      ctx.strokeRect(
        o.bbox[0] * s,
        o.bbox[1] * s,
        (o.bbox[2] - o.bbox[0]) * s,
        (o.bbox[3] - o.bbox[1]) * s,
      );
      ctx.fillStyle = "rgba(82,196,26,0.9)";
      ctx.font = "11px sans-serif";
      ctx.fillText(o.label, o.bbox[0] * s, Math.max(10, o.bbox[1] * s - 3));
    }
  }, [roi, overlays]);

  useEffect(() => draw(), [draw]);
  useEffect(() => {
    if (deviceId) shotMutation.mutate();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [deviceId]);

  const toDevice = (e: React.MouseEvent): [number, number] => {
    const rect = canvasRef.current!.getBoundingClientRect();
    return [
      Math.round((e.clientX - rect.left) / scale),
      Math.round((e.clientY - rect.top) / scale),
    ];
  };

  const probeMutation = useMutation({
    mutationFn: async () => {
      if (!probe || !deviceId) return [];
      if (probe.kind === "ocr") {
        const items = await perceptionApi.ocr(deviceId, roi);
        return items.map((it) => ({
          bbox: it.bbox,
          label: `${it.text} (${it.score.toFixed(2)})`,
        }));
      }
      if (!probe.template) return [];
      const r = await perceptionApi.findTemplate(deviceId, probe.template, {
        threshold: probe.threshold,
        roi,
        multi: true,
      });
      return r.matches.map((m) => ({
        bbox: m.bbox,
        label: `${probe.template} (${m.score.toFixed(2)})`,
      }));
    },
    onSuccess: (list) => setOverlays(list),
  });

  return (
    <Modal
      open
      title="截图框选 ROI（设备原始像素）"
      width={620}
      onCancel={onCancel}
      footer={[
        <Button key="reshot" onClick={() => shotMutation.mutate()} disabled={!deviceId}>
          重新截图
        </Button>,
        probe && (
          <Button
            key="probe"
            onClick={() => probeMutation.mutate()}
            loading={probeMutation.isPending}
            disabled={!deviceId || !imgRef.current}
          >
            {probe.kind === "ocr" ? "OCR 试读" : "模板试匹配"}
          </Button>
        ),
        <Button key="ok" type="primary" disabled={!roi} onClick={() => roi && onOk(roi)}>
          使用此 ROI
        </Button>,
      ]}
    >
      <Space direction="vertical" style={{ width: "100%" }}>
        <Space>
          <span>设备:</span>
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
          {roi && <Tag color="blue">{`[${roi.join(", ")}]`}</Tag>}
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
              setRoi([
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
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          在图上按住拖动画框；坐标为设备原始像素。
        </Typography.Text>
      </Space>
    </Modal>
  );
}
