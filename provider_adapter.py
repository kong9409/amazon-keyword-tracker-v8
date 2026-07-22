from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from sorftime_adapter import (
    ABA_RANK_KEYS,
    AD_POSITION_KEYS,
    ASIN_KEYS,
    EMPTY,
    ORGANIC_POSITION_KEYS,
    POSITION_KEYS,
    PRICE_KEYS,
    RANK_KEYS,
    RATING_KEYS,
    REVIEW_KEYS,
    SALES_KEYS,
    SEARCH_VOLUME_KEYS,
    TRAFFIC_SHARE_KEYS,
    VALUE_KEYS,
    SorftimeMcpClient,
    amazon_product_url,
    classify_coupon,
    collect_dict_rows,
    deep_parse_json,
    extract_category_ranks,
    find_keyword_row,
    find_value,
    first_non_empty,
    normalize_decimal,
    normalize_keyword,
    normalize_marketplace,
    normalize_money,
    normalize_number,
    normalize_percent,
    normalize_position,
    normalize_yes_no,
    parse_product_detail,
    parse_tool_result,
    row_keyword,
    scalar_rank,
    unwrap_response_envelope,
    build_sorftime_client,
    test_sorftime_connection,
)


SELLERSPRITE_MCP_URL = "https://mcp.sellersprite.com/mcp"

PROVIDER_LABELS = {
    "sorftime": "Sorftime",
    "sellersprite": "卖家精灵",
    "sif": "SIF",
    "xiyou": "西柚洞察",
    "custom": "其他软件",
}


class DataClient(Protocol):
    source_name: str

    def capture_keyword(self, asin: str, keyword: str, marketplace: str) -> dict[str, Any]: ...
    def stats(self) -> dict[str, Any]: ...
    def check_ready(self) -> dict[str, Any]: ...
    def close(self) -> None: ...


def provider_label(connection: dict[str, Any] | None) -> str:
    provider = str((connection or {}).get("provider") or "sorftime").lower()
    return PROVIDER_LABELS.get(provider, "其他软件")


def _blank_result() -> dict[str, Any]:
    return {
        "keyword_rank": "", "organic_position": "", "organic_time": "",
        "ad_position": "", "ad_time": "", "traffic_share": "",
        "aba_rank": "", "search_volume": "", "price": "",
        "coupon_type": "", "coupon_value": "", "deal_status": "否",
        "deal_price": "", "prime_discount_price": "", "estimated_sales": "",
        "product_rank": "", "small_category_rank": "", "rating": "", "review_count": "", "product_url": "",
    }


def _finish_result(
    *,
    provider: str,
    asin: str,
    site: str,
    raw: dict[str, Any],
    traffic_share: Any = "",
    aba_rank: Any = "",
    search_volume: Any = "",
    organic_position: Any = "",
    ad_position: Any = "",
    price: Any = "",
    coupon_value: Any = "",
    deal_price: Any = "",
    prime_price: Any = "",
    sales: Any = "",
    product_rank: Any = "",
    small_category_rank: Any = "",
    rating: Any = "",
    review_count: Any = "",
    product_url: Any = "",
    organic_time: Any = "",
    ad_time: Any = "",
) -> dict[str, Any]:
    values = {
        "流量占比": traffic_share,
        "ABA热度": aba_rank,
        "搜索量": search_volume,
        "自然位": organic_position,
        "广告位": ad_position,
        "价格": price,
        "月销量": sales,
        "大类排名": product_rank,
        "评分": rating,
        "评价数": review_count,
    }
    found_any = any(value not in EMPTY for value in values.values())
    missing = [label for label, value in values.items() if value in EMPTY]
    status = "ok" if found_any and not missing else ("partial" if found_any else "not_found")
    message = "" if not missing else f"{provider} 未返回：" + "、".join(missing)
    if not found_any:
        errors = []
        for key, value in raw.items():
            if key.endswith("_error") and value:
                errors.append(f"{key.removesuffix('_error')}：{value}")
        if errors:
            message += "；" + "；".join(errors[:4])
    return {
        **_blank_result(),
        "keyword_rank": first_non_empty(organic_position, ad_position),
        "organic_position": normalize_position(organic_position),
        "organic_time": organic_time,
        "ad_position": normalize_position(ad_position),
        "ad_time": ad_time,
        "traffic_share": normalize_percent(traffic_share),
        "aba_rank": normalize_number(aba_rank),
        "search_volume": normalize_number(search_volume),
        "price": normalize_money(price),
        "coupon_type": classify_coupon(coupon_value),
        "coupon_value": coupon_value,
        "deal_status": normalize_yes_no("", bool(deal_price)),
        "deal_price": normalize_money(deal_price),
        "prime_discount_price": normalize_money(prime_price),
        "estimated_sales": normalize_number(sales),
        "product_rank": normalize_number(scalar_rank(product_rank)),
        "small_category_rank": normalize_number(scalar_rank(small_category_rank)),
        "rating": normalize_decimal(rating),
        "review_count": normalize_number(review_count),
        "product_url": product_url or amazon_product_url(asin, site),
        "status": status,
        "message": message,
        "raw": raw,
    }


def _percent_value(value: Any) -> Any:
    """Convert documented 0..1 ratios to a readable percentage string."""
    if value in EMPTY:
        return ""
    if isinstance(value, str) and "%" in value:
        return value
    number = normalize_number(value)
    if isinstance(number, (int, float)) and 0 <= number <= 1:
        return f"{round(number * 100, 6):g}%"
    return value


def _position_value(value: Any) -> Any:
    if isinstance(value, dict):
        return first_non_empty(value.get("position"), value.get("totalRank"), value.get("rank"), value.get("index"))
    return value


class BaseApiClient:
    source_name = "api"
    provider_name = "API"

    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        self.started_at = time.perf_counter()
        self._calls = 0
        self._tool_calls: Counter[str] = Counter()
        self._tool_seconds: Counter[str] = Counter()
        self._lock = threading.Lock()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "mcp_calls": self._calls,
                "elapsed_seconds": round(time.perf_counter() - self.started_at, 2),
                "tool_calls": dict(sorted(self._tool_calls.items())),
                "tool_seconds": {key: round(value, 2) for key, value in sorted(self._tool_seconds.items())},
            }

    def headers(self) -> dict[str, str]:
        raise NotImplementedError

    def _request(self, method: str, path: str, payload: Any = None, *, tool_name: str = "") -> Any:
        url = path if re.match(r"^https?://", path, re.I) else f"{self.base_url}/{path.lstrip('/')}"
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers=self.headers(), method=method.upper())
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                text = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:800]
            raise RuntimeError(f"{self.provider_name} API HTTP {exc.code}：{detail or exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"无法连接{self.provider_name} API：{exc.reason}") from exc
        finally:
            elapsed = time.perf_counter() - started
            if tool_name:
                with self._lock:
                    self._calls += 1
                    self._tool_calls[tool_name] += 1
                    self._tool_seconds[tool_name] += elapsed
        if not text.strip():
            return {}
        parsed = deep_parse_json(text)
        return unwrap_response_envelope(parsed)

    def close(self) -> None:
        return



class XiyouApiClient(BaseApiClient):
    source_name = "xiyou_api"
    provider_name = "西柚洞察"

    def __init__(self, base_url: str, api_key: str) -> None:
        if not api_key.strip():
            raise ValueError("请填写西柚洞察 API Key")
        super().__init__(base_url or "https://openapi.xydc.com", api_key)
        self._reverse_cache: dict[tuple[str, str], Any] = {}
        self._keyword_cache: dict[tuple[str, str], Any] = {}
        self._detail_cache: dict[tuple[str, str], Any] = {}
        self._orders_cache: dict[tuple[str, str], Any] = {}
        self._bsr_cache: dict[tuple[str, str], Any] = {}

    def headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json", "Accept": "application/json",
            "X-Auth-Version": "2.0", "X-Api-Key": self.api_key,
        }

    def check_ready(self) -> dict[str, Any]:
        return {
            "source": self.source_name,
            "tool_count": 5,
            "recognized_tools": ["reverse_asin", "keyword_info", "asin_info", "orders", "bsr_trend"],
            "missing_tools": [],
            "note": "API Key 已读取；为避免消耗额度，真实数据权限将在抓取时验证。",
        }

    def _reverse(self, asin: str, site: str) -> Any:
        key = (asin, site)
        if key not in self._reverse_cache:
            payload = {"asin": asin, "country": site, "page": 1, "pageSize": 1000, "period": "last7days"}
            self._reverse_cache[key] = self._request("POST", "/v1/asins/research/list/period", payload, tool_name="asins_research")
        return self._reverse_cache[key]

    def _keyword(self, keyword: str, site: str) -> Any:
        key = (keyword.casefold(), site)
        if key not in self._keyword_cache:
            payload = {"country": site, "searchTerms": [keyword], "sort": {"field": "weeklySearchVolume", "order": "desc"}}
            self._keyword_cache[key] = self._request("POST", "/v1/searchTerms/info", payload, tool_name="search_terms_info")
        return self._keyword_cache[key]

    def _detail(self, asin: str, site: str) -> Any:
        key = (asin, site)
        if key not in self._detail_cache:
            payload = {"entities": [{"country": site, "asin": asin}]}
            self._detail_cache[key] = self._request("POST", "/v1/asins/info", payload, tool_name="asins_info")
        return self._detail_cache[key]

    def _orders(self, asin: str, site: str) -> Any:
        key = (asin, site)
        if key not in self._orders_cache:
            payload = {"country": site, "asins": [asin]}
            self._orders_cache[key] = self._request("POST", "/v1/asins/orders", payload, tool_name="asins_orders")
        return self._orders_cache[key]

    def _bsr(self, asin: str, site: str) -> Any:
        key = (asin, site)
        if key not in self._bsr_cache:
            end = datetime.now(timezone.utc).date()
            start = end - timedelta(days=7)
            payload = {"country": site, "asin": asin, "startDate": start.isoformat(), "endDate": end.isoformat()}
            self._bsr_cache[key] = self._request("POST", "/v1/asins/bsrInfo/trends/daily", payload, tool_name="bsr_trend")
        return self._bsr_cache[key]

    @staticmethod
    def _positions(row: dict[str, Any]) -> tuple[Any, Any]:
        organic, ad = "", ""
        ranks = row.get("ranks") if isinstance(row, dict) else None
        if isinstance(ranks, list):
            for item in ranks:
                if not isinstance(item, dict):
                    continue
                kind = str(item.get("position") or item.get("type") or "").lower()
                value = first_non_empty(item.get("totalRank"), item.get("rank"), item.get("positionRank"))
                if kind in {"or", "organic"} and organic in EMPTY:
                    organic = value
                elif kind in {"sp", "ad", "sponsored"} and ad in EMPTY:
                    ad = value
        return organic, ad

    @staticmethod
    def _latest_bsr_ranks(payload: Any) -> tuple[Any, Any]:
        """Return latest root-category and deepest sub-category BSR values."""
        root_ids: set[str] = set()
        category_meta: dict[str, tuple[bool, int, int]] = {}
        trend_rows: list[dict[str, Any]] = []
        order_counter = 0

        def walk(value: Any) -> None:
            nonlocal order_counter
            if isinstance(value, list):
                for item in value:
                    walk(item)
                return
            if not isinstance(value, dict):
                return
            tree = value.get("categoryTree")
            if isinstance(tree, list):
                for item in tree:
                    if not isinstance(item, dict):
                        continue
                    category_id = first_non_empty(item.get("categoryId"), item.get("id"), item.get("nodeId"))
                    if category_id in EMPTY:
                        continue
                    order_counter += 1
                    root = bool(item.get("root") or item.get("isRoot") or item.get("is_root"))
                    try:
                        level = int(float(str(first_non_empty(item.get("level"), item.get("depth"), item.get("categoryLevel"), order_counter))))
                    except (TypeError, ValueError):
                        level = order_counter
                    category_meta[str(category_id)] = (root, level, order_counter)
                    if root:
                        root_ids.add(str(category_id))
            trends = value.get("trends")
            if isinstance(trends, list):
                trend_rows.extend(item for item in trends if isinstance(item, dict))
            for child in value.values():
                if isinstance(child, (dict, list)):
                    walk(child)

        walk(payload)
        candidates: list[tuple[str, Any, str, bool, int, int]] = []
        for trend in trend_rows:
            date_value = str(first_non_empty(trend.get("date"), trend.get("day"), trend.get("time"), ""))
            values = trend.get("values")
            if not isinstance(values, list):
                continue
            for value_row in values:
                if not isinstance(value_row, dict):
                    continue
                rank = first_non_empty(value_row.get("rank"), value_row.get("bsrRank"), value_row.get("value"))
                if rank in EMPTY:
                    continue
                category_id = first_non_empty(value_row.get("categoryId"), value_row.get("id"), value_row.get("nodeId"))
                category_key = str(category_id) if category_id not in EMPTY else ""
                meta = category_meta.get(category_key, (category_key in root_ids, 0, 0))
                candidates.append((date_value, rank, category_key, meta[0], meta[1], meta[2]))

        if candidates:
            latest_date = max(item[0] for item in candidates)
            latest = [item for item in candidates if item[0] == latest_date]
            roots = [item for item in latest if item[3]]
            leaves = [item for item in latest if not item[3]]
            main_rank = (roots or latest)[0][1]
            small_rank = ""
            if leaves:
                leaves.sort(key=lambda item: (item[4], item[5]), reverse=True)
                small_rank = leaves[0][1]
            return main_rank, small_rank
        return extract_category_ranks(payload)

    @staticmethod
    def _latest_root_bsr(payload: Any) -> Any:
        return XiyouApiClient._latest_bsr_ranks(payload)[0]

    @staticmethod
    def _current_month_sales(payload: Any, now: datetime | None = None) -> Any:
        """Read the current calendar month's order/sales value from trend data."""
        current = now or datetime.now(timezone.utc)
        target = f"{current.year:04d}-{current.month:02d}"
        rows = collect_dict_rows(payload)
        dated_rows: list[tuple[str, dict[str, Any]]] = []
        undated_rows: list[dict[str, Any]] = []
        date_keys = {"month", "yearMonth", "year_month", "statMonth", "period", "date", "day", "time"}
        value_keys = SALES_KEYS | {
            "orders", "orderCount", "orderVolume", "monthlyOrders", "monthOrders",
            "units", "unitsSold", "quantity", "salesCount", "value",
        }
        for row in rows:
            raw_period = find_value(row, date_keys)
            if raw_period in EMPTY:
                undated_rows.append(row)
                continue
            text = str(raw_period)
            digits = re.sub(r"\D", "", text)
            month_key = ""
            match = re.search(r"(20\d{2})\D?([01]?\d)", text)
            if match:
                month_key = f"{int(match.group(1)):04d}-{int(match.group(2)):02d}"
            elif len(digits) >= 6:
                month_key = f"{digits[:4]}-{digits[4:6]}"
            dated_rows.append((month_key, row))
        for month_key, row in dated_rows:
            if month_key == target:
                value = find_value(row, value_keys)
                if value not in EMPTY:
                    return value
        direct = find_value(payload, {"currentMonthOrders", "currentMonthSales", "thisMonthOrders", "monthToDateOrders"})
        if direct not in EMPTY:
            return direct
        if not dated_rows and undated_rows:
            value = find_value(undated_rows[0], value_keys)
            if value not in EMPTY:
                return value
        return ""

    def capture_keyword(self, asin: str, keyword: str, marketplace: str) -> dict[str, Any]:
        site = normalize_marketplace(marketplace)
        asin, keyword = asin.strip().upper(), keyword.strip()
        raw: dict[str, Any] = {}
        try:
            reverse = self._reverse(asin, site)
            raw["asins_research"] = reverse
            row = find_keyword_row(reverse, keyword)
        except Exception as exc:
            row = {}
            raw["asins_research_error"] = str(exc)
        try:
            keyword_data = self._keyword(keyword, site)
            raw["search_terms_info"] = keyword_data
            keyword_row = find_keyword_row(keyword_data, keyword) or (collect_dict_rows(keyword_data)[0] if collect_dict_rows(keyword_data) else {})
        except Exception as exc:
            keyword_row = {}
            raw["search_terms_info_error"] = str(exc)
        try:
            detail = self._detail(asin, site)
            raw["asins_info"] = detail
            product = parse_product_detail(detail)
        except Exception as exc:
            detail, product = {}, {}
            raw["asins_info_error"] = str(exc)
        try:
            orders = self._orders(asin, site)
            raw["asins_orders"] = orders
        except Exception as exc:
            orders = {}
            raw["asins_orders_error"] = str(exc)
        rank = product.get("product_rank", "")
        small_rank = product.get("small_category_rank", "")
        if rank in EMPTY or small_rank in EMPTY:
            try:
                bsr = self._bsr(asin, site)
                raw["bsr_trend"] = bsr
                trend_main, trend_small = self._latest_bsr_ranks(bsr)
                rank = first_non_empty(rank, trend_main)
                small_rank = first_non_empty(small_rank, trend_small)
            except Exception as exc:
                raw["bsr_trend_error"] = str(exc)
        organic, ad = self._positions(row)
        traffic_share = find_value(row, TRAFFIC_SHARE_KEYS | {"trafficAcquisitionRate", "total"})
        # Avoid a generic nested 'total' stealing unrelated values when the documented object exists.
        summary = row.get("trafficSummary") if isinstance(row, dict) else None
        if isinstance(summary, dict):
            rate = summary.get("trafficAcquisitionRate")
            if isinstance(rate, dict):
                traffic_share = first_non_empty(rate.get("total"), traffic_share)
        aba_report = keyword_row.get("abaReport") if isinstance(keyword_row, dict) else None
        aba_rank = find_value(aba_report or keyword_row, ABA_RANK_KEYS | {"searchFrequencyRank"})
        return _finish_result(
            provider=self.provider_name, asin=asin, site=site, raw=raw,
            traffic_share=_percent_value(traffic_share), aba_rank=aba_rank,
            search_volume=find_value(keyword_row, SEARCH_VOLUME_KEYS | {"weeklySearchVolume"}),
            organic_position=first_non_empty(organic, find_value(row, ORGANIC_POSITION_KEYS)),
            ad_position=first_non_empty(ad, find_value(row, AD_POSITION_KEYS)),
            price=first_non_empty(product.get("price", ""), find_value(detail, PRICE_KEYS)),
            coupon_value=product.get("coupon_value", ""), deal_price=product.get("deal_price", ""),
            prime_price=product.get("prime_discount_price", ""),
            sales=first_non_empty(self._current_month_sales(orders), find_value(orders, SALES_KEYS | {"orders", "orderCount"})),
            product_rank=rank, small_category_rank=small_rank,
            rating=first_non_empty(product.get("rating", ""), find_value(detail, RATING_KEYS | {"stars"})),
            review_count=first_non_empty(product.get("review_count", ""), find_value(detail, REVIEW_KEYS | {"ratings"})),
            product_url=find_value(detail, {"amazonUrl", "url", "productUrl"}),
        )


class GenericMcpClient(SorftimeMcpClient):
    """Dynamic MCP adapter for SIF and other Amazon data providers.

    It discovers tools from tools/list and scores their name/description/input schema.
    No provider-specific tool name is hard-coded, so upgrades remain compatible when
    a provider keeps the same semantic inputs but changes namespaces.
    """

    def __init__(self, url: str, token: str, provider_name: str = "其他软件", source_name: str = "custom_mcp") -> None:
        super().__init__(url, token)
        self.provider_name = provider_name
        self.source_name = source_name
        self._generic_tools: list[dict[str, Any]] = []
        self._generic_cache: dict[tuple[str, str, str, str], Any] = {}

    def _auth_headers(self) -> dict[str, str]:
        if not self.token:
            return {}
        bearer = self.token if self.token.lower().startswith("bearer ") else f"Bearer {self.token}"
        # Common MCP/API-key conventions. Servers ignore headers they do not use,
        # while this avoids forcing users to understand provider-specific naming.
        return {"Authorization": bearer, "X-API-Key": self.token, "MCP-Key": self.token}

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return super()._post(payload)
        except Exception as exc:
            raise RuntimeError(str(exc).replace("Sorftime MCP", f"{self.provider_name} MCP")) from exc

    def list_tools(self) -> list[str]:
        self._ensure_initialized()
        # MCP tools/list supports cursor pagination.  SellerSprite can expose more
        # than forty tools and may return them in several pages; reading only the
        # first page can make a valid key look as if it has no ASIN/keyword tools.
        tools: list[dict[str, Any]] = []
        cursor = ""
        seen_cursors: set[str] = set()
        for page_index in range(20):
            params: dict[str, Any] = {}
            if cursor:
                params["cursor"] = cursor
            response = self._post({
                "jsonrpc": "2.0",
                "id": int(time.time() * 1000) + page_index,
                "method": "tools/list",
                "params": params,
            })
            if "error" in response:
                error = response.get("error") or {}
                raise RuntimeError(error.get("message") or f"{self.provider_name} MCP tools/list failed")
            result = response.get("result", {}) or {}
            page_tools = result.get("tools", []) if isinstance(result, dict) else []
            tools.extend(item for item in (page_tools or []) if isinstance(item, dict) and item.get("name"))
            next_cursor = ""
            if isinstance(result, dict):
                next_cursor = str(result.get("nextCursor") or result.get("next_cursor") or "").strip()
            if not next_cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        # Preserve server order but remove duplicate definitions that can occur
        # when a provider changes the cursor while a request is in flight.
        unique: list[dict[str, Any]] = []
        seen_names: set[str] = set()
        for item in tools:
            name = str(item.get("name") or "").strip()
            if not name or name in seen_names:
                continue
            seen_names.add(name)
            unique.append(item)
        self._generic_tools = unique
        return [str(item.get("name")) for item in self._generic_tools]

    @staticmethod
    def _tool_blob(tool: dict[str, Any]) -> str:
        """Build a searchable description from the complete MCP tool metadata.

        Some MCP servers expose an opaque ``name`` and put the human/tool code in
        ``title`` or ``annotations.title``.  The previous matcher looked only at
        name, description and inputSchema, which produced false negatives.
        """
        annotations = tool.get("annotations") if isinstance(tool.get("annotations"), dict) else {}
        meta = tool.get("_meta") if isinstance(tool.get("_meta"), dict) else {}
        parts = [
            tool.get("name"), tool.get("title"), tool.get("displayName"), tool.get("description"),
            annotations.get("title"), annotations.get("description"),
            meta.get("title"), meta.get("description"),
            tool.get("inputSchema") or tool.get("input_schema") or {},
            tool.get("outputSchema") or tool.get("output_schema") or {},
        ]
        return " ".join(
            json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value or "")
            for value in parts
        ).lower()

    def check_ready(self) -> dict[str, Any]:
        names = self.list_tools()
        if not names:
            raise RuntimeError(f"{self.provider_name} MCP 已连接，但 tools/list 没有返回工具")
        recognized = []
        for kind in ("traffic", "keyword", "product", "ranking", "sales"):
            tool = self._select_tool(kind)
            if tool:
                recognized.append(str(tool.get("name")))
        if not recognized:
            raise RuntimeError(f"{self.provider_name} MCP 已连接，但没有识别到 Amazon ASIN/关键词数据工具")
        return {"source": self.source_name, "tool_count": len(names), "recognized_tools": sorted(set(recognized)), "missing_tools": []}

    def _select_tool(self, kind: str) -> dict[str, Any] | None:
        weighted = {
            "traffic": (("asin", 5), ("keyword", 3), ("关键词", 3), ("traffic", 6), ("流量", 6), ("reverse", 5), ("反查", 5), ("term", 2)),
            "keyword": (("keyword", 6), ("关键词", 6), ("搜索词", 5), ("detail", 4), ("info", 3), ("search", 3), ("market", 2), ("aba", 5), ("搜索量", 5)),
            "product": (("asin", 4), ("product", 5), ("产品", 5), ("商品", 4), ("detail", 5), ("详情", 5), ("info", 3)),
            "ranking": (("rank", 7), ("排名", 7), ("位置", 5), ("asin", 3), ("keyword", 3), ("关键词", 3), ("trend", 2), ("趋势", 2)),
            "sales": (("sales", 7), ("销量", 7), ("volume", 5), ("order", 5), ("订单", 5), ("asin", 3)),
        }[kind]
        best: tuple[int, dict[str, Any]] | None = None
        for tool in self._generic_tools:
            blob = self._tool_blob(tool)
            score = sum(weight for token, weight in weighted if token in blob)
            # Discourage non-Amazon namespaces.
            if any(token in blob for token in ("tiktok", "temu", "shopee", "walmart", "ebay")):
                score -= 50
            if best is None or score > best[0]:
                best = (score, tool)
        return best[1] if best and best[0] >= 7 else None

    @staticmethod
    def _schema_arguments(tool: dict[str, Any], asin: str, keyword: str, site: str) -> dict[str, Any]:
        schema = tool.get("inputSchema") or tool.get("input_schema") or {}
        properties = schema.get("properties") if isinstance(schema, dict) else {}
        properties = properties if isinstance(properties, dict) else {}
        args: dict[str, Any] = {}
        end_date = datetime.now(timezone.utc).date()
        start_date = end_date - timedelta(days=30)
        current_month = end_date.strftime("%Y-%m")
        for name, prop in properties.items():
            raw_name = str(name)
            key = re.sub(r"[^a-z0-9]", "", raw_name.lower())
            compact_cn = re.sub(r"[\s_\-]", "", raw_name)
            prop_type = prop.get("type") if isinstance(prop, dict) else None
            enum_values = prop.get("enum") if isinstance(prop, dict) else None
            if key in {"asin", "productasin", "parentasin"} or compact_cn in {"产品asin", "商品asin"}:
                args[name] = asin
            elif key in {"asins", "asinlist", "products"} or compact_cn in {"asin列表", "产品列表"}:
                args[name] = [asin]
            elif key in {"keyword", "searchterm", "query", "term"} or compact_cn in {"关键词", "搜索词"}:
                args[name] = keyword
            elif key in {"keywords", "searchterms", "keywordlist"} or compact_cn in {"关键词列表", "搜索词列表"}:
                args[name] = [keyword]
            elif key in {"marketplace", "country", "site", "domain", "market", "amzsite", "keywordsupportsite"} or compact_cn in {"站点", "国家", "市场"}:
                candidate = site
                if isinstance(enum_values, list) and enum_values:
                    by_upper = {str(item).upper(): item for item in enum_values}
                    candidate = by_upper.get(site.upper(), by_upper.get("UK" if site == "GB" else site.upper(), enum_values[0]))
                args[name] = candidate
            elif key in {"page", "pageindex", "pagenum"} or compact_cn in {"页码", "页数"}:
                args[name] = 1
            elif key in {"size", "pagesize", "limit"} or compact_cn in {"每页数量", "分页大小"}:
                args[name] = 100
            elif key in {"positiontype"}:
                args[name] = 0
            elif key in {"entities"}:
                args[name] = [{"asin": asin, "country": site}]
            elif key in {"startdate", "fromdate", "begindate", "starttime"} or compact_cn in {"开始日期", "起始日期"}:
                args[name] = start_date.isoformat()
            elif key in {"enddate", "todate", "finishdate", "endtime"} or compact_cn in {"结束日期", "截止日期"}:
                args[name] = end_date.isoformat()
            elif key in {"startmonth", "frommonth", "beginmonth"} or compact_cn in {"开始月份", "起始月份"}:
                args[name] = current_month
            elif key in {"endmonth", "tomonth", "finishmonth", "month"} or compact_cn in {"结束月份", "月份"}:
                args[name] = current_month
            elif key in {"period", "daterange", "timerange"} or compact_cn in {"周期", "时间范围"}:
                args[name] = enum_values[0] if isinstance(enum_values, list) and enum_values else "last30days"
            elif prop_type == "boolean" and key in {"exact", "exactflag", "matchexact"}:
                args[name] = True
            elif isinstance(enum_values, list) and enum_values and name in (schema.get("required") or []):
                args[name] = enum_values[0]
        # Fallback for permissive schemas.
        if not properties:
            args = {"asin": asin, "keyword": keyword, "marketplace": site}
        required = schema.get("required") if isinstance(schema, dict) else []
        missing = [name for name in (required or []) if name not in args]
        if missing:
            raise RuntimeError(f"工具 {tool.get('name')} 缺少无法自动推断的必填参数：{'、'.join(map(str, missing))}")
        return args

    def _call_kind(self, kind: str, asin: str, keyword: str, site: str) -> Any:
        key = (kind, asin, keyword.casefold(), site)
        if key in self._generic_cache:
            return self._generic_cache[key]
        tool = self._select_tool(kind)
        if not tool:
            self._generic_cache[key] = {}
            return {}
        name = str(tool.get("name"))
        args = self._schema_arguments(tool, asin, keyword, site)
        started = time.perf_counter()
        response = self._post({"jsonrpc": "2.0", "id": int(time.time() * 1000), "method": "tools/call", "params": {"name": name, "arguments": args}})
        elapsed = time.perf_counter() - started
        with self._lock:
            self._mcp_calls += 1
            self._tool_calls[name] += 1
            self._tool_seconds[name] += elapsed
        if "error" in response:
            error = response.get("error") or {}
            raise RuntimeError(str(error.get("message") or error.get("data") or f"{name} 调用失败"))
        result = response.get("result", {})
        if isinstance(result, dict) and result.get("isError"):
            raise RuntimeError(str(parse_tool_result(result)))
        parsed = parse_tool_result(result)
        self._generic_cache[key] = parsed
        return parsed

    def capture_keyword(self, asin: str, keyword: str, marketplace: str) -> dict[str, Any]:
        self._ensure_initialized()
        if not self._generic_tools:
            self.list_tools()
        site = normalize_marketplace(marketplace)
        asin, keyword = asin.strip().upper(), keyword.strip()
        raw: dict[str, Any] = {}
        data: dict[str, Any] = {}
        for kind in ("traffic", "keyword", "product", "ranking", "sales"):
            try:
                data[kind] = self._call_kind(kind, asin, keyword, site)
                raw[kind] = data[kind]
            except Exception as exc:
                data[kind] = {}
                raw[f"{kind}_error"] = str(exc)
        traffic_row = find_keyword_row(data["traffic"], keyword) or data["traffic"]
        keyword_row = find_keyword_row(data["keyword"], keyword) or data["keyword"]
        product = parse_product_detail(data["product"])
        return _finish_result(
            provider=self.provider_name, asin=asin, site=site, raw=raw,
            traffic_share=find_value(traffic_row, TRAFFIC_SHARE_KEYS),
            aba_rank=find_value(keyword_row, ABA_RANK_KEYS),
            search_volume=find_value(keyword_row, SEARCH_VOLUME_KEYS),
            organic_position=first_non_empty(find_value(traffic_row, ORGANIC_POSITION_KEYS), find_value(data["ranking"], ORGANIC_POSITION_KEYS | RANK_KEYS)),
            ad_position=find_value(traffic_row, AD_POSITION_KEYS),
            price=product.get("price", ""), coupon_value=product.get("coupon_value", ""),
            deal_price=product.get("deal_price", ""), prime_price=product.get("prime_discount_price", ""),
            sales=first_non_empty(product.get("estimated_sales", ""), find_value(data["sales"], SALES_KEYS | VALUE_KEYS)),
            product_rank=first_non_empty(product.get("product_rank", ""), find_value(data["ranking"], RANK_KEYS)),
            small_category_rank=product.get("small_category_rank", ""),
            rating=product.get("rating", ""), review_count=product.get("review_count", ""),
            product_url=find_value(data["product"], {"url", "productUrl", "amazonUrl"}),
        )


class SellerSpriteMcpClient(GenericMcpClient):
    """卖家精灵 MCP 固定工具直连适配器。

    卖家精灵模式不再因为 tools/list 的名称、命名空间或授权列表未被
    识别而终止整个任务。连接成功后，程序按官方工具 Code 直接调用；
    tools/list 只用于读取真实工具名与 inputSchema。某个工具不可用时，
    仅对应字段留空并记录错误，其他工具继续执行。
    """

    DIRECT_TOOL_GROUPS = {
        "traffic": ("traffic_keyword", "traffic_keyword_stat", "traffic_extend"),
        "aba": ("aba_research_monthly", "aba_research_weekly"),
        "keyword": ("keyword_research", "keyword_research_trends", "keyword_miner"),
        "product": ("asin_detail_with_coupon_trend", "asin_detail", "keepa_info"),
        "sales": ("competitor_lookup", "asin_sales_trend", "asin_prediction"),
    }

    TRACKER_TOOL_CODES = tuple(dict.fromkeys(
        code for values in DIRECT_TOOL_GROUPS.values() for code in values
    ))

    def __init__(self, url: str = SELLERSPRITE_MCP_URL, token: str = "") -> None:
        # 地址固定为卖家精灵官方 MCP，避免把密钥发送到用户误填的地址。
        super().__init__(
            SELLERSPRITE_MCP_URL,
            str(token or "").strip(),
            provider_name="卖家精灵",
            source_name="sellersprite_mcp",
        )
        self._direct_cache: dict[tuple[str, str, str, str], Any] = {}

    def _auth_headers(self) -> dict[str, str]:
        if not self.token:
            return {}
        return {"secret-key": self.token}

    @staticmethod
    def _normalize_tool_text(value: Any) -> str:
        return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")

    def _official_code(self, tool: dict[str, Any]) -> str:
        """从 name/title/metadata/description 中定位卖家精灵官方 Code。"""
        annotations = tool.get("annotations") if isinstance(tool.get("annotations"), dict) else {}
        meta = tool.get("_meta") if isinstance(tool.get("_meta"), dict) else {}
        values = [
            tool.get("name"), tool.get("title"), tool.get("displayName"),
            annotations.get("title"), meta.get("title"), tool.get("description"),
        ]
        normalized_values = [self._normalize_tool_text(value) for value in values if value]
        for expected in self.TRACKER_TOOL_CODES:
            expected_norm = self._normalize_tool_text(expected)
            compact_expected = expected_norm.replace("_", "")
            for value in normalized_values:
                if value == expected_norm or value.endswith("_" + expected_norm):
                    return expected
                if compact_expected and compact_expected in value.replace("_", ""):
                    return expected
        return ""

    @staticmethod
    def _display_tool(tool: dict[str, Any]) -> str:
        annotations = tool.get("annotations") if isinstance(tool.get("annotations"), dict) else {}
        title = tool.get("title") or tool.get("displayName") or annotations.get("title")
        name = str(tool.get("name") or "").strip()
        return f"{name}（{title}）" if title and str(title).strip() != name else name

    def _tool_for_code(self, code: str) -> dict[str, Any] | None:
        """优先使用 tools/list 返回的真实名称和 Schema。"""
        for tool in self._generic_tools:
            if self._official_code(tool) == code:
                return tool
        # 有些服务直接把 Code 放在 name 中，但 metadata 不完整。
        normalized_code = self._normalize_tool_text(code)
        for tool in self._generic_tools:
            name = self._normalize_tool_text(tool.get("name"))
            if name == normalized_code or name.endswith("_" + normalized_code):
                return tool
        return None

    def _select_tool(self, kind: str) -> dict[str, Any] | None:
        # 保留给页面诊断和旧测试使用；实际抓取使用 _call_first_direct。
        alias = "aba" if kind == "keyword" else kind
        codes = self.DIRECT_TOOL_GROUPS.get(alias, ())
        for code in codes:
            tool = self._tool_for_code(code)
            if tool:
                return tool
        if kind == "ranking":
            for code in self.DIRECT_TOOL_GROUPS["traffic"]:
                tool = self._tool_for_code(code)
                if tool:
                    return tool
        return None

    @staticmethod
    def _dedupe_arguments(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        unique: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in candidates:
            key = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        return unique

    def _argument_candidates(
        self,
        code: str,
        tool: dict[str, Any] | None,
        asin: str,
        keyword: str,
        site: str,
    ) -> list[dict[str, Any]]:
        """生成直连参数；有 inputSchema 时始终优先按真实 Schema 组装。"""
        candidates: list[dict[str, Any]] = []
        if tool:
            try:
                candidates.append(self._schema_arguments(tool, asin, keyword, site))
            except Exception:
                # Schema 中存在无法自动推断的字段时，继续尝试官方常见参数。
                pass

        page = {"page": 1, "pageSize": 100}
        site_variants = (
            {"marketplace": site},
            {"country": site},
            {"site": site},
            {"market": site},
        )
        if code.startswith("traffic_") or code.startswith("asin_") or code in {
            "competitor_lookup", "keepa_info",
        }:
            for site_args in site_variants:
                candidates.extend([
                    {"asin": asin, **site_args},
                    {"asin": asin, "keyword": keyword, **site_args},
                    {"asin": asin, **site_args, **page},
                    {"asins": [asin], **site_args},
                ])
        if code.startswith("keyword_") or code.startswith("aba_"):
            for site_args in site_variants:
                candidates.extend([
                    {"keyword": keyword, **site_args},
                    {"keyword": keyword, **site_args, **page},
                    {"keywords": [keyword], **site_args},
                ])

        # 最后的宽松参数用于服务端 Schema 允许附加字段的情况。
        candidates.extend([
            {"asin": asin, "keyword": keyword, "marketplace": site},
            {"asin": asin, "keyword": keyword, "country": site},
        ])
        unique = self._dedupe_arguments(candidates)
        # 避免无 Schema 时为了猜参数消耗过多 MCP 次数。实时 Schema 参数始终排在首位。
        return unique[:6]

    @staticmethod
    def _is_retryable_argument_error(message: str) -> bool:
        text = str(message or "").lower()
        return any(token in text for token in (
            "invalid params", "invalid parameter", "missing", "required",
            "参数", "必填", "field required", "validation", "schema",
        ))

    def _invoke_direct_name(self, name: str, arguments: dict[str, Any], code: str) -> Any:
        started = time.perf_counter()
        response = self._post({
            "jsonrpc": "2.0",
            "id": int(time.time() * 1000),
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
        elapsed = time.perf_counter() - started
        with self._lock:
            self._mcp_calls += 1
            self._tool_calls[code] += 1
            self._tool_seconds[code] += elapsed
        if "error" in response:
            error = response.get("error") or {}
            detail = error.get("data")
            suffix = f"：{detail}" if detail else ""
            raise RuntimeError(str(error.get("message") or f"{code} 调用失败") + suffix)
        result = response.get("result", {})
        if isinstance(result, dict) and result.get("isError"):
            raise RuntimeError(str(parse_tool_result(result)))
        return parse_tool_result(result)

    def _call_direct_code(self, code: str, asin: str, keyword: str, site: str) -> Any:
        cache_key = (code, asin, keyword.casefold(), site)
        if cache_key in self._direct_cache:
            return self._direct_cache[cache_key]

        tool = self._tool_for_code(code)
        # 若 tools/list 无法识别 Code，仍直接用官方 Code 调用。
        actual_name = str((tool or {}).get("name") or code).strip()
        errors: list[str] = []
        for arguments in self._argument_candidates(code, tool, asin, keyword, site):
            try:
                parsed = self._invoke_direct_name(actual_name, arguments, code)
                self._direct_cache[cache_key] = parsed
                return parsed
            except Exception as exc:
                message = str(exc)
                errors.append(message)
                if not self._is_retryable_argument_error(message):
                    break
        raise RuntimeError(f"{code}：" + "；".join(errors[:3]))

    def _call_first_direct(
        self,
        group: str,
        asin: str,
        keyword: str,
        site: str,
        raw: dict[str, Any],
    ) -> Any:
        errors: list[str] = []
        for code in self.DIRECT_TOOL_GROUPS[group]:
            try:
                data = self._call_direct_code(code, asin, keyword, site)
                raw[code] = data
                if data not in EMPTY:
                    return data
            except Exception as exc:
                raw[f"{code}_error"] = str(exc)
                errors.append(str(exc))
        if errors:
            raw[f"{group}_error"] = "；".join(errors[:4])
        return {}

    def check_ready(self) -> dict[str, Any]:
        names = self.list_tools()
        if not names:
            raise RuntimeError(
                "卖家精灵 MCP 已连接，但 tools/list 没有返回工具。请检查 MCP Key、套餐和密钥状态。"
            )

        authorized_codes = sorted({
            code for code in (self._official_code(tool) for tool in self._generic_tools) if code
        })
        resolved_codes = {
            code: str(tool.get("name") or "")
            for code in self.TRACKER_TOOL_CODES
            if (tool := self._tool_for_code(code))
        }
        resolved_tools: dict[str, str] = {}
        for group, codes in self.DIRECT_TOOL_GROUPS.items():
            for code in codes:
                if code in resolved_codes:
                    resolved_tools[group] = resolved_codes[code]
                    break
        if "traffic" in resolved_tools:
            resolved_tools.setdefault("ranking", resolved_tools["traffic"])
        return {
            "source": self.source_name,
            "tool_count": len(names),
            "recognized_tools": sorted(set(resolved_codes.values())),
            "resolved_tools": resolved_tools,
            "resolved_codes": resolved_codes,
            "authorized_codes": authorized_codes,
            "available_tools": [self._display_tool(tool) for tool in self._generic_tools],
            "missing_tools": [code for code in self.TRACKER_TOOL_CODES if code not in resolved_codes],
            "direct_call": True,
            "note": (
                f"已连接卖家精灵 MCP，读取 {len(names)} 个工具。"
                "抓取时将按官方 Code 直接调用；未在 tools/list 中识别到的 Code 不再阻止任务。"
            ),
        }

    def capture_keyword(self, asin: str, keyword: str, marketplace: str) -> dict[str, Any]:
        self._ensure_initialized()
        if not self._generic_tools:
            self.list_tools()
        site = normalize_marketplace(marketplace)
        asin, keyword = asin.strip().upper(), keyword.strip()
        raw: dict[str, Any] = {}

        traffic = self._call_first_direct("traffic", asin, keyword, site, raw)
        aba = self._call_first_direct("aba", asin, keyword, site, raw)
        keyword_data = self._call_first_direct("keyword", asin, keyword, site, raw)
        product_data = self._call_first_direct("product", asin, keyword, site, raw)
        sales_data = self._call_first_direct("sales", asin, keyword, site, raw)

        traffic_row = find_keyword_row(traffic, keyword) or traffic
        aba_row = find_keyword_row(aba, keyword) or aba
        keyword_row = find_keyword_row(keyword_data, keyword) or keyword_data
        product = parse_product_detail(product_data)

        return _finish_result(
            provider=self.provider_name,
            asin=asin,
            site=site,
            raw=raw,
            traffic_share=_percent_value(find_value(
                traffic_row,
                TRAFFIC_SHARE_KEYS | {"trafficPercentage", "trafficRate", "trafficShare"},
            )),
            aba_rank=first_non_empty(
                find_value(aba_row, ABA_RANK_KEYS | {"searchRank", "searchesRank", "abaRank"}),
                find_value(keyword_row, ABA_RANK_KEYS | {"searchRank", "searchesRank", "abaRank"}),
            ),
            search_volume=first_non_empty(
                find_value(keyword_row, SEARCH_VOLUME_KEYS | {"searches", "monthlySearches", "searchVolume"}),
                find_value(aba_row, SEARCH_VOLUME_KEYS | {"searches", "monthlySearches", "searchVolume"}),
            ),
            organic_position=_position_value(find_value(
                traffic_row,
                ORGANIC_POSITION_KEYS | {"rankPosition", "organicPosition", "organicRank"},
            )),
            ad_position=_position_value(find_value(
                traffic_row,
                AD_POSITION_KEYS | {"adPosition", "sponsoredPosition", "adRank"},
            )),
            price=product.get("price", ""),
            coupon_value=product.get("coupon_value", ""),
            deal_price=product.get("deal_price", ""),
            prime_price=product.get("prime_discount_price", ""),
            sales=first_non_empty(
                product.get("estimated_sales", ""),
                find_value(sales_data, SALES_KEYS | {
                    "units", "monthlyUnits", "monthSales", "sales30Days", "salesVolume",
                }),
            ),
            product_rank=product.get("product_rank", ""),
            small_category_rank=product.get("small_category_rank", ""),
            rating=product.get("rating", ""),
            review_count=product.get("review_count", ""),
            product_url=find_value(product_data, {"url", "productUrl", "amazonUrl"}),
        )


class XiyouMcpClient(GenericMcpClient):
    """西柚洞察远程 MCP 适配器。

    官方接入使用 Streamable HTTP URL + Authorization: Bearer Token。
    工具名称优先按西柚公开命名匹配，同时保留动态 tools/list 兜底，
    避免服务端新增命名空间后失效。
    """

    PREFERRED_TOOLS = {
        "traffic": (
            "get_asin_keywords",
            "get_asin_keywords_monthly",
            "get_asin_keyword_traffic_trends",
            "get_asin_traffic",
            "get_asin_traffic_trends",
        ),
        "keyword": (
            "get_keyword_info",
            "get_keyword_analysis_monthly",
            "get_keyword_aba_trends",
            "get_keyword_asin_analysis",
        ),
        "product": (
            "get_asin_info",
            "get_asin_variations",
            "get_asin_info_trends",
        ),
        "ranking": (
            "get_asin_keyword_rank_trends",
            "get_asin_keyword_rank_hourly",
        ),
        "sales": (
            "get_asin_order_trends",
            "get_asin_info",
        ),
        "bsr": (
            "get_asin_bsr_trends",
            "get_asin_info",
        ),
    }

    def __init__(self, url: str, token: str) -> None:
        if not token.strip():
            raise ValueError("请填写西柚洞察 MCP Token")
        super().__init__(url or "https://mcp.xydc.com/mcp", token, provider_name="西柚洞察", source_name="xiyou_mcp")

    def _auth_headers(self) -> dict[str, str]:
        bearer = self.token if self.token.lower().startswith("bearer ") else f"Bearer {self.token}"
        return {"Authorization": bearer}

    def _select_tool(self, kind: str) -> dict[str, Any] | None:
        preferred = self.PREFERRED_TOOLS.get(kind, ())
        by_name = {str(tool.get("name") or "").strip().lower(): tool for tool in self._generic_tools}
        for expected in preferred:
            if expected in by_name:
                return by_name[expected]
            for actual_name, tool in by_name.items():
                if actual_name.endswith("." + expected) or actual_name.endswith("/" + expected) or actual_name.endswith("_" + expected):
                    return tool
        if kind == "bsr":
            candidates = []
            for tool in self._generic_tools:
                blob = " ".join([
                    str(tool.get("name") or ""),
                    str(tool.get("description") or ""),
                    json.dumps(tool.get("inputSchema") or {}, ensure_ascii=False),
                ]).lower()
                score = sum(weight for token, weight in (("bsr", 10), ("类目排名", 10), ("asin", 3), ("rank", 4), ("趋势", 2), ("trend", 2)) if token in blob)
                if score:
                    candidates.append((score, tool))
            return max(candidates, key=lambda item: item[0])[1] if candidates else None
        return super()._select_tool(kind)

    def _call_optional_kind(self, kind: str, asin: str, keyword: str, site: str) -> Any:
        tool = self._select_tool(kind)
        if not tool:
            return {}
        cache_kind = kind if kind in {"traffic", "keyword", "product", "ranking", "sales"} else f"xiyou_{kind}"
        key = (cache_kind, asin, keyword.casefold(), site)
        if key in self._generic_cache:
            return self._generic_cache[key]
        name = str(tool.get("name"))
        args = self._schema_arguments(tool, asin, keyword, site)
        started = time.perf_counter()
        response = self._post({"jsonrpc": "2.0", "id": int(time.time() * 1000), "method": "tools/call", "params": {"name": name, "arguments": args}})
        elapsed = time.perf_counter() - started
        with self._lock:
            self._mcp_calls += 1
            self._tool_calls[name] += 1
            self._tool_seconds[name] += elapsed
        if "error" in response:
            error = response.get("error") or {}
            raise RuntimeError(str(error.get("message") or error.get("data") or f"{name} 调用失败"))
        result = response.get("result", {})
        if isinstance(result, dict) and result.get("isError"):
            raise RuntimeError(str(parse_tool_result(result)))
        parsed = parse_tool_result(result)
        self._generic_cache[key] = parsed
        return parsed

    def _call_rank_variant(self, asin: str, keyword: str, site: str, variant: str) -> Any:
        """Call get_asin_keyword_rank_trends for a specific organic/ad type.

        Some Xiyou schemas expose one rank type per request.  The generic argument
        builder naturally chooses the first enum (usually organic), so an explicit
        second call is required to obtain sponsored/ad position data.
        """
        tool = self._select_tool("ranking")
        if not tool:
            return {}
        schema = tool.get("inputSchema") or tool.get("input_schema") or {}
        properties = schema.get("properties") if isinstance(schema, dict) else {}
        properties = properties if isinstance(properties, dict) else {}
        target_name = ""
        target_value: Any = None
        organic_tokens = {"0", "or", "organic", "natural"}
        ad_tokens = {"2", "sp", "ad", "sponsored"}
        desired = ad_tokens if variant == "ad" else organic_tokens
        for name, prop in properties.items():
            key = re.sub(r"[^a-z0-9]", "", str(name).lower())
            if key not in {"positiontype", "positiontypes", "ranktype", "ranktypes", "rankingtype", "placement", "type"}:
                continue
            enum_values = prop.get("enum") if isinstance(prop, dict) else None
            candidates = enum_values if isinstance(enum_values, list) else [0, 2, "or", "sp", "organic", "ad"]
            for candidate in candidates:
                if str(candidate).strip().lower() in desired:
                    target_name = str(name)
                    target_value = candidate
                    if isinstance(prop, dict) and prop.get("type") == "array":
                        target_value = [candidate]
                    break
            if target_name:
                break
        if not target_name:
            return {}
        cache_kind = f"xiyou_ranking_{variant}"
        cache_key = (cache_kind, asin, keyword.casefold(), site)
        if cache_key in self._generic_cache:
            return self._generic_cache[cache_key]
        args = self._schema_arguments(tool, asin, keyword, site)
        args[target_name] = target_value
        name = str(tool.get("name"))
        started = time.perf_counter()
        response = self._post({"jsonrpc": "2.0", "id": int(time.time() * 1000), "method": "tools/call", "params": {"name": name, "arguments": args}})
        elapsed = time.perf_counter() - started
        with self._lock:
            self._mcp_calls += 1
            self._tool_calls[name] += 1
            self._tool_seconds[name] += elapsed
        if "error" in response:
            error = response.get("error") or {}
            raise RuntimeError(str(error.get("message") or error.get("data") or f"{name} 调用失败"))
        result = response.get("result", {})
        if isinstance(result, dict) and result.get("isError"):
            raise RuntimeError(str(parse_tool_result(result)))
        parsed = parse_tool_result(result)
        self._generic_cache[cache_key] = parsed
        return parsed

    @staticmethod
    def _traffic_share(payload: Any, keyword: str) -> Any:
        row = find_keyword_row(payload, keyword) or payload
        value = find_value(row, TRAFFIC_SHARE_KEYS | {"trafficAcquisitionRate", "trafficRateTotal"})
        if isinstance(row, dict):
            for container_key in ("trafficSummary", "traffic", "trafficInfo"):
                container = row.get(container_key)
                if not isinstance(container, dict):
                    continue
                rate = container.get("trafficAcquisitionRate")
                if isinstance(rate, dict):
                    value = first_non_empty(rate.get("total"), rate.get("all"), value)
        return _percent_value(value)

    @staticmethod
    def _rank_positions(payload: Any, keyword: str) -> tuple[Any, Any, Any, Any]:
        """Extract organic/ad positions from get_asin_keyword_rank_trends."""
        rows = collect_dict_rows(payload)
        target = normalize_keyword(keyword)
        matched = [row for row in rows if not row_keyword(row) or normalize_keyword(row_keyword(row)) == target]
        candidates = matched or rows
        candidates.sort(key=lambda row: str(find_value(row, {"date", "day", "time", "recordDate", "statDate"}) or ""), reverse=True)
        organic = ad = organic_time = ad_time = ""
        for row in candidates:
            row_date = find_value(row, {"date", "day", "time", "recordDate", "statDate"})
            direct_org = find_value(row, ORGANIC_POSITION_KEYS)
            direct_ad = find_value(row, AD_POSITION_KEYS)
            if direct_org not in EMPTY and organic in EMPTY:
                organic, organic_time = direct_org, row_date
            if direct_ad not in EMPTY and ad in EMPTY:
                ad, ad_time = direct_ad, row_date

            nested = row.get("ranks") if isinstance(row, dict) else None
            rank_rows = nested if isinstance(nested, list) else [row]
            for item in rank_rows:
                if not isinstance(item, dict):
                    continue
                kind = str(first_non_empty(
                    item.get("position"), item.get("positionType"), item.get("rankType"),
                    item.get("type"), item.get("placement"), item.get("source"),
                )).strip().lower()
                value = first_non_empty(
                    item.get("totalRank"), item.get("rank"), item.get("positionRank"),
                    item.get("value"), item.get("index"),
                )
                if value in EMPTY:
                    continue
                if kind in {"0", "or", "organic", "natural", "自然", "自然位"} and organic in EMPTY:
                    organic, organic_time = value, first_non_empty(row_date, item.get("date"), item.get("time"))
                elif kind in {"2", "sp", "ad", "sponsored", "广告", "广告位"} and ad in EMPTY:
                    ad, ad_time = value, first_non_empty(row_date, item.get("date"), item.get("time"))
            if organic not in EMPTY and ad not in EMPTY:
                break
        return organic, ad, organic_time, ad_time

    def capture_keyword(self, asin: str, keyword: str, marketplace: str) -> dict[str, Any]:
        self._ensure_initialized()
        if not self._generic_tools:
            self.list_tools()
        site = normalize_marketplace(marketplace)
        asin, keyword = asin.strip().upper(), keyword.strip()
        raw: dict[str, Any] = {}
        data: dict[str, Any] = {}
        for kind in ("traffic", "keyword", "product", "ranking", "sales", "bsr"):
            try:
                data[kind] = self._call_optional_kind(kind, asin, keyword, site)
                raw[kind] = data[kind]
            except Exception as exc:
                data[kind] = {}
                raw[f"{kind}_error"] = str(exc)

        traffic_row = find_keyword_row(data["traffic"], keyword) or data["traffic"]
        keyword_row = find_keyword_row(data["keyword"], keyword) or data["keyword"]
        product = parse_product_detail(data["product"])
        positions = XiyouApiClient._positions(traffic_row if isinstance(traffic_row, dict) else {})
        rank_org, rank_ad, rank_org_time, rank_ad_time = self._rank_positions(data["ranking"], keyword)
        if rank_ad in EMPTY:
            try:
                ad_ranking = self._call_rank_variant(asin, keyword, site, "ad")
                raw["ranking_ad"] = ad_ranking
                _, rank_ad, _, rank_ad_time = self._rank_positions(ad_ranking, keyword)
                if rank_ad in EMPTY:
                    rank_ad = find_value(ad_ranking, AD_POSITION_KEYS | POSITION_KEYS | RANK_KEYS)
            except Exception as exc:
                raw["ranking_ad_error"] = str(exc)
        organic = first_non_empty(
            positions[0],
            rank_org,
            find_value(traffic_row, ORGANIC_POSITION_KEYS),
            find_value(data["ranking"], ORGANIC_POSITION_KEYS | POSITION_KEYS | RANK_KEYS),
        )
        ad = first_non_empty(rank_ad, positions[1], find_value(traffic_row, AD_POSITION_KEYS), find_value(data["ranking"], AD_POSITION_KEYS))
        bsr_main, bsr_small = XiyouApiClient._latest_bsr_ranks(data["bsr"])
        product_rank = first_non_empty(
            product.get("product_rank", ""),
            bsr_main,
            find_value(data["bsr"], RANK_KEYS),
        )
        small_rank = first_non_empty(product.get("small_category_rank", ""), bsr_small)
        sales = first_non_empty(
            XiyouApiClient._current_month_sales(data["sales"]),
            product.get("estimated_sales", ""),
        )
        aba_container = keyword_row.get("abaReport") if isinstance(keyword_row, dict) else None
        return _finish_result(
            provider=self.provider_name, asin=asin, site=site, raw=raw,
            traffic_share=self._traffic_share(data["traffic"], keyword),
            aba_rank=find_value(aba_container or keyword_row, ABA_RANK_KEYS | {"searchFrequencyRank"}),
            search_volume=find_value(keyword_row, SEARCH_VOLUME_KEYS | {"weeklySearchVolume"}),
            organic_position=organic,
            ad_position=ad,
            organic_time=rank_org_time,
            ad_time=rank_ad_time,
            price=first_non_empty(product.get("price", ""), find_value(data["product"], PRICE_KEYS)),
            coupon_value=product.get("coupon_value", ""),
            deal_price=product.get("deal_price", ""),
            prime_price=product.get("prime_discount_price", ""),
            sales=sales,
            product_rank=product_rank,
            small_category_rank=small_rank,
            rating=first_non_empty(product.get("rating", ""), find_value(data["product"], RATING_KEYS)),
            review_count=first_non_empty(product.get("review_count", ""), find_value(data["product"], REVIEW_KEYS)),
            product_url=find_value(data["product"], {"url", "productUrl", "amazonUrl"}),
        )


class GenericApiClient(BaseApiClient):
    source_name = "custom_api"
    provider_name = "其他软件"

    def __init__(self, endpoint: str, api_key: str, header_name: str = "Authorization") -> None:
        if not endpoint.strip():
            raise ValueError("请填写其他软件 API Endpoint")
        self.endpoint = endpoint.strip()
        self.header_name = header_name.strip() or "Authorization"
        super().__init__(self.endpoint, api_key)

    def headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            value = self.api_key
            if self.header_name.lower() == "authorization" and not value.lower().startswith(("bearer ", "basic ")):
                value = f"Bearer {value}"
            headers[self.header_name] = value
        return headers

    def check_ready(self) -> dict[str, Any]:
        return {"source": self.source_name, "tool_count": 1, "recognized_tools": ["custom_endpoint"], "missing_tools": [], "note": "Endpoint 配置已读取；响应字段将在抓取时按通用字段名解析。"}

    def capture_keyword(self, asin: str, keyword: str, marketplace: str) -> dict[str, Any]:
        site = normalize_marketplace(marketplace)
        asin, keyword = asin.strip().upper(), keyword.strip()
        raw: dict[str, Any] = {}
        try:
            data = self._request("POST", self.endpoint, {"asin": asin, "keyword": keyword, "marketplace": site}, tool_name="custom_endpoint")
            raw["custom_endpoint"] = data
        except Exception as exc:
            data = {}
            raw["custom_endpoint_error"] = str(exc)
        product = parse_product_detail(data)
        keyword_row = find_keyword_row(data, keyword) or data
        return _finish_result(
            provider=self.provider_name, asin=asin, site=site, raw=raw,
            traffic_share=find_value(keyword_row, TRAFFIC_SHARE_KEYS), aba_rank=find_value(keyword_row, ABA_RANK_KEYS),
            search_volume=find_value(keyword_row, SEARCH_VOLUME_KEYS), organic_position=find_value(keyword_row, ORGANIC_POSITION_KEYS),
            ad_position=find_value(keyword_row, AD_POSITION_KEYS), price=product.get("price", ""),
            coupon_value=product.get("coupon_value", ""), deal_price=product.get("deal_price", ""),
            prime_price=product.get("prime_discount_price", ""), sales=product.get("estimated_sales", ""),
            product_rank=product.get("product_rank", ""), small_category_rank=product.get("small_category_rank", ""), rating=product.get("rating", ""),
            review_count=product.get("review_count", ""), product_url=find_value(data, {"url", "productUrl", "amazonUrl"}),
        )


def build_data_client(connection: dict[str, Any] | None = None) -> DataClient:
    connection = connection or {}
    provider = str(connection.get("provider") or "sorftime").lower()
    mode = str(connection.get("mode") or "").lower()
    if provider == "sorftime":
        return build_sorftime_client(connection)
    if provider == "sellersprite":
        return SellerSpriteMcpClient(token=str(connection.get("mcp_token") or ""))
    if provider == "xiyou":
        if mode == "mcp_url":
            return XiyouMcpClient(
                str(connection.get("mcp_url") or "https://mcp.xydc.com/mcp"),
                str(connection.get("mcp_token") or ""),
            )
        return XiyouApiClient(str(connection.get("api_url") or "https://openapi.xydc.com"), str(connection.get("api_key") or ""))
    if provider == "sif":
        url = str(connection.get("mcp_url") or "https://mcp.sif.com/mcp")
        token = str(connection.get("mcp_token") or "")
        if not token.strip():
            raise ValueError("请填写 SIF MCP Key")
        return GenericMcpClient(url, token, provider_name="SIF", source_name="sif_mcp")
    if provider == "custom":
        if mode == "api":
            return GenericApiClient(str(connection.get("api_url") or ""), str(connection.get("api_key") or ""), str(connection.get("api_key_header") or "Authorization"))
        url = str(connection.get("mcp_url") or "")
        if not url:
            raise ValueError("请填写其他软件 MCP URL")
        return GenericMcpClient(url, str(connection.get("mcp_token") or ""), provider_name="其他软件", source_name="custom_mcp")
    raise ValueError("请选择有效的数据源")


def test_data_connection(connection: dict[str, Any]) -> dict[str, Any]:
    provider = str(connection.get("provider") or "sorftime").lower()
    if provider == "sorftime":
        return test_sorftime_connection(connection)
    client = build_data_client(connection)
    started = time.perf_counter()
    try:
        result = client.check_ready()
        result["elapsed_seconds"] = round(time.perf_counter() - started, 2)
        result["provider"] = provider
        return result
    finally:
        client.close()
