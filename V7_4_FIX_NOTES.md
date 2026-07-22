# V7.4 修复说明：卖家精灵 MCP 固定工具直连

## 问题

此前卖家精灵 MCP 已完成初始化，但程序会先判断 `tools/list` 中是否识别到 Amazon ASIN/关键词工具。工具使用命名空间、标题字段或服务端返回形式变化时，会在真正调用前中断，并显示“没有识别到 Amazon ASIN/关键词数据工具”。

## 修复

1. 卖家精灵模式不再使用工具识别结果阻断抓取。
2. `tools/list` 只用于获取真实工具名及 `inputSchema`。
3. 抓取时按照卖家精灵官方 Code 直接调用：
   - `traffic_keyword` → `traffic_keyword_stat` → `traffic_extend`
   - `aba_research_monthly` → `aba_research_weekly`
   - `keyword_research` → `keyword_research_trends` → `keyword_miner`
   - `asin_detail_with_coupon_trend` → `asin_detail` → `keepa_info`
   - `competitor_lookup` → `asin_sales_trend` → `asin_prediction`
4. 如果 `tools/list` 无法把某个工具映射出来，仍直接使用官方 Code 发起 `tools/call`。
5. 有实时 `inputSchema` 时优先按 Schema 传参；没有时自动尝试 ASIN、关键词、站点的常见参数格式。
6. 单个工具失败只记录在该字段备注中，其他工具和其他字段继续抓取。
7. 连接测试只验证 MCP 是否可连接并能读取工具，不再因未识别到预设工具而失败。

## 安全

卖家精灵 MCP URL 继续固定为 `https://mcp.sellersprite.com/mcp`，鉴权继续使用 `secret-key` 请求头，用户 Key 不写入源码或普通任务 JSON。
