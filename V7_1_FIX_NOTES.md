# V7.1 修复说明：卖家精灵改为 MCP

- 页面数据源选项由“卖家精灵（API）”改为“卖家精灵（MCP）”。
- 输入项改为卖家精灵 MCP URL 与 MCP Token / Key。
- 后端不再调用卖家精灵 HTTP API 路由。
- 运行时通过 MCP `initialize`、`tools/list`、`tools/call` 调用。
- 字段匹配优先级依据《各插件 MCP 目录表》：
  - 流量占比、自然位、广告位：`traffic_keyword` → `traffic_keyword_stat`
  - ABA热度、搜索量：`aba_research_monthly` → `aba_research_weekly` → `keyword_research`
  - 价格、优惠、大小类排名、评分、评价数：`asin_detail_with_coupon_trend` → `asin_detail` → `keepa_info`
  - 月销量：`competitor_lookup` → `asin_sales_trend`
- 字段前置、流量占比两位百分比、小类排名、飞书、Excel和每日09:00定时抓取均保持不变。
