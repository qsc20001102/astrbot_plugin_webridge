const bridge = window.AstrBotPluginView;
const el = (id) => document.getElementById(id);
const terminalStates = new Set(["cancelled", "succeeded", "failed", "expired", "rejected", "needs_login", "needs_review"]);
let accounts = [];
let currentOperation = null;
let pollTimer = null;
let statusTimer = null;
let busy = false;
let ready = false;
let sequence = 0;
let createRequest = null;
let selectedAccountId = null;
let accountListKey = "";
const connectionResults = new Map();

function uuid() {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  const hex = [...bytes].map((v) => v.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function selected() { return accounts.find((a) => a.id === selectedAccountId); }
function error(message = "") { el("error").hidden = !message; el("error").textContent = message; }
function notice(message = "") { el("notice").hidden = !message; el("notice").textContent = message; }
async function call(method, route, data) {
  const result = await bridge[method === "GET" ? "apiGet" : "apiPost"](`native/${route}`, data || {});
  if (!result || result.ok !== true) throw new Error(result?.reason || "请求失败，请刷新页面后重试");
  return result;
}
function stopPolling() { clearTimeout(pollTimer); pollTimer = null; currentOperation = null; sequence += 1; }

function renderAccounts() {
  const key = JSON.stringify(accounts.map((a) => [a.id, a.alias]));
  // Keep existing buttons during status refresh so keyboard focus is preserved.
  if (key !== accountListKey) {
    const focused = document.activeElement?.dataset.accountId;
    el("account-list").replaceChildren();
    for (const account of accounts) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "account-item";
      button.dataset.accountId = account.id;
      button.setAttribute("aria-label", account.alias || account.id);
      const monogram = document.createElement("span");
      monogram.className = "account-monogram";
      monogram.setAttribute("aria-hidden", "true");
      monogram.textContent = [...(account.alias || account.id)][0];
      const text = document.createElement("span");
      text.className = "account-text";
      const alias = document.createElement("span");
      alias.className = "account-alias";
      alias.textContent = account.alias || account.id;
      const status = document.createElement("span");
      status.className = "account-status";
      text.append(alias, status);
      button.append(monogram, text);
      button.addEventListener("click", () => {
        if (busy || selectedAccountId === account.id) return;
        selectedAccountId = account.id;
        stopPolling(); error(); notice(); render(); resumeSelectedLogin();
      });
      el("account-list").append(button);
      if (focused === account.id) button.focus();
    }
    accountListKey = key;
  }
  const byId = new Map(accounts.map((a) => [a.id, a]));
  for (const button of el("account-list").children) {
    const account = byId.get(button.dataset.accountId);
    button.disabled = busy;
    button.setAttribute("aria-pressed", String(account.id === selectedAccountId));
    button.dataset.online = String(account.authenticated && account.connection === "online");
    const status = account.authenticated ? (account.connection === "online" ? "在线" : "正在连接") : account.login_operation_id ? "登录中" : "未登录";
    button.querySelector(".account-status").textContent = status;
    button.setAttribute("aria-description", status);
  }
  el("account-count").textContent = String(accounts.length);
}

function render() {
  const account = selected();
  const loggedIn = account?.authenticated === true;
  const loggingIn = currentOperation && currentOperation.account === account?.id;
  renderAccounts();
  el("account-name").textContent = account?.alias || account?.id || "连接你的微信";
  el("account-details").hidden = !account;
  el("empty-state").hidden = accounts.length > 0;
  el("account-state").textContent = loggedIn ? (account.connection === "online" ? "已登录 · 在线" : "已登录 · 正在连接") : "未登录";
  el("platform-state").textContent = account?.platform_id || "尚未接入";
  el("login-success").hidden = !loggedIn || !!loggingIn;
  el("qr-placeholder").hidden = loggedIn || !!loggingIn;
  el("qr-content").hidden = !loggingIn;
  el("login-status").hidden = !loggingIn;
  el("login-actions").hidden = loggedIn && !loggingIn;
  el("qr-hint").hidden = loggedIn && !loggingIn;
  el("qr-waiting").hidden = !el("qr-image").hidden;
  el("login-description").textContent = loggedIn ? (account.platform_id ? "微信已登录。可在 AstrBot 的“机器人”页面调整唤醒、模型与插件配置。" : "微信已登录。接入 AstrBot 后，即可开始收发消息。")
    : account ? "用手机微信扫描二维码，并在手机上确认登录。刷新页面后可继续查看进度。" : "先添加一个微信账号，再用手机微信扫码完成登录。";
  el("success-caption").textContent = account?.platform_id ? "已配置 AstrBot 接入" : "可以接入 AstrBot";
  el("step-login").dataset.complete = String(loggedIn);
  el("step-connect").dataset.complete = String(!!account?.platform_id);
  el("step-login").setAttribute("aria-current", loggedIn ? "false" : "step");
  el("step-connect").setAttribute("aria-current", loggedIn && !account?.platform_id ? "step" : "false");
  el("step-login-state").textContent = loggedIn ? "已登录" : loggingIn ? "进行中" : "待登录";
  el("step-connect-state").textContent = account?.platform_id ? "已配置" : "待接入";
  el("connect-result").textContent = connectionResults.get(account?.id) || "";
  el("connect-result").hidden = !el("connect-result").textContent;
  el("login").hidden = loggedIn || !!loggingIn;
  el("login").disabled = busy || !ready || !account;
  el("cancel-login").hidden = !loggingIn;
  el("cancel-login").disabled = busy;
  el("connect").disabled = busy || !ready || !loggedIn || !!loggingIn;
  el("connect").textContent = account?.platform_id ? "启动消息平台" : "一键接入 AstrBot";
  el("logout").hidden = !loggedIn || !!loggingIn;
  el("logout").disabled = busy;
  el("add-account").disabled = busy || !ready;
  el("create-account").disabled = busy || !ready;
  el("account-alias").disabled = busy;
  el("cancel-add").disabled = busy;
  el("refresh").disabled = busy;
  el("start-engine").disabled = busy;
}

async function loadAccounts(preferred) {
  const result = await call("GET", "status");
  ready = result.engine_ready === true;
  el("engine-status").textContent = ready ? "引擎已就绪" : "引擎未就绪";
  el("engine-status").dataset.ready = String(ready);
  el("engine-help").hidden = ready;
  el("engine-reason").textContent = result.reason || "请启动内置微信引擎后重试。";
  el("version").textContent = result.engine_version ? `引擎 ${result.engine_version}` : "";
  const previous = preferred || selectedAccountId;
  accounts = result.accounts || [];
  selectedAccountId = accounts.some((a) => a.id === previous) ? previous : accounts[0]?.id || null;
  if (currentOperation && currentOperation.account !== selectedAccountId) stopPolling();
  render();
  resumeSelectedLogin();
}

function resumeSelectedLogin() {
  const account = selected();
  if (account?.login_operation_id && currentOperation?.id !== account.login_operation_id) {
    void trackLogin(account.id, account.login_operation_id);
  }
}

async function action(handler) {
  if (busy) return;
  busy = true; error(); notice(); render();
  try { await handler(); }
  catch (failure) { error(failure.message || "操作失败，请稍后重试"); }
  finally { busy = false; render(); }
}

async function pollLogin(account, operationId, token) {
  if (token !== sequence || selected()?.id !== account) return;
  try {
    const result = await call("GET", "login-status", {account_id: account, operation_id: operationId});
    if (token !== sequence || selected()?.id !== account) return;
    error();
    if (result.terminal === true || terminalStates.has(result.state)) {
      stopPolling();
      await loadAccounts(account);
      if (selected()?.authenticated) notice("微信登录成功，可以一键接入 AstrBot。");
      else error(result.reason || (result.state === "cancelled" ? "登录已取消" : "本次登录未完成，请重新获取二维码。"));
      return;
    }
    el("login-status").textContent = result.state === "authenticating" ? "手机已确认，正在完成登录…"
      : result.state === "resuming" ? "正在恢复微信登录…"
      : result.state === "awaiting_approval" ? "请在手机微信上确认登录"
      : result.qr_available ? "等待手机微信扫码" : "正在获取二维码，请稍候…";
    if (typeof result.qr === "string" && result.qr.startsWith("data:image/png;base64,")) {
      el("qr-image").src = result.qr;
      el("qr-image").hidden = false;
    } else { el("qr-image").hidden = true; }
    el("qr-waiting").hidden = !el("qr-image").hidden;
    pollTimer = setTimeout(() => pollLogin(account, operationId, token), 2000);
  } catch (failure) {
    if (token !== sequence) return;
    error(failure.message || "登录状态查询失败，正在重试");
    pollTimer = setTimeout(() => pollLogin(account, operationId, token), 4000);
  }
}

async function trackLogin(account, operationId) {
  stopPolling();
  el("qr-image").removeAttribute("src");
  el("qr-image").hidden = true;
  currentOperation = {account, id: operationId};
  el("login-status").textContent = "正在获取二维码，请稍候…";
  render();
  if (window.innerWidth <= 760) el("step-login").scrollIntoView({behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth", block: "start"});
  await pollLogin(account, operationId, sequence);
}

async function login(account) {
  const result = await call("POST", "login", {account_id: account});
  await trackLogin(account, result.operation_id);
}

el("refresh").addEventListener("click", () => action(() => loadAccounts()));
el("start-engine").addEventListener("click", () => action(async () => { await call("POST", "start"); await loadAccounts(); }));
el("add-account").addEventListener("click", () => { createRequest ||= uuid(); el("add-form").hidden = false; el("account-alias").focus(); });
function closeAddForm() { el("add-form").hidden = true; createRequest = null; el("add-account").focus(); }
el("cancel-add").addEventListener("click", closeAddForm);
el("add-form").addEventListener("keydown", (event) => { if (event.key === "Escape" && !busy) { event.preventDefault(); closeAddForm(); } });
el("add-form").addEventListener("submit", (event) => {
  event.preventDefault();
  action(async () => {
    createRequest ||= uuid();
    const result = await call("POST", "accounts", {alias: el("account-alias").value.trim(), request_id: createRequest});
    el("add-form").hidden = true;
    createRequest = null;
    await loadAccounts(result.account_id);
    await login(result.account_id);
  });
});
el("login").addEventListener("click", () => action(() => login(selected().id)));
el("cancel-login").addEventListener("click", () => action(async () => {
  await call("POST", "cancel-login", {account_id: selected().id});
  stopPolling(); await loadAccounts(); notice("已取消扫码登录。");
}));
el("connect").addEventListener("click", () => action(async () => {
  const accountId = selected().id;
  const result = await call("POST", "platform", {account_id: accountId});
  connectionResults.set(accountId, `已接入 ${result.platform_id}，可在“机器人”页面选择 AstrBot 配置文件。`);
  await loadAccounts(); notice("消息平台已启动。");
}));
el("logout").addEventListener("click", () => {
  const account = selected();
  if (!window.confirm(`确定退出“${account.alias || "当前账号"}”的微信登录？登录数据和聊天记录会保留。`)) return;
  const requestId = uuid();
  action(async () => {
    await call("POST", "logout", {account_id: account.id, confirm: account.id, request_id: requestId});
    await loadAccounts(); notice("已提交退出登录，请等待账号状态更新。");
  });
});

async function refreshLoop() {
  try { if (!busy && !currentOperation) await loadAccounts(); }
  catch { /* Explicit actions surface failures; preserve existing state during background refresh. */ }
  statusTimer = setTimeout(refreshLoop, 5000);
}
window.addEventListener("pagehide", () => { stopPolling(); clearTimeout(statusTimer); });
try { await bridge.ready(); await loadAccounts(); statusTimer = setTimeout(refreshLoop, 5000); }
catch { error("无法连接 AstrBot，请确认登录状态后重新打开微信消息适配器界面。"); }
