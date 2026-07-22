from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path

import app
from provider_adapter import GenericApiClient, GenericMcpClient, SellerSpriteMcpClient, XiyouApiClient, XiyouMcpClient, build_data_client


class FakeSellerSprite(SellerSpriteMcpClient):
    def __init__(self):
        super().__init__("https://mcp.example.com/mcp", "secret")
        self._generic_tools = [
            {"name": "traffic_keyword", "description": "关键词反查"},
            {"name": "aba_research_monthly", "description": "ABA月数据"},
            {"name": "asin_detail", "description": "ASIN详情"},
            {"name": "competitor_lookup", "description": "查竞品销量"},
        ]

    def _ensure_initialized(self):
        return None

    def list_tools(self):
        return [item["name"] for item in self._generic_tools]

    def _call_first_direct(self, group, asin, keyword, site, raw):
        if group == "traffic":
            return {"data": [{"keywords": "关键词1", "trafficPercentage": 0.135, "rankPosition": {"position": 7}, "adPosition": {"position": 2}}]}
        if group == "aba":
            return {"data": [{"keyword": "关键词1", "searchRank": 321}]}
        if group == "keyword":
            return {"data": [{"keyword": "关键词1", "searches": 10000}]}
        if group == "product":
            return {"data": {"price": 29.99, "bsrRank": 456, "smallCategoryRank": 45, "rating": 4.6, "ratings": 789}}
        if group == "sales":
            return {"data": [{"asin": "B000000001", "units": 654}]}
        return {}


class PaginatedSellerSprite(SellerSpriteMcpClient):
    def __init__(self):
        super().__init__(token="secret")
        self.requests = []

    def _ensure_initialized(self):
        return None

    def _post(self, payload):
        self.requests.append(payload)
        cursor = (payload.get("params") or {}).get("cursor")
        if not cursor:
            return {
                "jsonrpc": "2.0",
                "id": payload.get("id"),
                "result": {
                    "tools": [{"name": "market_research", "description": "市场研究"}],
                    "nextCursor": "page-2",
                },
            }
        return {
            "jsonrpc": "2.0",
            "id": payload.get("id"),
            "result": {
                "tools": [
                    {"name": "opaque-tool-1", "title": "traffic_keyword", "description": "关键词反查"},
                    {"name": "seller-sprite:asin-detail", "description": "ASIN详情"},
                    {"name": "competitor_lookup", "description": "查竞品销量"},
                ]
            },
        }


class FakeXiyou(XiyouApiClient):
    def __init__(self, base_url, api_key):
        super().__init__(base_url, api_key)
        self.requests = []

    def _request(self, method, path, payload=None, *, tool_name=""):
        self.requests.append((method, path, payload))
        with self._lock:
            self._calls += 1
            self._tool_calls[tool_name] += 1
        if path == "/v1/asins/research/list/period":
            return {"data": [{
                "searchTerm": "关键词1",
                "trafficSummary": {"trafficAcquisitionRate": {"total": "8.2%"}},
                "ranks": [{"position": "or", "totalRank": 5}, {"position": "sp", "totalRank": 1}],
            }]}
        if path == "/v1/searchTerms/info":
            return {"data": [{"searchTerm": "关键词1", "weeklySearchVolume": 12000, "abaReport": {"searchFrequencyRank": 222}}]}
        if path == "/v1/asins/info":
            return {"data": [{"asin": "B000000001", "price": 39.5, "stars": 4.7, "ratings": 888, "amazonUrl": "https://example.com/p"}]}
        if path == "/v1/asins/orders":
            return {"data": [{"asin": "B000000001", "orders": 777}]}
        if path == "/v1/asins/bsrInfo/trends/daily":
            return {"data": [{
                "categoryTree": [
                    {"categoryId": "root-1", "root": True},
                    {"categoryId": "leaf-1", "root": False},
                ],
                "trends": [
                    {"date": "2026-07-20", "values": [{"categoryId": "root-1", "rank": 1200}]},
                    {"date": "2026-07-21", "values": [
                        {"categoryId": "leaf-1", "rank": 12},
                        {"categoryId": "root-1", "rank": 999},
                    ]},
                ],
            }]}
        raise AssertionError(path)


class ProviderTests(unittest.TestCase):
    def test_sellersprite_maps_required_fields(self):
        client = FakeSellerSprite()
        result = client.capture_keyword("B000000001", "关键词1", "US")
        self.assertEqual(result["traffic_share"], "13.50%")
        self.assertEqual(result["aba_rank"], 321)
        self.assertEqual(result["organic_position"], 7)
        self.assertEqual(result["ad_position"], 2)
        self.assertEqual(result["estimated_sales"], 654)
        self.assertEqual(result["product_rank"], 456)
        self.assertEqual(result["small_category_rank"], 45)
        self.assertEqual(result["rating"], 4.6)
        self.assertEqual(result["review_count"], 789)

    def test_xiyou_maps_required_fields(self):
        client = FakeXiyou("https://openapi.xydc.com", "secret")
        result = client.capture_keyword("B000000001", "关键词1", "US")
        self.assertEqual(result["traffic_share"], "8.20%")
        self.assertEqual(result["aba_rank"], 222)
        self.assertEqual(result["search_volume"], 12000)
        self.assertEqual(result["organic_position"], 5)
        self.assertEqual(result["ad_position"], 1)
        self.assertEqual(result["estimated_sales"], 777)
        self.assertEqual(result["product_rank"], 999)
        self.assertEqual(result["small_category_rank"], 12)
        self.assertEqual(result["product_url"], "https://example.com/p")
        info_payloads = [payload for _, path, payload in client.requests if path == "/v1/asins/info"]
        self.assertEqual(info_payloads, [{"entities": [{"country": "US", "asin": "B000000001"}]}])

    def test_connection_normalization_and_redaction(self):
        connection = app.normalize_connection({
            "provider": "sellersprite", "mode": "mcp_url",
            "mcp_url": "https://mcp.example.com/mcp", "mcp_token": "top-secret",
        })
        self.assertTrue(app.connection_has_value(connection))
        self.assertEqual(connection["mcp_url"], "https://mcp.sellersprite.com/mcp")
        clean = app.sanitize_payload_for_disk({"connection": connection, "lark": {"feishu_app_secret": "secret"}})
        self.assertEqual(clean["connection"]["provider"], "sellersprite")
        self.assertEqual(clean["connection"]["mcp_token"], "")
        self.assertEqual(clean["lark"]["feishu_app_secret"], "")

    def test_build_provider_clients(self):
        sellersprite = build_data_client({"provider": "sellersprite", "mode": "mcp_url", "mcp_url": "https://mcp.example.com/mcp", "mcp_token": "x"})
        self.assertEqual(sellersprite.source_name, "sellersprite_mcp")
        self.assertEqual(sellersprite.url, "https://mcp.sellersprite.com/mcp")
        self.assertEqual(sellersprite._auth_headers(), {"secret-key": "x"})
        self.assertEqual(build_data_client({"provider": "xiyou", "mode": "api", "api_key": "x"}).source_name, "xiyou_api")
        self.assertEqual(build_data_client({"provider": "xiyou", "mode": "mcp_url", "mcp_url": "https://mcp.xydc.com/mcp", "mcp_token": "x"}).source_name, "xiyou_mcp")
        self.assertEqual(build_data_client({"provider": "sif", "mode": "mcp_url", "mcp_url": "https://mcp.sif.com/mcp", "mcp_token": "x"}).source_name, "sif_mcp")
        self.assertIsInstance(build_data_client({"provider": "custom", "mode": "api", "api_url": "https://example.com/data"}), GenericApiClient)
        self.assertIsInstance(build_data_client({"provider": "custom", "mode": "mcp_url", "mcp_url": "https://example.com/mcp"}), GenericMcpClient)


    def test_sellersprite_mcp_prefers_directory_tool_names(self):
        client = SellerSpriteMcpClient(token="token")
        client._generic_tools = [
            {"name": "traffic_keyword", "description": "关键词反查"},
            {"name": "aba_research_monthly", "description": "ABA按月"},
            {"name": "asin_detail_with_coupon_trend", "description": "详情和优惠"},
            {"name": "competitor_lookup", "description": "销量销额"},
        ]
        self.assertEqual(client._select_tool("traffic")["name"], "traffic_keyword")
        self.assertEqual(client._select_tool("keyword")["name"], "aba_research_monthly")
        self.assertEqual(client._select_tool("product")["name"], "asin_detail_with_coupon_trend")
        self.assertEqual(client._select_tool("ranking")["name"], "traffic_keyword")
        self.assertEqual(client._select_tool("sales")["name"], "competitor_lookup")

    def test_sellersprite_reads_all_tool_pages_and_title_metadata(self):
        client = PaginatedSellerSprite()
        names = client.list_tools()
        self.assertEqual(len(names), 4)
        self.assertEqual((client.requests[1].get("params") or {}).get("cursor"), "page-2")
        self.assertEqual(client._select_tool("traffic")["name"], "opaque-tool-1")
        self.assertEqual(client._select_tool("product")["name"], "seller-sprite:asin-detail")
        ready = client.check_ready()
        self.assertGreaterEqual(ready["tool_count"], 4)
        self.assertIn("traffic", ready["resolved_tools"])
        self.assertIn("product", ready["resolved_tools"])

    def test_sellersprite_unrecognized_tools_do_not_block_direct_mode(self):
        client = SellerSpriteMcpClient(token="secret")
        client._generic_tools = [{"name": "trademark_list", "description": "商标列表"}]
        client.list_tools = lambda: ["trademark_list"]
        ready = client.check_ready()
        self.assertTrue(ready["direct_call"])
        self.assertEqual(ready["recognized_tools"], [])
        self.assertIn("直接调用", ready["note"])

    def test_sellersprite_directly_calls_official_code_when_not_listed(self):
        class DirectSellerSprite(SellerSpriteMcpClient):
            def __init__(self):
                super().__init__(token="secret")
                self.calls = []
                self._generic_tools = [{"name": "trademark_list", "description": "商标列表"}]

            def _ensure_initialized(self):
                return None

            def _post(self, payload):
                self.calls.append(payload)
                params = payload.get("params") or {}
                if payload.get("method") == "tools/call":
                    self.assert_name = params.get("name")
                    return {"jsonrpc": "2.0", "id": payload.get("id"), "result": {"structuredContent": {"data": []}}}
                return {"jsonrpc": "2.0", "id": payload.get("id"), "result": {}}

        client = DirectSellerSprite()
        client._call_direct_code("traffic_keyword", "B000000001", "关键词1", "US")
        self.assertEqual(client.assert_name, "traffic_keyword")

    def test_xiyou_mcp_prefers_official_tool_names(self):
        client = XiyouMcpClient("https://mcp.xydc.com/mcp", "token")
        client._generic_tools = [
            {"name": "get_asin_keywords", "description": "ASIN关键词"},
            {"name": "get_keyword_info", "description": "关键词详情"},
            {"name": "get_asin_info", "description": "ASIN详情"},
            {"name": "get_asin_keyword_rank_hourly", "description": "关键词小时排名"},
            {"name": "get_asin_keyword_rank_trends", "description": "关键词日排名趋势"},
            {"name": "get_asin_order_trends", "description": "订单趋势"},
            {"name": "get_asin_bsr_trends", "description": "BSR趋势"},
        ]
        self.assertEqual(client._select_tool("traffic")["name"], "get_asin_keywords")
        self.assertEqual(client._select_tool("keyword")["name"], "get_keyword_info")
        self.assertEqual(client._select_tool("product")["name"], "get_asin_info")
        self.assertEqual(client._select_tool("ranking")["name"], "get_asin_keyword_rank_trends")
        self.assertEqual(client._select_tool("sales")["name"], "get_asin_order_trends")
        self.assertEqual(client._select_tool("bsr")["name"], "get_asin_bsr_trends")
        self.assertEqual(client._auth_headers(), {"Authorization": "Bearer token"})

    def test_xiyou_current_month_sales_and_rank_trend_positions(self):
        now = datetime(2026, 7, 22, tzinfo=timezone.utc)
        sales = XiyouApiClient._current_month_sales({
            "data": [
                {"month": "2026-06", "orders": 500},
                {"month": "2026-07", "orders": 888},
            ]
        }, now=now)
        self.assertEqual(sales, 888)

        organic, ad, organic_time, ad_time = XiyouMcpClient._rank_positions({
            "data": [
                {"date": "2026-07-21", "rankType": "or", "rank": 7},
                {"date": "2026-07-21", "rankType": "sp", "rank": 2},
            ]
        }, "关键词1")
        self.assertEqual(organic, 7)
        self.assertEqual(ad, 2)
        self.assertEqual(organic_time, "2026-07-21")
        self.assertEqual(ad_time, "2026-07-21")

    def test_xiyou_defaults_to_mcp_and_token_is_redacted(self):
        connection = app.normalize_connection({"provider": "xiyou", "mcp_token": "private-token"})
        self.assertEqual(connection["mode"], "mcp_url")
        self.assertEqual(connection["mcp_url"], "https://mcp.xydc.com/mcp")
        self.assertTrue(app.connection_has_value(connection))
        clean = app.sanitize_payload_for_disk({"connection": connection, "lark": {"feishu_app_secret": ""}})
        self.assertEqual(clean["connection"]["mcp_token"], "")

    def test_generic_mcp_sends_common_key_headers(self):
        client = GenericMcpClient("https://example.com/mcp", "key-123")
        headers = client._auth_headers()
        self.assertEqual(headers["Authorization"], "Bearer key-123")
        self.assertEqual(headers["X-API-Key"], "key-123")
        self.assertEqual(headers["MCP-Key"], "key-123")

    def test_ui_contains_all_provider_options_and_no_sensitive_keyword_examples(self):
        html = Path(app.STATIC_DIR / "index.html").read_text(encoding="utf-8")
        for value in ("sorftime", "sellersprite", "sif", "xiyou", "custom"):
            self.assertIn(f'value="{value}"', html)
        self.assertIn("Sorftime CLI", html)
        self.assertIn("Sorftime MCP", html)
        self.assertIn("卖家精灵（MCP）", html)
        self.assertIn("https://mcp.sellersprite.com/mcp", html)
        self.assertNotIn('name="sellersprite_mcp_url"', html)
        self.assertIn("卖家精灵 MCP Key", html)
        self.assertIn("西柚洞察 MCP", html)
        self.assertIn("https://mcp.xydc.com/mcp", html)
        self.assertIn("STEP 1 · 监控字段", html)
        self.assertIn("小类排名", html)
        self.assertIn("关键词1", html)
        self.assertNotIn("真实业务关键词", html)

        mapping = Path(app.STATIC_DIR / "field-mapping.json").read_text(encoding="utf-8")
        self.assertIn("get_asin_keyword_rank_trends · 广告位", mapping)
        self.assertIn("get_asin_order_trends · 当月销量", mapping)
        self.assertIn("sellersprite_mcp", mapping)
        self.assertNotIn("sellersprite_api", mapping)


if __name__ == "__main__":
    unittest.main()
