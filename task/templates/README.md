# 模板图库（OpenCV 模板匹配）

这里存放**视觉内容的模板图**（PNG/JPG），供 OpenCV `matchTemplate` 在游戏画面上定位
那些**文字通道看不见的图形**——单 Surface 渲染时 uiautomator 没有节点、OCR 只能读文字标签，
无文字的图标 / 按钮 / 场景贴图只能靠模板匹配认出来。

> 图库内容是**接入方资产**（各游戏自己采集），默认不入库；框架侧只提供匹配能力。

## 怎么用

1. **采集模板**：MCP `capture_template(device_id, name, region=[x1,y1,x2,y2])`
   ——截当前屏，把目标元素的包围盒裁出来存成 `<name>.png`。
   （先用 `screenshot` 或 `screenshot_marked` 看坐标。）
2. **匹配**：
   - 即时定位：MCP `find_template(device_id, "<name>")` → 返回 `center` 可直接 `click`；
     `multi=True` 找出画面上所有同类元素。
   - 任务门控：节点 `recognition` 写 `{"type": "template", "template": "<name>"}`，
     配合 `action.target = "recognized"` 点击命中中心。

## 文件约定

- 文件名 = 模板名（`settings_icon.png` → 名字 `settings_icon`，`list_templates` / `find_template` 用 stem）。
- **带透明通道的 PNG**：alpha 会被当作匹配掩膜——非矩形图标不会把背景拖进相关性计算，
  建议把图标裁成带透明边的 PNG。
- **裁紧一点**，避开会动的覆盖物（倒计时、红点徽标），匹配更稳。
- matchTemplate **不抗缩放/旋转**：换分辨率会失配。同一图标想跨分辨率，匹配时传
  `scales: [0.9, 1.0, 1.1]` 扫一遍尺寸。

> 本目录只放图库，不放代码。匹配逻辑见 `perception/template_matcher.py`。
> 模板目录可在 `config.yaml` 用 `templates.dir` 覆盖。
