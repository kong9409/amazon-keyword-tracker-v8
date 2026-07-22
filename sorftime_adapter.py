from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

BASE_DIR = Path(__file__).resolve().parent
EMPTY = (None, "", [], {})

REQUIRED_SORFTIME_TOOLS = {
    "product_traffic_terms",
    "keyword_detail",
    "keyword_search_results",
    "product_ranking_trend_by_keyword",
    "product_detail",
    "product_trend",
}
CORE_SORFTIME_TOOLS = {
    "product_traffic_terms",
    "keyword_detail",
    "keyword_search_results",
    "product_detail",
}


class SorftimeClient(Protocol):
    source_name: str

    def capture_keyword(self, asin: str, keyword: str, marketplace: str) -> dict[str, Any]: ...

    def stats(self) -> dict[str, Any]: ...

    def check_ready(self) -> dict[str, Any]: ...

    def close(self) -> None: ...


class SorftimeMcpClient:
    """Sorftime MCP client optimized for ASIN × keyword batch collection.

    Data-source mapping follows the uploaded 86-tool matrix:
    - product_traffic_terms: ASIN-specific traffic terms and recent exposure data
    - keyword_detail: keyword ABA/search-volume metrics
    - keyword_search_results(positionType=0/2): current organic/ad result position
    - product_ranking_trend_by_keyword: organic rank fallback
    - product_detail: price/coupon/deal/Prime/sales/rank/rating/reviews
    - product_trend: sales/price/rank fallback only when detail is incomplete
    """

    source_name = "sorftime_mcp_http"

    def __init__(self, url: str, token: str = "") -> None:
        self.url = url
        self.token = token.strip()
        self._session_id = ""
        self._tool_name_map: dict[str, str] = {}
        self._tool_schemas: dict[str, dict[str, Any]] = {}
        self._initialized = False
        self._lock = threading.Lock()
        self.started_at = time.perf_counter()
        self._mcp_calls = 0
        self._tool_calls: Counter[str] = Counter()
        self._tool_seconds: Counter[str] = Counter()

        self.max_traffic_pages = max(1, int(os.getenv("SORFTIME_MAX_TRAFFIC_PAGES", "20")))
        self.max_search_pages = max(1, int(os.getenv("SORFTIME_MAX_SEARCH_PAGES", "3")))
        self.search_page_size = max(1, int(os.getenv("SORFTIME_SEARCH_PAGE_SIZE", "48")))

        self._traffic_rows: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._traffic_next_page: dict[tuple[str, str], int] = {}
        self._traffic_done: set[tuple[str, str]] = set()
        self._keyword_detail_cache: dict[tuple[str, str], Any] = {}
        self._keyword_results_cache: dict[tuple[str, str, int, int], Any] = {}
        self._product_detail_cache: dict[tuple[str, str], Any] = {}
        self._product_report_cache: dict[tuple[str, str], Any] = {}
        self._product_trend_cache: dict[tuple[str, str, str], Any] = {}
        self._ranking_cache: dict[tuple[str, str, str], Any] = {}

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "mcp_calls": self._mcp_calls,
                "elapsed_seconds": round(time.perf_counter() - self.started_at, 2),
                "tool_calls": dict(sorted(self._tool_calls.items())),
                "tool_seconds": {
                    key: round(value, 2)
                    for key, value in sorted(self._tool_seconds.items())
                },
            }

    def capture_keyword(self, asin: str, keyword: str, marketplace: str) -> dict[str, Any]:
        """Collect one ASIN × keyword row with strict, user-approved sources.

        V4 source contract:
        - traffic_share: product_traffic_terms only
        - aba_rank/search_volume: keyword_detail only
        - estimated_sales: product_detail -> product_trend(SalesVolume)
        - product_rank: product_detail -> product_trend(Rank/Ranking)
        Other product metrics come from product_detail, while organic/ad positions
        keep their dedicated search-result/ranking fallbacks.
        """
        self._ensure_initialized()
        site = normalize_marketplace(marketplace)
        asin = asin.strip().upper()
        keyword = keyword.strip()
        raw: dict[str, Any] = {}

        traffic_row = self.find_traffic_term(asin, keyword, site)
        raw["product_traffic_terms_match"] = traffic_row
        traffic_share = extract_traffic_share(traffic_row)

        organic_position = first_non_empty(
            find_value(traffic_row, ORGANIC_POSITION_KEYS),
            traffic_row.get("最近自然曝光位置", ""),
        )
        organic_time = first_non_empty(
            find_value(traffic_row, ORGANIC_TIME_KEYS),
            traffic_row.get("最近自然曝光时间", ""),
        )
        ad_position = first_non_empty(
            find_value(traffic_row, AD_POSITION_KEYS),
            traffic_row.get("最近广告曝光位置", ""),
        )
        ad_time = first_non_empty(
            find_value(traffic_row, AD_TIME_KEYS),
            traffic_row.get("最近广告曝光时间", ""),
        )

        if organic_position in EMPTY:
            organic_match = self.find_in_keyword_results(asin, keyword, site, position_type=0)
            raw["keyword_search_results_organic"] = organic_match
            organic_position = extract_result_position(organic_match, self.search_page_size)
        if ad_position in EMPTY:
            ad_match = self.find_in_keyword_results(asin, keyword, site, position_type=2)
            raw["keyword_search_results_ad"] = ad_match
            ad_position = extract_result_position(ad_match, self.search_page_size)
        if organic_position in EMPTY:
            ranking = self.ranking_trend(asin, keyword, site)
            raw["product_ranking_trend_by_keyword"] = ranking
            organic_position = extract_keyword_rank(ranking)
            organic_time = first_non_empty(organic_time, extract_latest_date(ranking))

        keyword_detail = self.keyword_detail(keyword, site)
        raw["keyword_detail"] = keyword_detail
        aba_rank = extract_aba_rank(keyword_detail)
        search_volume = extract_search_volume(keyword_detail)

        detail = self.product_detail(asin, site)
        raw["product_detail"] = detail
        product = parse_product_detail(detail)
        price = product.get("price", "")
        coupon_value = product.get("coupon_value", "")
        coupon_type = product.get("coupon_type", "")
        deal_status = product.get("deal_status", "")
        deal_price = product.get("deal_price", "")
        prime_price = product.get("prime_discount_price", "")
        sales = product.get("estimated_sales", "")
        rank = product.get("product_rank", "")
        small_rank = product.get("small_category_rank", "")
        rating = product.get("rating", "")
        reviews = product.get("review_count", "")

        if price in EMPTY:
            trend = self.try_product_trends(asin, site, ("Price",))
            raw["product_trend_price"] = trend
            price = extract_latest_metric(trend, PRICE_KEYS)
        if sales in EMPTY:
            trend = self.try_product_trends(asin, site, ("SalesVolume", "Sales", "MonthlySales"))
            raw["product_trend_sales"] = trend
            sales = extract_latest_metric(trend, SALES_KEYS | VALUE_KEYS)
        if rank in EMPTY:
            trend = self.try_product_trends(asin, site, ("Rank", "Ranking", "SalesRank"))
            raw["product_trend_rank"] = trend
            rank = extract_latest_rank(trend)

        keyword_rank = first_non_empty(organic_position, ad_position)
        values = (
            traffic_share, aba_rank, search_volume, organic_position, ad_position,
            price, coupon_value, deal_price, prime_price, sales, rank, small_rank, rating, reviews,
        )
        found_any = any(value not in EMPTY for value in values)

        source_contract = {
            "流量占比": (traffic_share, "product_traffic_terms"),
            "ABA热度": (aba_rank, "keyword_detail"),
            "搜索量": (search_volume, "keyword_detail"),
            "自然位": (organic_position, "keyword_search_results / product_ranking_trend_by_keyword"),
            "广告位": (ad_position, "keyword_search_results"),
            "价格": (price, "product_detail / product_trend"),
            "月销量": (sales, "product_detail / product_trend"),
            "大类排名": (rank, "product_detail / product_trend"),
            "评分": (rating, "product_detail"),
            "评价数": (reviews, "product_detail"),
        }
        missing = [
            f"{label}（{source}）"
            for label, (value, source) in source_contract.items()
            if value in EMPTY
        ]
        status = "ok" if found_any and not missing else ("partial" if found_any else "not_found")
        message = ""
        if missing:
            message = "Sorftime 未返回：" + "、".join(missing)
        if not found_any:
            diagnostic = summarize_errors(raw)
            if diagnostic:
                message = (message + "；" if message else "") + diagnostic

        return {
            "keyword_rank": keyword_rank,
            "organic_position": normalize_position(organic_position),
            "organic_time": organic_time,
            "ad_position": normalize_position(ad_position),
            "ad_time": ad_time,
            "traffic_share": normalize_percent(traffic_share),
            "aba_rank": normalize_number(aba_rank),
            "search_volume": normalize_number(search_volume),
            "price": normalize_money(price),
            "coupon_type": first_non_empty(coupon_type, classify_coupon(coupon_value)),
            "coupon_value": coupon_value,
            "deal_status": normalize_yes_no(deal_status, bool(deal_price)),
            "deal_price": normalize_money(deal_price),
            "prime_discount_price": normalize_money(prime_price),
            "estimated_sales": normalize_number(sales),
            "product_rank": normalize_number(rank),
            "small_category_rank": normalize_number(small_rank),
            "rating": normalize_decimal(rating),
            "review_count": normalize_number(reviews),
            "product_url": amazon_product_url(asin, site),
            "status": status,
            "message": message,
            "raw": raw,
        }

    def find_traffic_term(self, asin: str, keyword: str, site: str) -> dict[str, Any]:
        cache_key = (asin, site)
        target = normalize_keyword(keyword)
        rows = self._traffic_rows.setdefault(cache_key, [])
        for row in rows:
            if normalize_keyword(row_keyword(row)) == target:
                return dict(row)

        next_page = self._traffic_next_page.get(cache_key, 1)
        while cache_key not in self._traffic_done and next_page <= self.max_traffic_pages:
            response = self.call_tool(
                "product_traffic_terms",
                {"asin": asin, "amzSite": site, "page": next_page},
            )
            page_rows = [
                row for row in collect_dict_rows(response)
                if isinstance(row, dict) and row_keyword(row)
            ]
            rows.extend(page_rows)
            self._traffic_next_page[cache_key] = next_page + 1
            for row in page_rows:
                if normalize_keyword(row_keyword(row)) == target:
                    return dict(row)
            if not page_rows or len(page_rows) < 20:
                self._traffic_done.add(cache_key)
                break
            next_page += 1

        self._traffic_done.add(cache_key)
        return find_keyword_row(rows, keyword)

    def keyword_detail(self, keyword: str, site: str) -> Any:
        key = (keyword.casefold(), site)
        if key not in self._keyword_detail_cache:
            self._keyword_detail_cache[key] = self.call_tool(
                "keyword_detail",
                {"keyword": keyword, "keywordSupportSite": site},
            )
        return self._keyword_detail_cache[key]

    def find_in_keyword_results(
        self,
        asin: str,
        keyword: str,
        site: str,
        position_type: int,
    ) -> dict[str, Any]:
        target = asin.upper()
        for page in range(1, self.max_search_pages + 1):
            result = self.keyword_search_results(keyword, site, position_type, page)
            rows = collect_dict_rows(result)
            for index, row in enumerate(rows, start=1):
                row_asin = str(find_value(row, ASIN_KEYS)).upper().strip()
                if row_asin == target:
                    match = dict(row)
                    match.setdefault("_page", page)
                    match.setdefault("_index", index)
                    match.setdefault("_position_type", position_type)
                    return match
            if not rows:
                break
        return {}

    def keyword_search_results(self, keyword: str, site: str, position_type: int, page: int) -> Any:
        key = (keyword.casefold(), site, position_type, page)
        if key not in self._keyword_results_cache:
            self._keyword_results_cache[key] = self.call_tool(
                "keyword_search_results",
                {
                    "keyword": keyword,
                    "keywordSupportSite": site,
                    "positionType": position_type,
                    "page": page,
                },
            )
        return self._keyword_results_cache[key]

    def ranking_trend(self, asin: str, keyword: str, site: str) -> Any:
        key = (asin, keyword.casefold(), site)
        if key not in self._ranking_cache:
            self._ranking_cache[key] = self.call_tool(
                "product_ranking_trend_by_keyword",
                {"asin": asin, "keyword": keyword, "marketplace": site},
            )
        return self._ranking_cache[key]

    def product_detail(self, asin: str, site: str) -> Any:
        key = (asin, site)
        if key not in self._product_detail_cache:
            self._product_detail_cache[key] = self.call_tool(
                "product_detail", {"asin": asin, "amzSite": site}
            )
        return self._product_detail_cache[key]

    def product_report(self, asin: str, site: str) -> Any:
        key = (asin, site)
        if key not in self._product_report_cache:
            self._product_report_cache[key] = self.call_tool(
                "product_report", {"asin": asin, "amzSite": site}
            )
        return self._product_report_cache[key]

    def product_trend(self, asin: str, trend_type: str, site: str) -> Any:
        key = (asin, trend_type, site)
        if key not in self._product_trend_cache:
            self._product_trend_cache[key] = self.call_tool(
                "product_trend",
                {"asin": asin, "amzSite": site, "productTrendType": trend_type},
            )
        return self._product_trend_cache[key]

    def try_product_trends(
        self,
        asin: str,
        site: str,
        trend_types: tuple[str, ...],
    ) -> Any:
        """Try equivalent trend enum names without aborting the whole row."""
        errors: list[str] = []
        for trend_type in trend_types:
            try:
                data = self.product_trend(asin, trend_type, site)
            except Exception as exc:
                errors.append(f"{trend_type}: {exc}")
                continue
            if data not in EMPTY:
                return data
        return {"_trend_errors": errors} if errors else {}

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self._ensure_initialized()
        if self._tool_name_map and name not in self._tool_name_map:
            raise RuntimeError(f"Sorftime MCP 未提供 Amazon 工具：{name}")
        actual_name = self._tool_name_map.get(name, name)
        schema = self._tool_schemas.get(name) or {}
        call_arguments = adapt_tool_arguments(name, arguments, schema)
        started = time.perf_counter()
        response = self._post(
            {
                "jsonrpc": "2.0",
                "id": int(time.time() * 1000),
                "method": "tools/call",
                "params": {"name": actual_name, "arguments": call_arguments},
            }
        )
        elapsed = time.perf_counter() - started
        with self._lock:
            self._mcp_calls += 1
            self._tool_calls[name] += 1
            self._tool_seconds[name] += elapsed
        if "error" in response:
            error = response.get("error") or {}
            detail = error.get("data")
            suffix = f"：{detail}" if detail else ""
            raise RuntimeError((error.get("message") or f"Sorftime tool failed: {name}") + suffix)
        result = response.get("result", {})
        if result.get("isError"):
            text = parse_tool_result(result)
            raise RuntimeError(f"Sorftime {name} 返回错误：{text}")
        parsed = parse_tool_result(result)
        validate_sorftime_payload(name, parsed)
        return parsed

    def list_tools(self) -> list[str]:
        self._ensure_initialized()
        response = self._post(
            {
                "jsonrpc": "2.0",
                "id": int(time.time() * 1000),
                "method": "tools/list",
                "params": {},
            }
        )
        if "error" in response:
            error = response.get("error") or {}
            raise RuntimeError(error.get("message") or "Sorftime MCP tools/list failed")
        tools = response.get("result", {}).get("tools", []) or []
        names = [str(item.get("name", "")).strip() for item in tools if isinstance(item, dict)]
        names = [name for name in names if name]
        self._tool_name_map = build_tool_name_map(names)
        by_actual = {str(item.get("name", "")).strip(): item for item in tools if isinstance(item, dict)}
        self._tool_schemas = {}
        for canonical, actual in self._tool_name_map.items():
            item = by_actual.get(actual) or {}
            schema = item.get("inputSchema") or item.get("input_schema") or {}
            if isinstance(schema, dict):
                self._tool_schemas[canonical] = schema
        return names

    def check_ready(self) -> dict[str, Any]:
        names = self.list_tools()
        recognized = sorted(name for name in REQUIRED_SORFTIME_TOOLS if name in self._tool_name_map)
        if not names:
            raise RuntimeError("MCP 已连接，但 tools/list 未返回任何工具")
        if not recognized:
            raise RuntimeError(
                "MCP 已连接，但没有识别到 Sorftime 工具。请确认输入的是 Sorftime MCP，而不是普通网页地址。"
            )
        missing = sorted(REQUIRED_SORFTIME_TOOLS - set(recognized))
        missing_core = sorted(CORE_SORFTIME_TOOLS - set(recognized))
        if missing_core:
            raise RuntimeError(
                "MCP 已连接，但缺少 Amazon 关键词监控核心工具："
                + "、".join(missing_core)
                + "。请确认连接的是 Sorftime Amazon MCP。"
            )
        return {
            "source": self.source_name,
            "tool_count": len(names),
            "recognized_tools": recognized,
            "resolved_tools": {name: self._tool_name_map[name] for name in recognized},
            "missing_tools": missing,
        }

    def close(self) -> None:
        return

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        response = self._post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "amazon-keyword-tracker", "version": "4.0"},
                },
            }
        )
        if "error" in response:
            error = response.get("error") or {}
            raise RuntimeError(error.get("message") or "Sorftime MCP initialize failed")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        self._initialized = True

    def _auth_headers(self) -> dict[str, str]:
        if not self.token:
            return {}
        return {"Authorization": self.token if self.token.lower().startswith("bearer ") else f"Bearer {self.token}"}

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        retryable_http = {429, 500, 502, 503, 504}
        last_error: Exception | None = None
        for attempt in range(3):
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-03-26",
            }
            if self._session_id:
                headers["Mcp-Session-Id"] = self._session_id
            headers.update(self._auth_headers())
            request = urllib.request.Request(
                self.url,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    session_id = response.headers.get("Mcp-Session-Id", "")
                    if session_id:
                        self._session_id = session_id
                    body = response.read().decode("utf-8", errors="replace")
                if not body.strip():
                    return {}
                return parse_mcp_response(body)
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                message = extract_http_error(body) or exc.reason
                last_error = RuntimeError(f"Sorftime MCP HTTP {exc.code}：{message}")
                if exc.code not in retryable_http or attempt >= 2:
                    raise last_error from exc
            except urllib.error.URLError as exc:
                last_error = RuntimeError(f"无法连接 Sorftime MCP：{exc.reason}")
                if attempt >= 2:
                    raise last_error from exc
            time.sleep(0.8 * (attempt + 1))
        if last_error:
            raise last_error
        return {}


class SorftimeCliClient:
    """Safe Sorftime CLI adapter for a hosted web application.

    The user supplies only an Account-SK. The server never executes a user-provided
    shell command. It invokes the installed ``sorftime`` binary with a fixed allowlist
    of API methods, stores the temporary profile in an isolated HOME directory, and
    deletes that directory when the job finishes.
    """

    source_name = "sorftime_cli"

    def __init__(self, account_sk: str) -> None:
        self.account_sk = account_sk.strip()
        if not self.account_sk:
            raise ValueError("请填写 Sorftime CLI Account-SK")
        self.cli_path = shutil.which("sorftime") or shutil.which("sorftime.cmd")
        if not self.cli_path:
            raise RuntimeError("服务器未安装 sorftime-cli，请重新部署包含 CLI 的 Dockerfile")
        self._tmp_home = Path(tempfile.mkdtemp(prefix="sorftime-kwt-"))
        self._env = os.environ.copy()
        self._env.update({
            "HOME": str(self._tmp_home),
            "USERPROFILE": str(self._tmp_home),
            "APPDATA": str(self._tmp_home / "AppData"),
            "XDG_CONFIG_HOME": str(self._tmp_home / ".config"),
        })
        self.started_at = time.perf_counter()
        self._calls = 0
        self._tool_calls: Counter[str] = Counter()
        self._tool_seconds: Counter[str] = Counter()
        self._lock = threading.Lock()
        self._configured = False
        self._keyword_rows_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._keyword_detail_cache: dict[tuple[str, str], Any] = {}
        self._product_cache: dict[tuple[str, str], Any] = {}

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "mcp_calls": self._calls,
                "elapsed_seconds": round(time.perf_counter() - self.started_at, 2),
                "tool_calls": dict(sorted(self._tool_calls.items())),
                "tool_seconds": {key: round(value, 2) for key, value in sorted(self._tool_seconds.items())},
            }

    def _run(self, args: list[str], *, tool_name: str = "", timeout: int = 120) -> Any:
        started = time.perf_counter()
        completed = subprocess.run(
            [self.cli_path, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=self._env,
        )
        elapsed = time.perf_counter() - started
        if tool_name:
            with self._lock:
                self._calls += 1
                self._tool_calls[tool_name] += 1
                self._tool_seconds[tool_name] += elapsed
        output = (completed.stdout or "").strip()
        error = (completed.stderr or "").strip()
        if completed.returncode != 0:
            raise RuntimeError(error or output or f"Sorftime CLI 执行失败（code={completed.returncode}）")
        if not output:
            return {}
        clean = re.sub(r"\x1b\[[0-9;]*m", "", output)
        match = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", clean)
        if not match:
            return {"text": clean}
        try:
            value = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Sorftime CLI 返回内容不是有效 JSON：{clean[:300]}") from exc
        if isinstance(value, dict) and value.get("Code") not in (None, 0):
            raise RuntimeError(str(value.get("Message") or value.get("Msg") or f"Sorftime Code {value.get('Code')}"))
        if isinstance(value, dict) and "Data" in value:
            return value.get("Data") or {}
        return value

    def _ensure_configured(self) -> None:
        if self._configured:
            return
        self._run(["add", "keyword-tracker", self.account_sk], timeout=60)
        self._run(["use", "keyword-tracker"], timeout=60)
        self._configured = True

    def check_ready(self) -> dict[str, Any]:
        self._ensure_configured()
        identity = self._run(["whoami"], timeout=60)
        return {
            "source": self.source_name,
            "tool_count": 3,
            "recognized_tools": ["ASINRequestKeywordv2", "KeywordRequest", "ProductRequest"],
            "missing_tools": [],
            "identity": identity,
        }

    def _api(self, endpoint: str, params: dict[str, Any], site: str) -> Any:
        self._ensure_configured()
        return self._run(
            ["api", endpoint, json.dumps(params, ensure_ascii=False, separators=(",", ":")), "--domain", str(domain_for_site(site))],
            tool_name=endpoint,
        )

    def keyword_rows(self, asin: str, site: str) -> list[dict[str, Any]]:
        key = (asin.upper(), site)
        if key not in self._keyword_rows_cache:
            data = self._api("ASINRequestKeywordv2", {"asin": asin, "pageIndex": 1, "pageSize": 1000}, site)
            self._keyword_rows_cache[key] = collect_dict_rows(data)
        return self._keyword_rows_cache[key]

    def keyword_detail(self, keyword: str, site: str) -> Any:
        key = (keyword.casefold(), site)
        if key not in self._keyword_detail_cache:
            self._keyword_detail_cache[key] = self._api("KeywordRequest", {"keyword": keyword}, site)
        return self._keyword_detail_cache[key]

    def product_detail(self, asin: str, site: str) -> Any:
        key = (asin.upper(), site)
        if key not in self._product_cache:
            self._product_cache[key] = self._api("ProductRequest", {"asin": asin}, site)
        return self._product_cache[key]

    def capture_keyword(self, asin: str, keyword: str, marketplace: str) -> dict[str, Any]:
        site = normalize_marketplace(marketplace)
        asin = asin.strip().upper()
        keyword = keyword.strip()
        target = normalize_keyword(keyword)
        keyword_row: dict[str, Any] = {}
        for row in self.keyword_rows(asin, site):
            if normalize_keyword(row_keyword(row)) == target:
                keyword_row = dict(row)
                break
        raw: dict[str, Any] = {"keyword_row": keyword_row}
        organic_position = find_value(keyword_row, ORGANIC_POSITION_KEYS)
        ad_position = find_value(keyword_row, AD_POSITION_KEYS)
        traffic_share = find_value(keyword_row, TRAFFIC_SHARE_KEYS)
        aba_rank = find_value(keyword_row, ABA_RANK_KEYS)
        search_volume = find_value(keyword_row, SEARCH_VOLUME_KEYS)

        # The CLI matrix maps keyword-level search volume/heat to KeywordRequest.
        # ASINRequestKeywordv2 remains the primary source for ASIN-specific share
        # and position; KeywordRequest is called only when keyword metrics are absent.
        if aba_rank in EMPTY or search_volume in EMPTY:
            try:
                keyword_data = self.keyword_detail(keyword, site)
                raw["keyword_detail"] = keyword_data
                aba_rank = first_non_empty(aba_rank, find_value(keyword_data, ABA_RANK_KEYS))
                search_volume = first_non_empty(search_volume, find_value(keyword_data, SEARCH_VOLUME_KEYS))
            except Exception as exc:
                raw["keyword_detail_error"] = str(exc)

        detail = self.product_detail(asin, site)
        raw["product_detail"] = detail
        product = parse_product_detail(detail)
        core_values = {
            "流量占比": traffic_share,
            "ABA热度": aba_rank,
            "搜索量": search_volume,
            "自然位": organic_position,
            "价格": product.get("price", ""),
            "月销量": product.get("estimated_sales", ""),
            "大类排名": product.get("product_rank", ""),
            "评分": product.get("rating", ""),
            "评价数": product.get("review_count", ""),
        }
        found_any = any(value not in EMPTY for value in [*core_values.values(), ad_position])
        missing_core = [label for label, value in core_values.items() if value in EMPTY]
        status = "ok" if found_any and not missing_core else ("partial" if found_any else "not_found")
        message = "" if not missing_core else "Sorftime CLI 未返回：" + "、".join(missing_core)
        if not found_any:
            message = "Sorftime CLI 未返回匹配数据。"
        return {
            "keyword_rank": first_non_empty(organic_position, ad_position),
            "organic_position": normalize_position(organic_position),
            "organic_time": find_value(keyword_row, ORGANIC_TIME_KEYS),
            "ad_position": normalize_position(ad_position),
            "ad_time": find_value(keyword_row, AD_TIME_KEYS),
            "traffic_share": normalize_percent(traffic_share),
            "aba_rank": normalize_number(aba_rank),
            "search_volume": normalize_number(search_volume),
            "price": normalize_money(product.get("price", "")),
            "coupon_type": product.get("coupon_type", ""),
            "coupon_value": product.get("coupon_value", ""),
            "deal_status": product.get("deal_status", ""),
            "deal_price": normalize_money(product.get("deal_price", "")),
            "prime_discount_price": normalize_money(product.get("prime_discount_price", "")),
            "estimated_sales": normalize_number(product.get("estimated_sales", "")),
            "product_rank": normalize_number(product.get("product_rank", "")),
            "small_category_rank": normalize_number(product.get("small_category_rank", "")),
            "rating": normalize_decimal(product.get("rating", "")),
            "review_count": normalize_number(product.get("review_count", "")),
            "product_url": amazon_product_url(asin, site),
            "status": status,
            "message": message,
            "raw": raw,
        }

    def close(self) -> None:
        shutil.rmtree(self._tmp_home, ignore_errors=True)


class SorftimeStdioMcpClient(SorftimeMcpClient):
    source_name = "sorftime_mcp_stdio"

    def __init__(self, command: str, cwd: str = "") -> None:
        super().__init__("stdio://local")
        self.command = command.strip()
        self.cwd = cwd.strip() or None
        self._process: subprocess.Popen[str] | None = None
        self._messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self._stderr_tail: list[str] = []
        self._write_lock = threading.Lock()

    def _start_process(self) -> None:
        if self._process and self._process.poll() is None:
            return
        if not self.command:
            raise RuntimeError("请填写本机 Sorftime MCP/CLI 命令")
        cwd = self.cwd
        if cwd and not Path(cwd).expanduser().is_dir():
            raise RuntimeError(f"CLI 工作目录不存在：{cwd}")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        self._process = subprocess.Popen(
            self.command,
            cwd=str(Path(cwd).expanduser()) if cwd else None,
            shell=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        process = self._process
        if not process or not process.stdout:
            return
        stream = process.stdout
        while True:
            line = stream.readline()
            if line == "":
                break
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.lower().startswith("content-length:"):
                try:
                    length = int(stripped.split(":", 1)[1].strip())
                    while True:
                        header = stream.readline()
                        if header in {"", "\n", "\r\n"}:
                            break
                    text = stream.read(length)
                    self._queue_json(text)
                except Exception as exc:
                    self._stderr_tail.append(f"stdio framing error: {exc}")
                continue
            self._queue_json(stripped)

    def _queue_json(self, text: str) -> None:
        try:
            value = json.loads(text)
            if isinstance(value, dict):
                self._messages.put(value)
        except json.JSONDecodeError:
            self._stderr_tail.append(text[-500:])
            self._stderr_tail = self._stderr_tail[-50:]

    def _read_stderr(self) -> None:
        process = self._process
        if not process or not process.stderr:
            return
        for line in process.stderr:
            text = line.rstrip()
            if text:
                self._stderr_tail.append(text[-500:])
                self._stderr_tail = self._stderr_tail[-50:]

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._start_process()
        process = self._process
        if not process or not process.stdin:
            raise RuntimeError("Sorftime CLI 未能启动")
        if process.poll() is not None:
            detail = "\n".join(self._stderr_tail[-8:])
            raise RuntimeError(f"Sorftime CLI 已退出（code={process.returncode}）{': ' + detail if detail else ''}")
        request_id = payload.get("id")
        with self._write_lock:
            process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            process.stdin.flush()
        if request_id is None:
            return {}
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if process.poll() is not None:
                detail = "\n".join(self._stderr_tail[-8:])
                raise RuntimeError(f"Sorftime CLI 提前退出（code={process.returncode}）{': ' + detail if detail else ''}")
            try:
                message = self._messages.get(timeout=min(0.5, max(0.01, deadline - time.monotonic())))
            except queue.Empty:
                continue
            if message.get("id") == request_id:
                return message
        detail = "\n".join(self._stderr_tail[-8:])
        raise RuntimeError(f"等待 Sorftime CLI 响应超时{': ' + detail if detail else ''}")

    def close(self) -> None:
        process = self._process
        self._process = None
        if not process or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=3)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass


NON_AMAZON_TOOL_TOKENS = {
    "tiktok", "temu", "shopee", "walmart", "ebay", "aliexpress",
    "lazada", "etsy", "shopify", "shein",
}


def normalize_tool_name(value: str) -> str:
    """Normalize MCP tool names while retaining namespace words for scoring."""
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")


def tool_match_score(actual: str, required: str) -> int:
    """Score a Sorftime tool candidate without crossing marketplace namespaces.

    The former ``endswith(required)`` matcher could resolve ``product_detail`` to
    ``tiktok_product_detail`` when TikTok tools appeared first in tools/list.
    Exact Amazon/Sorftime candidates now win, and non-Amazon platform names are
    rejected completely.
    """
    normalized = normalize_tool_name(actual)
    tokens = set(normalized.split("_"))
    if tokens & NON_AMAZON_TOOL_TOKENS:
        return -1
    if normalized == required:
        return 100
    if normalized == f"amazon_{required}":
        return 98
    if normalized == f"sorftime_{required}":
        return 96
    if normalized.endswith(f"_amazon_{required}"):
        return 94
    if normalized.endswith(f"_sorftime_{required}"):
        return 92
    if normalized.endswith(f"_{required}"):
        return 80
    return -1


def build_tool_name_map(names: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for required in REQUIRED_SORFTIME_TOOLS:
        scored = [
            (tool_match_score(actual, required), index, actual)
            for index, actual in enumerate(names)
        ]
        scored = [item for item in scored if item[0] >= 0]
        if scored:
            # Highest semantic score wins; original tools/list order breaks ties.
            _, _, actual = sorted(scored, key=lambda item: (-item[0], item[1]))[0]
            mapping[required] = actual
    return mapping


def extract_http_error(body: str) -> str:
    try:
        payload = parse_mcp_response(body)
        error = payload.get("error") or {}
        return str(error.get("message") or error.get("data") or "").strip()
    except Exception:
        return body.strip()[:500]


def build_sorftime_client(connection: dict[str, Any] | None = None) -> SorftimeClient:
    connection = connection or {}
    mode = str(connection.get("mode", "")).strip()
    if mode == "mcp_url":
        url = str(connection.get("mcp_url", "")).strip()
        if not url:
            raise ValueError("请先输入 Sorftime MCP URL")
        if not re.match(r"^https?://", url, re.I):
            raise ValueError("Sorftime MCP URL 必须以 http:// 或 https:// 开头")
        return SorftimeMcpClient(url, str(connection.get("mcp_token", "")))
    if mode == "cli_account":
        return SorftimeCliClient(str(connection.get("cli_account_sk", "")))
    if mode == "mcp_stdio":  # backward compatibility for local-only legacy profiles
        command = str(connection.get("cli_command", "")).strip()
        if not command:
            raise ValueError("请先输入本机 Sorftime MCP/CLI 命令")
        return SorftimeStdioMcpClient(command, str(connection.get("cli_cwd", "")))
    raise ValueError("请选择 Sorftime CLI 或 MCP，并填写连接信息")


def test_sorftime_connection(connection: dict[str, Any]) -> dict[str, Any]:
    client = build_sorftime_client(connection)
    started = time.perf_counter()
    try:
        result = client.check_ready()
        result["elapsed_seconds"] = round(time.perf_counter() - started, 2)
        return result
    finally:
        client.close()


def capture_batch(
    client: SorftimeClient,
    asins: list[str],
    keywords: list[str],
    marketplace: str,
    progress_callback: Any | None = None,
) -> list[dict[str, Any]]:
    captured_at = datetime.now().isoformat(timespec="seconds")
    capture_date = datetime.now().date().isoformat()
    records: list[dict[str, Any]] = []
    total = max(1, len(asins) * len(keywords))
    done = 0
    for asin in asins:
        for keyword in keywords:
            done += 1
            if progress_callback:
                progress_callback(done, total, asin, keyword, client.stats())
            try:
                result = client.capture_keyword(asin, keyword, marketplace)
            except Exception as exc:
                result = empty_result(str(exc))
            records.append(
                {
                    "date": capture_date,
                    "captured_at": captured_at,
                    "marketplace": marketplace,
                    "asin": asin,
                    "keyword": keyword,
                    **{key: result.get(key, "") for key in RESULT_KEYS},
                    "source": client.source_name,
                    "status": result.get("status", "ok"),
                    "message": result.get("message", ""),
                    "raw": result.get("raw", result),
                }
            )
    return records


RESULT_KEYS = [
    "keyword_rank", "organic_position", "organic_time", "ad_position", "ad_time",
    "traffic_share", "aba_rank", "search_volume", "price", "coupon_type",
    "coupon_value", "deal_status", "deal_price", "prime_discount_price",
    "estimated_sales", "product_rank", "rating", "review_count", "product_url",
]


def empty_result(message: str) -> dict[str, Any]:
    result = {key: "" for key in RESULT_KEYS}
    result.update({"status": "failed", "message": message, "raw": {}})
    return result


def parse_mcp_response(body: str) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    for line in body.splitlines():
        if line.startswith("data:"):
            text = line[5:].strip()
            if text and text != "[DONE]":
                events.append(json.loads(text))
    if events:
        return events[-1]
    return json.loads(body)


def parse_tool_result(result: Any) -> Any:
    """Return usable tool data from modern and legacy MCP result shapes.

    Newer MCP servers may place JSON exclusively in ``structuredContent``.
    Older servers usually serialize it in TextContent. Sorftime responses have
    appeared in both forms, including fenced JSON and nested JSON strings.
    """
    if not isinstance(result, dict):
        return deep_parse_json(result)
    structured = result.get("structuredContent")
    if structured not in EMPTY:
        return unwrap_response_envelope(deep_parse_json(structured))
    for key in ("data", "Data", "output", "resultData"):
        if result.get(key) not in EMPTY:
            return unwrap_response_envelope(deep_parse_json(result.get(key)))
    return unwrap_response_envelope(parse_tool_content(result.get("content", [])))


def parse_tool_content(content: Any) -> Any:
    parsed: list[Any] = []
    if isinstance(content, dict):
        content = [content]
    if not isinstance(content, list):
        return deep_parse_json(content)
    for item in content:
        if not isinstance(item, dict):
            parsed.append(deep_parse_json(item))
            continue
        item_type = str(item.get("type") or "").lower()
        if item_type == "text" or "text" in item:
            parsed.append(deep_parse_json(item.get("text", "")))
        elif item_type == "resource" and isinstance(item.get("resource"), dict):
            resource = item["resource"]
            parsed.append(deep_parse_json(resource.get("text") or resource.get("blob") or resource))
        elif "data" in item:
            parsed.append(deep_parse_json(item["data"]))
        elif "json" in item:
            parsed.append(deep_parse_json(item["json"]))
    parsed = [value for value in parsed if value not in EMPTY]
    if not parsed:
        return {}
    return parsed[0] if len(parsed) == 1 else parsed


def deep_parse_json(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        fence = re.fullmatch(r"```(?:json|javascript|js)?\s*([\s\S]*?)\s*```", text, re.I)
        if fence:
            text = fence.group(1).strip()
        candidates = [text]
        match = re.search(r"([\[{][\s\S]*[\]}])", text)
        if match and match.group(1) != text:
            candidates.append(match.group(1))
        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
                return deep_parse_json(parsed, depth + 1)
            except (json.JSONDecodeError, TypeError):
                continue
        return text
    if isinstance(value, list):
        return [deep_parse_json(item, depth + 1) for item in value]
    if isinstance(value, dict):
        return {key: deep_parse_json(item, depth + 1) for key, item in value.items()}
    return value


def unwrap_response_envelope(value: Any) -> Any:
    """Unwrap common Sorftime/API success envelopes without dropping metadata."""
    current = value
    for _ in range(5):
        if not isinstance(current, dict):
            break
        code = current.get("Code", current.get("code"))
        if code not in (None, 0, "0", 200, "200"):
            return current
        next_value = None
        for key in ("Data", "data", "Result", "result", "payload", "rows", "items"):
            candidate = current.get(key)
            if candidate not in EMPTY:
                next_value = candidate
                break
        if next_value is None or next_value is current:
            break
        current = deep_parse_json(next_value)
    return current


def adapt_tool_arguments(tool_name: str, arguments: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Adapt logical arguments to the actual inputSchema returned by tools/list."""
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict) or not properties:
        return dict(arguments)
    allowed = set(properties)
    result: dict[str, Any] = {}
    # Sorftime's public matrix documents camelCase parameter names, while the
    # live MCP inputSchema currently exposes several of them in snake_case
    # (for example ``amz_site`` and ``keyword_support_site``).  Keep both forms
    # in the same alias group so the site is never silently dropped.
    aliases = {
        "asin": ("asin", "ASIN", "productAsin", "product_asin"),
        "keyword": ("keyword", "searchTerm", "search_term", "keywordName", "keyword_name"),
        "amzSite": (
            "amzSite", "amz_site", "keywordSupportSite", "keyword_support_site",
            "marketplace", "site", "amazonSite", "amazon_site",
        ),
        "amz_site": (
            "amz_site", "amzSite", "keyword_support_site", "keywordSupportSite",
            "marketplace", "site", "amazon_site", "amazonSite",
        ),
        "keywordSupportSite": (
            "keywordSupportSite", "keyword_support_site", "amzSite", "amz_site",
            "marketplace", "site", "amazonSite", "amazon_site",
        ),
        "keyword_support_site": (
            "keyword_support_site", "keywordSupportSite", "amz_site", "amzSite",
            "marketplace", "site", "amazon_site", "amazonSite",
        ),
        "marketplace": (
            "marketplace", "amz_site", "amzSite", "keyword_support_site",
            "keywordSupportSite", "site", "amazon_site", "amazonSite",
        ),
        "page": ("page", "pageIndex", "page_index", "pageNum", "page_num"),
        "positionType": ("positionType", "position_type", "type"),
        "productTrendType": (
            "productTrendType", "product_trend_type", "trendType", "trend_type",
        ),
    }
    for source_key, value in arguments.items():
        candidates = aliases.get(source_key, (source_key,))
        target = next((name for name in candidates if name in allowed), None)
        if target:
            property_schema = properties.get(target) if isinstance(properties, dict) else None
            result[target] = adapt_schema_value(source_key, value, property_schema)
    required = schema.get("required") or []
    # Keep original keys only when schema is permissive or aliases did not cover them.
    if schema.get("additionalProperties") is not False:
        for key, value in arguments.items():
            if key in allowed and key not in result:
                result[key] = value
    missing = [name for name in required if name not in result]
    if missing:
        raise RuntimeError(
            f"Sorftime {tool_name} 参数无法匹配 MCP inputSchema，缺少：{'、'.join(map(str, missing))}"
        )
    return result


def adapt_schema_value(source_key: str, value: Any, property_schema: Any) -> Any:
    """Map logical values to the enum spelling exposed by the live MCP schema."""
    if not isinstance(property_schema, dict):
        return value
    enum = property_schema.get("enum")
    if not isinstance(enum, list) or not enum:
        return value
    if value in enum:
        return value
    normalized = normalize_field_key(value)
    for item in enum:
        if normalize_field_key(item) == normalized:
            return item
    if source_key == "productTrendType":
        aliases = {
            "rank": {"rank", "ranking", "salesrank", "categoryrank", "bsr"},
            "ranking": {"rank", "ranking", "salesrank", "categoryrank", "bsr"},
            "salesrank": {"rank", "ranking", "salesrank", "categoryrank", "bsr"},
            "salesvolume": {"salesvolume", "sales", "monthlysales", "unitsold"},
            "monthlysales": {"salesvolume", "sales", "monthlysales", "unitsold"},
            "price": {"price", "salesprice", "listingprice"},
        }
        wanted = aliases.get(normalized, {normalized})
        for item in enum:
            if normalize_field_key(item) in wanted:
                return item
    return value


def validate_sorftime_payload(tool_name: str, payload: Any) -> None:
    """Raise clear errors for Sorftime business-level failures hidden in tool data."""
    # Sorftime sometimes returns validation failures as plain TextContent rather
    # than a JSON error envelope.  Treat those strings as real errors instead of
    # letting the tracker mislabel them as "未返回匹配数据".
    if isinstance(payload, str):
        text = payload.strip()
        if text and is_error_text(text):
            raise RuntimeError(f"Sorftime {tool_name} 返回错误：{text}")
        return
    if not isinstance(payload, dict):
        return
    code = payload.get("Code", payload.get("code"))
    message = first_non_empty(
        payload.get("Message"), payload.get("message"), payload.get("Msg"), payload.get("msg")
    )
    if code not in (None, 0, "0", 200, "200"):
        raise RuntimeError(f"Sorftime {tool_name} 业务错误（Code={code}）：{message or payload}")
    request_left = first_non_empty(
        payload.get("RequestLeft"), payload.get("requestLeft"), payload.get("request_left")
    )
    data = first_non_empty(payload.get("Data"), payload.get("data"), payload.get("Result"), payload.get("result"))
    if request_left not in EMPTY:
        try:
            exhausted = float(str(request_left).replace(",", "")) <= 0
        except ValueError:
            exhausted = False
        if exhausted and data in EMPTY:
            raise RuntimeError(f"Sorftime {tool_name} 可用请求次数不足（RequestLeft={request_left}）")
    if message not in EMPTY and is_error_text(str(message)) and data in EMPTY:
        raise RuntimeError(f"Sorftime {tool_name} 返回错误：{message}")


def collect_dict_rows(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        rows: list[dict[str, Any]] = []
        for item in data:
            rows.extend(collect_dict_rows(item))
        return rows
    if isinstance(data, dict):
        likely_row = any(key in data for key in ASIN_KEYS | KEYWORD_KEYS | POSITION_KEYS | DATE_KEYS)
        nested_rows: list[dict[str, Any]] = []
        had_nested_container = False
        for value in data.values():
            if isinstance(value, (list, dict)):
                had_nested_container = True
                nested_rows.extend(collect_dict_rows(value))
        if likely_row:
            return [data]
        # An API wrapper such as {"data": []} is an empty result, not one row.
        # Treating it as a row would unnecessarily request every fallback page.
        if had_nested_container:
            return nested_rows
        return [data]
    return []


def normalize_field_key(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)


def normalize_keyword(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    return re.sub(r"\s+", " ", text)


def find_value(data: Any, keys: set[str]) -> Any:
    normalized_keys = {normalize_field_key(key) for key in keys}
    if isinstance(data, dict):
        for key, value in data.items():
            if (key in keys or normalize_field_key(key) in normalized_keys) and value not in EMPTY and not is_error_text(value):
                return value
        for value in data.values():
            found = find_value(value, keys)
            if found not in EMPTY:
                return found
    elif isinstance(data, list):
        for item in data:
            found = find_value(item, keys)
            if found not in EMPTY:
                return found
    elif isinstance(data, str):
        parsed = deep_parse_json(data)
        if parsed is not data and parsed != data:
            return find_value(parsed, keys)
    return ""


def extract_traffic_share(data: Any) -> Any:
    return find_value(data, TRAFFIC_SHARE_KEYS)


def extract_aba_rank(data: Any) -> Any:
    return find_value(data, ABA_DETAIL_KEYS)


def extract_search_volume(data: Any) -> Any:
    return find_value(data, SEARCH_VOLUME_KEYS)


def extract_latest_metric(data: Any, keys: set[str]) -> Any:
    rows = collect_dict_rows(data)
    rows.sort(key=lambda row: str(find_value(row, DATE_KEYS) or ""), reverse=True)
    for row in rows:
        value = find_value(row, keys)
        if value not in EMPTY:
            return scalar_metric(value, keys)
    value = find_value(data, keys)
    return scalar_metric(value, keys)


def extract_latest_rank(data: Any) -> Any:
    return scalar_rank(extract_latest_metric(data, RANK_KEYS | VALUE_KEYS))


def scalar_metric(value: Any, keys: set[str] | None = None) -> Any:
    if value in EMPTY:
        return ""
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        series = parse_series_string(value)
        return series or value
    if isinstance(value, dict):
        nested = find_value(value, (keys or set()) | {"value", "Value", "number", "amount"})
        if nested is value:
            return ""
        return scalar_metric(nested, keys)
    if isinstance(value, list):
        for item in value:
            metric = scalar_metric(item, keys)
            if metric not in EMPTY:
                return metric
    return value


def scalar_rank(value: Any) -> Any:
    if value in EMPTY:
        return ""
    if isinstance(value, dict):
        rows = collect_dict_rows(value)
        fallback: list[Any] = []
        for row in rows:
            label = str(first_non_empty(
                find_value(row, {"categoryName", "category", "类别", "类目", "rankType", "type"})
            )).casefold()
            rank_value = find_value(row, RANK_KEYS | {"value", "position"})
            if rank_value not in EMPTY:
                if any(token in label for token in ("root", "main", "大类", "一级")):
                    return scalar_rank(rank_value)
                fallback.append(rank_value)
        if fallback:
            return scalar_rank(fallback[0])
    if isinstance(value, list):
        for item in value:
            rank = scalar_rank(item)
            if rank not in EMPTY:
                return rank
    return value


def extract_category_ranks(detail: Any) -> tuple[Any, Any]:
    """Extract root-category and leaf/sub-category BSR values.

    Product providers represent category ranks in several ways: dedicated root/
    subcategory fields, arrays of category objects, or a generic ``rank`` field
    accompanied by category metadata.  The helper deliberately prefers explicit
    fields, then category rows marked as root/main or leaf/subcategory.
    """
    main_rank = find_value(detail, MAIN_RANK_KEYS)
    small_rank = find_value(detail, SMALL_RANK_KEYS)

    main_candidates: list[tuple[int, Any]] = []
    small_candidates: list[tuple[int, Any]] = []
    for row in collect_dict_rows(detail):
        if not isinstance(row, dict):
            continue
        label = str(first_non_empty(
            find_value(row, {"categoryName", "category", "nodeName", "browseNodeName", "类别", "类目", "rankType", "type"}),
            "",
        )).casefold()
        root_flag = row.get("root") is True or row.get("isRoot") is True or row.get("is_root") is True
        level_value = find_value(row, {"level", "depth", "categoryLevel", "nodeLevel", "层级"})
        try:
            level = int(float(str(level_value))) if level_value not in EMPTY else 0
        except (TypeError, ValueError):
            level = 0

        explicit_main = find_value(row, MAIN_RANK_KEYS)
        explicit_small = find_value(row, SMALL_RANK_KEYS)
        generic_rank = find_value(row, {"rank", "ranking", "bsr", "BSR", "value", "position"})

        if explicit_main not in EMPTY:
            main_candidates.append((level, explicit_main))
        if explicit_small not in EMPTY:
            small_candidates.append((level, explicit_small))

        has_category_context = bool(label) or root_flag or any(
            key in row for key in ("categoryId", "nodeId", "browseNodeId", "categoryTree")
        )
        if generic_rank not in EMPTY and has_category_context:
            if root_flag or any(token in label for token in ("root", "main", "大类", "一级", "根类目")):
                main_candidates.append((level, generic_rank))
            elif any(token in label for token in ("sub", "leaf", "small", "小类", "子类", "细分类")) or level > 0:
                small_candidates.append((level, generic_rank))

    if main_rank in EMPTY and main_candidates:
        main_candidates.sort(key=lambda item: item[0])
        main_rank = main_candidates[0][1]
    if small_rank in EMPTY and small_candidates:
        small_candidates.sort(key=lambda item: item[0], reverse=True)
        small_rank = small_candidates[0][1]
    return scalar_rank(main_rank), scalar_rank(small_rank)


def parse_product_detail(detail: Any) -> dict[str, Any]:
    main_rank, small_rank = extract_category_ranks(detail)
    direct = {
        "price": find_value(detail, PRICE_KEYS),
        "coupon_value": find_value(detail, COUPON_KEYS),
        "deal_status": find_value(detail, DEAL_STATUS_KEYS),
        "deal_price": find_value(detail, DEAL_PRICE_KEYS),
        "prime_discount_price": find_value(detail, PRIME_PRICE_KEYS),
        "estimated_sales": find_value(detail, SALES_KEYS),
        "product_rank": main_rank,
        "small_category_rank": small_rank,
        "rating": find_value(detail, RATING_KEYS),
        "review_count": find_value(detail, REVIEW_KEYS),
    }
    text = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
    direct["price"] = first_non_empty(direct["price"], regex_value(text, r"(?:当前)?价格[:：]\s*[\$€£￥¥]?\s*([0-9,.]+)"))
    direct["coupon_value"] = first_non_empty(direct["coupon_value"], regex_text(text, r"(?:优惠券|coupon)[:：]\s*([^,，;；\n}]+)"))
    direct["deal_price"] = first_non_empty(direct["deal_price"], regex_value(text, r"(?:秒杀价格|秒杀价|dealPrice)[:：]\s*[\$€£￥¥]?\s*([0-9,.]+)"))
    direct["prime_discount_price"] = first_non_empty(direct["prime_discount_price"], regex_value(text, r"Prime(?:专享价|价格|折扣价)?[:：]\s*[\$€£￥¥]?\s*([0-9,.]+)"))
    direct["estimated_sales"] = first_non_empty(direct["estimated_sales"], regex_value(text, r"(?:月销量|monthlySales|ListingSalesVolumeOfMonth)[:：]\s*([0-9,.]+)"))
    direct["product_rank"] = first_non_empty(direct["product_rank"], regex_value(text, r"(?:大类排名|BSR|SalesRank|productRank)[:：#\s]*([0-9,.]+)"))
    direct["small_category_rank"] = first_non_empty(
        direct["small_category_rank"],
        regex_value(text, r"(?:小类排名|子类排名|SubcategoryRank|SmallCategoryRank)[:：#\s]*([0-9,.]+)"),
    )
    direct["rating"] = first_non_empty(direct["rating"], regex_value(text, r"(?:星级|rating|Rating)[:：]\s*([0-9.]+)"))
    direct["review_count"] = first_non_empty(direct["review_count"], regex_value(text, r"(?:评论数|评价数量|reviewCount|ratingCount)[:：]\s*([0-9,.]+)"))
    direct["estimated_sales"] = scalar_metric(direct["estimated_sales"], SALES_KEYS)
    direct["product_rank"] = scalar_rank(direct["product_rank"])
    direct["small_category_rank"] = scalar_rank(direct["small_category_rank"])
    direct["coupon_type"] = classify_coupon(direct["coupon_value"])
    direct["deal_status"] = normalize_yes_no(direct["deal_status"], bool(direct["deal_price"]))
    return direct


def extract_result_position(row: dict[str, Any], page_size: int) -> Any:
    direct = find_value(row, POSITION_KEYS)
    if direct not in EMPTY:
        return direct
    page = int(row.get("_page", 1) or 1)
    index = int(row.get("_index", 0) or 0)
    return (page - 1) * page_size + index if index else ""


def extract_keyword_rank(data: Any) -> Any:
    rows = collect_dict_rows(data)
    rows.sort(key=lambda row: str(find_value(row, DATE_KEYS) or ""), reverse=True)
    for row in rows:
        value = find_value(row, ORGANIC_POSITION_KEYS | POSITION_KEYS)
        if value not in EMPTY:
            return value
    return find_value(data, ORGANIC_POSITION_KEYS | POSITION_KEYS)


def extract_latest_date(data: Any) -> Any:
    dates = [find_value(row, DATE_KEYS) for row in collect_dict_rows(data)]
    dates = [value for value in dates if value not in EMPTY]
    return sorted(map(str, dates), reverse=True)[0] if dates else ""


def extract_latest_number(data: Any) -> Any:
    rows = collect_dict_rows(data)
    rows.sort(key=lambda row: str(find_value(row, DATE_KEYS) or ""), reverse=True)
    for row in rows:
        value = find_value(row, VALUE_KEYS)
        if value not in EMPTY:
            return parse_series_string(value) or value
    value = find_value(data, VALUE_KEYS)
    return parse_series_string(value) or value


def parse_series_string(value: Any) -> Any:
    if not isinstance(value, str) or "=" not in value:
        return ""
    points: list[tuple[str, str]] = []
    for part in value.replace("，", ",").split(","):
        if "=" not in part:
            continue
        label, val = part.split("=", 1)
        if label.strip() and val.strip():
            points.append((label.strip(), val.strip()))
    return sorted(points)[-1][1] if points else ""


def first_non_empty(*values: Any) -> Any:
    for value in values:
        if value not in EMPTY and not is_error_text(value):
            return value
    return ""


def row_keyword(row: dict[str, Any]) -> str:
    return str(find_value(row, KEYWORD_KEYS) or "").strip()


def find_keyword_row(data: Any, keyword: str) -> dict[str, Any]:
    target = normalize_keyword(keyword)
    rows = [row for row in collect_dict_rows(data) if row_keyword(row)]
    for row in rows:
        if normalize_keyword(row_keyword(row)) == target:
            return dict(row)
    target_tokens = set(re.findall(r"[0-9a-z]+", target))
    candidates: list[dict[str, Any]] = []
    if target_tokens:
        for row in rows:
            row_tokens = set(re.findall(r"[0-9a-z]+", normalize_keyword(row_keyword(row))))
            if target_tokens == row_tokens or (
                len(target_tokens) >= 2 and len(target_tokens & row_tokens) / len(target_tokens) >= 0.8
            ):
                candidates.append(row)
    return dict(candidates[0]) if len(candidates) == 1 else {}


def regex_value(text: str, pattern: str) -> str:
    match = re.search(pattern, text, re.IGNORECASE)
    return match.group(1).replace(",", "").strip() if match else ""


def regex_text(text: str, pattern: str) -> str:
    match = re.search(pattern, text, re.IGNORECASE)
    return match.group(1).strip() if match else ""


def normalize_marketplace(marketplace: str) -> str:
    value = (marketplace or "US").upper()
    return "GB" if value == "UK" else value


def normalize_number(value: Any) -> Any:
    if value in EMPTY:
        return ""
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip().replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return value
    number = float(match.group())
    return int(number) if number.is_integer() else number


def normalize_decimal(value: Any) -> Any:
    result = normalize_number(value)
    return result


def normalize_money(value: Any) -> Any:
    return normalize_number(value)


def normalize_percent(value: Any) -> Any:
    if value in EMPTY:
        return ""
    text = str(value).strip()
    number = normalize_number(text)
    if number == "":
        return value
    try:
        numeric = float(number)
    except (TypeError, ValueError):
        return value
    if "%" not in text and -1 <= numeric <= 1:
        numeric *= 100
    return f"{numeric:.2f}%"


def normalize_position(value: Any) -> Any:
    if value in EMPTY:
        return ""
    text = str(value).strip()
    if ">" in text:
        return text
    return normalize_number(value)


def normalize_yes_no(value: Any, fallback: bool = False) -> str:
    if value in EMPTY:
        return "是" if fallback else "否"
    if isinstance(value, bool):
        return "是" if value else "否"
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y", "是", "有", "active"}:
        return "是"
    if text in {"false", "0", "no", "n", "否", "无", "none"}:
        return "否"
    return str(value)


def classify_coupon(value: Any) -> str:
    if value in EMPTY:
        return ""
    text = str(value)
    if "%" in text or "percent" in text.lower() or "折" in text:
        return "百分比"
    if re.search(r"[$€£￥¥]|\b(?:USD|EUR|GBP|JPY)\b|\d+(?:\.\d+)?\s*(?:off|减|元)", text, re.I):
        return "金额"
    return "其他"


def is_error_text(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.lower()
    return any(mark in text for mark in (
        "not found", "no data", "no result", "failed", "error", "unauthorized",
        "authentication required", "please specify", "required parameter",
        "missing parameter", "invalid parameter", "method signature",
        "未查询到", "请求数量不足", "缺少参数", "参数错误", "requestleft",
    ))


def summarize_errors(raw: dict[str, Any]) -> str:
    messages: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key.lower() in {"error", "message", "msg", "_sf_error"} and item not in EMPTY:
                    text = str(item)
                    if text not in messages:
                        messages.append(text)
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(raw)
    if messages:
        return "; ".join(messages)
    shapes: list[str] = []
    for name, value in raw.items():
        if value in EMPTY:
            continue
        if isinstance(value, dict):
            keys = list(value.keys())[:12]
            shapes.append(f"{name}: object keys={','.join(map(str, keys)) or '(none)'}")
        elif isinstance(value, list):
            first = value[0] if value else None
            if isinstance(first, dict):
                keys = list(first.keys())[:12]
                shapes.append(f"{name}: list[{len(value)}] keys={','.join(map(str, keys))}")
            else:
                shapes.append(f"{name}: list[{len(value)}]")
        else:
            text = str(value).replace("\n", " ")[:160]
            shapes.append(f"{name}: {type(value).__name__} {text}")
    if shapes:
        return "Sorftime 已返回内容但未识别到目标字段；响应结构：" + " | ".join(shapes[:8])
    return "Sorftime 未返回匹配数据。"


def domain_for_site(site: str) -> int:
    return {
        "US": 1, "GB": 2, "UK": 2, "DE": 3, "FR": 4, "IN": 5,
        "CA": 6, "JP": 7, "ES": 8, "IT": 9, "MX": 10, "AE": 11,
        "AU": 12, "BR": 13, "SA": 14,
    }.get(site.upper(), 1)


def amazon_product_url(asin: str, site: str) -> str:
    domains = {
        "US": "amazon.com", "GB": "amazon.co.uk", "DE": "amazon.de",
        "FR": "amazon.fr", "IT": "amazon.it", "ES": "amazon.es",
        "CA": "amazon.ca", "JP": "amazon.co.jp", "MX": "amazon.com.mx",
        "AU": "amazon.com.au", "BR": "amazon.com.br", "AE": "amazon.ae",
        "SA": "amazon.sa", "IN": "amazon.in",
    }
    return f"https://www.{domains.get(site, 'amazon.com')}/dp/{asin}"


KEYWORD_KEYS = {
    "关键词", "keyword", "Keyword", "searchTerm", "SearchTerm", "keywordName",
    "KeywordName", "searchKeyword", "SearchKeyword", "keywordText", "KeywordText", "keywords", "Keywords",
}
ASIN_KEYS = {"ASIN", "asin", "Asin", "parentAsin", "childAsin"}
POSITION_KEYS = {
    "排名", "位置", "position", "Position", "rank", "Rank", "searchRank",
    "SearchRank", "keywordRank", "KeywordRank", "searchPosition", "SearchPosition",
    "organicRank", "naturalRank", "adRank", "AdPosition", "resultPosition",
}
ORGANIC_POSITION_KEYS = POSITION_KEYS | {
    "自然位", "自然排名", "最近自然位置", "最近自然曝光位置", "organic_position",
    "organicPosition", "natural_position", "NaturalPosition",
}
AD_POSITION_KEYS = {
    "广告位", "广告排名", "最近广告位置", "最近广告曝光位置", "ad_position",
    "adPosition", "sponsoredPosition", "SponsoredPosition", "AdPosition",
}
ORGANIC_TIME_KEYS = {
    "自然曝光时间", "最近自然曝光时间", "organic_time", "organicTime",
    "searchPositionDate", "lastOrganicExposureTime",
}
AD_TIME_KEYS = {
    "广告曝光时间", "最近广告曝光时间", "ad_time", "adTime", "AdPositionDate",
    "lastAdExposureTime",
}
TRAFFIC_SHARE_KEYS = {
    "关键词流量占比", "流量占比", "流量占比%", "点击份额", "点击占比", "流量份额",
    "自然流量占比", "广告流量占比", "流量比例", "流量贡献", "traffic_share",
    "trafficShare", "flowShare", "flow_share", "clickShare", "click_share", "share",
    "TrafficShare", "ClickShare", "FlowShare", "trafficRate", "flowRatio",
    "trafficPercentage", "TrafficPercentage", "trafficPercent", "searchTrafficShare",
    "trafficRatio", "TrafficRatio", "trafficRate", "TrafficRate", "flowRate",
    "keywordTrafficShare", "KeywordTrafficShare", "searchTermTrafficShare",
    "trafficContribution", "TrafficContribution", "流量贡献率", "关键词流量份额",
}
ABA_RANK_KEYS = {
    "ABA热度排名", "ABA排名", "ABA", "ABA Rank", "aba_rank", "abaRank",
    "searchFrequencyRank", "search_frequency_rank", "SearchFrequencyRank",
    "关键词热度排名", "搜索频率排名", "Search Frequency Rank", "SFR",
    "weeklySearchFrequencyRank", "abaWeeklyRank", "ABA周排名",
    "SearchFrequencyRanking", "searchFrequencyRanking", "ABARanking", "abaHeatRank",
}
ABA_DETAIL_KEYS = ABA_RANK_KEYS | {
    "rank", "Rank", "ranking", "Ranking", "hotRank", "heatRank",
    "searchRank", "keywordRank", "frequencyRank", "weeklyRank",
}
SEARCH_VOLUME_KEYS = {
    "搜索量", "月搜索量", "周搜索量", "关键词搜索量", "search_volume", "searchVolume",
    "monthly_search_volume", "MonthlySearchVolume", "SearchVolume", "searches",
    "keywordSearchVolume", "monthlySearchVolume", "weeklySearchVolume",
    "searchVolumeOfMonth", "SearchVolumeOfMonth", "searchVolumeOfWeek",
    "SearchVolumeOfWeek", "monthlySearches", "weeklySearches",
}
PRICE_KEYS = {
    "price", "价格", "currentPrice", "current_price", "buyBoxPrice", "buybox_price",
    "salePrice", "SalesPrice", "ListingSalesPrice", "amazonPrice", "BuyBoxPrice",
    "listingPrice", "ListingPrice", "listingPriceAmount", "priceValue", "lowestPrice",
}
COUPON_KEYS = {
    "优惠券", "coupon", "Coupon", "couponValue", "coupon_value", "couponAmount",
    "CouponAmount", "couponDiscount", "优惠券金额", "优惠券折扣", "couponPercent",
    "couponPercentage", "CouponPercent", "couponSavings", "coupon_savings",
}
DEAL_STATUS_KEYS = {
    "是否秒杀", "秒杀", "deal", "isDeal", "is_deal", "dealStatus", "DealStatus",
    "促销状态", "lightningDeal", "LightningDeal", "isLightningDeal",
}
DEAL_PRICE_KEYS = {
    "秒杀价格", "秒杀价", "lightningDealPrice", "lightning_deal_price", "dealPrice",
    "DealPrice", "flashDealPrice", "促销价", "LDPrice", "LightningDealPrice",
}
PRIME_PRICE_KEYS = {
    "Prime专享价", "Prime价格", "Prime折扣价", "primePrice", "prime_price",
    "prime_discount_price", "PrimeDiscountPrice", "primeExclusivePrice",
    "primeExclusiveDiscountPrice", "PrimeExclusiveDiscountPrice",
}
SALES_KEYS = {
    "本产品月销量", "月销量", "sales", "monthSales", "monthlySales", "month_sales_volume",
    "monthly_sales", "salesVolume", "ListingSalesVolumeOfMonth", "MonthSaleVolume",
    "SalesVolume", "estimatedSales", "estimateSales",
    "SalesVolumeOfMonth", "salesVolumeOfMonth", "ListingSalesVolume", "monthlySaleVolume",
    "MonthSalesVolume", "monthSalesVolume", "CurrentSalesVolume", "currentSalesVolume",
    "ASINMonthSalesVolume", "asinMonthSalesVolume", "monthlyUnitsSold", "unitsSoldLastMonth",
    "EstimatedMonthlySales", "estimatedMonthlySales", "salesVolume30Days", "SalesVolume30Days",
}
SMALL_RANK_KEYS = {
    "小类排名", "子类排名", "细分类排名", "smallCategoryRank", "SmallCategoryRank",
    "subCategoryRank", "SubCategoryRank", "subcategoryRank", "SubcategoryRank",
    "subcategorySalesVolumeRank", "childCategoryRank", "leafCategoryRank",
    "browseNodeRank", "smallBsrRank", "subBsrRank", "minorCategoryRank",
}
MAIN_RANK_KEYS = {
    "产品排名", "大类排名", "mainCategoryRank", "MainCategoryRank",
    "rootCategoryRank", "RootCategoryRank", "bigCategoryRank", "BigCategoryRank",
    "parentCategoryRank", "categoryBsrRank", "SalesRankOfCategory",
    "salesRankOfCategory", "AmazonBestSellerRank", "amazonBestSellerRank",
    "bestSellerRank", "BestSellerRank", "SalesRank", "salesRank", "BSR", "bsr", "bsrRank", "BsrRank",
}
RANK_KEYS = MAIN_RANK_KEYS | SMALL_RANK_KEYS | {
    "产品排名", "大类排名", "rank", "bsr", "BSR", "productRank", "categoryRank",
    "bestSellerRank", "CategoryRank", "SalesRank",
    "BestSellerRank", "parentCategoryRank",
    "bigCategoryRank", "BigCategoryRank", "rankingOfCategory", "categoryBsrRank", "bsrRank", "BsrRank",
    "MainCategoryRank", "mainCategoryRank", "RootCategoryRank", "rootCategoryRank",
    "SalesRankOfCategory", "salesRankOfCategory", "AmazonBestSellerRank", "amazonBestSellerRank",
}
RATING_KEYS = {
    "星级", "评分", "rating", "ratings", "reviewRating", "linkRating", "Rating",
    "Star", "star", "stars", "averageRating",
    "starRating", "ratingValue", "reviewScore",
}
REVIEW_KEYS = {
    "评论数", "评价数量", "评价数", "review_count", "reviews", "ratingCount",
    "reviewCount", "Ratings", "ReviewCount", "ratingsCount", "totalReviews",
    "reviewNum", "reviewsNum", "commentCount", "reviewAmount",
}
DATE_KEYS = {
    "date", "time", "recordDate", "captureDate", "exposureTime", "statDate",
    "日期", "时间", "record_time", "lastExposureTime",
}
VALUE_KEYS = {
    "value", "val", "dataValue", "trendValue", "rank", "price", "sales", "价格",
    "销量", "月销量", "SalesVolume", "Rank", "Price",
}
