# V7.2 修复说明：内置卖家精灵 MCP URL

- 卖家精灵官方 MCP URL 固定为 `https://mcp.sellersprite.com/mcp`。
- 页面删除“卖家精灵 MCP URL”输入框，只保留 MCP Key。
- 后台忽略客户端提交的卖家精灵 URL，防止误填及凭证发送到非官方地址。
- 卖家精灵鉴权改为官方要求的 `secret-key` 请求头，不再使用通用 Bearer/X-API-Key 组合。
- 定时任务与连接测试自动使用内置 URL。
- 未填写 MCP Key 时直接提示，不会只凭内置 URL 误判为已配置。
