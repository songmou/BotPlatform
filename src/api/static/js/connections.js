/* ===== My connections page ===== */
function initConnections() {
    "use strict";

    window.addEventListener("unhandledrejection", function (event) {
        console.error("connections unhandled rejection:", event.reason, event.reason && event.reason.stack);
    });
    window.onerror = function (msg, url, line, col, error) {
        console.error("connections window.onerror:", msg, url, line, col, error && error.stack);
    };

    var listEl = document.getElementById("connections-list");
    var modal = document.getElementById("connection-modal");
    var form = document.getElementById("connection-form");
    var platformSelect = document.getElementById("connection-platform");
    var orgSelect = document.getElementById("connection-organization");
    var agentSelect = document.getElementById("connection-agent");
    var formPanel = document.getElementById("connection-form-panel");
    var bindPanel = document.getElementById("connection-bind-panel");
    var bindWechat = document.getElementById("connection-bind-wechat");
    var bindWecom = document.getElementById("connection-bind-wecom");
    var bindFeishu = document.getElementById("connection-bind-feishu");
    var qrArea = document.getElementById("connection-wechat-qr-area");
    var stepNext = document.getElementById("connection-step-next");
    var stepBack = document.getElementById("connection-step-back");
    var bindSave = document.getElementById("connection-bind-save");
    if (!listEl) return;

    var items = [];
    var pollTimer = null;
    var pollingId = null;
    var creatingId = null;

    function api(url, options) {
        return fetch(url, options).then(function (response) {
            return response.json().catch(function () { return {}; }).then(function (body) {
                if (!response.ok) throw new Error(body.detail || "请求失败");
                return body;
            });
        });
    }

    function platformName(platform) {
        return { wechat: "微信", wecom: "企业微信" }[platform] || platform;
    }

    function stopPoll() {
        if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
        pollingId = null;
    }

    function statePill(item) {
        if (!item.enabled) return '<span class="organization-pill">已暂停</span>';
        var state = item.state || "";
        if (state === "running") return '<span class="organization-pill">已连接</span>';
        if (state === "authentication_required") return '<span class="organization-pill">待配置凭据</span>';
        if (state === "failed") return '<span class="organization-pill">连接失败</span>';
        return '<span class="organization-pill">' + escapeHtml(state || "等待启动") + "</span>";
    }

    function renderWechatStatusHtml(status) {
        if (status.state === "pending" || status.state === "scanned") {
            var hint = status.state === "scanned"
                ? "已扫码，请在手机上确认…"
                : "打开微信，扫描二维码连接";
            return (status.qr
                ? '<img class="wechat-qr" src="' + status.qr + '" alt="微信登录二维码">'
                : '<div class="wecom-qr-placeholder">正在生成二维码…</div>') +
                '<p class="wechat-connect-hint">' + hint + "</p>";
        }
        if (status.connected) {
            return '<div class="wechat-connect-status">' +
                '<span class="badge badge-success">已连接</span>' +
                (status.bot_id ? '<span class="text-muted"> bot_id: ' + escapeHtml(status.bot_id) + "</span>" : "") +
                "</div>" +
                '<p class="wechat-connect-hint">若在别处重新绑定了该微信号导致掉线，可点此重新扫码夺回连接。</p>' +
                '<button type="button" class="btn-secondary" data-action="wechat-login">重新扫码（换号/重连）</button>';
        }
        if (status.state === "failed") {
            return '<div class="wechat-connect-status">' +
                '<span class="badge badge-muted">未连接</span></div>' +
                (status.error ? '<p class="wechat-connect-error">' + escapeHtml(status.error) + "</p>" : "") +
                '<button type="button" class="btn-primary" data-action="wechat-login">刷新二维码</button>';
        }
        return '<div class="wechat-connect-status">' +
            '<span class="badge badge-muted">未登录</span></div>' +
            '<button type="button" class="btn-primary" data-action="wechat-login">扫码登录</button>';
    }

    function cardHtml(item) {
        var actions = "";
        if (item.platform === "wecom") {
            actions += '<button type="button" class="btn-secondary" data-action="wecom-credentials" data-id="' +
                escapeHtml(item.connection_id) + '">更新凭证</button>';
        }
        if (item.platform === "wechat") {
            actions += '<button type="button" class="btn-secondary" data-action="wechat-credentials" data-id="' +
                escapeHtml(item.connection_id) + '">更新凭证</button>';
        }
        actions += '<button type="button" class="btn-secondary" data-action="edit" data-id="' +
            escapeHtml(item.connection_id) + '">编辑</button>';
        actions += '<button type="button" class="btn-secondary" data-action="toggle" data-id="' +
            escapeHtml(item.connection_id) + '" data-enabled="' + (item.enabled ? "1" : "0") + '">' +
            (item.enabled ? "暂停" : "启用") + "</button>";
        actions += '<button type="button" class="btn-danger" data-action="delete" data-id="' +
            escapeHtml(item.connection_id) + '">删除</button>';
        return '<article class="organization-card">' +
            "<h3>" + escapeHtml(platformName(item.platform)) +
            (item.bot_account_id ? "（" + escapeHtml(item.bot_account_id) + "）" : "") + "</h3>" +
            "<p>" + escapeHtml(item.organization_name || item.organization_id) +
            " · 智能体 " + escapeHtml(item.agent_id) + "</p>" +
            '<div class="organization-card-meta">' + statePill(item) +
            (item.detail ? '<span class="text-muted">' + escapeHtml(item.detail) + "</span>" : "") +
            "</div>" +
            '<div class="organization-card-actions">' + actions + "</div></article>";
    }

    function render() {
        var summary = document.getElementById("connections-summary");
        if (summary) summary.textContent = "共 " + items.length + " 项";
        listEl.innerHTML = items.length
            ? items.map(cardHtml).join("")
            : '<div class="organization-empty">还没有连接，点击右上角「新建连接」开始。</div>';
    }

    function loadList() {
        return api("/api/connections").then(function (data) {
            var orgId = activeOrganizationId();
            items = (data.items || []).filter(function (item) {
                return item.organization_id === orgId;
            });
            render();
            if (pollingId && items.some(function (item) { return item.connection_id === pollingId; })) {
                pollWechatStatus(pollingId);
            }
        }).catch(function (error) {
            console.error("loadList error:", error, error && error.stack);
            listEl.innerHTML = '<div class="organization-empty">' + escapeHtml(error.message) + "</div>";
        });
    }

    function pollWechatStatus(connectionId, areaEl, modalMode, onConfirmCb) {
        stopPoll();
        pollingId = connectionId;
        api("/api/connections/" + encodeURIComponent(connectionId) + "/wechat/status").then(function (status) {
            var area = areaEl || listEl.querySelector('[data-role="wechat-area"][data-id="' + connectionId + '"]');
            if (status.connected || status.state === "success") {
                pollingId = null;
                if (modalMode) {
                    if (area) {
                        area.innerHTML = '<div class="wechat-connect-status">' +
                            '<span class="badge badge-success">扫码成功，正在启用机器人…</span></div>';
                    }
                    (onConfirmCb || autoConfirmWechat)(connectionId);
                    return;
                }
                loadList();
                return;
            }
            if (area) area.innerHTML = renderWechatStatusHtml(status);
            if (status.state === "pending" || status.state === "scanned") {
                pollTimer = setTimeout(function () { pollWechatStatus(connectionId, areaEl, modalMode, onConfirmCb); }, 2000);
            }
        }).catch(function () {
            pollingId = null;
        });
    }

    function startWechatLogin(connectionId, areaEl, modalMode, onConfirmCb) {
        api("/api/connections/" + encodeURIComponent(connectionId) + "/wechat/login", { method: "POST" })
            .then(function () { pollWechatStatus(connectionId, areaEl, modalMode, onConfirmCb); })
            .catch(function (error) { showToast("启动扫码失败：" + error.message, "error"); });
    }

    function autoConfirmWechat(connectionId) {
        api("/api/connections/" + encodeURIComponent(connectionId) + "/wechat/confirm", { method: "POST" })
            .then(function () {
                showToast("机器人已启用", "success");
                creatingId = null;
                closeModal();
                loadList();
            })
            .catch(function (error) { showToast("启用失败：" + error.message, "error"); });
    }

    function editConnection(connectionId) {
        var item = items.filter(function (it) { return it.connection_id === connectionId; })[0];
        if (!item) { showToast("连接不存在", "error"); return; }
        api("/api/v2/orgs/" + encodeURIComponent(item.organization_id) + "/agents").then(function (data) {
            var options = (data.items || []).map(function (agent) {
                var payload = agent.payload || {};
                return { value: agent.resource_id, label: payload.name || agent.resource_id };
            });
            if (!options.length) { showToast("该组织暂无可用智能体", "error"); return; }
            showFormDialog({
                title: "更换接待智能体",
                fields: [{
                    name: "agent_id",
                    label: "接待智能体",
                    type: "select",
                    value: item.agent_id,
                    options: options
                }]
            }).then(function (value) {
                if (!value) return null;
                if (value.agent_id === item.agent_id) return null;
                return api("/api/connections/" + encodeURIComponent(connectionId) + "/agent", {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ agent_id: value.agent_id })
                });
            }).then(function (result) {
                if (result) { showToast("接待智能体已更换", "success"); loadList(); }
            }).catch(function (error) { showToast(error.message, "error"); });
        }).catch(function (error) { showToast(error.message, "error"); });
    }

    function updateWecomCredentials(connectionId) {
        var item = items.filter(function (it) { return it.connection_id === connectionId; })[0];
        var currentBotId = (item && item.bot_account_id) || "";
        showFormDialog({
            title: "更新企微凭证",
            fields: [
                { name: "bot_id", label: "Bot ID", value: currentBotId, required: true },
                { name: "secret", label: "Secret（已配置则留空跳过校验）", type: "password" }
            ]
        }).then(function (value) {
            if (!value) return null;
            return api("/api/connections/" + encodeURIComponent(connectionId) + "/wecom/credentials", {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(value)
            });
        }).then(function (result) {
            if (result) { showToast("企业微信凭证已保存", "success"); loadList(); }
        }).catch(function (error) { showToast(error.message, "error"); });
    }

    function updateWechatCredentials(connectionId) {
        var overlay = document.createElement("div");
        overlay.className = "modal-overlay";
        overlay.style.display = "";
        overlay.innerHTML = '<div class="modal"><div class="modal-header"><h3>微信扫码登录</h3><button type="button" class="modal-close-btn" onclick="this.closest(\'.modal-overlay\').remove()">&times;</button></div><div class="modal-body"><div id="qr-update-area" class="wechat-connect"><div class="wecom-qr-placeholder">正在生成二维码…</div></div></div></div>';
        document.body.appendChild(overlay);
        var qrArea = overlay.querySelector("#qr-update-area") || overlay.querySelector(".wechat-connect");
        function confirmAndClose(id) {
            api("/api/connections/" + encodeURIComponent(id) + "/wechat/confirm", { method: "POST" })
                .then(function () {
                    showToast("机器人已启用", "success");
                    overlay.remove();
                    loadList();
                })
                .catch(function (error) { showToast("启用失败：" + error.message, "error"); });
        }
        api("/api/connections/" + encodeURIComponent(connectionId) + "/wechat/login", { method: "POST" })
            .then(function () { pollWechatStatus(connectionId, qrArea, true, confirmAndClose); })
            .catch(function (error) { showToast("启动扫码失败：" + error.message, "error"); });
    }

    listEl.addEventListener("click", function (event) {
        var button = event.target.closest("[data-action]");
        if (!button) return;
        var action = button.getAttribute("data-action");
        var connectionId = button.getAttribute("data-id");
        // Buttons rendered inside the wechat status area (renderWechatStatusHtml)
        // do not carry a data-id; read it from the parent area element.
        if (!connectionId && action === "wechat-login") {
            var area = button.closest('[data-role="wechat-area"]');
            if (area) connectionId = area.getAttribute("data-id");
        }
        if (action === "wechat-login") { startWechatLogin(connectionId); return; }
        if (action === "wecom-credentials") { updateWecomCredentials(connectionId); return; }
        if (action === "wechat-credentials") { updateWechatCredentials(connectionId); return; }
        if (action === "edit") { editConnection(connectionId); return; }
        if (action === "toggle") {
            api("/api/connections/" + encodeURIComponent(connectionId) + "/status", {
                method: "PATCH",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ enabled: button.getAttribute("data-enabled") !== "1" })
            }).then(loadList).catch(function (error) { showToast(error.message, "error"); });
            return;
        }
        if (action === "delete") {
            showConfirm("确定删除该连接？删除后对应的微信/企微接入会立即断开。").then(function (ok) {
                if (!ok) return;
                api("/api/connections/" + encodeURIComponent(connectionId), { method: "DELETE" })
                    .then(function () { showToast("已删除连接", "success"); loadList(); })
                    .catch(function (error) { showToast(error.message, "error"); });
            });
        }
    });

    function showStep(step) {
        document.querySelectorAll("[data-conn-tab]").forEach(function (button) {
            button.classList.toggle("active", button.getAttribute("data-conn-tab") === step);
        });
        formPanel.style.display = step === "form" ? "" : "none";
        bindPanel.style.display = step === "bind" ? "" : "none";
        var platform = platformSelect.value;
        if (step === "bind") {
            bindWechat.style.display = platform === "wechat" ? "" : "none";
            bindWecom.style.display = platform === "wecom" ? "" : "none";
            bindFeishu.style.display = platform === "feishu" ? "" : "none";
        }
        stepNext.style.display = step === "form" ? "" : "none";
        stepBack.style.display = step === "bind" ? "" : "none";
        bindSave.style.display = step === "bind" && platform === "wecom" ? "" : "none";
    }

    function discardUnboundWechat() {
        if (!creatingId) return Promise.resolve();
        var pendingId = creatingId;
        creatingId = null;
        return api("/api/connections").then(function (data) {
            var item = (data.items || []).filter(function (it) {
                return it.connection_id === pendingId;
            })[0];
            // A connection that already has credentials is live; keep it.
            if (item && item.credential_configured) return;
            return api("/api/connections/" + encodeURIComponent(pendingId), { method: "DELETE" })
                .catch(function () {});
        }).catch(function () {});
    }

    function loadAgents(organizationId) {
        if (!organizationId) { agentSelect.innerHTML = ""; return Promise.resolve(); }
        return api("/api/v2/orgs/" + encodeURIComponent(organizationId) + "/agents").then(function (data) {
            agentSelect.innerHTML = (data.items || []).map(function (item) {
                var payload = item.payload || {};
                return '<option value="' + escapeHtml(item.resource_id) + '">' +
                    escapeHtml(payload.name || item.resource_id) + "</option>";
            }).join("");
        }).catch(function () { agentSelect.innerHTML = ""; });
    }

    function openModal() {
        api("/api/connections/options").then(function (data) {
            orgSelect.innerHTML = (data.organizations || []).map(function (item) {
                return '<option value="' + escapeHtml(item.organization_id) + '">' +
                    escapeHtml(item.name) + "</option>";
            }).join("");
            if (!orgSelect.options.length) {
                showToast("你还没有加入任何组织，请先加入组织", "error");
                return;
            }
            orgSelect.value = activeOrganizationId() || orgSelect.options[0].value;
            if (activeOrganizationId()) {
                orgSelect.disabled = true;
                document.getElementById("connection-organization").title = "连接归属于页面当前组织";
            } else {
                orgSelect.disabled = false;
            }
            creatingId = null;
            document.getElementById("connection-bot-id").value = "";
            document.getElementById("connection-secret").value = "";
            qrArea.innerHTML = '<div class="tool-empty">连接已创建，正在生成二维码…</div>';
            modal.style.display = "";
            showStep("form");
            loadAgents(orgSelect.value);
            orgSelect.onchange = function () { loadAgents(orgSelect.value); };
            document.getElementById("connection-modal-cancel").onclick = closeModal;
            document.getElementById("connection-modal-close").onclick = closeModal;
            stepNext.onclick = goNext;
            stepBack.onclick = function () {
                discardUnboundWechat().then(function () { showStep("form"); });
            };
            form.onsubmit = submitWecom;
        }).catch(function (error) { showToast(error.message, "error"); });
    }

    function goNext() {
        if (!orgSelect.value || !agentSelect.value) {
            showToast("请选择归属组织和智能体", "error");
            return;
        }
        var platform = platformSelect.value;
        if (platform === "wechat") {
            createWechatConnection();
            return;
        }
        showStep("bind");
    }

    function createWechatConnection() {
        api("/api/connections", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                platform: "wechat",
                organization_id: orgSelect.value,
                agent_id: agentSelect.value
            })
        }).then(function (created) {
            creatingId = created.connection_id;
            showStep("bind");
            startWechatLogin(creatingId, qrArea, true, autoConfirmWechat);
        }).catch(function (error) { showToast(error.message, "error"); });
    }

    function submitWecom(event) {
        event.preventDefault();
        if (bindSave.disabled) return;
        var botId = document.getElementById("connection-bot-id").value.trim();
        var secret = document.getElementById("connection-secret").value.trim();
        if (!botId || !secret) {
            showToast("请填写 Bot ID 和 Secret", "error");
            return;
        }
        bindSave.disabled = true;
        var originalText = bindSave.textContent;
        bindSave.textContent = "正在保存…";
        api("/api/connections", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                platform: "wecom",
                organization_id: orgSelect.value,
                agent_id: agentSelect.value,
                bot_id: botId,
                secret: secret
            })
        }).then(function () {
            showToast("连接已创建并完成绑定", "success");
            closeModal();
            loadList();
        }).catch(function (error) {
            bindSave.disabled = false;
            bindSave.textContent = originalText;
            showToast(error.message, "error");
        });
    }

    function closeModal() {
        discardUnboundWechat();
        modal.style.display = "none";
        stopPoll();
        form.onsubmit = null;
        orgSelect.onchange = null;
        stepNext.onclick = null;
        stepBack.onclick = null;
        document.getElementById("connection-modal-cancel").onclick = null;
        document.getElementById("connection-modal-close").onclick = null;
    }

    document.getElementById("connections-create-btn").addEventListener("click", openModal);

    (window.BP_CONTEXT_READY || Promise.resolve()).then(function (me) {
        try {
            var organizations = (me && me.organizations) || [];
            var organization = organizations.filter(function (item) {
                return item.organization_id === activeOrganizationId();
            })[0];
            document.getElementById("organization-name").textContent =
                organization ? organization.name : "组织工作台";
            if (activeOrganizationId()) {
                loadList();
            }
        } catch (error) {
            console.error("connections init error:", error, error.stack);
            listEl.innerHTML = '<div class="organization-empty">' + escapeHtml(error.message) + "</div>";
        }
    }).catch(function (error) {
        console.error("connections init promise error:", error, error.stack);
        listEl.innerHTML = '<div class="organization-empty">' + escapeHtml(error.message) + "</div>";
    });
}
