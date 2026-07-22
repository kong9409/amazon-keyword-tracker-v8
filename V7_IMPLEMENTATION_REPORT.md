# V7 实施与测试报告

## 完成内容

1. 将监控字段从结果层前置到 STEP 1，并新增数据源接口匹配面板。
2. 根据《各插件 MCP 目录表.xlsx》生成前端字段映射 JSON 和新版 Excel 对照表。
3. 将结果字段从 17 个扩展为 18 个，新增“小类排名”。
4. 流量占比统一为百分比两位小数，Excel 使用原生百分比数值。
5. 西柚洞察广告位固定优先使用 `get_asin_keyword_rank_trends`。
6. 西柚洞察月销量固定从 `get_asin_order_trends` 中读取当前自然月。
7. 西柚 BSR 趋势拆分根类目和最深子类目，分别写入大类排名和小类排名。
8. 数据库通过字段定义自动增加 `small_category_rank`，旧任务数据无需重建数据库。

## 验证结果

```text
python -m py_compile app.py provider_adapter.py sorftime_adapter.py lark_writer.py：通过
node --check static/app.js：通过
python -m unittest discover -s tests -v：33 项通过
```

## 边界说明

- SIF 目录工作表为空，当前实现为 MCP 动态发现，不声称固定工具名称。
- 小类排名、优惠券、秒杀价和 Prime 价取决于接口真实返回。
- 自动化测试采用本地模拟响应，没有消耗用户付费接口额度。


## V7.1 更正

卖家精灵连接方式已由 API 更正为 MCP；页面、后台客户端、字段映射、定时任务和测试均使用 `sellersprite_mcp`。
