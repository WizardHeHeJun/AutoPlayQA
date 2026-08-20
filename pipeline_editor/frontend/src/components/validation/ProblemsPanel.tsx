/** 问题面板：后端校验错误 + 服务端 lint（W001-W006）+ 前端本地提示，点击定位节点。 */
import { Empty, List, Tag, Typography } from "antd";
import { useMemo } from "react";

import { localLint } from "../../graph/localLint";
import { useEditorStore } from "../../store/editorStore";
import { useUiStore } from "../../store/uiStore";
import type { LintWarning, StructuredError } from "../../types/api";

interface Props {
  validateError: StructuredError | null;
  lintWarnings: LintWarning[];
}

interface Problem {
  key: string;
  level: "error" | "warning";
  node: string | null;
  tag: string;
  message: string;
  suggestion?: string;
}

export function ProblemsPanel({ validateError, lintWarnings }: Props) {
  const doc = useEditorStore((s) => s.doc);
  const select = useUiStore((s) => s.select);
  const setError = useUiStore((s) => s.setError);

  const problems = useMemo<Problem[]>(() => {
    const out: Problem[] = [];
    if (validateError) {
      out.push({
        key: "validate",
        level: "error",
        node: validateError.node,
        tag: "校验",
        message: validateError.message,
      });
    }
    for (const [i, w] of lintWarnings.entries()) {
      out.push({
        key: `lint-${i}`,
        level: "warning",
        node: w.node,
        tag: w.rule_id,
        message: w.message,
        suggestion: w.suggestion,
      });
    }
    if (doc) {
      for (const [i, w] of localLint(doc).entries()) {
        out.push({
          key: `local-${i}`,
          level: w.level,
          node: w.node,
          tag: "本地",
          message: w.message,
        });
      }
    }
    return out.sort((a, b) => (a.level === b.level ? 0 : a.level === "error" ? -1 : 1));
  }, [validateError, lintWarnings, doc]);

  if (problems.length === 0) {
    return <Empty description="没有问题 ✓" style={{ marginTop: 48 }} />;
  }

  return (
    <List
      size="small"
      style={{ overflow: "auto", height: "100%", padding: "4px 8px" }}
      dataSource={problems}
      renderItem={(p) => (
        <List.Item
          style={{ cursor: p.node ? "pointer" : "default", alignItems: "flex-start" }}
          onClick={() => {
            if (p.node) {
              select(p.node);
              setError(p.node, p.level === "error" ? p.message : null);
            }
          }}
        >
          <List.Item.Meta
            avatar={
              <Tag color={p.level === "error" ? "red" : "orange"} style={{ marginTop: 2 }}>
                {p.tag}
              </Tag>
            }
            title={
              <Typography.Text style={{ fontSize: 12 }}>
                {p.node && <Tag>{p.node}</Tag>}
                {p.message}
              </Typography.Text>
            }
            description={
              p.suggestion && (
                <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                  {p.suggestion}
                </Typography.Text>
              )
            }
          />
        </List.Item>
      )}
    />
  );
}
