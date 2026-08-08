let currentAnalysis = null;
let priceChart = null;
let indicatorChart = null;
let latestScanResults = [];
let aiStatus = { configured: false, provider: "local_rules", model: "" };
let recentCodesExpanded = false;
let currentUser = null;
let currentEntitlement = null;
let watchSymbolsCache = [];
let signalSnapshotsCache = {};
let signalEventsCache = [];
let sendCodeCooldownTimer = null;
let lastStatusKey = "topbar.statusIdle";
let lastStatusVars = null;
let lastRenderedReport = null;
let reportBodyMode = "empty";
let paymentPollTimer = null;
let paymentPollAttempts = 0;
let currentPaymentOrderId = "";
let paymentView = { key: "", vars: null, tone: "" };
let subscriptionRefreshTimer = null;
const expandedReportIds = new Set();
const RECENT_CODE_PREVIEW_LIMIT = 12;
const RECENT_CODE_STORE_LIMIT = 100;
const PAYMENT_ORDER_STORAGE_KEY = "mb_waffo_subscription_order";
const PAYMENT_POLL_LIMIT = 150;

const $ = (id) => document.getElementById(id);

class ApiError extends Error {
  constructor(message, status, payload = {}) {
    super(message);
    this.status = status;
    this.code = payload.code || "";
    this.metric = payload.metric || "";
    this.limit = payload.limit;
    this.used = payload.used;
    this.remaining = payload.remaining;
    this.payload = payload;
  }
}

function localeTag() {
  return typeof dateLocale === "function" ? dateLocale() : "zh-CN";
}

function assetTypeLabel(type) {
  if (!type) return "";
  const key = `asset.${type}`;
  const translated = t(key);
  return translated === key ? type : translated;
}

function stanceLabel(stance) {
  const map = { bullish: "stance.bullish", neutral: "stance.neutral", bearish: "stance.bearish" };
  return map[stance] ? t(map[stance]) : stance || "--";
}

function fmt(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  return Number(value).toLocaleString(localeTag(), {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits,
  });
}

function fmtCompact(value) {
  if (value === null || value === undefined) return "--";
  return Number(value).toLocaleString(localeTag(), { notation: "compact", maximumFractionDigits: 2 });
}

function pctClass(value) {
  if (value > 0) return "positive";
  if (value < 0) return "negative";
  return "";
}

function setStatus(text) {
  $("statusText").textContent = text;
}

function setStatusKey(key, vars) {
  lastStatusKey = key;
  lastStatusVars = vars || null;
  setStatus(t(key, vars));
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    credentials: "include",
    headers: {
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(options.headers || {}),
    },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && !url.startsWith("/api/auth/")) {
      showLogin();
    }
    throw new ApiError(data.error || `HTTP ${response.status}`, response.status, data);
  }
  return data;
}

function formatPlanDate(value) {
  if (!value) return "";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return parsed.toLocaleDateString(localeTag(), {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

function quotaMessage(error) {
  if (error?.code !== "quota_exceeded") return error?.message || t("unknown");
  const keys = {
    analysis_daily: "quota.analysis",
    scan_daily: "quota.scan",
    scan_symbols: "quota.scanSymbols",
    watchlist_symbols: "quota.watchlist",
  };
  const key = keys[error.metric] || "quota.generic";
  return t(key, { limit: error.limit ?? 0, used: error.used ?? 0 });
}

function usageFor(metric) {
  return currentEntitlement?.usage?.[metric] || { used: 0, limit: 0, remaining: 0 };
}

function setUsageMeter(metric, textId, barId) {
  const usage = usageFor(metric);
  const limit = Math.max(1, Number(usage.limit) || 1);
  const used = Math.max(0, Number(usage.used) || 0);
  $(textId).textContent = `${used} / ${Number(usage.limit) || 0}`;
  $(barId).max = limit;
  $(barId).value = Math.min(used, limit);
}

function subscriptionStatusText() {
  const entitlement = currentEntitlement;
  if (!entitlement) return "";
  if (entitlement.status === "trial") {
    return t("plan.trialUntil", { date: formatPlanDate(entitlement.active_until) });
  }
  const subscription = entitlement.subscription;
  if (!subscription) {
    return t("plan.freeStatus");
  }
  const date = formatPlanDate(
    subscription.status === "past_due"
      ? subscription.grace_until
      : subscription.current_period_end,
  );
  const keys = {
    active: "plan.activeUntil",
    canceling: "plan.cancelingUntil",
    past_due: "plan.pastDueUntil",
    canceled: "plan.canceled",
  };
  return t(keys[subscription.status] || "plan.freeStatus", { date });
}

function hasLiveSubscription() {
  const subscription = currentEntitlement?.subscription;
  return ["active", "canceling", "past_due"].includes(subscription?.status);
}

function renderPlanState() {
  if (!currentUser || !currentEntitlement) return;
  const isPro = currentEntitlement.plan === "pro";
  const liveSubscription = hasLiveSubscription();
  const subscription = currentEntitlement.subscription;
  const ai = usageFor("ai_report_monthly");
  const scans = usageFor("scan_daily");

  $("planBadge").textContent = isPro ? "Pro" : "Free";
  $("planBadge").className = `planBadge${isPro ? " pro" : ""}`;
  $("dialogPlanName").textContent = isPro ? "Pro" : "Free";
  $("subscriptionStatus").textContent = subscriptionStatusText();
  $("usageSummary").textContent = t("plan.usageCompact", {
    aiUsed: ai.used || 0,
    aiLimit: ai.limit || 0,
    scanUsed: scans.used || 0,
    scanLimit: scans.limit || 0,
  });

  $("checkoutBtn").hidden = liveSubscription;
  $("managePlanBtn").hidden = !liveSubscription;
  $("upgradeProBtn").hidden = liveSubscription;
  $("cancelSubscriptionBtn").hidden = !(
    liveSubscription && ["active", "past_due"].includes(subscription?.status)
  );
  $("checkoutBtn").textContent = isPro ? t("plan.subscribe") : t("plan.upgrade");

  setUsageMeter("analysis_daily", "analysisUsageText", "analysisUsageBar");
  setUsageMeter("scan_daily", "scanUsageText", "scanUsageBar");
  setUsageMeter("ai_report_monthly", "aiUsageText", "aiUsageBar");
}

function applyUsage(usage) {
  if (!usage?.metric || !currentEntitlement?.usage) return;
  if (usage.plan && usage.plan !== currentEntitlement.plan) {
    refreshEntitlement().catch(() => {});
    return;
  }
  currentEntitlement.usage[usage.metric] = {
    ...currentEntitlement.usage[usage.metric],
    ...usage,
  };
  renderPlanState();
}

async function refreshEntitlement() {
  const data = await fetchJson("/api/auth/me");
  currentUser = data.user;
  currentEntitlement = data.entitlement;
  renderPlanState();
  return currentEntitlement;
}

function openPlanDialog() {
  renderPlanState();
  const dialog = $("planDialog");
  if (!dialog.open) dialog.showModal();
}

function clearSubscriptionRefresh() {
  if (subscriptionRefreshTimer) clearTimeout(subscriptionRefreshTimer);
  subscriptionRefreshTimer = null;
}

function pollSubscriptionRefresh(attempts = 12) {
  clearSubscriptionRefresh();
  let remaining = attempts;
  const refresh = async () => {
    try {
      await refreshEntitlement();
    } catch {
      return;
    }
    remaining -= 1;
    if (
      remaining > 0 &&
      ["active", "past_due"].includes(currentEntitlement?.subscription?.status)
    ) {
      subscriptionRefreshTimer = setTimeout(refresh, 2000);
    }
  };
  subscriptionRefreshTimer = setTimeout(refresh, 1200);
}

async function cancelCurrentSubscription() {
  if (!window.confirm(t("confirm.cancelSubscription"))) return;
  const button = $("cancelSubscriptionBtn");
  button.disabled = true;
  try {
    await fetchJson("/api/subscription/cancel", {
      method: "POST",
      body: "{}",
    });
    setPaymentState("subscription.cancelRequested", null, "pending");
    pollSubscriptionRefresh();
  } catch (error) {
    setPaymentState("subscription.cancelFailed", { error: error.message }, "error");
  } finally {
    button.disabled = false;
  }
}

function setPaymentState(key = "", vars = null, tone = "") {
  paymentView = { key, vars, tone };
  const status = $("paymentStatus");
  status.hidden = !key;
  status.className = `paymentStatus${tone ? ` ${tone}` : ""}`;
  status.textContent = key ? t(key, vars) : "";
}

function clearPaymentPoll() {
  if (paymentPollTimer) clearTimeout(paymentPollTimer);
  paymentPollTimer = null;
  paymentPollAttempts = 0;
}

function rememberPaymentOrder(orderId) {
  currentPaymentOrderId = String(orderId || "");
  try {
    if (currentPaymentOrderId) sessionStorage.setItem(PAYMENT_ORDER_STORAGE_KEY, currentPaymentOrderId);
    else sessionStorage.removeItem(PAYMENT_ORDER_STORAGE_KEY);
  } catch {
    /* ignore */
  }
}

function storedPaymentOrder() {
  const fromUrl = new URLSearchParams(window.location.search).get("payment");
  if (fromUrl) return fromUrl;
  try {
    return sessionStorage.getItem(PAYMENT_ORDER_STORAGE_KEY) || "";
  } catch {
    return "";
  }
}

function cleanPaymentReturnParam(orderId) {
  const url = new URL(window.location.href);
  if (url.searchParams.get("payment") !== orderId) return;
  url.searchParams.delete("payment");
  window.history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`);
}

async function pollPaymentOrder(orderId, returnedFromCheckout = false) {
  clearPaymentPoll();
  rememberPaymentOrder(orderId);

  const check = async () => {
    try {
      const data = await fetchJson(`/api/payments/status?order=${encodeURIComponent(orderId)}`);
      const order = data.order;
      if (order?.status === "completed") {
        setPaymentState(
          "payment.completed",
          { amount: order.amount || "19.99", currency: order.currency || "USD" },
          "success",
        );
        $("checkoutBtn").disabled = false;
        $("upgradeProBtn").disabled = false;
        cleanPaymentReturnParam(orderId);
        rememberPaymentOrder("");
        try {
          await refreshEntitlement();
        } catch {
          /* The subscription is active; plan details can be refreshed later. */
        }
        return;
      }
      if (order?.status === "failed") {
        setPaymentState("payment.failed", null, "error");
        $("checkoutBtn").disabled = false;
        $("upgradeProBtn").disabled = false;
        cleanPaymentReturnParam(orderId);
        rememberPaymentOrder("");
        return;
      }
      setPaymentState(
        returnedFromCheckout ? "payment.waitingWebhook" : "payment.awaiting",
        null,
        "pending",
      );
    } catch (error) {
      if (error.status === 401) return;
      setPaymentState("payment.waitingWebhook", null, "pending");
    }

    paymentPollAttempts += 1;
    if (paymentPollAttempts < PAYMENT_POLL_LIMIT) {
      paymentPollTimer = setTimeout(check, 2000);
    } else {
      setPaymentState("payment.pollTimedOut", null, "error");
      $("checkoutBtn").disabled = false;
      $("upgradeProBtn").disabled = false;
    }
  };

  await check();
}

async function restorePaymentState() {
  const orderId = storedPaymentOrder();
  if (!orderId) return;
  const returned = new URLSearchParams(window.location.search).get("payment") === orderId;
  await pollPaymentOrder(orderId, returned);
}

async function startProCheckout() {
  const popup = window.open("about:blank", "_blank");
  if (!popup) {
    setPaymentState("payment.popupBlocked", null, "error");
    return;
  }
  if ($("planDialog").open) $("planDialog").close();
  try {
    popup.opener = null;
    popup.document.title = "MarketBrief AI Pro";
    popup.document.body.textContent = t("payment.creating");
  } catch {
    /* The blank tab can still be navigated below. */
  }

  clearPaymentPoll();
  $("checkoutBtn").disabled = true;
  $("upgradeProBtn").disabled = true;
  setPaymentState("payment.creating", null, "pending");
  try {
    const data = await fetchJson("/api/payments/checkout", {
      method: "POST",
      body: JSON.stringify({ locale: typeof getLocale === "function" ? getLocale() : "zh-CN" }),
    });
    rememberPaymentOrder(data.order);
    popup.location.replace(data.checkoutUrl);
    setPaymentState("payment.awaiting", null, "pending");
    await pollPaymentOrder(data.order, false);
  } catch (error) {
    try {
      popup.close();
    } catch {
      /* ignore */
    }
    if (error.code === "checkout_in_progress" && error.payload?.order) {
      rememberPaymentOrder(error.payload.order);
      setPaymentState("payment.awaiting", null, "pending");
      await pollPaymentOrder(error.payload.order, false);
      return;
    }
    setPaymentState("payment.startFailed", { error: error.message }, "error");
    $("checkoutBtn").disabled = false;
    $("upgradeProBtn").disabled = false;
  }
}

function showLogin(message = "") {
  clearPaymentPoll();
  clearSubscriptionRefresh();
  setPaymentState();
  currentUser = null;
  currentEntitlement = null;
  if ($("planDialog").open) $("planDialog").close();
  $("appShell").hidden = true;
  $("loginGate").hidden = false;
  $("userChip").hidden = true;
  document.body.classList.add("loginOnly");
  if (message) {
    $("loginError").hidden = false;
    $("loginError").textContent = message;
  }
}

function showApp() {
  $("loginGate").hidden = true;
  $("appShell").hidden = false;
  document.body.classList.remove("loginOnly");
  if (currentUser?.email) {
    $("userChip").hidden = false;
    $("userEmail").textContent = currentUser.email;
    renderPlanState();
  }
}

async function refreshSession() {
  try {
    const data = await fetchJson("/api/auth/me");
    currentUser = data.user;
    currentEntitlement = data.entitlement;
    showApp();
    return true;
  } catch {
    showLogin();
    return false;
  }
}

function setLoginError(message) {
  $("loginError").hidden = !message;
  $("loginError").textContent = message || "";
}

function startSendCodeCooldown(seconds) {
  const button = $("sendCodeBtn");
  let remain = seconds;
  button.disabled = true;
  button.textContent = `${remain}s`;
  clearInterval(sendCodeCooldownTimer);
  sendCodeCooldownTimer = setInterval(() => {
    remain -= 1;
    if (remain <= 0) {
      clearInterval(sendCodeCooldownTimer);
      sendCodeCooldownTimer = null;
      button.disabled = false;
      button.textContent = t("login.sendCode");
      return;
    }
    button.textContent = `${remain}s`;
  }, 1000);
}

async function sendLoginCode() {
  setLoginError("");
  const email = $("loginEmail").value.trim();
  if (!email) {
    setLoginError(t("login.emailRequired"));
    return;
  }
  $("sendCodeBtn").disabled = true;
  try {
    const data = await fetchJson("/api/auth/send-code", {
      method: "POST",
      body: JSON.stringify({ email }),
    });
    $("loginHint").textContent = data.delivery === "server_log" ? t("login.hintLog") : t("login.hintSent");
    startSendCodeCooldown(60);
  } catch (error) {
    $("sendCodeBtn").disabled = false;
    setLoginError(error.message);
  }
}

async function submitLogin(event) {
  event.preventDefault();
  setLoginError("");
  const email = $("loginEmail").value.trim();
  const code = $("loginCode").value.trim();
  $("loginSubmit").disabled = true;
  try {
    const data = await fetchJson("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, code }),
    });
    currentUser = data.user;
    currentEntitlement = data.entitlement;
    showApp();
    await bootstrapApp();
  } catch (error) {
    setLoginError(error.message);
  } finally {
    $("loginSubmit").disabled = false;
  }
}

async function logout() {
  try {
    await fetchJson("/api/auth/logout", { method: "POST", body: "{}" });
  } catch {
    /* ignore */
  }
  currentUser = null;
  currentEntitlement = null;
  watchSymbolsCache = [];
  signalSnapshotsCache = {};
  signalEventsCache = [];
  rememberPaymentOrder("");
  showLogin();
}

async function loadAiStatus() {
  try {
    aiStatus = await fetchJson("/api/ai-status");
  } catch {
    aiStatus = { configured: false, provider: "local_rules", model: "" };
  }
  $("aiStatus").textContent = aiStatus.configured
    ? t("report.aiConfigured", { provider: providerLabel(aiStatus.provider), model: aiStatus.model })
    : t("report.aiLocal");
}

async function analyze(symbol) {
  setStatusKey("status.analyzing");
  $("generateReport").disabled = true;
  $("reportBody").className = "reportBody empty";
  $("reportBody").textContent = t("report.analyzing");
  reportBodyMode = "analyzing";
  lastRenderedReport = null;
  try {
    const data = await fetchJson(`/api/analyze?symbol=${encodeURIComponent(symbol)}`);
    applyUsage(data.entitlement_usage);
    delete data.entitlement_usage;
    currentAnalysis = data;
    rememberRecentCode(data);
    renderAnalysis(data);
    renderRecentCodes();
    $("generateReport").disabled = false;
    $("reportBody").className = "reportBody empty";
    $("reportBody").textContent = data.news?.length ? t("report.readyWithNews") : t("report.readyNoNews");
    reportBodyMode = data.news?.length ? "readyWithNews" : "readyNoNews";
    setStatusKey("status.analyzed");
  } catch (error) {
    setStatusKey("status.analyzeFailed");
    $("reportBody").className = "reportBody empty";
    $("reportBody").textContent = quotaMessage(error);
    reportBodyMode = "error";
  }
}

function renderAnalysis(data) {
  $("assetType").textContent = `${assetTypeLabel(data.asset_type)} · ${data.exchange || "Yahoo Finance"}`;
  $("title").textContent = `${data.symbol} ${data.name && data.name !== data.symbol ? "· " + data.name : ""}`;  $("price").textContent = `${fmt(data.quote.price, 2)} ${data.currency || ""}`.trim();
  $("change").textContent = `${fmt(data.quote.change_pct, 2)}%`;
  $("change").className = pctClass(data.quote.change_pct);
  $("volume").textContent = fmtCompact(data.quote.volume);
  $("overall").textContent = fmt(data.scores.overall, 1);
  renderDataWarnings(data.data_warnings || []);
  renderDataHealth(data.data_health || {});
  renderSignal(data);
  renderTimeframeSignals(data.timeframe_signals || [], data.symbol);
  $("quoteDate").textContent = data.quote.date;
  $("scoreTrend").textContent = fmt(data.scores.trend, 1);
  $("scoreMomentum").textContent = fmt(data.scores.momentum, 1);
  $("scoreVolume").textContent = fmt(data.scores.volume, 1);
  $("scoreRisk").textContent = fmt(data.scores.risk_control, 1);
  renderIndicators(data.latest_indicators, data.quote.performance);
  renderMarketBrief(data);
  renderNews(data.news || []);
  renderCharts(data);
}

function renderDataWarnings(warnings) {
  const rows = toArray(warnings);
  $("dataWarnings").hidden = !rows.length;
  $("dataWarnings").innerHTML = rows.map((item) => `<div>${escapeHtml(item)}</div>`).join("");
}

function renderDataHealth(health) {
  $("dataHealth").hidden = !health || !health.status;
  if ($("dataHealth").hidden) return;
  const suggestions = toArray(health.suggestions)
    .map(
      (item) => `
        <button class="ghost healthSuggestion" data-suggest-symbol="${escapeHtml(item.symbol || "")}">
          ${escapeHtml(t("health.suggest", { symbol: item.symbol || "" }))}
        </button>
      `,
    )
    .join("");
  $("dataHealth").innerHTML = `
    <div class="healthHead">
      <div>
        <strong>${escapeHtml(t("health.title"))}</strong>
        <span class="healthStatus ${escapeHtml(health.status || "ok")}">${escapeHtml(health.status_label || t("health.ok"))}</span>
      </div>
      <span>${escapeHtml(health.provider || "Yahoo Finance")}</span>
    </div>
    <div class="healthGrid">
      <div><span>${escapeHtml(t("health.source"))}</span><b>${escapeHtml(health.source_name || "--")}</b></div>
      <div><span>${escapeHtml(t("health.exchange"))}</span><b>${escapeHtml(health.exchange || "--")}</b></div>
      <div><span>${escapeHtml(t("health.lastBar"))}</span><b>${escapeHtml(health.last_date || "--")}</b></div>
      <div><span>${escapeHtml(t("health.samples"))}</span><b>${fmt(health.bar_count, 0)}</b></div>
    </div>
    ${suggestions ? `<div class="healthActions">${suggestions}</div>` : ""}
  `;
}

function renderSignal(data) {
  const signal = data.signal;
  if (!signal) return;
  $("signalLabel").textContent = signal.label || "--";
  $("signalMetric").className = `metric signalMetric ${signal.tone || "neutral"}`;
  const reasons = toArray(signal.reasons).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  const cautions = toArray(signal.cautions).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  const levels = signal.levels || {};
  $("signalDetail").className = "signalDetail";
  $("signalDetail").innerHTML = `
    <div class="signalHeader">
      <div>
        <span class="signalBadge ${signal.tone || "neutral"}">${escapeHtml(signal.label || t("signal.pending"))}</span>
        <div class="signalContext">${escapeHtml(data.symbol)} · ${escapeHtml(t("signal.currentSymbol"))} · ${escapeHtml(data.quote?.date || "--")}</div>
      </div>
      <strong>${fmt(signal.confidence, 1)}%</strong>
    </div>
    ${reasons ? `<div><strong>${escapeHtml(t("signal.reasons"))}</strong><ul>${reasons}</ul></div>` : ""}
    ${cautions ? `<div><strong>${escapeHtml(t("signal.cautions"))}</strong><ul>${cautions}</ul></div>` : ""}
    <div class="signalLevels">
      ${escapeHtml(t("signal.support"))}：${(levels.support || []).join(" / ") || "--"}<br>
      ${escapeHtml(t("signal.resistance"))}：${(levels.resistance || []).join(" / ") || "--"}<br>
      ${escapeHtml(t("signal.stop"))}：${levels.stop_reference ?? "--"}
    </div>
    <div class="muted">${escapeHtml(signal.disclaimer || t("signal.disclaimer"))}</div>
  `;
}

async function searchSymbols() {
  const query = normalizedInputSymbol();
  if (!query) return;
  $("symbolSuggestions").hidden = false;
  $("symbolSuggestions").innerHTML = renderSuggestionShell(`<div class="suggestionEmpty">${escapeHtml(t("suggest.loading"))}</div>`);  try {
    const data = await fetchJson(`/api/search?q=${encodeURIComponent(query)}`);
    renderSymbolSuggestions(data.results || []);
  } catch (error) {
    $("symbolSuggestions").innerHTML = renderSuggestionShell(`<div class="suggestionEmpty">${escapeHtml(error.message)}</div>`);
  }
}

function renderSymbolSuggestions(results) {
  $("symbolSuggestions").hidden = false;
  const body = results.length
    ? results
        .map(
          (item) => `
            <button type="button" class="suggestionItem" data-suggestion-symbol="${escapeHtml(item.symbol)}">
              <strong>${escapeHtml(item.symbol)}</strong>
              <span>${escapeHtml(item.name || "")}</span>
              <em>${escapeHtml([item.exchange, item.type].filter(Boolean).join(" · "))}</em>
            </button>
          `,
        )
        .join("")
    : `<div class="suggestionEmpty">${escapeHtml(t("suggest.empty"))}</div>`;
  $("symbolSuggestions").innerHTML = renderSuggestionShell(body);
}

function renderSuggestionShell(body) {
  return `
    <div class="suggestionHead">
      <strong>${escapeHtml(t("suggest.title"))}</strong>
      <button type="button" class="ghost closeSuggestions" data-close-suggestions="1">${escapeHtml(t("suggest.close"))}</button>
    </div>
    ${body}
  `;
}

function hideSymbolSuggestions() {
  $("symbolSuggestions").hidden = true;
}

function renderTimeframeSignals(signals, symbol) {
  const rows = toArray(signals);
  if (!rows.length) {
    const symbolLabel = symbol || t("timeframe.currentSymbol");
    $("timeframeSignals").innerHTML = `<div class="emptyNews">${escapeHtml(t("timeframe.none", { symbol: symbolLabel }))}</div>`;
    return;
  }
  $("timeframeSignals").innerHTML = `
    <div class="timeframeTitle">
      <strong>${escapeHtml(t("timeframe.title", { symbol: symbol || t("timeframe.currentSymbol") }))}</strong>
      <span>${escapeHtml(t("timeframe.range"))}</span>
    </div>
    ${rows
      .map((item) => {
        const notes = toArray(item.notes).map((note) => `<li>${escapeHtml(note)}</li>`).join("");
        return `
          <article class="timeframeCard ${item.tone || "neutral"}">
            <div class="timeframeHead">
              <div>
                <strong>${escapeHtml(symbol || "--")} · ${escapeHtml(item.name || "--")}</strong>
                <span>${escapeHtml(item.horizon || "")}</span>
              </div>
              <span class="signalBadge ${item.tone || "neutral"}">${escapeHtml(item.label || t("signal.pending"))}</span>
            </div>
            <div class="timeframeMetric">
              <span>${escapeHtml(t("signal.confidence", { n: fmt(item.confidence, 1) }))}</span>
              <span class="${pctClass(Number(item.performance))}">${fmt(item.performance, 2)}%</span>
            </div>
            <p>${escapeHtml(item.focus || "")}</p>
            ${notes ? `<ul>${notes}</ul>` : ""}
          </article>
        `;
      })
      .join("")}
  `;
}

function renderMarketBrief(data) {
  const i = data.latest_indicators;
  const p = data.quote.performance;
  const rows = [
    [t("indicator.perf20"), p["20d"], "%", true],
    [t("indicator.perf60"), p["60d"], "%", true],
    [t("indicator.perf120"), p["120d"], "%", true],
    [t("indicator.volAnnual"), i.volatility_60d_annualized, "%", false],
    ["ATR14", i.atr14, "", false],
    [t("chart.volumeRatio"), i.volume_ratio, "", false],
  ];
  $("marketBrief").innerHTML = rows
    .map(([label, value, suffix, signed]) => {
      const cls = signed ? pctClass(Number(value)) : "";
      return `<div class="briefItem"><span>${label}</span><strong class="${cls}">${fmt(value, 2)}${suffix}</strong></div>`;
    })
    .join("");
}

function renderIndicators(indicators, performance) {
  const rows = [
    ["MA20", indicators.ma20],
    ["MA60", indicators.ma60],
    ["RSI14", indicators.rsi14],
    [t("chart.macdHist"), indicators.macd_hist],
    [t("chart.bollUpperLabel"), indicators.boll_upper],
    [t("chart.bollLowerLabel"), indicators.boll_lower],
    ["ATR14", indicators.atr14],
    [t("indicator.volumeRatio20"), indicators.volume_ratio],
    [t("indicator.volAnnual60"), indicators.volatility_60d_annualized, "%"],
    [t("indicator.perf20"), performance["20d"], "%"],
    [t("indicator.perf60"), performance["60d"], "%"],
    [t("indicator.perf120"), performance["120d"], "%"],
  ];
  $("indicatorList").innerHTML = rows
    .map(([label, value, suffix]) => {
      const numeric = Number(value);
      const cls = label.includes(t("indicator.perfMarker")) ? pctClass(numeric) : "";
      return `<div class="indicator"><span>${label}</span><strong class="${cls}">${fmt(value, 2)}${suffix || ""}</strong></div>`;
    })
    .join("");
}

function renderCharts(data) {
  if (!priceChart) priceChart = echarts.init($("priceChart"));
  if (!indicatorChart) indicatorChart = echarts.init($("indicatorChart"));
  const indicators = data.latest_indicators || {};
  $("priceChartTitle").textContent = t("chart.priceTitleWithSymbol", { symbol: data.symbol });
  $("quoteDate").textContent = `${data.quote.date} · ${data.exchange || "Yahoo Finance"}`;
  $("indicatorChartTitle").textContent = t("chart.indicatorTitleWithSymbol", { symbol: data.symbol });
  $("indicatorChartMeta").textContent = t("chart.indicatorMetaWithDate", { date: data.quote.date });
  $("priceChartLegend").innerHTML = renderChartValueLegend([
    ["close", t("chart.close"), data.quote.price],
    ["ma20", "MA20", indicators.ma20],
    ["ma60", "MA60", indicators.ma60],
    ["boll", t("chart.bollUpperLabel"), indicators.boll_upper],
    ["boll", t("chart.bollLowerLabel"), indicators.boll_lower],
  ]);
  $("indicatorChartLegend").innerHTML = renderChartValueLegend([
    ["rsi", "RSI14", indicators.rsi14],
    ["macd", "MACD", indicators.macd],
    ["signal", t("chart.macdHist"), indicators.macd_hist],
    ["volume", t("chart.volumeRatio"), indicators.volume_ratio],
  ]);
  const dates = data.series.map((row) => row.date);
  const candle = data.series.map((row) => [row.open, row.close, row.low, row.high]);
  const close = data.series.map((row) => row.close);
  const ma20 = data.series.map((row) => row.ma20);
  const ma60 = data.series.map((row) => row.ma60);
  const upper = data.series.map((row) => row.boll_upper);
  const lower = data.series.map((row) => row.boll_lower);
  priceChart.setOption({
    animation: false,
    tooltip: { trigger: "axis" },
    legend: { top: 0, data: [t("chart.candle"), t("chart.close"), "MA20", "MA60", t("chart.bollUpper"), t("chart.bollLower")] },
    grid: { left: 55, right: 20, top: 45, bottom: 42 },
    xAxis: { type: "category", data: dates, boundaryGap: true, axisLabel: { hideOverlap: true } },
    yAxis: { scale: true },
    dataZoom: [{ type: "inside" }, { type: "slider", height: 22, bottom: 8 }],
    series: [
      { name: t("chart.candle"), type: "candlestick", data: candle, itemStyle: { color: "#15803d", color0: "#b42318", borderColor: "#15803d", borderColor0: "#b42318" } },
      { name: t("chart.close"), type: "line", data: close, showSymbol: false, smooth: true, lineStyle: { width: 1.3, color: "#334155" } },
      { name: "MA20", type: "line", data: ma20, showSymbol: false, smooth: true, lineStyle: { width: 1.5, color: "#0f766e" } },
      { name: "MA60", type: "line", data: ma60, showSymbol: false, smooth: true, lineStyle: { width: 1.5, color: "#2456a6" } },
      { name: t("chart.bollUpper"), type: "line", data: upper, showSymbol: false, smooth: true, lineStyle: { width: 1, color: "#b7791f", type: "dashed" } },
      { name: t("chart.bollLower"), type: "line", data: lower, showSymbol: false, smooth: true, lineStyle: { width: 1, color: "#b7791f", type: "dashed" } },
    ],
  });

  indicatorChart.setOption({
    animation: false,
    tooltip: { trigger: "axis" },
    legend: { top: 0, data: ["RSI", "MACD", "Signal", "Histogram"] },
    grid: [
      { left: 50, right: 20, top: 42, height: 95 },
      { left: 50, right: 20, top: 178, height: 75 },
    ],
    xAxis: [
      { type: "category", data: dates, axisLabel: { hideOverlap: true } },
      { type: "category", data: dates, gridIndex: 1, axisLabel: { hideOverlap: true } },
    ],
    yAxis: [{ scale: true }, { scale: true, gridIndex: 1 }],
    series: [
      { name: "RSI", type: "line", data: data.series.map((row) => row.rsi), showSymbol: false, xAxisIndex: 0, yAxisIndex: 0, lineStyle: { color: "#0f766e" } },
      { name: "MACD", type: "line", data: data.series.map((row) => row.macd), showSymbol: false, xAxisIndex: 1, yAxisIndex: 1, lineStyle: { color: "#2456a6" } },
      { name: "Signal", type: "line", data: data.series.map((row) => row.macd_signal), showSymbol: false, xAxisIndex: 1, yAxisIndex: 1, lineStyle: { color: "#b7791f" } },
      { name: "Histogram", type: "bar", data: data.series.map((row) => row.macd_hist), xAxisIndex: 1, yAxisIndex: 1, itemStyle: { color: "#94a3b8" } },
    ],
  });
}

function renderChartValueLegend(items) {
  return items
    .map(
      ([tone, label, value]) => `
        <span class="chartLegendPill ${tone}">
          <i></i>${escapeHtml(label)} <b>${fmt(value, 2)}</b>
        </span>
      `,
    )
    .join("");
}

function renderNews(news) {
  $("newsCount").textContent = news.length ? t("news.count", { n: news.length }) : t("news.none");
  if (!news.length) {
    $("newsList").innerHTML = `<div class="emptyNews">${escapeHtml(t("news.unavailable"))}</div>`;
    return;
  }
  $("newsList").innerHTML = news
    .map((item) => {
      const title = escapeHtml(item.title || "Untitled");
      const publisher = escapeHtml(item.publisher || "Yahoo Finance");
      const date = escapeHtml(item.published_at || "");
      const summary = escapeHtml(item.summary || "");
      const href = item.link ? escapeHtml(item.link) : "#";
      return `
        <article class="newsItem">
          <a href="${href}" target="_blank" rel="noreferrer">${title}</a>
          <div class="newsMeta">${publisher}${date ? " · " + date : ""}</div>
          ${summary ? `<div class="newsSummary">${summary}</div>` : ""}
        </article>
      `;
    })
    .join("");
}

async function generateReport() {
  if (!currentAnalysis) return;
  setStatusKey("status.generatingReport");
  $("generateReport").disabled = true;
  $("reportBody").className = "reportBody empty";
  $("reportBody").textContent = t("report.generating");
  reportBodyMode = "generating";
  try {
    const data = await fetchJson("/api/report", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbol: currentAnalysis.symbol, analysis: currentAnalysis }),
    });
    applyUsage(data.analysis_usage);
    applyUsage(data.usage);
    lastRenderedReport = data.report;
    reportBodyMode = "report";
    renderReport(data.report);
    await loadReports();
    if (data.ai?.quota_exceeded) {
      setStatusKey("status.reportSavedQuotaFallback", {
        id: data.id,
        limit: data.usage?.limit || usageFor("ai_report_monthly").limit,
      });
    } else if (data.ai?.used) {
      setStatusKey("status.reportSavedAi", { provider: providerLabel(data.ai.provider), id: data.id });
    } else {
      const detail = data.ai?.error ? ` · ${data.ai.error}` : "";
      setStatusKey("status.reportSavedLocal", { id: data.id, detail });
    }
  } catch (error) {
    $("reportBody").className = "reportBody empty";
    $("reportBody").textContent = error.message;
    reportBodyMode = "error";
    setStatusKey("status.reportFailed");
  } finally {
    $("generateReport").disabled = false;
  }
}

function renderReport(report) {
  lastRenderedReport = report;
  reportBodyMode = "report";
  const support = toArray(report.key_levels?.support);
  const resistance = toArray(report.key_levels?.resistance);
  const source = report.source === "ai" ? providerLabel(report.provider || aiStatus.provider) : t("report.localRules");
  $("reportBody").className = "reportBody";
  $("reportBody").innerHTML = `
    <div class="reportMeta">
      <span class="stance ${report.stance || "neutral"}">${escapeHtml(stanceLabel(report.stance))}</span>
      <span class="sourceBadge">${escapeHtml(source)}</span>
    </div>
    <div><h4>${escapeHtml(t("report.summary"))}</h4><div>${escapeHtml(report.summary || "")}</div></div>
    ${renderList(t("report.opportunities"), report.opportunities)}
    ${renderList(t("report.risks"), report.risks)}
    ${renderList(t("report.catalysts"), report.catalysts)}
    ${renderList(t("report.actionChecklist"), report.action_checklist)}
    ${renderReportSignal(report.trade_signal)}
    ${renderNewsBrief(report.news_brief)}
    <div><h4>${escapeHtml(t("report.keyLevels"))}</h4><div>${escapeHtml(t("report.supportResistance", { support: support.join(" / ") || "--", resistance: resistance.join(" / ") || "--" }))}</div></div>
    ${renderList(t("report.watchPlan"), report.watch_plan)}
    <div class="muted">${escapeHtml(report.disclaimer || t("report.disclaimer"))}</div>
  `;
}

function providerLabel(provider) {
  return {
    deepseek: "DeepSeek",
    openrouter: "OpenRouter",
    openai: "OpenAI",
    local_rules: t("report.localRules"),
  }[provider] || provider || "AI";
}

function renderReportSignal(signal) {
  if (!signal) return "";
  return `
    <div>
      <h4>${escapeHtml(t("report.tradeSignal"))}</h4>
      <div><span class="signalBadge ${signal.tone || "neutral"}">${escapeHtml(signal.label || "")}</span> ${escapeHtml(t("signal.confidence", { n: fmt(signal.confidence, 1) }))}</div>
    </div>
  `;
}

function renderNewsBrief(items) {
  items = toArray(items);
  if (!items.length) return "";
  const safe = items
    .map((item) => {
      if (typeof item === "string") return `<li>${escapeHtml(item)}</li>`;
      return `<li>${escapeHtml(item.title || JSON.stringify(item))} <span class="muted">${escapeHtml(item.publisher || "")}</span></li>`;
    })
    .join("");
  return `<div><h4>${escapeHtml(t("report.newsBrief"))}</h4><ul>${safe}</ul></div>`;
}

function renderList(title, items) {
  const safe = toArray(items).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  return `<div><h4>${title}</h4><ul>${safe || "<li>--</li>"}</ul></div>`;
}

function toArray(value) {
  if (value === null || value === undefined || value === "") return [];
  if (Array.isArray(value)) return value;
  if (typeof value === "object") {
    return Object.entries(value).map(([key, val]) => `${key}: ${typeof val === "object" ? JSON.stringify(val) : val}`);
  }
  return [String(value)];
}

function escapeHtml(text) {
  return String(text)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function normalizedInputSymbol() {
  return $("symbolInput").value.trim().toUpperCase();
}

function getRecentCodes() {
  const raw = JSON.parse(localStorage.getItem("quantRecentCodes") || "[]");
  return raw
    .map((item) => {
      if (typeof item === "string") return { symbol: item, name: "", exchange: "", last_seen: "" };
      return item || {};
    })
    .filter((item) => item.symbol);
}

function saveRecentCodes(items) {
  localStorage.setItem("quantRecentCodes", JSON.stringify(items.slice(0, RECENT_CODE_STORE_LIMIT)));
}

function rememberRecentCode(data) {
  const symbol = data.symbol || normalizedInputSymbol();
  if (!symbol) return;
  const next = {
    symbol,
    name: data.name && data.name !== symbol ? data.name : "",
    exchange: data.exchange || data.asset_type || "",
    last_seen: new Date().toISOString(),
  };
  const rest = getRecentCodes().filter((item) => item.symbol !== symbol);
  saveRecentCodes([next, ...rest]);
}

function renderRecentCodes() {
  const rows = getRecentCodes();
  const visibleRows = recentCodesExpanded ? rows : rows.slice(0, RECENT_CODE_PREVIEW_LIMIT);
  $("toggleRecentCodes").hidden = rows.length <= RECENT_CODE_PREVIEW_LIMIT;
  $("toggleRecentCodes").textContent = recentCodesExpanded ? t("action.collapse") : t("action.moreCount", { n: rows.length });
  $("recentCodeList").innerHTML = visibleRows.length
    ? visibleRows
        .map(
          (item) => `
            <div class="recentCodeRow">
              <button class="recentCodeItem" data-recent-symbol="${escapeHtml(item.symbol)}">
                <strong>${escapeHtml(item.symbol)}</strong>
                <span>${escapeHtml([item.name, item.exchange].filter(Boolean).join(" · ") || t("recent.fallback"))}</span>
              </button>
              <button class="ghost dangerAction" data-delete-recent="${escapeHtml(item.symbol)}">${escapeHtml(t("action.delete"))}</button>
            </div>
          `,
        )
        .join("")
    : `<div class="historyItem"><p>${escapeHtml(t("recent.empty"))}</p></div>`;
}

function removeRecentCode(symbol) {
  saveRecentCodes(getRecentCodes().filter((item) => item.symbol !== symbol));
  renderRecentCodes();
}

function getWatchSymbols() {
  return watchSymbolsCache;
}

async function persistWatchSymbols(symbols) {
  const data = await fetchJson("/api/watchlist", {
    method: "PUT",
    body: JSON.stringify({ symbols }),
  });
  watchSymbolsCache = data.symbols || [];
  const allowed = new Set(watchSymbolsCache);
  signalSnapshotsCache = Object.fromEntries(
    Object.entries(signalSnapshotsCache).filter(([symbol]) => allowed.has(symbol)),
  );
  renderWatchList();
}

async function loadWatchlist() {
  try {
    const data = await fetchJson("/api/watchlist");
    watchSymbolsCache = data.symbols || [];
  } catch {
    watchSymbolsCache = [];
  }
  renderWatchList();
}

async function addWatchSymbol() {
  const symbol = normalizedInputSymbol();
  if (!symbol) return;
  const symbols = [...getWatchSymbols()];
  if (!symbols.includes(symbol)) {
    symbols.unshift(symbol);
  }
  try {
    await persistWatchSymbols(symbols);
  } catch (error) {
    window.alert(t("alert.watchSaveFailed", { error: quotaMessage(error) }));
  }
}

async function removeWatchSymbol(symbol) {
  try {
    await persistWatchSymbols(getWatchSymbols().filter((item) => item !== symbol));
  } catch (error) {
    window.alert(t("alert.watchRemoveFailed", { error: quotaMessage(error) }));
  }
}

function renderWatchList() {
  const symbols = getWatchSymbols();
  $("watchList").innerHTML = symbols.length
    ? symbols
        .map(
          (symbol) => `
            <div class="watchItem">
              <button class="watchSymbol" data-watch-symbol="${escapeHtml(symbol)}">${escapeHtml(symbol)}</button>
              <button class="ghost watchRemove" data-remove-symbol="${escapeHtml(symbol)}">${escapeHtml(t("action.remove"))}</button>
            </div>
          `,
        )
        .join("")
    : `<div class="historyItem"><p>${escapeHtml(t("watch.empty"))}</p></div>`;
}

function getSignalSnapshots() {
  return signalSnapshotsCache;
}

function getSignalEvents() {
  return signalEventsCache;
}

async function loadSignalState() {
  try {
    const data = await fetchJson("/api/signal-events");
    signalEventsCache = data.events || [];
    signalSnapshotsCache = data.snapshots || {};
  } catch {
    signalEventsCache = [];
    signalSnapshotsCache = {};
  }
  renderSignalChanges();
}

async function scanWatchlist() {
  const symbols = getWatchSymbols();
  if (!symbols.length) {
    $("scanSummary").textContent = t("scanner.needWatch");
    $("scanResults").innerHTML = "";
    $("signalChanges").innerHTML = "";
    return;
  }
  setStatusKey("status.scanning");
  $("scanWatch").disabled = true;
  $("scanWatchTop").disabled = true;
  $("scanSummary").textContent = t("scanner.scanning", { n: symbols.length });
  try {
    const data = await fetchJson("/api/scan", {
      method: "POST",
      body: JSON.stringify({ symbols }),
    });
    applyUsage(data.usage);
    latestScanResults = data.results || [];
    await recordSignalChanges(latestScanResults);
    renderScanResults();
    renderSignalChanges();
    setStatusKey("status.scanDone");
  } catch (error) {
    $("scanSummary").textContent = quotaMessage(error);
    setStatusKey("status.scanFailed");
  } finally {
    $("scanWatch").disabled = false;
    $("scanWatchTop").disabled = false;
  }
}

async function recordSignalChanges(results) {
  const snapshots = { ...getSignalSnapshots() };
  const newEvents = [];
  const checkedAt = new Date().toISOString();
  results.forEach((item) => {
    if (item.error || !item.symbol || !item.signal) return;
    const previous = snapshots[item.symbol];
    const current = {
      action: item.signal.action,
      label: item.signal.label,
      tone: item.signal.tone,
      checked_at: checkedAt,
    };
    if (previous && previous.action !== current.action) {
      newEvents.push({
        symbol: item.symbol,
        name: item.name || "",
        previous_label: previous.label || t("unknown"),
        current_label: current.label || t("unknown"),
        tone: current.tone || "neutral",
        price: item.price,
        overall: item.overall,
        confidence: item.signal.confidence,
        quote_date: item.date,
        checked_at: checkedAt,
      });
    }
    snapshots[item.symbol] = current;
  });
  signalSnapshotsCache = snapshots;
  if (!newEvents.length && Object.keys(snapshots).length) {
    try {
      await fetchJson("/api/signal-events", {
        method: "POST",
        body: JSON.stringify({ events: [], snapshots }),
      });
    } catch {
      /* ignore snapshot sync failure */
    }
    return;
  }
  try {
    const data = await fetchJson("/api/signal-events", {
      method: "POST",
      body: JSON.stringify({ events: newEvents, snapshots }),
    });
    signalEventsCache = data.events || [...newEvents, ...signalEventsCache].slice(0, 80);
  } catch (error) {
    console.warn("signal sync failed", error);
    signalEventsCache = [...newEvents, ...signalEventsCache].slice(0, 80);
  }
}

function renderScanResults() {
  const rows = [...latestScanResults].sort((a, b) => scanSortValue(b) - scanSortValue(a));
  const valid = rows.filter((item) => !item.error);
  const changed = getSignalEvents().filter((event) => rows.some((item) => item.symbol === event.symbol)).length;
  $("scanSummary").textContent = valid.length
    ? t("scanner.doneSummary", {
        n: valid.length,
        detail: changed ? t("scanner.changes", { n: changed }) : t("scanner.noChanges"),
      })
    : t("scanner.noResults");
  $("scanResults").innerHTML = rows.length
    ? rows.map(renderScanCard).join("")
    : `<div class="emptyNews">${escapeHtml(t("scanner.empty"))}</div>`;
}

function scanSortValue(item) {
  if (item.error) return -Infinity;
  const sort = $("scanSort").value;
  if (sort === "change") return Number(item.change_pct ?? -Infinity);
  if (sort === "risk") return Number(item.risk_control ?? -Infinity);
  if (sort === "signal") return Number(item.signal?.score ?? -Infinity);
  return Number(item.overall ?? -Infinity);
}

function renderScanCard(item) {
  if (item.error) {
    return `
      <article class="scanCard error">
        <strong>${escapeHtml(item.symbol || "--")}</strong>
        <p>${escapeHtml(item.error)}</p>
      </article>
    `;
  }
  const signal = item.signal || {};
  const warning = toArray(item.warnings)[0];
  return `
    <article class="scanCard ${signal.tone || "neutral"}" data-scan-symbol="${escapeHtml(item.symbol)}">
      <div class="scanHead">
        <div>
          <strong>${escapeHtml(item.symbol)}</strong>
          <span>${escapeHtml(item.name || "")}</span>
        </div>
        <span class="signalBadge ${signal.tone || "neutral"}">${escapeHtml(signal.label || "--")}</span>
      </div>
      <div class="scanMetrics">
        <span>${escapeHtml(t("scanner.price"))} <b>${fmt(item.price, 2)}</b></span>
        <span>${escapeHtml(t("scanner.change"))} <b class="${pctClass(Number(item.change_pct))}">${fmt(item.change_pct, 2)}%</b></span>
        <span>${escapeHtml(t("scanner.score"))} <b>${fmt(item.overall, 1)}</b></span>
        <span>${escapeHtml(t("scanner.risk"))} <b>${fmt(item.risk_control, 1)}</b></span>
      </div>
      <div class="scanFoot">
        <span>${escapeHtml(item.date || "")}</span>
        <span>${escapeHtml(t("scanner.confidence", { n: fmt(signal.confidence, 1) }))}</span>
      </div>
      ${warning ? `<div class="scanWarning">${escapeHtml(warning)}</div>` : ""}
    </article>
  `;
}

function renderSignalChanges() {
  const events = getSignalEvents().slice(0, 8);
  $("signalChanges").innerHTML = events.length
    ? `
      <div class="panelHead compactHead">
        <div>
          <h3>${escapeHtml(t("scanner.changesTitle"))}</h3>
          <div class="muted">${escapeHtml(t("scanner.changesHint"))}</div>
        </div>
        <div class="muted">${escapeHtml(t("scanner.recentN", { n: events.length }))}</div>
      </div>
      <div class="changeList">
        ${events
          .map(
            (event) => `
              <div class="changeItem">
                <div class="changeSymbol">
                  <strong>${escapeHtml(event.symbol)}</strong>
                  <span>${escapeHtml(event.name || event.quote_date || "")}</span>
                </div>
                <div class="changeFlow">
                  <span>${escapeHtml(event.previous_label)}</span>
                  <b>→</b>
                  <span class="signalBadge ${event.tone || "neutral"}">${escapeHtml(event.current_label)}</span>
                </div>
                <div class="changeMetrics">
                  <span>${escapeHtml(t("scanner.price"))} <b>${fmt(event.price, 2)}</b></span>
                  <span>${escapeHtml(t("scanner.score"))} <b>${fmt(event.overall, 1)}</b></span>
                  <span>${escapeHtml(t("scanner.confidence", { n: fmt(event.confidence, 1) }))}</span>
                </div>
                <span class="muted">${new Date(event.checked_at).toLocaleString(localeTag())}</span>
              </div>
            `,
          )
          .join("")}
      </div>
    `
    : "";
}

async function loadReports() {
  try {
    const data = await fetchJson("/api/reports");
    $("historyList").innerHTML = data.reports.length
      ? data.reports
          .map(
            (item) => `
            <div class="historyItem">
              <strong>${escapeHtml(item.symbol)} · ${escapeHtml(stanceLabel(item.stance))}</strong>
              <p>${escapeHtml(item.summary || "")}</p>
              <p>${new Date(item.created_at).toLocaleString(localeTag())}</p>
              <div class="historyActions">
                <button class="ghost" data-toggle-report="${item.id}">
                  ${expandedReportIds.has(item.id) ? escapeHtml(t("action.collapse")) : escapeHtml(t("action.expand"))}
                </button>
                <button class="ghost dangerAction" data-delete-report="${item.id}">${escapeHtml(t("action.delete"))}</button>
              </div>
              <div class="historyReportDetail" ${expandedReportIds.has(item.id) ? "" : "hidden"}>
                ${renderHistoricalReport(item.report || {})}
              </div>
            </div>
          `,
          )
          .join("")
      : `<div class="historyItem"><p>${escapeHtml(t("reports.empty"))}</p></div>`;
  } catch {
    $("historyList").innerHTML = `<div class="historyItem"><p>${escapeHtml(t("reports.loadFailed"))}</p></div>`;
  }
}

function renderHistoricalReport(report) {
  return `
    <div class="reportMeta">
      <span class="stance ${escapeHtml(report.stance || "neutral")}">${escapeHtml(stanceLabel(report.stance))}</span>
      <span>${escapeHtml(report.model || (report.source === "ai" ? "AI" : t("report.localRules")))}</span>
    </div>
    <div><h4>${escapeHtml(t("report.summary"))}</h4><div>${escapeHtml(report.summary || "")}</div></div>
    ${renderList(t("report.opportunities"), report.opportunities)}
    ${renderList(t("report.risks"), report.risks)}
    ${renderReportSignal(report.trade_signal)}
    ${renderNewsBrief(report.news_brief)}
    ${renderList(t("report.watchPlan"), report.watch_plan)}
    <div class="muted">${escapeHtml(report.disclaimer || t("report.disclaimer"))}</div>
  `;
}

async function deleteHistoricalReport(reportId) {
  if (!window.confirm(t("confirm.deleteReport"))) return;
  try {
    await fetchJson(`/api/reports/${reportId}`, { method: "DELETE" });
    expandedReportIds.delete(reportId);
    await loadReports();
  } catch (error) {
    window.alert(t("alert.deleteFailed", { error: error.message }));
  }
}

async function bootstrapApp() {
  await Promise.all([
    loadAiStatus(),
    loadReports(),
    loadWatchlist(),
    loadSignalState(),
    restorePaymentState(),
    loadBriefPreferences(),
  ]);
  renderRecentCodes();
}

async function loadBriefPreferences() {
  try {
    const data = await fetchJson("/api/brief/preferences");
    const preferences = data.preferences || {};
    $("briefEnabled").checked = Boolean(preferences.enabled);
    $("briefTime").value = preferences.delivery_time || "18:00";
    $("briefTimezone").value = preferences.timezone || "Asia/Shanghai";
    $("briefConfidence").value = preferences.min_confidence ?? 40;
    $("briefOnlyChanges").checked = preferences.only_changes !== false;
    const selected = new Set(preferences.channels || ["email"]);
    document.querySelectorAll("[data-brief-channel]").forEach((input) => {
      input.checked = selected.has(input.value);
    });
    const configured = Object.entries(data.channels || {})
      .filter(([, enabled]) => enabled)
      .map(([channel]) => channel);
    $("briefChannelStatus").textContent = configured.length
      ? `已配置：${configured.join("、")}`
      : "尚未配置推送凭据；可先保存设置，配置后再启用。";
    const summaries = data.channel_configs || {};
    $("channelConfigStatus").textContent = Object.entries(summaries).length
      ? Object.entries(summaries).map(([key, value]) => `${key}: ${value.label}${value.configured ? "" : "（需重配）"}`).join("；")
      : "尚未保存个人渠道凭据。";
    renderBriefSetup(data.setup || {});
    renderBriefJobs(data.jobs || []);
    renderBriefDeliveries(data.deliveries || []);
  } catch (error) {
    $("briefStatus").textContent = error.message;
  }
}

function renderBriefSetup(setup) {
  const rows = [
    [setup.watchlist_ready, `关注列表（${setup.watch_count || 0} 个标的）`],
    [setup.channel_ready, "至少一个所选通知渠道已配置"],
    [setup.brief_enabled, "每日简报已启用"],
  ];
  $("briefSetupChecklist").innerHTML = rows
    .map(([ready, label]) => `<div class="historyItem"><strong>${ready ? "✓" : "○"} ${escapeHtml(label)}</strong></div>`)
    .join("");
}

function renderBriefJobs(rows) {
  $("briefJobs").innerHTML = rows.length
    ? rows.map((row) => `<div class="historyItem"><strong>${escapeHtml(row.local_date)} · ${escapeHtml(row.status)}</strong><p>${row.symbols.length} 个标的 · 成功 ${row.sent_count} · 失败 ${row.failed_count}${row.error ? ` · ${escapeHtml(row.error)}` : ""}</p></div>`).join("")
    : `<div class="historyItem"><p>暂无任务记录</p></div>`;
}

function renderBriefDeliveries(rows) {
  $("briefDeliveries").innerHTML = rows.length
    ? rows
        .map(
          (row) => `<div class="historyItem"><strong>${escapeHtml(row.channel)} · ${escapeHtml(row.status)}</strong><p>${escapeHtml(row.sent_at || row.created_at || "")} · 尝试 ${row.attempt_count} 次${row.error ? ` · ${escapeHtml(row.error)}` : ""}</p>${row.status === "failed" ? `<button type="button" class="ghost compactButton" data-retry-delivery="${row.id}">重新发送</button>` : ""}</div>`,
        )
        .join("")
    : `<div class="historyItem"><p>暂无推送记录</p></div>`;
}

async function saveBriefPreferences(event) {
  event.preventDefault();
  $("saveBriefSettings").disabled = true;
  try {
    const channels = [...document.querySelectorAll("[data-brief-channel]:checked")].map(
      (input) => input.value,
    );
    await fetchJson("/api/brief/preferences", {
      method: "PUT",
      body: JSON.stringify({
        enabled: $("briefEnabled").checked,
        delivery_time: $("briefTime").value,
        timezone: $("briefTimezone").value,
        min_confidence: Number($("briefConfidence").value),
        only_changes: $("briefOnlyChanges").checked,
        channels,
      }),
    });
    $("briefStatus").textContent = "自动简报设置已保存。";
    await loadBriefPreferences();
  } catch (error) {
    $("briefStatus").textContent = error.message;
  } finally {
    $("saveBriefSettings").disabled = false;
  }
}

async function runBriefNow() {
  $("runBriefNow").disabled = true;
  $("briefStatus").textContent = "正在生成每日简报...";
  try {
    const data = await fetchJson("/api/brief/run", { method: "POST", body: "{}" });
    if (data.status === "already_run") $("briefStatus").textContent = "今天已经生成过简报。";
    else if (data.status === "failed") $("briefStatus").textContent = data.error || "简报生成失败。";
    else $("briefStatus").textContent = `简报已生成，成功发送 ${data.sent || 0} 个渠道。`;
    await loadBriefPreferences();
  } catch (error) {
    $("briefStatus").textContent = error.message;
  } finally {
    $("runBriefNow").disabled = false;
  }
}

async function testBriefChannel() {
  const selected = document.querySelector("[data-brief-channel]:checked");
  if (!selected) {
    $("briefStatus").textContent = "请先选择一个通知渠道。";
    return;
  }
  $("testBriefChannel").disabled = true;
  $("briefStatus").textContent = `正在测试 ${selected.value}...`;
  try {
    const data = await fetchJson("/api/brief/test", {
      method: "POST",
      body: JSON.stringify({ channel: selected.value }),
    });
    $("briefStatus").textContent = `${data.channel} 测试推送成功（尝试 ${data.attempts} 次）。`;
  } catch (error) {
    $("briefStatus").textContent = error.message;
  } finally {
    $("testBriefChannel").disabled = false;
  }
}

function updateChannelConfigFields() {
  const telegram = $("channelConfigType").value === "telegram";
  $("webhookConfigField").hidden = telegram;
  $("telegramTokenField").hidden = !telegram;
  $("telegramChatField").hidden = !telegram;
}

async function saveChannelConfig() {
  const channel = $("channelConfigType").value;
  const payload = channel === "telegram"
    ? { channel, bot_token: $("telegramBotToken").value, chat_id: $("telegramChatId").value }
    : { channel, webhook_url: $("channelWebhookUrl").value };
  $("saveChannelConfig").disabled = true;
  try {
    const data = await fetchJson("/api/brief/channel-config", { method: "PUT", body: JSON.stringify(payload) });
    $("channelConfigStatus").textContent = `${data.channel} 已加密保存：${data.label}`;
    $("channelWebhookUrl").value = "";
    $("telegramBotToken").value = "";
    await loadBriefPreferences();
  } catch (error) {
    $("channelConfigStatus").textContent = error.message;
  } finally {
    $("saveChannelConfig").disabled = false;
  }
}

async function deleteChannelConfig() {
  const channel = $("channelConfigType").value;
  if (!window.confirm(`确定删除 ${channel} 的个人推送配置吗？`)) return;
  try {
    await fetchJson(`/api/brief/channel-config/${channel}`, { method: "DELETE" });
    $("channelConfigStatus").textContent = `${channel} 配置已删除。`;
    await loadBriefPreferences();
  } catch (error) {
    $("channelConfigStatus").textContent = error.message;
  }
}

async function retryBriefDelivery(deliveryId) {
  $("briefStatus").textContent = "正在重新发送...";
  try {
    await fetchJson(`/api/brief/deliveries/${deliveryId}/retry`, { method: "POST", body: "{}" });
    $("briefStatus").textContent = "重新发送成功。";
  } catch (error) {
    $("briefStatus").textContent = error.message;
  }
  await loadBriefPreferences();
}

async function submitFeedback(event) {
  event.preventDefault();
  $("submitFeedback").disabled = true;
  try {
    await fetchJson("/api/feedback", { method: "POST", body: JSON.stringify({ category: $("feedbackCategory").value, message: $("feedbackMessage").value }) });
    $("feedbackStatus").textContent = "感谢反馈，我们已经收到。";
    $("feedbackMessage").value = "";
  } catch (error) {
    $("feedbackStatus").textContent = error.message;
  } finally {
    $("submitFeedback").disabled = false;
  }
}

$("loginForm").addEventListener("submit", submitLogin);
$("sendCodeBtn").addEventListener("click", sendLoginCode);
$("logoutBtn").addEventListener("click", logout);
$("checkoutBtn").addEventListener("click", startProCheckout);
$("upgradeProBtn").addEventListener("click", startProCheckout);
$("planDetailsBtn").addEventListener("click", openPlanDialog);
$("managePlanBtn").addEventListener("click", openPlanDialog);
$("closePlanDialog").addEventListener("click", () => $("planDialog").close());
$("briefSettingsBtn").addEventListener("click", async () => {
  await loadBriefPreferences();
  $("briefDialog").showModal();
});
$("closeBriefDialog").addEventListener("click", () => $("briefDialog").close());
$("briefForm").addEventListener("submit", saveBriefPreferences);
$("runBriefNow").addEventListener("click", runBriefNow);
$("testBriefChannel").addEventListener("click", testBriefChannel);
$("channelConfigType").addEventListener("change", updateChannelConfigFields);
$("saveChannelConfig").addEventListener("click", saveChannelConfig);
$("deleteChannelConfig").addEventListener("click", deleteChannelConfig);
$("briefDeliveries").addEventListener("click", (event) => {
  const button = event.target.closest("[data-retry-delivery]");
  if (button) retryBriefDelivery(Number(button.dataset.retryDelivery));
});
$("feedbackBtn").addEventListener("click", () => $("feedbackDialog").showModal());
$("closeFeedbackDialog").addEventListener("click", () => $("feedbackDialog").close());
$("feedbackForm").addEventListener("submit", submitFeedback);
$("cancelSubscriptionBtn").addEventListener("click", cancelCurrentSubscription);
$("planDialog").addEventListener("click", (event) => {
  if (event.target === $("planDialog")) $("planDialog").close();
});

$("symbolForm").addEventListener("submit", (event) => {
  event.preventDefault();
  hideSymbolSuggestions();
  analyze($("symbolInput").value);
});

document.querySelectorAll("[data-symbol]").forEach((button) => {
  button.addEventListener("click", () => {
    $("symbolInput").value = button.dataset.symbol;
    hideSymbolSuggestions();
    analyze(button.dataset.symbol);
  });
});

$("generateReport").addEventListener("click", generateReport);
$("refreshReports").addEventListener("click", loadReports);
$("addWatch").addEventListener("click", () => {
  hideSymbolSuggestions();
  addWatchSymbol();
});
$("searchSymbol").addEventListener("click", searchSymbols);
$("toggleRecentCodes").addEventListener("click", () => {
  recentCodesExpanded = !recentCodesExpanded;
  renderRecentCodes();
});
$("mobileListsToggle").addEventListener("click", () => {
  const sidebar = document.querySelector(".sidebar");
  const opened = sidebar.classList.toggle("mobileListsOpen");
  $("mobileListsToggle").textContent = opened
    ? "收起最近记录与关注列表"
    : "展开最近记录与关注列表";
});
$("scanWatch").addEventListener("click", scanWatchlist);
$("scanWatchTop").addEventListener("click", scanWatchlist);
$("scanSort").addEventListener("change", renderScanResults);
$("symbolSuggestions").addEventListener("click", (event) => {
  if (event.target.closest("[data-close-suggestions]")) {
    hideSymbolSuggestions();
    return;
  }
  const symbol = event.target.closest("[data-suggestion-symbol]")?.dataset.suggestionSymbol;
  if (!symbol) return;
  $("symbolInput").value = symbol;
  hideSymbolSuggestions();
  analyze(symbol);
});
$("dataHealth").addEventListener("click", (event) => {
  const symbol = event.target.closest("[data-suggest-symbol]")?.dataset.suggestSymbol;
  if (!symbol) return;
  $("symbolInput").value = symbol;
  hideSymbolSuggestions();
  analyze(symbol);
});
$("recentCodeList").addEventListener("click", (event) => {
  const deleteSymbol = event.target.closest("[data-delete-recent]")?.dataset.deleteRecent;
  if (deleteSymbol) {
    if (window.confirm(t("confirm.deleteRecent", { symbol: deleteSymbol }))) removeRecentCode(deleteSymbol);
    return;
  }
  const symbol = event.target.closest("[data-recent-symbol]")?.dataset.recentSymbol;
  if (!symbol) return;
  $("symbolInput").value = symbol;
  hideSymbolSuggestions();
  analyze(symbol);
});
$("historyList").addEventListener("click", (event) => {
  const deleteId = Number(event.target.closest("[data-delete-report]")?.dataset.deleteReport);
  if (deleteId) {
    deleteHistoricalReport(deleteId);
    return;
  }
  const toggleId = Number(event.target.closest("[data-toggle-report]")?.dataset.toggleReport);
  if (!toggleId) return;
  if (expandedReportIds.has(toggleId)) expandedReportIds.delete(toggleId);
  else expandedReportIds.add(toggleId);
  loadReports();
});
$("watchList").addEventListener("click", (event) => {
  const watchSymbol = event.target.closest("[data-watch-symbol]")?.dataset.watchSymbol;
  const removeSymbol = event.target.closest("[data-remove-symbol]")?.dataset.removeSymbol;
  if (watchSymbol) {
    $("symbolInput").value = watchSymbol;
    hideSymbolSuggestions();
    analyze(watchSymbol);
  }
  if (removeSymbol) removeWatchSymbol(removeSymbol);
});
$("scanResults").addEventListener("click", (event) => {
  const symbol = event.target.closest("[data-scan-symbol]")?.dataset.scanSymbol;
  if (!symbol) return;
  $("symbolInput").value = symbol;
  hideSymbolSuggestions();
  analyze(symbol);
});
document.addEventListener("click", (event) => {
  if ($("symbolSuggestions").hidden) return;
  if (event.target.closest("#symbolSuggestions") || event.target.closest("#searchSymbol")) return;
  hideSymbolSuggestions();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") hideSymbolSuggestions();
});
window.addEventListener("resize", () => {
  priceChart?.resize();
  indicatorChart?.resize();
});

function applyIdleShellCopy() {
  $("assetType").textContent = t("topbar.waiting");
  $("title").textContent = t("topbar.start");
  $("priceChartTitle").textContent = t("chart.priceTitle");
  $("indicatorChartTitle").textContent = t("chart.indicatorTitle");
  $("indicatorChartMeta").textContent = t("chart.indicatorMeta");
  $("signalDetail").className = "signalDetail emptySignal";
  $("signalDetail").textContent = t("signal.wait");
  $("timeframeSignals").innerHTML = `<div class="emptyNews">${escapeHtml(t("timeframe.empty"))}</div>`;
  $("newsCount").textContent = t("news.waiting");
  $("newsList").innerHTML = `<div class="emptyNews">${escapeHtml(t("news.empty"))}</div>`;
  if (!latestScanResults.length) {
    $("scanSummary").textContent = t("scanner.empty");
    $("scanResults").innerHTML = "";
  }
  if (reportBodyMode === "empty" || !lastRenderedReport) {
    if (reportBodyMode === "readyWithNews") $("reportBody").textContent = t("report.readyWithNews");
    else if (reportBodyMode === "readyNoNews") $("reportBody").textContent = t("report.readyNoNews");
    else if (reportBodyMode === "analyzing") $("reportBody").textContent = t("report.analyzing");
    else if (reportBodyMode === "generating") $("reportBody").textContent = t("report.generating");
    else if (reportBodyMode !== "error" && reportBodyMode !== "report") {
      $("reportBody").className = "reportBody empty";
      $("reportBody").textContent = t("report.empty");
    }
  }
}

window.rerenderI18n = function rerenderI18n() {
  if (!sendCodeCooldownTimer) {
    $("sendCodeBtn").textContent = t("login.sendCode");
  }
  if (!currentUser) return;
  setStatus(t(lastStatusKey, lastStatusVars));
  renderPlanState();
  if (paymentView.key) {
    setPaymentState(paymentView.key, paymentView.vars, paymentView.tone);
  }
  loadAiStatus();
  renderRecentCodes();
  renderWatchList();
  loadReports();
  if (currentAnalysis) {
    renderAnalysis(currentAnalysis);
    if (lastRenderedReport) renderReport(lastRenderedReport);
    else if (reportBodyMode === "readyWithNews" || reportBodyMode === "readyNoNews") {
      $("reportBody").className = "reportBody empty";
      $("reportBody").textContent =
        reportBodyMode === "readyWithNews" ? t("report.readyWithNews") : t("report.readyNoNews");
    }
  } else {
    applyIdleShellCopy();
  }
  if (latestScanResults.length) {
    renderScanResults();
    renderSignalChanges();
  } else {
    renderSignalChanges();
    if (!currentAnalysis) $("scanSummary").textContent = t("scanner.empty");
  }
};

(async function init() {
  applyIdleShellCopy();
  setStatusKey("topbar.statusIdle");
  const loggedIn = await refreshSession();
  if (loggedIn) await bootstrapApp();
})();
