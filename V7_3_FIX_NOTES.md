# V7.3 修复说明：卖家精灵 MCP 已连接但识别不到工具

## 现象

任务显示：

```text
卖家精灵 MCP 已连接，但没有识别到 Amazon ASIN/关键词数据工具
```

这说明网络和密钥鉴权已经走到 `tools/list`，但旧版没有从返回内容中识别出监控所需工具。

## 修复

1. `tools/list` 支持 MCP 标准游标分页，最多读取 20 页并去重。
2. 工具识别同时读取 `name`、`title`、`displayName`、`annotations.title`、`_meta.title`、描述和 schema。
3. 支持命名空间、冒号、点号、斜杠、横线和下划线等工具名变体。
4. 卖家精灵使用官方工具代码优先匹配：
   - `traffic_keyword`
   - `traffic_keyword_stat`
   - `aba_research_monthly`
   - `aba_research_weekly`
   - `keyword_research`
   - `asin_detail_with_coupon_trend`
   - `asin_detail`
   - `keepa_info`
   - `competitor_lookup`
   - `asin_sales_trend`
5. 若当前密钥未授权上述工具，错误会列出 `tools/list` 实际返回的工具，并提示进入“卖家精灵数据开放平台 → 我的密钥 → 授权”。
6. 页面在 MCP Key 下增加工具授权提醒。

## 建议授权工具

至少勾选：

```text
traffic_keyword
keyword_research
aba_research_monthly
asin_detail
competitor_lookup
asin_sales_trend
```

为了补齐优惠、流量统计和趋势数据，建议同时勾选：

```text
traffic_keyword_stat
aba_research_weekly
asin_detail_with_coupon_trend
keepa_info
```

## 测试

- Python 单元测试：36 项通过
- 卖家精灵多页 `tools/list`：通过
- 工具代码位于 `title` 的识别：通过
- 未授权工具诊断：通过
