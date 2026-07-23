(() => {
  window.__keywordTrackerLoaded = true;
  const $ = (id) => document.getElementById(id);
  const form = $("captureForm");
  const runButton = $("runCapture");
  const historyBody = $("historyBody");
  const sourceBadge = $("sourceBadge");
  const ownerId = getOwnerId();
  let jobTimer = null;
  let dailyControlTouched = false;
  let allHistoryRecords = [];

  const tableFields = [
    "date", "asin", "keyword", "traffic_share", "aba_rank", "search_volume",
    "organic_position", "ad_position", "landed_price", "buy_box_price",
    "shipping_fee", "coupon_value", "list_price", "deal_label", "deal_price",
    "prime_discount_price", "estimated_sales", "parent_estimated_sales", "stock",
    "code_promotion", "business_price", "product_rank", "small_category_rank", "rating",
    "review_count", "product_url", "status", "message"
  ];

  function getOwnerId() {
    const saved = localStorage.getItem("keywordTrackerOwnerId");
    if (saved && /^[A-Za-z0-9_-]{16,100}$/.test(saved)) return saved;
    const bytes = new Uint8Array(18);
    crypto.getRandomValues(bytes);
    const id = `browser_${Array.from(bytes, b => b.toString(16).padStart(2, "0")).join("")}`;
    localStorage.setItem("keywordTrackerOwnerId", id);
    return id;
  }

  function toast(message, isError = false) {
    const node = $("toast");
    node.textContent = message;
    node.style.background = isError ? "#9a3412" : "#102a43";
    node.classList.add("show");
    window.clearTimeout(node._hideTimer);
    node._hideTimer = window.setTimeout(() => node.classList.remove("show"), 4200);
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;").replaceAll("'", "&#039;");
  }

  async function api(url, options = {}) {
    const response = await fetch(url, { cache: "no-store", ...options });
    let payload;
    try { payload = await response.json(); }
    catch (_) { throw new Error(`服务返回异常（HTTP ${response.status}）`); }
    if (!response.ok || payload.ok === false) throw new Error(payload.error || `请求失败（HTTP ${response.status}）`);
    return payload;
  }

  const providerLabels = {
    sorftime: "Sorftime",
    sellersprite: "卖家精灵",
    sif: "SIF",
    xiyou: "西柚洞察",
    custom: "其他软件"
  };

  function selectedProvider() {
    return $("dataProvider").value;
  }

  function selectedProviderLabel() {
    return providerLabels[selectedProvider()] || "其他软件";
  }

  function showSorftimeModeFields() {
    const isMcp = $("sorftimeMode").value === "mcp_url";
    $("cliAccountField").hidden = isMcp;
    $("mcpUrlField").hidden = !isMcp;
    $("mcpTokenField").hidden = !isMcp;
    $("sorftimeCliAccountSk").required = !isMcp && selectedProvider() === "sorftime";
    $("sorftimeMcpUrl").required = isMcp && selectedProvider() === "sorftime";
  }

  function showXiyouModeFields() {
    const isApi = $("xiyouMode").value === "api";
    $("xiyouMcpUrlField").hidden = isApi;
    $("xiyouMcpTokenField").hidden = isApi;
    $("xiyouApiKeyField").hidden = !isApi;
    $("xiyouApiUrlField").hidden = !isApi;
    const selected = selectedProvider() === "xiyou";
    $("xiyouMcpUrl").required = selected && !isApi;
    $("xiyouMcpToken").required = selected && !isApi;
    $("xiyouApiKey").required = selected && isApi;
  }

  function showCustomModeFields() {
    const isApi = $("customMode").value === "api";
    $("customMcpUrlField").hidden = isApi;
    $("customMcpTokenField").hidden = isApi;
    $("customApiUrlField").hidden = !isApi;
    $("customApiKeyField").hidden = !isApi;
    $("customApiHeaderField").hidden = !isApi;
    $("customMcpUrl").required = selectedProvider() === "custom" && !isApi;
    $("customApiUrl").required = selectedProvider() === "custom" && isApi;
  }

  function showKeepaFields() {
    const enabled = $("keepaEnabled").checked;
    $("keepaFields").hidden = !enabled;
    $("keepaApiKey").required = enabled;
    $("keepaApiUrl").required = enabled;
  }

  function showConnectionFields() {
    const provider = selectedProvider();
    const sections = {
      sorftime: "sorftimeFields",
      sellersprite: "sellerSpriteFields",
      sif: "sifFields",
      xiyou: "xiyouFields",
      custom: "customFields"
    };
    Object.entries(sections).forEach(([key, id]) => { $(id).hidden = key !== provider; });
    showSorftimeModeFields();
    showXiyouModeFields();
    showCustomModeFields();

    $("sellerSpriteMcpToken").required = provider === "sellersprite";
    $("sifMcpUrl").required = provider === "sif";
    $("sifMcpToken").required = provider === "sif";

    let detail = "填写关键词数据源连接信息后可直接测试或开始抓取";
    if (provider === "sorftime") detail = $("sorftimeMode").value === "mcp_url" ? "填写 Sorftime MCP URL 和 Token" : "填写 Sorftime CLI Account-SK";
    if (provider === "sellersprite") detail = "卖家精灵 MCP URL 已内置；输入 Key 后按官方工具 Code 直接调用";
    if (provider === "sif") detail = "填写 SIF MCP URL 和 MCP Key";
    if (provider === "xiyou") detail = $("xiyouMode").value === "api" ? "填写西柚洞察 OpenAPI Key" : "填写西柚洞察 MCP URL 和 Token";
    if (provider === "custom") detail = $("customMode").value === "api" ? "填写其他软件 API Endpoint" : "填写其他软件 MCP URL";
    setConnectionState("disconnected", detail);
  }

  function showOutputFields() {
    const mode = $("outputMode").value;
    const needsFeishu = mode === "lark" || mode === "both";
    const hasExcel = mode === "excel" || mode === "both";
    $("feishuFields").hidden = !needsFeishu;
    [$("feishuAppId"), $("feishuAppSecret"), $("feishuBaseUrl")].forEach(input => {
      input.required = needsFeishu;
    });

    const checkbox = $("autoDownload");
    const card = $("autoDownloadCard");
    if (!hasExcel) {
      checkbox.dataset.previousChecked = String(checkbox.checked);
      checkbox.checked = false;
      checkbox.disabled = true;
      card.classList.add("is-disabled");
    } else {
      checkbox.disabled = false;
      if (checkbox.dataset.previousChecked === "true") checkbox.checked = true;
      card.classList.remove("is-disabled");
    }
  }

  function setConnectionState(state, detail) {
    const connected = state === "connected";
    const testing = state === "testing";
    const label = selectedProviderLabel();
    sourceBadge.textContent = connected ? `${label} 已连接` : (testing ? `正在测试 ${label}` : `${label} 未连接`);
    sourceBadge.className = `source-pill ${connected ? "source-on" : (testing ? "source-testing" : "source-off")}`;
    $("connectionStatus").textContent = detail || "";
    $("connectionStatus").className = `connection-status ${connected ? "connection-ok" : ""}`;
  }

  function connectionFormData() {
    const data = new FormData(form);
    data.set("owner_id", ownerId);
    data.set("remember_connection", "false");
    return data;
  }

  function captureFormData() {
    const data = new FormData(form);
    data.set("owner_id", ownerId);
    data.set("outputMode", $("outputMode").value);
    data.set("auto_download", (!$("autoDownload").disabled && $("autoDownload").checked) ? "true" : "false");
    data.set("daily_enabled", $("dailyEnabled").checked ? "true" : "false");
    data.set("run_time", "09:00");
    data.set("timezone", "Asia/Shanghai");
    data.set("remember_connection", "false");
    return data;
  }

  async function testConnection() {
    const button = $("testConnection");
    button.disabled = true;
    const provider = selectedProvider();
    const isApi = (provider === "xiyou" && $("xiyouMode").value === "api") || (provider === "custom" && $("customMode").value === "api");
    const isCli = provider === "sorftime" && $("sorftimeMode").value === "cli_account";
    setConnectionState("testing", isCli ? "正在验证 Sorftime Account-SK…" : (isApi ? "正在检查 API 配置…" : "正在初始化 MCP…"));
    try {
      const payload = await api("/api/connection/test", { method: "POST", body: connectionFormData() });
      const info = payload.connection || {};
      const count = Array.isArray(info.recognized_tools) ? info.recognized_tools.length : Number(info.tool_count || 0);
      const note = info.note ? `；${info.note}` : "；数据权限将在抓取时验证";
      const productInfo = payload.product_connection;
      const productNote = productInfo ? `；Keepa 已验证${productInfo.tokens_left !== undefined ? `，剩余 tokens：${productInfo.tokens_left}` : ""}` : "";
      setConnectionState("connected", `连接配置有效，识别接口 ${count} 个${note}${productNote}，用时 ${Number(info.elapsed_seconds || 0).toFixed(2)} 秒`);
      toast(`${selectedProviderLabel()} 连接检查通过`);
    } catch (error) {
      setConnectionState("disconnected", error.message);
      toast(error.message, true);
    } finally {
      button.disabled = false;
    }
  }

  function downloadFile(url) {
    if (!url) return;
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "";
    anchor.style.display = "none";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  }

  function setActiveTab(tabName) {
    document.querySelectorAll(".tab-button").forEach(button => {
      button.classList.toggle("active", button.dataset.tab === tabName);
    });
    $("tasksTab").hidden = tabName !== "tasks";
    $("dashboardTab").hidden = tabName !== "dashboard";
    $("tasksTab").classList.toggle("active", tabName === "tasks");
    $("dashboardTab").classList.toggle("active", tabName === "dashboard");
  }

  function resetVisitorIdentity() {
    localStorage.removeItem("keywordTrackerOwnerId");
    window.location.href = `${window.location.pathname}?v=visitor-reset-${Date.now()}`;
  }

  function toNumber(value) {
    if (value === null || value === undefined || value === "") return null;
    const normalized = String(value).replace(/[%,$,\s]/g, "");
    const number = Number(normalized);
    return Number.isFinite(number) ? number : null;
  }

  function normalizeRankPosition(value) {
    if (value === null || value === undefined || value === "") return "";
    const text = String(value).trim();
    if (text.includes(">") && /\d/.test(text)) return text;
    const number = toNumber(text);
    return number !== null && number > 0 ? number : "";
  }

  function average(values) {
    const numbers = values.map(toNumber).filter(value => value !== null);
    if (!numbers.length) return null;
    return numbers.reduce((sum, value) => sum + value, 0) / numbers.length;
  }

  function sum(values) {
    const numbers = values.map(toNumber).filter(value => value !== null);
    if (!numbers.length) return null;
    return numbers.reduce((total, value) => total + value, 0);
  }

  function bestRank(values) {
    const numbers = values.map(toNumber).filter(value => value !== null && value > 0);
    return numbers.length ? Math.min(...numbers) : null;
  }

  function latestValue(records, field) {
    for (let index = records.length - 1; index >= 0; index -= 1) {
      const value = records[index][field];
      if (value !== null && value !== undefined && String(value).trim() !== "") return value;
    }
    return "";
  }

  function formatMetric(value, digits = 0, prefix = "") {
    if (value === null || value === undefined || Number.isNaN(value)) return "-";
    return `${prefix}${Number(value).toLocaleString("zh-CN", { maximumFractionDigits: digits, minimumFractionDigits: digits })}`;
  }

  function recordDate(record) {
    const raw = String(record.date || "").slice(0, 10);
    const date = raw ? new Date(`${raw}T00:00:00`) : null;
    return date && !Number.isNaN(date.getTime()) ? date : null;
  }

  function weekKey(date) {
    const copy = new Date(date);
    const day = copy.getDay() || 7;
    copy.setDate(copy.getDate() - day + 1);
    return copy.toISOString().slice(0, 10);
  }

  function groupKey(record) {
    const date = recordDate(record);
    if (!date) return "";
    return $("bucketMode").value === "week" ? weekKey(date) : date.toISOString().slice(0, 10);
  }

  function recordSource(record) {
    return record.data_provider || record.source || record.source_name || record.provider || "";
  }

  function recordMarketplace(record) {
    return record.marketplace || record.site || record.country || "";
  }

  function setSelectOptions(id, values, label) {
    const select = $(id);
    const current = select.value;
    const options = [...new Set(values.filter(Boolean).map(String))].sort((a, b) => a.localeCompare(b, "zh-CN"));
    select.innerHTML = `<option value="">全部${label}</option>${options.map(value => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join("")}`;
    if (options.includes(current)) select.value = current;
  }

  function updateDashboardOptions(records) {
    setSelectOptions("filterAsin", records.map(record => record.asin), " ASIN");
    setSelectOptions("filterKeyword", records.map(record => record.keyword), "关键词");
    setSelectOptions("filterMarketplace", records.map(recordMarketplace), "站点");
    setSelectOptions("filterSource", records.map(recordSource), "数据源");
  }

  function filteredHistory() {
    const from = $("dateFrom").value ? new Date(`${$("dateFrom").value}T00:00:00`) : null;
    const to = $("dateTo").value ? new Date(`${$("dateTo").value}T23:59:59`) : null;
    const asin = $("filterAsin").value;
    const keyword = $("filterKeyword").value;
    const marketplace = $("filterMarketplace").value;
    const source = $("filterSource").value;
    return allHistoryRecords.filter(record => {
      const date = recordDate(record);
      if (from && (!date || date < from)) return false;
      if (to && (!date || date > to)) return false;
      if (asin && String(record.asin || "") !== asin) return false;
      if (keyword && String(record.keyword || "") !== keyword) return false;
      if (marketplace && String(recordMarketplace(record)) !== marketplace) return false;
      if (source && String(recordSource(record)) !== source) return false;
      return true;
    });
  }

  function groupedRecords(records) {
    const groups = new Map();
    records.forEach(record => {
      const key = groupKey(record);
      if (!key) return;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(record);
    });
    return [...groups.entries()].sort(([left], [right]) => left.localeCompare(right));
  }

  function makeSeries(groups, field, reducer = average) {
    return groups.map(([key, records]) => ({ key, value: reducer(records.map(record => record[field])) }))
      .filter(point => point.value !== null);
  }

  function renderLineChart(containerId, seriesList, emptyText) {
    const container = $(containerId);
    const series = seriesList.filter(item => item.points.length);
    if (!series.length) {
      container.innerHTML = `<div class="chart-empty">${escapeHtml(emptyText)}</div>`;
      return;
    }
    const labels = [...new Set(series.flatMap(item => item.points.map(point => point.key)))].sort();
    const values = series.flatMap(item => item.points.map(point => point.value));
    const min = Math.min(...values);
    const max = Math.max(...values);
    const range = max - min || 1;
    const width = 720;
    const height = 260;
    const pad = { top: 20, right: 22, bottom: 44, left: 48 };
    const x = (key) => {
      const index = labels.indexOf(key);
      return pad.left + (labels.length === 1 ? 0.5 : index / (labels.length - 1)) * (width - pad.left - pad.right);
    };
    const y = (value) => pad.top + (1 - (value - min) / range) * (height - pad.top - pad.bottom);
    const grid = [0, .25, .5, .75, 1].map(step => {
      const yy = pad.top + step * (height - pad.top - pad.bottom);
      const label = max - step * range;
      return `<line x1="${pad.left}" y1="${yy}" x2="${width - pad.right}" y2="${yy}" stroke="#e6edf3"/><text x="8" y="${yy + 4}" fill="#64748b" font-size="11">${formatMetric(label, label < 10 ? 1 : 0)}</text>`;
    }).join("");
    const paths = series.map(item => {
      const points = item.points.map(point => `${x(point.key)},${y(point.value)}`).join(" ");
      const dots = item.points.map(point => `<circle cx="${x(point.key)}" cy="${y(point.value)}" r="3" fill="${item.color}"><title>${escapeHtml(item.name)} ${escapeHtml(point.key)}: ${formatMetric(point.value, 1)}</title></circle>`).join("");
      return `<polyline fill="none" stroke="${item.color}" stroke-width="3" points="${points}" />${dots}`;
    }).join("");
    const xLabels = labels.map((label, index) => {
      if (labels.length > 8 && index % Math.ceil(labels.length / 8) !== 0 && index !== labels.length - 1) return "";
      return `<text x="${x(label)}" y="${height - 16}" fill="#64748b" font-size="11" text-anchor="middle">${escapeHtml(label.slice(5))}</text>`;
    }).join("");
    const legend = series.map((item, index) => `<g transform="translate(${pad.left + index * 126}, 12)"><circle r="4" fill="${item.color}"></circle><text x="10" y="4" fill="#102a43" font-size="12">${escapeHtml(item.name)}</text></g>`).join("");
    container.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img">${grid}<line x1="${pad.left}" y1="${height - pad.bottom}" x2="${width - pad.right}" y2="${height - pad.bottom}" stroke="#cad6e2"/>${paths}${xLabels}${legend}</svg>`;
  }

  function renderDashboard() {
    updateDashboardOptions(allHistoryRecords);
    const records = filteredHistory();
    $("dashboardCount").textContent = `${records.length} 条`;
    $("metricLandedPrice").textContent = formatMetric(average(records.map(record => record.landed_price)), 2, "¥");
    $("metricBuyBoxPrice").textContent = formatMetric(average(records.map(record => record.buy_box_price)), 2, "¥");
    $("metricShipping").textContent = formatMetric(average(records.map(record => record.shipping_fee)), 2, "¥");
    $("metricCoupon").textContent = latestValue(records, "coupon_value") || "-";
    $("metricListPrice").textContent = formatMetric(average(records.map(record => record.list_price)), 2, "¥");
    $("metricDeal").textContent = latestValue(records, "deal_label") || formatMetric(average(records.map(record => record.deal_price)), 2, "¥");
    $("metricDealPrice").textContent = formatMetric(average(records.map(record => record.deal_price)), 2, "¥");
    $("metricPrimePrice").textContent = formatMetric(average(records.map(record => record.prime_discount_price)), 2, "¥");
    $("metricSales").textContent = formatMetric(sum(records.map(record => record.estimated_sales)), 0);
    $("metricParentSales").textContent = formatMetric(average(records.map(record => record.parent_estimated_sales)), 0);
    $("metricStock").textContent = formatMetric(toNumber(latestValue(records, "stock")), 0);
    $("metricCodePromotion").textContent = latestValue(records, "code_promotion") || "-";
    $("metricBusinessPrice").textContent = formatMetric(average(records.map(record => record.business_price)), 2, "¥");
    $("metricRank").textContent = formatMetric(bestRank(records.map(record => record.product_rank)), 0);
    $("metricSmallRank").textContent = formatMetric(bestRank(records.map(record => record.small_category_rank)), 0);
    $("metricRating").textContent = formatMetric(average(records.map(record => record.rating)), 1);
    $("metricReviews").textContent = formatMetric(sum(records.map(record => record.review_count)), 0);
    const groups = groupedRecords(records);
    const rankSeries = [
      { name: "自然位", color: "#176b87", points: makeSeries(groups, "organic_position", bestRank) },
      { name: "广告位", color: "#b45309", points: makeSeries(groups, "ad_position", bestRank) }
    ];
    const metricSeries = [
      { name: "到手价", color: "#176b87", points: makeSeries(groups, "landed_price", average) },
      { name: "月销量", color: "#087f5b", points: makeSeries(groups, "estimated_sales", sum) },
      { name: "大类排名", color: "#c2410c", points: makeSeries(groups, "product_rank", bestRank) }
    ];
    $("rankChartSummary").textContent = groups.length ? `${groups.length} 个${$("bucketMode").value === "week" ? "周" : "日期"}` : "暂无数据";
    $("metricChartSummary").textContent = groups.length ? `${records.length} 条记录` : "暂无数据";
    renderLineChart("rankChart", rankSeries, "暂无排名趋势数据");
    renderLineChart("metricChart", metricSeries, "暂无指标趋势数据");
    renderRows(records);
  }

  function setHistoryRecords(records) {
    allHistoryRecords = Array.isArray(records) ? records.map(record => ({
      ...record,
      organic_position: normalizeRankPosition(record.organic_position),
      ad_position: normalizeRankPosition(record.ad_position)
    })) : [];
    renderDashboard();
  }

  function sumToolCalls(toolCalls, predicate) {
    return Object.entries(toolCalls || {}).reduce((total, [name, count]) => {
      return predicate(name) ? total + Number(count || 0) : total;
    }, 0);
  }

  function setProgress(job) {
    const statusText = {
      queued: "任务排队中", running: "正在抓取关键词数据", saving: "正在输出结果",
      completed: "抓取完成", completed_with_warning: "抓取完成（有提示）", failed: "任务失败"
    }[job.status] || "任务处理中";
    const percent = Number(job.percent || 0);
    $("progressTitle").textContent = statusText;
    const larkMessage = job.lark && job.lark.message ? job.lark.message : "";
    $("progressSub").textContent = job.error || larkMessage || (["completed", "completed_with_warning"].includes(job.status) ? "结果已生成。" : "正在调用所选数据源的 Amazon 数据接口。");
    $("progressPct").textContent = `${percent}%`;
    $("progressFill").style.width = `${Math.max(0, Math.min(100, percent))}%`;
    const toolCalls = job.tool_calls || {};
    const keepaCalls = sumToolCalls(toolCalls, name => String(name).startsWith("keepa:"));
    const totalCalls = Number(job.mcp_calls || 0);
    $("mcpCalls").textContent = keepaCalls ? `${totalCalls}（Keepamore ${keepaCalls}）` : String(totalCalls);
    $("elapsedTime").textContent = `${Number(job.elapsed_seconds || 0).toFixed(2)} 秒`;
    $("doneCount").textContent = `${job.done || 0} / ${job.total || 0}`;
    $("recordCount").textContent = String(job.records_count || 0);
    const pluginCalls = keepaCalls ? Math.max(0, totalCalls - keepaCalls) : totalCalls;
    const callBreakdown = keepaCalls ? `调用拆分：插件 ${pluginCalls} 次 · Keepamore ${keepaCalls} 次 · 总计 ${totalCalls} 次` : "";
    const toolSummary = Object.entries(toolCalls).map(([name, count]) => `${name}: ${count}`).join(" · ");
    const logs = Array.isArray(job.logs) ? job.logs.join("\n") : "暂无运行日志";
    $("logBox").textContent = [logs, callBreakdown, toolSummary ? `接口调用：${toolSummary}` : ""].filter(Boolean).join("\n\n");
    $("logBox").scrollTop = $("logBox").scrollHeight;
    const links = [];
    if (job.excel) links.push(`<a href="${escapeHtml(job.excel)}" download>下载 Excel</a>`);
    if (job.lark && job.lark.message) {
      const cls = job.lark.ok ? "lark-result" : "lark-result lark-error";
      links.push(`<span class="${cls}">${escapeHtml(job.lark.message)}</span>`);
    }
    $("resultLinks").innerHTML = links.join("");
  }

  function renderRows(records) {
    if (!records.length) {
      historyBody.innerHTML = '<tr><td colspan="28" class="empty">暂无结果</td></tr>';
      return;
    }
    historyBody.innerHTML = records.map(record => {
      const cells = tableFields.map(field => {
        const value = record[field] ?? "";
        if (field === "product_url" && value) return `<td><a href="${escapeHtml(value)}" target="_blank" rel="noopener">打开链接</a></td>`;
        if (field === "status") {
          const normalized = String(value).toLowerCase();
          const cls = ["success", "ok"].includes(normalized) ? "status-ok" : (normalized === "partial" ? "status-warning" : "status-failed");
          return `<td class="${cls}">${escapeHtml(value || "-")}</td>`;
        }
        return `<td title="${escapeHtml(value)}">${escapeHtml(value)}</td>`;
      }).join("");
      return `<tr>${cells}</tr>`;
    }).join("");
  }

  async function loadHistory(showToast = false) {
    try {
      const payload = await api(`/api/history?owner_id=${encodeURIComponent(ownerId)}`);
      setHistoryRecords(payload.records || []);
      if (showToast) toast("结果已刷新");
    } catch (error) {
      if (showToast) toast(error.message, true);
    }
  }

  function renderDailyStatus(job) {
    const status = $("dailyStatus");
    const links = $("dailyLinks");
    status.classList.remove("daily-error");
    links.innerHTML = "";
    if (!job) {
      status.textContent = "首次开始抓取后生效。Zeabur 需挂载持久化卷到 /app/data。";
      return;
    }
    if (!dailyControlTouched) $("dailyEnabled").checked = Boolean(job.enabled);
    const summary = job.payload_summary || {};
    const providerName = providerLabels[summary.data_provider] || "数据源";
    const scope = summary.asin_count && summary.keyword_count
      ? `${providerName} · ${summary.asin_count} 个 ASIN × ${summary.keyword_count} 个关键词 · ${summary.marketplace || "US"}`
      : "";
    if (!job.enabled) {
      status.textContent = "每日定时抓取已关闭。勾选后再次点击“开始抓取”即可开启。";
      return;
    }
    const lastRun = job.latest_run_at ? `；最近执行：${job.latest_run_at}` : "；尚未到首次执行时间";
    status.textContent = `已开启：每天 ${job.run_time || "09:00"}（北京时间）${scope ? ` · ${scope}` : ""}${lastRun}`;
    if (job.last_error) {
      status.textContent += `；提示：${job.last_error}`;
      status.classList.add("daily-error");
    }
    if (job.latest_excel) {
      links.innerHTML = `<a href="${escapeHtml(job.latest_excel)}" download>下载最近一次定时 Excel</a>`;
    }
  }

  async function loadDailyStatus() {
    try {
      const payload = await api(`/api/daily?owner_id=${encodeURIComponent(ownerId)}`);
      renderDailyStatus(payload.job || null);
    } catch (error) {
      $("dailyStatus").textContent = `读取定时状态失败：${error.message}`;
      $("dailyStatus").classList.add("daily-error");
    }
  }

  async function pollJob(jobId) {
    window.clearTimeout(jobTimer);
    try {
      const payload = await api(`/api/jobs/${encodeURIComponent(jobId)}?owner_id=${encodeURIComponent(ownerId)}`);
      const job = payload.job;
      setProgress(job);
      if (["completed", "completed_with_warning"].includes(job.status)) {
        runButton.disabled = false;
        runButton.textContent = "开始抓取";
        const results = await api(`/api/jobs/${encodeURIComponent(jobId)}/results?owner_id=${encodeURIComponent(ownerId)}`);
        setHistoryRecords(results.records || []);
        if (job.auto_download && job.excel) downloadFile(job.excel);
        if (job.lark && !job.lark.ok) {
          toast(`抓取完成，但飞书写入失败：${job.lark.message || "请检查配置"}`, true);
        } else if (job.auto_download && job.excel && job.lark && job.lark.ok) {
          toast("抓取完成，Excel 已下载并写入飞书");
        } else if (job.auto_download && job.excel) {
          toast("抓取完成，Excel 已开始下载");
        } else if (job.lark && job.lark.ok) {
          toast("抓取完成，数据已写入飞书");
        } else {
          toast("抓取完成");
        }
        await loadDailyStatus();
        return;
      }
      if (job.status === "failed") {
        runButton.disabled = false;
        runButton.textContent = "开始抓取";
        toast(job.error || "任务失败", true);
        return;
      }
      jobTimer = window.setTimeout(() => pollJob(jobId), 900);
    } catch (error) {
      $("progressSub").textContent = `状态刷新失败，正在自动重试：${error.message}`;
      jobTimer = window.setTimeout(() => pollJob(jobId), 1800);
    }
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    runButton.disabled = true;
    runButton.textContent = "任务创建中…";
    $("resultLinks").innerHTML = "";
    setProgress({ status: "queued", percent: 1, done: 0, total: 0, logs: ["正在提交任务…"] });
    try {
      const payload = await api("/api/jobs", { method: "POST", body: captureFormData() });
      setProgress(payload.job);
      if (payload.job.daily) renderDailyStatus(payload.job.daily);
      runButton.textContent = "正在抓取…";
      pollJob(payload.job.id);
    } catch (error) {
      runButton.disabled = false;
      runButton.textContent = "开始抓取并导出";
      toast(error.message, true);
    }
  });

  $("testConnection").addEventListener("click", testConnection);
  document.querySelectorAll(".tab-button").forEach(button => {
    button.addEventListener("click", () => setActiveTab(button.dataset.tab));
  });
  $("dataProvider").addEventListener("change", showConnectionFields);
  $("sorftimeMode").addEventListener("change", showConnectionFields);
  $("xiyouMode").addEventListener("change", showConnectionFields);
  $("customMode").addEventListener("change", showConnectionFields);
  $("outputMode").addEventListener("change", showOutputFields);
  $("keepaEnabled").addEventListener("change", showKeepaFields);
  $("dailyEnabled").addEventListener("change", () => { dailyControlTouched = true; });
  $("refreshHistory").addEventListener("click", () => loadHistory(true));
  $("resetVisitor").addEventListener("click", resetVisitorIdentity);
  ["dateFrom", "dateTo", "bucketMode", "filterAsin", "filterKeyword", "filterMarketplace", "filterSource"].forEach(id => {
    $(id).addEventListener("change", renderDashboard);
  });

  async function initialize() {
    try {
      const health = await api("/api/health");
      if (!health.ok) throw new Error("服务未就绪");
    } catch (error) {
      toast(`服务检查失败：${error.message}`, true);
    }
    showConnectionFields();
    showOutputFields();
    showKeepaFields();
    await Promise.all([loadHistory(false), loadDailyStatus()]);
    window.setInterval(loadDailyStatus, 60000);
  }

  initialize();
})();
