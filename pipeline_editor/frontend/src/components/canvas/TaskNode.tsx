import { LockOutlined } from "@ant-design/icons";
import { Handle, Position, type NodeProps } from "@xyflow/react";
import { Tooltip } from "antd";
import { Fragment, memo } from "react";

import type { TaskFlowNode } from "../../graph/taskToGraph";

function stepBadgeClass(step?: string): string {
  if (!step || step === "?") return "pe-step-badge unreachable";
  return step.includes(".") ? "pe-step-badge branch" : "pe-step-badge";
}

/** 参数在哪改的零成本引导：挂在分组小节标题的原生 title 上，不占卡片空间 */
const EDIT_HINT = "单击节点后，在右侧「节点」属性面板里编辑这些参数";

export const TaskNode = memo(function TaskNode({ data, selected }: NodeProps<TaskFlowNode>) {
  const classes = ["pe-node"];
  if (selected) classes.push("selected");
  if (data.readonly) classes.push("readonly");
  if (data.errorMessage) classes.push("has-error");
  else if (data.warnings.length > 0) classes.push("has-warning");
  if (data.runState === "current") classes.push("run-current");
  else if (data.runState === "visited") classes.push("run-visited");

  return (
    <div className={classes.join(" ")}>
      <Handle type="target" position={Position.Top} className="pe-handle-target" />
      <div className="pe-node-header">
        {data.step !== undefined && (
          <span className={stepBadgeClass(data.step)}>{data.step}</span>
        )}
        <Tooltip title={data.name}>
          <span className="pe-node-title">{data.name}</span>
        </Tooltip>
        {data.readonly && (
          <Tooltip title={`来自 include: ${data.includeFrom ?? ""}（只读）`}>
            <LockOutlined style={{ color: "#98a2b3" }} />
          </Tooltip>
        )}
      </div>
      <div className="pe-node-body">
        {data.showDetails && data.details.length > 0 ? (
          data.details.map((section) => (
            <div key={section.title} className={`pe-node-section ${section.kind}`}>
              <div className="pe-section-title" title={EDIT_HINT}>
                {section.title}
              </div>
              <div className="pe-detail-rows">
                {section.rows.map((row, i) => {
                  const label = <span className="pe-detail-label">{row.label}</span>;
                  return (
                    <Fragment key={`${row.label}:${i}`}>
                      {/* 有中文说明才包 Tooltip，避免给一堆行挂空浮层 */}
                      {row.hint ? <Tooltip title={row.hint}>{label}</Tooltip> : label}
                      <Tooltip title={row.full ?? row.value}>
                        <span className="pe-detail-value">{row.value}</span>
                      </Tooltip>
                    </Fragment>
                  );
                })}
              </div>
            </div>
          ))
        ) : (
          <>
            <div className="pe-node-line">👁 {data.recSummary}</div>
            <div className="pe-node-line">⚡ {data.actSummary}</div>
          </>
        )}
      </div>
      {data.showDetails && data.nextTargets.length > 0 && (
        <Tooltip title={data.nextTargets.join(", ")}>
          <div className="pe-node-footer">next → {data.nextTargets.join(", ")}</div>
        </Tooltip>
      )}
      <div className="pe-node-badges">
        {data.isEntry && <span className="pe-badge entry">入口</span>}
        {data.isTerminal && <span className="pe-badge terminal">终点 ✓</span>}
        {data.hasOnTimeout && <span className="pe-badge">⏱ 超时兜底</span>}
        {data.hasFinding && (
          <span className={`pe-badge finding-${data.findingSeverity ?? "error"}`}>
            🚩 finding
          </span>
        )}
        {data.warnings.length > 0 && (
          <Tooltip title={data.warnings.join("\n")}>
            <span className="pe-badge warn">⚠ {data.warnings.length}</span>
          </Tooltip>
        )}
        {data.errorMessage && (
          <Tooltip title={data.errorMessage}>
            <span className="pe-badge finding-error">✗ 校验错误</span>
          </Tooltip>
        )}
      </div>
      <Handle
        type="source"
        position={Position.Bottom}
        id="next"
        className="pe-handle-next"
      />
      <Handle
        type="source"
        position={Position.Right}
        id="timeout"
        className="pe-handle-timeout"
      />
    </div>
  );
});
