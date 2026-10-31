/* 微信控制台 Web 界面：只与同源后端通信（后端另有只允许本机访问的中间件）。 */
const $ = (id) => document.getElementById(id);

const STATE_TEXT = {
  pending: "排队中", sending: "发送中", sent: "已回复", failed: "失败",
  uncertain: "结果待核实", expired: "已过期", merged: "已合并",
};

function fmtTime(value) {
  if (!value) return "-";
  const t = new Date(value * 1000);
  return `${String(t.getHours()).padStart(2, "0")}:${String(t.getMinutes()).padStart(2, "0")}:${String(t.getSeconds()).padStart(2, "0")}`;
}

async function api(path, options = {}) {
  try {
    const response = await fetch(`/api/${path}`, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      return { success: false, message: data.detail || data.message || `HTTP ${response.status}` };
    }
    return data;
  } catch (error) {
    return { success: false, message: String(error) };
  }
}

function notice(text, ok = true) {
  const box = $("notice");
  box.textContent = text || "";
  box.style.color = ok ? "var(--ok)" : "var(--danger)";
}

function renderStatus(data) {
  if (!data || data.mode === undefined) {
    $("s-running").textContent = "后台未连接";
    return;
  }
  $("s-running").textContent = data.running ? "运行中" : "已停止";
  $("s-running").className = `value ${data.running ? "ok" : ""}`;

  const conn = data.connection || {};
  const online = !!conn.online;
  $("s-wechat").textContent = online ? "已连接" : "未连接";
  $("s-wechat").className = `value ${online ? "ok" : "bad"}`;
  let detail = `${conn.backend || "-"} · 收 ${conn.received || 0} / 发 ${conn.sent || 0}（失败 ${conn.send_failed || 0}）`;
  if (conn.admin === false) detail += " · 无管理员权限";
  $("s-account").textContent = `${conn.account || conn.self_id || "-"} ｜ ${detail}`;
  $("s-model").textContent = `${data.model || "-"} ｜ ${data.ai_test || "-"}`;
  $("s-plugins").textContent = String(data.plugins ?? "-");

  const rows = data.messages || [];
  $("messages").innerHTML = rows.length ? rows.map((m) => `
    <tr>
      <td>${fmtTime(m.time)}</td>
      <td>${escapeHtml(m.room || "-")}</td>
      <td>${escapeHtml(m.sender || "-")}</td>
      <td>${escapeHtml((m.text || "").slice(0, 60))}</td>
      <td>${escapeHtml((m.reply || "").slice(0, 60))}${m.error ? `<br><span class="bad">${escapeHtml(m.error)}</span>` : ""}</td>
      <td>${STATE_TEXT[m.state] || escapeHtml(m.state || "-")}</td>
    </tr>`).join("") : '<tr><td colspan="6">还没有消息</td></tr>';

  const events = (data.events || []).slice().reverse();
  $("events").textContent = events.length
    ? events.map((e) => `[${fmtTime(e.time)}] ${e.text}`).join("\n")
    : "暂无记录";
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function refresh() {
  const data = await api("status");
  renderStatus(data);
}

async function loadSettings() {
  const data = await api("settings");
  if (data.success === false) return;
  $("backend").value = data.backend || "auto";
  $("db_dir").value = data.db_dir || "";
  $("groups").value = (data.groups || []).join("\n");
  $("listen_private").checked = data.listen_private !== false;
  $("api_url").value = data.api_url || "";
  $("model").value = data.model || "";
  $("api_style").value = data.api_style || "auto";
  $("api_key").placeholder = data.api_key_set ? "已保存（留空保留）" : "API Key";
}

async function collectSettings() {
  return {
    backend: $("backend").value,
    db_dir: $("db_dir").value.trim(),
    groups: $("groups").value.split("\n").map((v) => v.trim()).filter(Boolean),
    listen_private: $("listen_private").checked,
    api_url: $("api_url").value.trim(),
    api_key: $("api_key").value,
    model: $("model").value.trim(),
    api_style: $("api_style").value,
  };
}

function bind() {
  $("btn-start").onclick = async () => {
    const result = await api("start", { method: "POST", body: "{}" });
    notice(result.message, result.success !== false);
    refresh();
  };
  $("btn-stop").onclick = async () => {
    const result = await api("stop", { method: "POST", body: "{}" });
    notice(result.message, result.success !== false);
    refresh();
  };
  $("btn-test-ai").onclick = async () => {
    const result = await api("test-ai", { method: "POST", body: "{}" });
    notice(result.message, result.success);
  };
  $("btn-test-wechat").onclick = async () => {
    const result = await api("test-wechat", { method: "POST", body: "{}" });
    notice(result.message, result.success);
  };
  $("btn-save").onclick = async () => {
    const result = await api("settings", { method: "POST", body: JSON.stringify(await collectSettings()) });
    notice(result.message, result.success !== false);
    $("api_key").value = "";
    loadSettings();
  };
  $("btn-groups").onclick = async () => {
    const data = await api("wechat/groups");
    if (data.success === false) {
      notice(data.message || "读取失败", false);
      return;
    }
    const existing = new Set($("groups").value.split("\n").map((v) => v.trim()).filter(Boolean));
    (data.groups || []).forEach((g) => { if (g.name) existing.add(g.name); });
    $("groups").value = Array.from(existing).join("\n");
    notice(`已读取 ${(data.groups || []).length} 个群，记得点「保存配置」`, true);
  };
}

bind();
loadSettings();
refresh();
setInterval(refresh, 3000);
