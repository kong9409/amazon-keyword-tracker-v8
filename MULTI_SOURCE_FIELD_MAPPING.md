# 多数据源需求字段匹配 · V7.1

页面 STEP 1 固定展示 18 个需求字段。用户切换数据源后，前端读取 `static/field-mapping.json`，显示该字段匹配到的接口及覆盖状态。

| 目标字段 | Sorftime | 卖家精灵 MCP | SIF MCP | 西柚洞察 MCP / API | 其他软件 |
|---|---|---|---|---|---|
| 日期 | 本地生成 | 本地生成 | 本地生成 | 本地生成 | 本地生成 |
| ASIN | 用户输入 | 用户输入 | 用户输入 | 用户输入 | 用户输入 |
| 关键词 | 用户输入 | 用户输入 | 用户输入 | 用户输入 | 用户输入 |
| 流量占比 | product_traffic_terms | traffic_keyword → traffic_keyword_stat | 动态匹配流量/反查工具 | MCP get_asin_keyword_traffic_trends → get_asin_keywords；API反查接口 | 动态 MCP / trafficShare |
| ABA热度 | keyword_detail | aba_research_monthly → aba_research_weekly | 动态匹配关键词详情工具 | get_keyword_info / get_keyword_aba_trends | 动态 MCP / searchFrequencyRank |
| 搜索量 | keyword_detail | keyword_research → ABA工具 | 动态匹配关键词详情工具 | get_keyword_info | 动态 MCP / searchVolume |
| 自然位 | keyword_search_results | traffic_keyword · rankPosition | 动态匹配排名工具 | **get_asin_keyword_rank_trends · 自然位** | dynamic organicPosition |
| 广告位 | keyword_search_results | traffic_keyword · adPosition | 动态匹配流量/排名工具 | **get_asin_keyword_rank_trends · 广告位** | dynamic adPosition |
| 价格 | product_detail | asin_detail_with_coupon_trend → asin_detail → keepa_info | 动态匹配产品详情工具 | get_asin_info | price |
| 优惠券 | product_detail | 产品详情实际返回 | 动态匹配产品详情工具 | get_asin_info 实际返回 | coupon |
| 秒杀价 | product_detail | 产品详情实际返回 | 动态匹配产品详情工具 | get_asin_info 实际返回 | dealPrice |
| Prime价 | product_detail | 产品详情实际返回 | 动态匹配产品详情工具 | get_asin_info 实际返回 | primePrice |
| 月销量 | product_detail → product_trend | competitor_lookup → asin_sales_trend | 动态匹配销量工具 | **get_asin_order_trends · 当前自然月** | monthlySales / orders |
| 大类排名 | product_detail → product_trend | asin_detail / keepa_info · 根类目BSR | 动态匹配根类目排名 | get_asin_bsr_trends · 根类目 | rootCategoryRank |
| 小类排名 | product_detail · 子类BSR | asin_detail / keepa_info · 小类BSR | 动态匹配子类/细分类排名 | get_asin_bsr_trends · 最深非根类目 | smallCategoryRank / subCategoryRank |
| 评分 | product_detail | asin_detail · rating | 动态匹配产品详情工具 | get_asin_info | rating |
| 评价数 | product_detail | asin_detail · ratings | 动态匹配产品详情工具 | get_asin_info | reviewCount |
| 链接 | 本地生成 | 本地生成 | 本地生成 | 接口返回或本地生成 | 接口返回或本地生成 |

## 输出规则

- 流量占比统一转换为百分比并保留两位小数，例如 `0.125 → 12.50%`、`12.5 → 12.50%`。
- Excel 使用真实数值 `0.125` 和格式 `0.00%`，便于筛选、排序和计算。
- 大类排名后固定插入小类排名。
- 优惠券、秒杀价和 Prime 价为条件字段，接口没有返回时保持空白，不生成模拟数据。
- SIF 原目录表为空，因此采用动态工具发现；接入后页面按实时 `tools/list` 结果匹配。
