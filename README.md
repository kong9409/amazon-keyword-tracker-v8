# Amazon 关键词监控工具 by kong · V7.4 卖家精灵 MCP 固定直连版

部署在 Zeabur 的 Amazon 关键词监控工具。页面先展示固定监控字段，再选择 Sorftime、卖家精灵、SIF、西柚洞察或其他 MCP/API；工具会基于《各插件 MCP 目录表》匹配所需接口，随后输入凭证、ASIN 和关键词抓取数据。

## V7 / V7.4 核心变化

- **监控字段前置**：STEP 1 先展示全部 18 个输出字段，选择数据源后立即显示该软件对应的接口、覆盖状态和输出格式。
- **新增小类排名**：位于“大类排名”之后，页面、Excel、飞书和数据库全部同步增加。
- **流量占比统一格式**：页面和飞书显示为百分比并保留 2 位小数，例如 `12.50%`；Excel 使用真实百分比数值和 `0.00%` 格式。
- **西柚广告位**：优先使用 `get_asin_keyword_rank_trends` 的广告排名结果。
- **西柚月销量**：使用 `get_asin_order_trends`，只取当前自然月的销量/订单值。
- **西柚类目排名**：`get_asin_bsr_trends` 中根类目作为大类排名，最深非根类目作为小类排名。

## 支持的数据源

| 页面选项 | 连接方式 | 页面需要填写 |
|---|---|---|
| Sorftime | Sorftime CLI / Sorftime MCP | Account-SK，或 MCP URL + Token |
| 卖家精灵 | MCP | 仅填写 MCP Key；官方 URL 已内置 |
| SIF | MCP | MCP URL + MCP Key |
| 西柚洞察 | MCP（默认）/ OpenAPI V2 | MCP URL + Token，或 API Key |
| 其他软件 | 自定义 MCP / API | MCP URL + Token，或 API Endpoint + Key/Header |

选择不同软件时，页面只显示该软件的连接输入框。原来的“CLI / MCP”已明确显示为 **Sorftime CLI / Sorftime MCP**。

## 使用步骤

1. STEP 1 查看固定监控字段以及当前数据源的接口匹配结果。
2. STEP 2 选择数据源和连接方式，填写 MCP、CLI 或 API 凭证并测试连接。
3. STEP 3 输入 ASIN。
4. STEP 4 输入关键词；示例仅使用 `关键词1`、`关键词2`、`关键词3`。
5. STEP 5 选择 Amazon 站点以及 Excel、飞书或 Excel + 飞书输出。
6. 保持“每日 09:00 自动抓取（北京时间）”勾选，可保存当前数据源和任务配置。
7. 点击“开始抓取”。

## 18 个监控字段

日期、ASIN、关键词、流量占比、ABA热度、搜索量、自然位、广告位、价格、优惠券、秒杀价、Prime价、月销量、大类排名、**小类排名**、评分、评价数、链接。

## 主要接口匹配

### Sorftime

- 流量占比：`product_traffic_terms`
- ABA热度、搜索量：`keyword_detail`
- 自然位、广告位：`keyword_search_results`
- 价格、优惠券、秒杀价、Prime价、月销量、大类排名、小类排名、评分、评价数：`product_detail`
- 月销量、大类排名缺失时：`product_trend`

### 卖家精灵 MCP

- 连接：官方 MCP URL `https://mcp.sellersprite.com/mcp` 已内置，页面只填写 MCP Key。
- 反查流量词、自然位、广告位：`traffic_keyword`，缺失时尝试 `traffic_keyword_stat`。
- ABA热度：`aba_research_monthly` → `aba_research_weekly`。
- 搜索量：`keyword_research` → ABA 工具。
- 产品详情、优惠和类目排名：`asin_detail_with_coupon_trend` → `asin_detail` → `keepa_info`。
- 月销量：`competitor_lookup` → `asin_sales_trend`。
- 后端通过 MCP `initialize`、`tools/list`、`tools/call` 调用，并使用官方要求的 `secret-key` 请求头；不再走卖家精灵 API。
- `tools/list` 仅用于读取真实工具名和 `inputSchema`；即使名称无法识别，也会直接调用 `traffic_keyword`、`asin_detail`、`keyword_research` 等官方 Code。
- 单个 Code 未授权、参数不匹配或无数据时，只在该字段备注中记录错误，不再导致整批任务失败。
- `tools/list` 会自动翻页读取全部授权工具，不再只读取第一页。
- 支持从 `name`、`title`、`annotations.title` 和命名空间中识别工具代码。
- 卖家精灵连接成功后，不再用“是否识别到 Amazon 工具”作为任务拦截条件。程序按官方工具 Code 直接调用；某个工具不可用只影响对应字段，其他字段继续抓取。

### SIF MCP

上传的《各插件 MCP 目录表》中 SIF 工作表暂无具体工具目录，因此 V7 使用 `tools/list`、工具描述和实时 `inputSchema` 动态匹配流量、ABA、排名、产品详情、销量及类目排名工具；页面会标记为“动态匹配”。

### 西柚洞察 MCP / OpenAPI

- 流量占比：`get_asin_keyword_traffic_trends`，缺失时再用 `get_asin_keywords`
- ABA热度、搜索量：`get_keyword_info` / `get_keyword_aba_trends`
- 自然位、广告位：`get_asin_keyword_rank_trends`
- 价格、优惠券、活动价、评分、评价数：`get_asin_info`
- 月销量：`get_asin_order_trends`，取当前自然月
- 大类排名、小类排名：`get_asin_bsr_trends`

### 其他软件

- 自定义 MCP：读取 `tools/list` 和实时 schema，按字段语义动态匹配。
- 自定义 API：向 Endpoint 发送 JSON POST：

```json
{
  "asin": "B0XXXXXXXX",
  "keyword": "关键词1",
  "marketplace": "US"
}
```

## 飞书与 Excel

- 飞书输入：App ID、App Secret、Base 链接。
- 支持读取真实字段类型并转换，避免 `TextFieldConvFail`。
- Excel、飞书、Excel + 飞书三种输出方式。
- Excel 中“流量占比”使用真实百分比格式 `0.00%`。
- Excel“任务汇总”页显示数据接口总调用次数、各接口次数和耗时。

## 每日 09:00 自动抓取

- 固定北京时间 `Asia/Shanghai` 09:00。
- 保存 ASIN、关键词、站点、数据源、输出方式与飞书配置。
- MCP/API/CLI 与飞书凭证加密保存，不写入普通任务 JSON。
- Zeabur 必须挂载持久化卷：`/app/data`。

## Zeabur 部署

把项目文件直接覆盖 GitHub 仓库根目录，确认至少存在：

```text
Dockerfile
app.py
provider_adapter.py
sorftime_adapter.py
lark_writer.py
requirements.txt
static/
```

Zeabur 不需要手动创建 `PORT`、`HOST`、`APP_MODE` 或 `python app.py` 变量。部署后检查：

```text
https://你的域名/api/health
```

## 测试

```bash
python -m unittest discover -s tests -v
node --check static/app.js
python -m py_compile app.py provider_adapter.py sorftime_adapter.py lark_writer.py
```

V7.4 已使用模拟响应测试字段前置匹配、百分比格式、小类排名、西柚广告位、当前月销量、卖家精灵 MCP 固定 Code 直连和部分工具失败不中断；未使用付费账号进行真实线上调用，各软件最终覆盖度仍取决于账号套餐、密钥授权和接口实际返回。
