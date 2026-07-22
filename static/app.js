(() => {
  const $ = (id) => document.getElementById(id);
  const form = $("captureForm");
  const runButton = $("runCapture");
  const historyBody = $("historyBody");
  const sourceBadge = $("sourceBadge");
  const ownerId = getOwnerId();
  let jobTimer = null;
  let dailyControlTouched = false;
  let fieldMapping = null;

  const tableFields = [
    "date", "asin", "keyword", "traffic_share", "aba_rank", "search_volume",
    "organic_position", "ad_position", "price", "coupon_value", "deal_price",
    "prime_discount_price", "estimated_sales", "product_rank", "small_category_rank", "rating",
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

  function selectedSourceKey() {
    const provider = selectedProvider();
    if (provider === "sorftime") return $("sorftimeMode").value === "mcp_url" ? "sorftime_mcp" : "sorftime_cli";
    if (provider === "sellersprite") return "sellersprite_mcp";
    if (provider === "sif") return "sif_mcp";
    if (provider === "xiyou") return $("xiyouMode").value === "api" ? "xiyou_api" : "xiyou_mcp";
    return $("customMode").value === "api" ? "custom_api" : "custom_mcp";
  }

  function renderFieldMapping() {
    if (!fieldMapping || !Array.isArray(fieldMapping.fields)) return;
    const sourceKey = selectedSourceKey();
    const sourceLabel = (fieldMapping.source_labels || {})[sourceKey] || selectedProviderLabel();
    const items = fieldMapping.fields.map(field => {
      const mapping = (field.sources || {})[sourceKey] || (field.sources || {}).default || { tool: "等待接口匹配", status: "dynamic" };
      return { ...field, mapping };
    });
    const full = items.filter(item => item.mapping.status === "full").length;
    const conditional = items.filter(item => item.mapping.status === "conditional").length;
    const dynamic = items.filter(item => item.mapping.status === "dynamic").length;
    $("fieldCount").textContent = `${items.length} 个字段`;
    $("fieldMatchTitle").textContent = `${sourceLabel} 字段匹配`;
    $("fieldMatchNote").textContent = `依据 ${fieldMapping.source_file || "字段目录表"} 自动匹配；流量占比统一按百分比保留 2 位小数。`;
    $("fieldCoverageBadge").textContent = `直接 ${full} · 条件 ${conditional} · 动态 ${dynamic}`;
    $("fieldMatchGrid").innerHTML = items.map(item => {
      const status = item.mapping.status || "dynamic";
      const statusText = status === "full" ? "直接" : (status === "conditional" ? "条件" : "动态");
      return `<div class="field-match-item match-${escapeHtml(status)}"><strong>${escapeHtml(item.label)} · ${statusText}</strong><small>${escapeHtml(item.mapping.tool || "")}</small></div>`;
    }).join("");
  }

  async function loadFieldMapping() {
    try {
      const response = await fetch("/static/field-mapping.json", { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      fieldMapping = await response.json();
      renderFieldMapping();
    } catch (error) {
      $("fieldMatchNote").textContent = `字段映射表读取失败：${error.message}`;
      $("fieldCoverageBadge").textContent = "读取失败";
    }
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

    let detail = "填写连接信息后可直接测试或开始抓取";
    if (provider === "sorftime") detail = $("sorftimeMode").value === "mcp_url" ? "填写 Sorftime MCP URL 和 Token" : "填写 Sorftime CLI Account-SK";
    if (provider === "sellersprite") detail = "卖家精灵 MCP URL 已内置；输入 Key 后按官方工具 Code 直接调用";
    if (provider === "sif") detail = "填写 SIF MCP URL 和 MCP Key";
    if (provider === "xiyou") detail = $("xiyouMode").value === "api" ? "填写西柚洞察 OpenAPI Key" : "填写西柚洞察 MCP URL 和 Token";
    if (provider === "custom") detail = $("customMode").value === "api" ? "填写其他软件 API Endpoint" : "填写其他软件 MCP URL";
    setConnectionState("disconnected", detail);
    renderFieldMapping();
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
      setConnectionState("connected", `连接配置有效，识别接口 ${count} 个${note}，用时 ${Number(info.elapsed_seconds || 0).toFixed(2)} 秒`);
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
    $("mcpCalls").textContent = String(job.mcp_calls || 0);
    $("elapsedTime").textContent = `${Number(job.elapsed_seconds || 0).toFixed(2)} 秒`;
    $("doneCount").textContent = `${job.done || 0} / ${job.total || 0}`;
    $("recordCount").textContent = String(job.records_count || 0);
    const toolSummary = Object.entries(job.tool_calls || {}).map(([name, count]) => `${name}: ${count}`).join(" · ");
    const logs = Array.isArray(job.logs) ? job.logs.join("\n") : "暂无运行日志";
    $("logBox").textContent = toolSummary ? `${logs}\n\n接口调用：${toolSummary}` : logs;
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
      historyBody.innerHTML = '<tr><td colspan="20" class="empty">暂无结果</td></tr>';
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
      renderRows(payload.records || []);
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
      const payload = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
      const job = payload.job;
      setProgress(job);
      if (["completed", "completed_with_warning"].includes(job.status)) {
        runButton.disabled = false;
        runButton.textContent = "开始抓取";
        const results = await api(`/api/jobs/${encodeURIComponent(jobId)}/results`);
        renderRows(results.records || []);
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
      runButton.disabled = false;
      runButton.textContent = "开始抓取并导出";
      toast(error.message, true);
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
  $("dataProvider").addEventListener("change", showConnectionFields);
  $("sorftimeMode").addEventListener("change", showConnectionFields);
  $("xiyouMode").addEventListener("change", showConnectionFields);
  $("customMode").addEventListener("change", showConnectionFields);
  $("outputMode").addEventListener("change", showOutputFields);
  $("dailyEnabled").addEventListener("change", () => { dailyControlTouched = true; });
  $("refreshHistory").addEventListener("click", () => loadHistory(true));

  async function initialize() {
    try {
      const health = await api("/api/health");
      if (!health.ok) throw new Error("服务未就绪");
    } catch (error) {
      toast(`服务检查失败：${error.message}`, true);
    }
    showConnectionFields();
    showOutputFields();
    await Promise.all([loadFieldMapping(), loadHistory(false), loadDailyStatus()]);
    window.setInterval(loadDailyStatus, 60000);
  }

  initialize();
})();
