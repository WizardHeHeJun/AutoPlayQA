/** 纯 UI 态：选中 / 右侧 tab / 图层开关 / 重排请求。不进 undo，不落 doc。 */
import { create } from "zustand";

import { useRunStore } from "./runStore";

export type RightTab = "node" | "task" | "problems" | "run";

export interface UiState {
  selectedNode: string | null;
  rightTab: RightTab;
  showJumpEdges: boolean;
  /** 画布节点卡片显示分组参数行（详情模式），关掉则回到两行摘要 */
  showNodeDetails: boolean;
  followRun: boolean;
  /** 感知工具与运行共用的当前设备 */
  deviceId: string | null;
  /** 校验错误定位：ProblemsPanel 点击后设置，画布红描边 */
  errorNode: string | null;
  errorMessage: string | null;
  /**
   * 「自动布局」请求计数器。工具条只自增它，真正的重排在 FlowCanvas 里做——
   * 只有 ReactFlow context 内部才拿得到节点的实测尺寸（`node.measured`）。
   */
  relayoutTick: number;

  select: (node: string | null) => void;
  setRightTab: (tab: RightTab) => void;
  requestRelayout: () => void;
  toggleJumpEdges: () => void;
  setShowNodeDetails: (v: boolean) => void;
  setFollowRun: (v: boolean) => void;
  setDeviceId: (id: string | null) => void;
  setError: (node: string | null, message: string | null) => void;
}

export const useUiStore = create<UiState>()((set) => ({
  selectedNode: null,
  rightTab: "node",
  showJumpEdges: false,
  showNodeDetails: true,
  followRun: true,
  deviceId: null,
  errorNode: null,
  errorMessage: null,
  relayoutTick: 0,

  /**
   * 选中节点顺带把右侧面板切到「节点」属性页——参数在哪改是真机反馈里最难发现的一环。
   * 两个例外：已经在「节点」页就不动（避免无谓 set）；运行中且面板停在「运行」页时
   * 不抢焦点（用户正在跟 run，切走会打断）。
   */
  select: (node) =>
    set((s) => {
      if (!node) return { selectedNode: null };
      const following = s.rightTab === "run" && useRunStore.getState().status === "running";
      if (s.rightTab === "node" || following) return { selectedNode: node };
      return { selectedNode: node, rightTab: "node" };
    }),
  setRightTab: (tab) => set({ rightTab: tab }),
  requestRelayout: () => set((s) => ({ relayoutTick: s.relayoutTick + 1 })),
  toggleJumpEdges: () => set((s) => ({ showJumpEdges: !s.showJumpEdges })),
  setShowNodeDetails: (v) => set({ showNodeDetails: v }),
  setFollowRun: (v) => set({ followRun: v }),
  setDeviceId: (id) => set({ deviceId: id }),
  setError: (node, message) => set({ errorNode: node, errorMessage: message }),
}));
