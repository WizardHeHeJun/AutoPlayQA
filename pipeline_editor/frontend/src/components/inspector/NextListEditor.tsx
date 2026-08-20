/** next 有序列表：顺序即识别优先级。dnd 拖拽排序 + 上下移按钮兜底。 */
import {
  ArrowDownOutlined,
  ArrowUpOutlined,
  DeleteOutlined,
  HolderOutlined,
  PlusOutlined,
} from "@ant-design/icons";
import { DndContext, closestCenter, type DragEndEvent } from "@dnd-kit/core";
import {
  SortableContext,
  useSortable,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { Button, Empty, Select, Space, Typography } from "antd";
import { useState } from "react";

interface Props {
  value: string[];
  nodeNames: string[]; // 可选目标（全部节点）
  onReorder: (from: number, to: number) => void;
  onRemove: (target: string, index: number) => void;
  onAdd: (target: string) => void;
  onLocate?: (target: string) => void;
  disabled?: boolean;
}

function SortableItem({
  id,
  index,
  total,
  onRemove,
  onMove,
  onLocate,
  disabled,
}: {
  id: string;
  index: number;
  total: number;
  onRemove: () => void;
  onMove: (dir: -1 | 1) => void;
  onLocate?: () => void;
  disabled?: boolean;
}) {
  const { attributes, listeners, setNodeRef, transform, transition } = useSortable({
    id: `${index}:${id}`,
    disabled,
  });
  return (
    <div
      ref={setNodeRef}
      style={{
        transform: CSS.Transform.toString(transform),
        transition,
        display: "flex",
        alignItems: "center",
        gap: 6,
        padding: "4px 6px",
        border: "1px solid #f0f0f0",
        borderRadius: 6,
        background: "#fff",
      }}
    >
      <span {...attributes} {...listeners} style={{ cursor: disabled ? "default" : "grab" }}>
        <HolderOutlined style={{ color: "#98a2b3" }} />
      </span>
      <span className="pe-edge-order">{index + 1}</span>
      <Typography.Text
        style={{ flex: 1, cursor: onLocate ? "pointer" : "default" }}
        ellipsis={{ tooltip: id }}
        onClick={onLocate}
      >
        {id}
      </Typography.Text>
      <Button size="small" type="text" icon={<ArrowUpOutlined />} disabled={disabled || index === 0}
        onClick={() => onMove(-1)} />
      <Button size="small" type="text" icon={<ArrowDownOutlined />}
        disabled={disabled || index === total - 1} onClick={() => onMove(1)} />
      <Button size="small" type="text" danger icon={<DeleteOutlined />} disabled={disabled}
        onClick={onRemove} />
    </div>
  );
}

export function NextListEditor({
  value,
  nodeNames,
  onReorder,
  onRemove,
  onAdd,
  onLocate,
  disabled,
}: Props) {
  const [adding, setAdding] = useState(false);

  const handleDragEnd = (e: DragEndEvent) => {
    if (!e.over || e.active.id === e.over.id) return;
    const from = Number(String(e.active.id).split(":")[0]);
    const to = Number(String(e.over.id).split(":")[0]);
    if (!Number.isNaN(from) && !Number.isNaN(to)) onReorder(from, to);
  };

  return (
    <Space direction="vertical" style={{ width: "100%" }} size={4}>
      {value.length === 0 && (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="next 为空 = 任务成功终点"
          style={{ margin: "4px 0" }}
        />
      )}
      <DndContext collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
        <SortableContext
          items={value.map((v, i) => `${i}:${v}`)}
          strategy={verticalListSortingStrategy}
        >
          {value.map((target, i) => (
            <SortableItem
              key={`${i}:${target}`}
              id={target}
              index={i}
              total={value.length}
              disabled={disabled}
              onRemove={() => onRemove(target, i)}
              onMove={(dir) => onReorder(i, i + dir)}
              onLocate={onLocate ? () => onLocate(target) : undefined}
            />
          ))}
        </SortableContext>
      </DndContext>
      {adding ? (
        <Select
          size="small"
          autoFocus
          showSearch
          style={{ width: "100%" }}
          placeholder="选择目标节点"
          options={nodeNames.map((n) => ({ value: n }))}
          onChange={(v) => {
            onAdd(v);
            setAdding(false);
          }}
          onBlur={() => setAdding(false)}
        />
      ) : (
        <Button size="small" icon={<PlusOutlined />} disabled={disabled}
          onClick={() => setAdding(true)}>
          添加候选
        </Button>
      )}
    </Space>
  );
}
