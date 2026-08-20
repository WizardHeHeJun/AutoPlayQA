import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App as AntdApp, ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import "@xyflow/react/dist/style.css";
import "antd/dist/reset.css";
import "./styles/canvas.css";

import { App } from "./App";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, refetchOnWindowFocus: false, staleTime: 30_000 },
  },
});

createRoot(document.getElementById("root")!).render(
  <ConfigProvider locale={zhCN}>
    <AntdApp style={{ height: "100%" }}>
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          <App />
        </BrowserRouter>
      </QueryClientProvider>
    </AntdApp>
  </ConfigProvider>,
);
