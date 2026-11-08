const bridge = window.AstrBotPluginView;
const status = document.getElementById("connection-status");
const online = document.getElementById("wechat-status");
const reason = document.getElementById("failure-reason");
const button = document.getElementById("test-connection");
const select = document.getElementById("robot-select");
let checking = false;
let botsLoaded = false;

function render(state, wechatOnline = null, failureReason = "") {
  status.dataset.state = state;
  status.textContent = {
    unchecked: "未检测", checking: "检测中",
    connected: "已连接", failed: "连接失败",
  }[state];
  online.textContent = wechatOnline === true ? "在线" : wechatOnline === false ? "离线" : "未检测";
  reason.hidden = !failureReason;
  reason.textContent = failureReason;
}

async function checkConnection() {
  if (checking) return;
  checking = true;
  button.disabled = select.disabled = true;
  render("checking");
  try {
    if (!botsLoaded) {
      const result = await bridge.apiGet("connection/bots");
      select.replaceChildren();
      for (const bot of result.bots) {
        const option = document.createElement("option");
        option.value = bot.id;
        option.textContent = bot.id;
        select.append(option);
      }
      document.getElementById("robot-picker").hidden = result.bots.length < 2;
      botsLoaded = result.bots.length > 0;
    }
    if (!select.value) {
      render("failed", null, "请先在机器人配置中保存 微信消息适配器 机器人");
      return;
    }
    const result = await bridge.apiGet("connection/check", { bot_id: select.value });
    if (result.state === "connected") {
      render("connected", result.wechat_online, result.reason || "");
    } else {
      render("failed", result.wechat_online, result.reason || "Native 连接检测失败");
    }
  } catch {
    render("failed", null, "检测请求失败，请确认 AstrBot 登录有效后重试");
  } finally {
    checking = false;
    button.disabled = select.disabled = false;
  }
}

button.addEventListener("click", checkConnection);
select.addEventListener("change", checkConnection);
try {
  await bridge.ready();
  await checkConnection();
} catch {
  render("failed", null, "检测请求失败，请确认 AstrBot 登录有效后重试");
  button.disabled = false;
}
