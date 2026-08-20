import { defineConfig } from 'vitepress'
import { withMermaid } from 'vitepress-plugin-mermaid'

// withMermaid 包一层即可：它注册 ```mermaid 代码围栏的 markdown 规则、
// 注入 Mermaid 组件（客户端渲染成 SVG），并按 <html> 上的 .dark 自动在
// mermaid 的 default / dark 主题间切换——所以这里不写死任何配色。
export default withMermaid(
  defineConfig({
    lang: 'zh-CN',
    title: 'PipelineEditor',
    description: 'AutoPlayQA 的 Web 可视化任务编排器',
    base: process.env.DOCS_BASE || '/',

    themeConfig: {
      nav: [
        { text: '指南', link: '/guide/intro' },
        { text: '编辑器', link: '/editor/concepts' },
        { text: '参考', link: '/reference/architecture' },
      ],

      sidebar: [
        {
          text: '指南',
          items: [
            { text: '是什么', link: '/guide/intro' },
            { text: '快速上手', link: '/guide/quick-start' },
          ],
        },
        {
          text: '编辑器',
          items: [
            { text: '核心概念', link: '/editor/concepts' },
            { text: '看懂画布', link: '/editor/canvas' },
            { text: '编排：连线、删边、增删节点', link: '/editor/editing' },
            { text: '属性面板', link: '/editor/inspector' },
            { text: '校验、lint 与保存', link: '/editor/validate-save' },
            { text: '运行与调试', link: '/editor/run' },
            { text: '套件与报告', link: '/editor/suites-reports' },
            { text: 'AI / MCP 协同编辑', link: '/editor/ai-mcp' },
            { text: '工具页', link: '/editor/tools' },
          ],
        },
        {
          text: '参考',
          items: [
            { text: '架构与数据流', link: '/reference/architecture' },
            { text: '测试与上游改动', link: '/reference/testing' },
          ],
        },
      ],

      search: {
        provider: 'local',
      },

      outline: { level: [2, 3], label: '本页目录' },
      docFooter: { prev: '上一页', next: '下一页' },
      returnToTopLabel: '回到顶部',
      sidebarMenuLabel: '菜单',
      darkModeSwitchLabel: '主题',
      lightModeSwitchTitle: '切换到浅色模式',
      darkModeSwitchTitle: '切换到深色模式',
    },
  })
)
