# 西柚洞察 MCP 接入说明

## 页面配置

在 STEP 1 选择：

```text
西柚洞察（MCP / API）
```

连接方式保持默认：

```text
西柚洞察 MCP（URL + Token）
```

MCP URL 已预填：

```text
https://mcp.xydc.com/mcp
```

在 Token 输入框粘贴新生成的 MCP Token，然后点击“测试连接”。

## 安全说明

- 项目文件不会内置或提交真实 Token。
- 普通任务 JSON 会清空 Token。
- 开启每日 09:00 任务时，Token 仅加密保存在 Zeabur 持久化卷 `/app/data`。
- Token 一旦曾经公开粘贴到聊天、日志或 GitHub，应在西柚后台撤销并重新生成。

## 调用流程

```text
initialize
→ tools/list
→ 按工具名称与 inputSchema 匹配 Amazon 数据工具
→ tools/call
→ 解析并写入页面、Excel或飞书
```

程序优先识别 ASIN关键词、关键词详情、ASIN详情、关键词排名、订单趋势和 BSR 趋势工具；未识别时使用动态语义匹配兜底。
