var BOT_CHANNEL_META = {
    ilink: { icon: "iL", color: "#07c160", name: "iLink", desc: "微信 iLink 机器人" },
    wecom: { icon: "W", color: "#07c160", name: "企业微信", desc: "企业微信自建应用" },
    feishu: { icon: "F", color: "#3370ff", name: "飞书", desc: "飞书/Lark 机器人" },
};

function initBots() {
    var listEl = document.getElementById("bot-list");
    var emptyEl = document.getElementById("bot-empty");
    if (!listEl) return;

    fetch("/api/bots")
        .then(function (r) { return r.json(); })
        .then(function (bots) {
            if (!bots.length) {
                emptyEl.style.display = "";
                listEl.style.display = "none";
                return;
            }
            emptyEl.style.display = "none";
            listEl.style.display = "";
            listEl.innerHTML = bots.map(function (b) {
                var meta = BOT_CHANNEL_META[b.channel] || { icon: "?", color: "#6b7280", name: b.channel, desc: "" };
                var statusBadge = b.connected
                    ? '<span class="badge badge-success">已连接</span>'
                    : '<span class="badge badge-muted">未连接</span>';
                return '<div class="plugin-tile">' +
                    '<div class="plugin-tile-header">' +
                        '<div class="plugin-avatar" style="background:' + meta.color + '">' + meta.icon + "</div>" +
                        '<div class="plugin-tile-info">' +
                            '<div class="plugin-tile-name">' + escapeHtml(meta.name) + "</div>" +
                            '<div class="plugin-tile-meta">' + statusBadge +
                            '<span class="text-muted">' + escapeHtml(b.id) + "</span></div>" +
                        "</div>" +
                    "</div>" +
                    '<p class="plugin-tile-desc">' + escapeHtml(meta.desc) + "</p>" +
                    (b.bot_id ? '<div class="plugin-tile-tags"><span class="tag">bot_id: ' + escapeHtml(b.bot_id) + "</span></div>" : "") +
                "</div>";
            }).join("");

            var upcoming = [
                { channel: "wecom", name: "企业微信", desc: "企业微信自建应用机器人" },
                { channel: "feishu", name: "飞书", desc: "飞书/Lark 机器人" },
            ];
            var existing = bots.map(function (b) { return b.channel; });
            upcoming.forEach(function (u) {
                if (existing.indexOf(u.channel) === -1) {
                    listEl.innerHTML += '<div class="plugin-tile" style="opacity:0.5">' +
                        '<div class="plugin-tile-header">' +
                            '<div class="plugin-avatar" style="background:#d1d5db">' + u.name.charAt(0) + "</div>" +
                            '<div class="plugin-tile-info">' +
                                '<div class="plugin-tile-name">' + escapeHtml(u.name) + "</div>" +
                                '<div class="plugin-tile-meta"><span class="badge badge-muted">即将上线</span></div>' +
                            "</div>" +
                        "</div>" +
                        '<p class="plugin-tile-desc">' + escapeHtml(u.desc) + "</p>" +
                    "</div>";
                }
            });
        });
}

