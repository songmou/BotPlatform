var CHANNEL_META = {
    wechat_ilink: { icon: "微", color: "#07c160", desc: "微信 iLink 私聊机器人" },
    wecom_aibot: { icon: "企", color: "#2d7ff9", desc: "企业微信智能机器人 WebSocket 长连接" },
    feishu: { icon: "飞", color: "#3370ff", desc: "飞书/Lark 机器人 WebSocket 长连接" }
};

var CHANNEL_TYPE_ORDER = ["wechat_ilink", "wecom_aibot", "feishu"];

var CHANNEL_STATE_LABELS = {
    connected: "已连接",
    running: "运行中",
    connecting: "连接中",
    failed: "连接失败",
    authentication_required: "需重新登录",
    missing_credentials: "缺少凭据",
    disabled: "已禁用",
    stopped: "已停止",
    restart_required: "待重启",
    unknown: "状态未知"
};

var channelPageData = { channels: [], providers: [], agents: [], restart_required: false };
var channelRefreshTimer = null;

function channelRequest(url, options) {
    return fetch(url, options || {}).then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (data) {
            if (!response.ok) throw new Error(data.detail || "请求失败");
            return data;
        });
    });
}

function stateBadge(channel) {
    var success = channel.state === "connected" || channel.state === "running";
    var warning = channel.state === "connecting" || channel.state === "restart_required" ||
        channel.state === "unknown";
    var cls = success ? "badge-success" : warning ? "badge-warning" : "badge-muted";
    return '<span class="badge ' + cls + '">' +
        escapeHtml(CHANNEL_STATE_LABELS[channel.state] || channel.state || "未知") + "</span>";
}

function providerName(type) {
    var provider = channelPageData.providers.find(function (item) { return item.type === type; });
    return provider ? provider.name : type;
}

function channelTileHtml(channel) {
    var meta = CHANNEL_META[channel.type] || { icon: "?", color: "#6b7280", desc: "" };
    var credential = channel.credential_configured ? "已配置" : "未配置";
    var policy = channel.settings.group_policy === "mention_only" ? "群聊 @ 响应" : "仅私聊";
    return '<div class="plugin-tile" data-channel-id="' + escapeHtml(channel.id) + '">' +
        '<div class="plugin-tile-header"><div class="plugin-avatar" style="background:' +
        meta.color + '">' + meta.icon + '</div><div class="plugin-tile-info">' +
        '<div class="plugin-tile-name">' + escapeHtml(channel.id) + '</div>' +
        '<div class="plugin-tile-meta">' + stateBadge(channel) +
        '<span class="text-muted">' + escapeHtml(channel.name) + '</span></div></div></div>' +
        '<div class="channel-detail"><div class="channel-detail-row"><span>智能体</span><span>' +
        escapeHtml(channel.agent_id) + '</span></div><div class="channel-detail-row"><span>消息范围</span><span>' +
        escapeHtml(policy) + '</span></div><div class="channel-detail-row"><span>平台凭据</span><span>' +
        escapeHtml(credential) + '</span></div></div>' +
        (channel.detail ? '<p class="text-muted">' + escapeHtml(channel.detail) + '</p>' : '') +
        '<div class="channel-tile-actions"><button class="btn-secondary" data-action="test">检查</button></div></div>';
}

function renderChannels() {
    var container = document.getElementById("channel-sections");
    var empty = document.getElementById("bot-empty");
    empty.style.display = channelPageData.channels.length ? "none" : "";
    container.style.display = channelPageData.channels.length ? "" : "none";
    document.getElementById("channel-restart-notice").style.display =
        channelPageData.restart_required ? "" : "none";

    var byType = {};
    channelPageData.channels.forEach(function (channel) {
        (byType[channel.type] = byType[channel.type] || []).push(channel);
    });
    var types = CHANNEL_TYPE_ORDER.slice();
    (channelPageData.providers || []).forEach(function (provider) {
        if (types.indexOf(provider.type) === -1) types.push(provider.type);
    });

    container.innerHTML = types.map(function (type) {
        var instances = byType[type] || [];
        var meta = CHANNEL_META[type] || { desc: "" };
        var body = instances.length
            ? '<div class="plugin-card-grid">' + instances.map(channelTileHtml).join("") + "</div>"
            : '<p class="text-muted channel-section-empty">暂无实例，请在智能体配置中添加</p>';
        return '<section class="channel-section"><div class="channel-section-head">' +
            '<h3>' + escapeHtml(providerName(type)) + '</h3>' +
            '<span class="text-muted">' + escapeHtml(meta.desc || "") + '</span></div>' +
            body + "</section>";
    }).join("");
}

function loadChannels() {
    return channelRequest("/api/channels").then(function (data) {
        channelPageData = data;
        renderChannels();
    }).catch(function (error) {
        showToast(error.message, "error");
    });
}

function startAutoRefresh() {
    stopAutoRefresh();
    channelRefreshTimer = window.setInterval(function () {
        if (!document.hidden) loadChannels();
    }, 15000);
}

function stopAutoRefresh() {
    if (channelRefreshTimer !== null) {
        window.clearInterval(channelRefreshTimer);
        channelRefreshTimer = null;
    }
}

function initBots() {
    var container = document.getElementById("channel-sections");
    if (!container) return;
    document.getElementById("channel-refresh-btn").addEventListener("click", loadChannels);
    container.addEventListener("click", function (event) {
        var tile = event.target.closest(".plugin-tile");
        var action = event.target.dataset.action;
        if (!tile || action !== "test") return;
        var channel = channelPageData.channels.find(function (item) {
            return item.id === tile.dataset.channelId;
        });
        if (!channel) return;
        channelRequest("/api/channels/" + encodeURIComponent(channel.id) + "/test", {
            method: "POST"
        }).then(function (data) {
            showToast(data.detail, data.ok ? "success" : "error");
        }).catch(function (error) {
            showToast(error.message, "error");
        });
    });
    document.addEventListener("visibilitychange", function () {
        if (!document.hidden) loadChannels();
    });
    loadChannels();
    startAutoRefresh();
}
