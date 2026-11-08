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

function uuid() {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  const hex = [...bytes].map((v) => v.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function selected() { return accounts.find((a) => a.id === el("account-select").value); }
function error(message = "") { el("error").hidden = !message; el("error").textContent = message; }
function notice(message = "") { el("notice").hidden = !message; el("notice").textContent = message; }
async function call(method, route, data) {
  const result = await bridge[method === "GET" ? "apiGet" : "apiPost"](`native/${route}`, data || {});
  if (!result || result.ok !== true) throw new Error(result?.reason || "请求失败，请刷新页面后重试");
  return result;
}
function stopPolling() { clearTimeout(pollTimer); pollTimer = null; currentOperation = null; sequence += 1; }

function render() {
  const account = selected();
  const loggedIn = account?.authenticated === true;
  const loggingIn = currentOperation && currentOperation.account === account?.id;
  el("account-select").hidden = el("account-label").hidden = !accounts.length;
  el("account-details").hidden = !account;
  el("empty-state").hidden = accounts.length > 0;
  el("account-state").textContent = loggedIn ? (account.connection === "online" ? "已登录 · 在线" : "已登录 · 正在连接") : "未登录";
  el("platform-state").textContent = account?.platform_id || "尚未接入";
  el("login-success").hidden = !loggedIn || !!loggingIn;
  el("qr-placeholder").hidden = loggedIn || !!loggingIn;
  el("qr-content").hidden = !loggingIn;
  el("login").hidden = loggedIn || !!loggingIn;
  el("login").disabled = busy || !ready || !account;
  el("cancel-login").hidden = !loggingIn;
  el("cancel-login").disabled = busy;
  el("connect").disabled = busy || !ready || !loggedIn || !!loggingIn;
  el("connect").textContent = account?.platform_id ? "启动消息平台" : "一键接入 AstrBot";
  el("logout").hidden = !loggedIn || !!loggingIn;
  el("logout").disabled = busy;
  el("add-account").disabled = busy || !ready;
  el("create-account").disabled = busy;
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
  const previous = preferred || el("account-select").value;
  accounts = result.accounts || [];
  el("account-select").replaceChildren();
  for (const account of accounts) {
    const option = document.createElement("option");
    option.value = account.id;
    option.textContent = account.alias || account.id;
    el("account-select").append(option);
  }
  if (accounts.some((a) => a.id === previous)) el("account-select").value = previous;
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
  if (window.innerWidth <= 650) el("login-heading").scrollIntoView({behavior: "smooth", block: "start"});
  await pollLogin(account, operationId, sequence);
}

async function login(account) {
  const result = await call("POST", "login", {account_id: account});
  await trackLogin(account, result.operation_id);
}

el("refresh").addEventListener("click", () => action(() => loadAccounts()));
el("start-engine").addEventListener("click", () => action(async () => { await call("POST", "start"); await loadAccounts(); }));
el("account-select").addEventListener("change", () => { stopPolling(); error(); notice(); render(); resumeSelectedLogin(); });
el("add-account").addEventListener("click", () => { createRequest = uuid(); el("add-form").hidden = false; el("account-alias").focus(); });
el("cancel-add").addEventListener("click", () => { el("add-form").hidden = true; createRequest = null; });
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
  const result = await call("POST", "platform", {account_id: selected().id});
  el("connect-result").textContent = `已接入 ${result.platform_id}，可在“机器人”页面选择 AstrBot 配置文件。`;
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
