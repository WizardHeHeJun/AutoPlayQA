import {
  BaseEdge,
  EdgeLabelRenderer,
  getBezierPath,
  type EdgeProps,
} from "@xyflow/react";

/** next 边：实线；同源多条时渲染优先级序号（顺序即识别优先级）。 */
export function NextEdge(props: EdgeProps) {
  const [path, labelX, labelY] = getBezierPath(props);
  const data = props.data as { order?: number; total?: number } | undefined;
  const showOrder = (data?.total ?? 1) > 1;
  return (
    <>
      <BaseEdge
        id={props.id}
        path={path}
        style={{ stroke: props.selected ? "#1677ff" : "#98a2b3", strokeWidth: 1.6 }}
        markerEnd={props.markerEnd}
      />
      {showOrder && (
        <EdgeLabelRenderer>
          <div
            className="pe-edge-order nodrag nopan"
            style={{
              position: "absolute",
              transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)`,
            }}
          >
            {(data?.order ?? 0) + 1}
          </div>
        </EdgeLabelRenderer>
      )}
    </>
  );
}

/** on_timeout 边：橙色虚线。 */
export function TimeoutEdge(props: EdgeProps) {
  const [path] = getBezierPath(props);
  return (
    <BaseEdge
      id={props.id}
      path={path}
      style={{ stroke: "#fa8c16", strokeWidth: 1.4, strokeDasharray: "6 4" }}
      markerEnd={props.markerEnd}
    />
  );
}

/** 全局跳转边（on_finding / watchdog skip_to）：灰色点划线，默认隐藏。 */
export function JumpEdge(props: EdgeProps) {
  const [path, labelX, labelY] = getBezierPath(props);
  const data = props.data as { label?: string } | undefined;
  return (
    <>
      <BaseEdge
        id={props.id}
        path={path}
        style={{ stroke: "#722ed1", strokeWidth: 1.2, strokeDasharray: "2 4" }}
        markerEnd={props.markerEnd}
      />
      {data?.label && (
        <EdgeLabelRenderer>
          <div
            className="nodrag nopan"
            style={{
              position: "absolute",
              transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)`,
              fontSize: 10,
              color: "#722ed1",
              background: "rgba(255,255,255,0.85)",
              padding: "0 4px",
              borderRadius: 3,
            }}
          >
            {data.label}
          </div>
        </EdgeLabelRenderer>
      )}
    </>
  );
}

export const edgeTypes = {
  nextEdge: NextEdge,
  timeoutEdge: TimeoutEdge,
  jumpEdge: JumpEdge,
};
