import {
  AppstoreOutlined,
  FileTextOutlined,
  PartitionOutlined,
  ToolOutlined,
} from "@ant-design/icons";
import { Layout, Menu } from "antd";
import { Link, Route, Routes, useLocation } from "react-router-dom";

import { EditorPage } from "./pages/EditorPage";
import { ReportsPage } from "./pages/ReportsPage";
import { SuitesPage } from "./pages/SuitesPage";
import { TaskListPage } from "./pages/TaskListPage";
import { ToolsPage } from "./pages/ToolsPage";

const { Header, Content } = Layout;

export function App() {
  const location = useLocation();
  const selected = location.pathname.startsWith("/suites")
    ? "suites"
    : location.pathname.startsWith("/reports")
      ? "reports"
      : location.pathname.startsWith("/tools")
        ? "tools"
        : "tasks";

  return (
    <Layout style={{ height: "100vh" }}>
      <Header
        style={{
          display: "flex",
          alignItems: "center",
          gap: 24,
          paddingInline: 24,
          height: 48,
          lineHeight: "48px",
        }}
      >
        <span style={{ color: "#fff", fontWeight: 700, fontSize: 15 }}>
          PipelineEditor
        </span>
        <Menu
          theme="dark"
          mode="horizontal"
          selectedKeys={[selected]}
          style={{ flex: 1, minWidth: 0, height: 48, lineHeight: "48px" }}
          items={[
            {
              key: "tasks",
              icon: <PartitionOutlined />,
              label: <Link to="/">任务</Link>,
            },
            {
              key: "suites",
              icon: <AppstoreOutlined />,
              label: <Link to="/suites">套件</Link>,
            },
            {
              key: "reports",
              icon: <FileTextOutlined />,
              label: <Link to="/reports">报告</Link>,
            },
            {
              key: "tools",
              icon: <ToolOutlined />,
              label: <Link to="/tools">工具</Link>,
            },
          ]}
        />
      </Header>
      <Content style={{ overflow: "hidden" }}>
        <Routes>
          <Route path="/" element={<TaskListPage />} />
          <Route path="/tasks/:name" element={<EditorPage />} />
          <Route path="/suites" element={<SuitesPage />} />
          <Route path="/suites/:name" element={<SuitesPage />} />
          <Route path="/reports" element={<ReportsPage />} />
          <Route path="/tools" element={<ToolsPage />} />
        </Routes>
      </Content>
    </Layout>
  );
}
