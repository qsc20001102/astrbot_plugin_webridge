# 微信消息适配器图标

图标使用绿色消息气泡与白色双向箭头，直接表达消息的接收与发送。小尺寸中不放文字，插件和平台显示名称统一为“微信消息适配器”。

- 主图：根目录 `logo.png`，1254×1254 RGB PNG。
- 微信管理与连接测试页面各保存一份相同的 `logo.png`，用于页面标识与 favicon。
- 消息平台通过已有 `logo_path="logo.png"` 和 AstrBot 文件接口加载图标。
- 使用内置 imagegen 生成；没有使用 API/CLI 回退或运行时图片处理。
- 此次只更新名称和视觉资产；内部插件/平台 ID 沿用，已有账号与平台配置继续使用。

## 最终生成提示词

```text
Use case: logo-brand. Create a polished, exceptionally clear application icon for a functional software plugin named 微信消息适配器 / WeChat Message Adapter. The single concept is bidirectional message delivery. Use ONE solid rich fresh emerald green rounded speech-bubble shape on a completely uniform warm-white square background #F7F8F5. The green speech bubble is a substantial softly rounded square with a small neat tail at bottom left, occupying about 72 percent of the canvas. Inside the bubble put TWO bold simple WHITE horizontal arrows, one above the other, pointing in opposite directions: top points right, bottom points left. Arrows have rounded shafts and confident geometric arrowheads, no thin strokes, ample separation, perfectly balanced optical spacing. These arrows are the only detail inside the bubble; they clearly represent receiving and sending. Carefully proportioned negative space, composed professional product logo, excellent at 24/32px, understated contemporary visual identity. Flat high-quality vector-style mark, solid fills and precise curves, centered composition, generous safe padding. Deliver a single finished square icon, no presentation board. No bridge, no boat, no eyes, no dot faces, no bot head, no robot mascot, no official WeChat double-bubble symbol, no text, no letters, no Chinese characters, no wordmark, no gradients, no glow, no bevels, no shadow, no texture, no 3D, no decorative elements, no border or frame, no watermark. Opaque white canvas, clean emerald bubble and white exchange arrows ONLY.
```
