function initOrganizationModule(requestedModule) {
    "use strict";

    var page = document.getElementById("organization-page");
    if (!page) {
        var host = document.getElementById("scoped-alternate-root");
        if (!host) return;
        host.innerHTML = '<div class="organization-page" id="organization-page">' +
            '<div class="page-header organization-page-header"><div>' +
            '<div class="organization-eyebrow" id="organization-name">组织工作台</div>' +
            '<h2 id="organization-module-title">加载中…</h2>' +
            '<p class="page-desc" id="organization-module-description"></p></div>' +
            '<div class="organization-actions">' +
            '<button id="organization-primary-action" class="btn-primary" type="button" hidden></button>' +
            '<button id="organization-refresh" class="btn-secondary" type="button">刷新</button>' +
            '</div></div><div id="organization-module-notice" class="status-card" hidden></div>' +
            '<div id="organization-module-summary" class="organization-summary"></div>' +
            '<div id="organization-module-list" class="organization-grid" aria-live="polite"></div></div>';
        page = document.getElementById("organization-page");
    }
    var module = requestedModule || page.getAttribute("data-module");
    page.setAttribute("data-module", module);
    var list = document.getElementById("organization-module-list");
    var summary = document.getElementById("organization-module-summary");
    var primary = document.getElementById("organization-primary-action");
    var notice = document.getElementById("organization-module-notice");
    var runsPanel = document.getElementById("organization-runs-panel");
    var scheduleTabs = document.querySelector(".organization-schedule-tabs");
    var state = { data: null, agentOptions: null, scheduleOptions: null };
    // Only the schedules module renders sub tabs; other modules keep tab "schedules".
    var runs = { tab: "schedules", limit: 20, offset: 0, total: 0, primaryAllowed: false };
    var definitions = {
        overview: ["组织概览", "查看当前 URL 指定组织的成员与运行概览。", ""],
        agents: ["智能体", "管理组织自有智能体；平台模板复制后独立维护。", "新建智能体"],
        channels: ["消息渠道", "组织专属渠道实例，凭据独立保存；启停由运行进程即时应用。", "新建渠道"],
        schedules: ["定时任务", "按平台统一时区执行，目标为组织内最近活跃渠道用户。", "新建任务"],
        models: ["模型", "平台发布的模型能力，只读展示；可在智能体中选择。", ""],
        tools: ["工具与 Skill", "平台发布的工具和 Skill，只读展示；可在智能体中授权。", ""],
        plugins: ["插件", "平台发布的插件，只读展示；插件工具可绑定到智能体。", ""],
        scripts: ["运维脚本", "平台发布的脚本只读展示，可用于已确认的组织定时任务。", ""],
        members: ["成员与设置", "管理组织成员、角色与邀请。", "邀请成员"],
        knowledge: ["知识库", "组织成员共享的知识内容。", "添加文本"],
        drive: ["文件库", "组织成员共享的文件空间。", "上传文件"]
    };

    function request(url, options) {
        return fetch(url, options).then(function (response) {
            return response.json().catch(function () { return {}; }).then(function (body) {
                if (!response.ok) throw new Error(body.detail || "请求失败");
                return body;
            });
        });
    }

    function loadAgentOptions() {
        if (state.agentOptions) return Promise.resolve(state.agentOptions);
        return request(organizationApi("/agent-editor-options")).then(function (data) {
            state.agentOptions = data;
            return data;
        });
    }

    function loadScheduleOptions() {
        if (state.scheduleOptions) return Promise.resolve(state.scheduleOptions);
        return request(organizationApi("/schedule-editor-options")).then(function (data) {
            state.scheduleOptions = data;
            return data;
        });
    }

    function agentCheckboxes(container, items, kind, selected) {
        selected = selected || {};
        container.innerHTML = items.length ? items.map(function (item) {
            var value = item.value || item.id || item.name;
            return '<label class="tool-check"><input type="checkbox" data-agent-kind="' + kind +
                '" value="' + escapeHtml(value) + '"' + (selected[value] ? " checked" : "") + '><span class="tool-info"><span class="tool-name">' +
                escapeHtml(item.label || item.name || value) + '</span><span class="tool-desc">' +
                escapeHtml(item.description || "") + '</span></span></label>';
        }).join("") : '<div class="tool-empty">暂无可选项</div>';
    }

    function datasourceDescription(item) {
        var parts = [];
        if (item.engine) parts.push(String(item.engine).toUpperCase());
        if (item.database) parts.push(item.database);
        if (item.table_count) parts.push("授权 " + item.table_count + " 张表");
        else parts.push("未限定表（授权范围为整库）");
        if (item.description) parts.push(item.description);
        return parts.join(" · ");
    }

    function renderAgentDatasources(options, selectedIds) {
        var container = document.getElementById("organization-agent-datasources");
        if (!container) return;
        var selected = {};
        (selectedIds || []).forEach(function (value) { selected[value] = true; });
        var items = (options || []).filter(function (item) { return item && item.id; });
        agentCheckboxes(container, items.map(function (item) {
            return {
                value: item.id,
                label: item.name || item.id,
                description: datasourceDescription(item)
            };
        }), "datasource", selected);
        if (!items.length) {
            container.innerHTML = '<div class="tool-empty">平台暂未开放可用数据源，请联系平台管理员在「系统工具 → 数据库」中新增并启用。</div>';
            return;
        }
        container.insertAdjacentHTML(
            "afterbegin",
            '<div class="tool-hint">绑定后，该智能体在对话中会自动获得只读检索能力' +
            '（db_list_tables / db_describe_table / db_query），并把授权表结构注入系统提示词。' +
            '写操作不会被自动开启。</div>'
        );
        var known = {};
        items.forEach(function (item) { known[item.id] = true; });
        var missing = (selectedIds || []).filter(function (value) { return !known[value]; });
        if (missing.length) {
            container.insertAdjacentHTML(
                "beforeend",
                '<div class="tool-warning">以下已绑定的数据源当前不可用（已停用或已删除），保存后会自动解除绑定：' +
                escapeHtml(missing.join("、")) + "</div>"
            );
        }
    }

    function selectedAgentValues(container) {
        var result = [];
        container.querySelectorAll("input:checked").forEach(function (box) { result.push(box.value); });
        return result;
    }

    function setAgentPanel(target) {
        document.querySelectorAll("[data-org-agent-tab]").forEach(function (button) {
            button.classList.toggle("active", button.getAttribute("data-org-agent-tab") === target);
        });
        document.querySelectorAll("[data-org-agent-panel]").forEach(function (panel) {
            panel.hidden = panel.getAttribute("data-org-agent-panel") !== target;
        });
    }

    function agentDialog(current, creating) {
        current = current || {};
        var modal = document.getElementById("organization-agent-modal");
        var form = document.getElementById("organization-agent-form");
        var options;
        return loadAgentOptions().then(function (data) {
            options = data;
            var payload = current.payload || current;
            var templateSelect = document.getElementById("organization-agent-template");
            var templateGroup = document.getElementById("organization-agent-template-group");
            templateSelect.innerHTML = '<option value="">空白智能体</option>' + (data.templates || []).map(function (item) {
                var p = item.payload || {};
                return '<option value="' + escapeHtml(item.id) + '">' + escapeHtml(p.name || item.id) +
                    '（' + escapeHtml(item.id) + ' · v' + item.revision + '）</option>';
            }).join("");
            templateGroup.hidden = !creating;
            document.getElementById("organization-agent-modal-title").textContent = creating ? "新建智能体" : "编辑智能体";
            document.getElementById("organization-agent-id").value = payload.id || current.resource_id || "";
            document.getElementById("organization-agent-id").disabled = !creating;
            document.getElementById("organization-agent-name").value = payload.name || "";
            document.getElementById("organization-agent-role").value = payload.role || "assistant";
            document.getElementById("organization-agent-description").value = payload.description || "";
            document.getElementById("organization-agent-enabled").checked = payload.enabled !== false;
            document.getElementById("organization-agent-model").innerHTML = '<option value="">跟随默认模型</option>' + (data.models || []).map(function (item) {
                var p = item.payload || {};
                return '<option value="' + escapeHtml(item.resource_id) + '">' + escapeHtml(p.name || item.resource_id) + '</option>';
            }).join("");
            document.getElementById("organization-agent-model").value = payload.model || "";
            document.getElementById("organization-agent-prompt").value = payload.system_prompt || "你是一个有帮助的助手。";
            document.getElementById("organization-agent-greeting").value = payload.greeting || "";
            document.getElementById("organization-agent-hints").value = (payload.greeting_hints || []).join("；");
            document.getElementById("organization-agent-temperature").value = payload.temperature == null ? "" : payload.temperature;
            document.getElementById("organization-agent-max-tokens").value = payload.max_tokens == null ? "" : payload.max_tokens;
            var tools = {}; (payload.tools || []).forEach(function (value) { tools[value] = true; });
            var skills = {}; (payload.skills || []).forEach(function (value) { skills[value] = true; });
            var mcp = {}; (payload.mcp_servers || []).forEach(function (value) { mcp[value] = true; });
            agentCheckboxes(document.getElementById("organization-agent-builtin-tools"), (data.builtin_tools || []).map(function (item) { return { value: item.name, label: item.name, description: item.description }; }), "builtin", tools);
            document.getElementById("organization-agent-plugin-tools").innerHTML = (data.plugins || []).map(function (plugin) {
                var checks = (plugin.tools || []).map(function (tool) {
                    var selected = (payload.plugin_tools || {})[plugin.id] || [];
                    return '<label class="tool-check"><input type="checkbox" data-agent-kind="plugin" data-plugin-id="' + escapeHtml(plugin.id) + '" value="' + escapeHtml(tool.name) + '"' + (selected.indexOf(tool.name) >= 0 ? " checked" : '') + '><span class="tool-info"><span class="tool-name">' + escapeHtml(tool.name) + '</span><span class="tool-desc">' + escapeHtml(tool.description || '') + '</span></span></label>';
                }).join("");
                return '<div class="tool-plugin-group"><div class="tool-plugin-name">' + escapeHtml(plugin.name || plugin.id) + '</div><div class="tool-checkboxes tool-checkboxes-nested">' + checks + '</div></div>';
            }).join("") || '<div class="tool-empty">暂无可选插件工具</div>';
            agentCheckboxes(document.getElementById("organization-agent-skills"), (data.skills || []).map(function (item) { var p = item.payload || {}; return { value: item.resource_id, label: p.name || item.resource_id, description: p.description }; }), "skill", skills);
            agentCheckboxes(document.getElementById("organization-agent-mcp"), (data.mcp || []).map(function (item) { var p = item.payload || {}; return { value: item.resource_id, label: p.name || item.resource_id, description: p.description }; }), "mcp", mcp);
            renderAgentDatasources(data.datasources, payload.datasources || []);
            var knowledge = {}; (current.knowledge_category_ids || []).forEach(function (value) { knowledge[value] = true; });
            var knowledgeRequest = creating ? Promise.resolve({ category_ids: [] }) : request(organizationApi("/agents/" + encodeURIComponent(payload.id || current.resource_id) + "/knowledge-categories"));
            return knowledgeRequest.then(function (bindings) {
                var selectedKnowledge = {}; (bindings.category_ids || []).forEach(function (value) { selectedKnowledge[value] = true; });
                agentCheckboxes(document.getElementById("organization-agent-knowledge"), data.knowledge || [], "knowledge", selectedKnowledge);
                var source = document.getElementById("organization-agent-source");
                source.hidden = creating || !current.base_resource_id;
                source.textContent = current.base_resource_id ? "来源平台模板：" + current.base_resource_id + "（复制后独立维护）" : "";
                setAgentPanel("basic");
                modal.style.display = "";
                return new Promise(function (resolve) {
                    var templateSelect = document.getElementById("organization-agent-template");
                    function applyTemplate() {
                        var selected = (options.templates || []).filter(function (item) { return item.id === templateSelect.value; })[0];
                        if (selected) {
                            var selectedPayload = selected.payload || {};
                            ["name", "role", "description", "enabled", "model", "system_prompt", "greeting", "greeting_hints", "temperature", "max_tokens", "tools", "plugin_tools", "skills", "mcp_servers", "datasources"].forEach(function () {
                                /* The form is refreshed through the same opening path below. */
                            });
                            fillAgentFromPayload(selectedPayload, options);
                        }
                    }
                    function close(value) { modal.style.display = "none"; form.onsubmit = null; templateSelect.onchange = null; document.getElementById("organization-agent-modal-cancel").onclick = null; document.getElementById("organization-agent-modal-close").onclick = null; resolve(value); }
                    templateSelect.onchange = applyTemplate;
                    document.querySelectorAll("[data-org-agent-tab]").forEach(function (button) { button.onclick = function () { setAgentPanel(button.getAttribute("data-org-agent-tab")); }; });
                    document.getElementById("organization-agent-modal-cancel").onclick = function () { close(null); };
                    document.getElementById("organization-agent-modal-close").onclick = function () { close(null); };
                    form.onsubmit = function (event) {
                        event.preventDefault();
                        if (!form.checkValidity()) { form.reportValidity(); return; }
                        var pluginTools = {};
                        document.querySelectorAll('#organization-agent-plugin-tools input:checked').forEach(function (box) { var id = box.getAttribute("data-plugin-id"); if (!pluginTools[id]) pluginTools[id] = []; pluginTools[id].push(box.value); });
                        var hints = document.getElementById("organization-agent-hints").value.trim();
                        var temperature = document.getElementById("organization-agent-temperature").value;
                        var maxTokens = document.getElementById("organization-agent-max-tokens").value;
                        close({
                            payload: {
                                id: document.getElementById("organization-agent-id").value.trim(),
                                name: document.getElementById("organization-agent-name").value.trim(),
                                role: document.getElementById("organization-agent-role").value.trim(),
                                description: document.getElementById("organization-agent-description").value.trim(),
                                system_prompt: document.getElementById("organization-agent-prompt").value.trim(),
                                enabled: document.getElementById("organization-agent-enabled").checked,
                                model: document.getElementById("organization-agent-model").value || null,
                                greeting: document.getElementById("organization-agent-greeting").value.trim() || null,
                                greeting_hints: hints ? hints.split(/[;；]/).map(function (item) { return item.trim(); }).filter(Boolean) : [],
                                temperature: temperature === "" ? null : parseFloat(temperature),
                                max_tokens: maxTokens === "" ? null : parseInt(maxTokens, 10),
                                capabilities: payload.capabilities || [],
                                tools: selectedAgentValues(document.getElementById("organization-agent-builtin-tools")),
                                plugin_tools: pluginTools,
                                skills: selectedAgentValues(document.getElementById("organization-agent-skills")),
                                mcp_servers: selectedAgentValues(document.getElementById("organization-agent-mcp")),
                                datasources: selectedAgentValues(document.getElementById("organization-agent-datasources"))
                            },
                            base_resource_id: creating ? (templateSelect.value || null) : (current.base_resource_id || null),
                            knowledge_category_ids: selectedAgentValues(document.getElementById("organization-agent-knowledge"))
                        });
                    };
                });
            });
        });
    }

    function fillAgentFromPayload(payload, options) {
        document.getElementById("organization-agent-name").value = payload.name || "";
        document.getElementById("organization-agent-role").value = payload.role || "assistant";
        document.getElementById("organization-agent-description").value = payload.description || "";
        document.getElementById("organization-agent-prompt").value = payload.system_prompt || "你是一个有帮助的助手。";
        document.getElementById("organization-agent-enabled").checked = payload.enabled !== false;
        document.getElementById("organization-agent-model").value = payload.model || "";
        document.getElementById("organization-agent-greeting").value = payload.greeting || "";
        document.getElementById("organization-agent-hints").value = (payload.greeting_hints || []).join("；");
        document.getElementById("organization-agent-temperature").value = payload.temperature == null ? "" : payload.temperature;
        document.getElementById("organization-agent-max-tokens").value = payload.max_tokens == null ? "" : payload.max_tokens;
        var selected = {}; (payload.tools || []).forEach(function (value) { selected[value] = true; });
        document.querySelectorAll('#organization-agent-builtin-tools input').forEach(function (box) { box.checked = !!selected[box.value]; });
        document.querySelectorAll('#organization-agent-skills input').forEach(function (box) { box.checked = (payload.skills || []).indexOf(box.value) >= 0; });
        document.querySelectorAll('#organization-agent-mcp input').forEach(function (box) { box.checked = (payload.mcp_servers || []).indexOf(box.value) >= 0; });
        document.querySelectorAll('#organization-agent-plugin-tools input').forEach(function (box) { box.checked = ((payload.plugin_tools || {})[box.getAttribute("data-plugin-id")] || []).indexOf(box.value) >= 0; });
        renderAgentDatasources((options || {}).datasources, payload.datasources || []);
    }

    function suggestChannelId(type) {
        var existing = {};
        (((state.data || {}).items) || []).forEach(function (item) { existing[item.id] = true; });
        var base = type || "channel";
        var candidate = base;
        var n = 0;
        while (existing[candidate]) { n += 1; candidate = base + "_" + n; }
        return candidate;
    }

    var channelWechatTimer = null;
    var lastChannelWechatQr = "";

    function channelEndpoint(channelId, isPersonal, suffix) {
        return isPersonal
            ? "/api/connections/" + encodeURIComponent(channelId) + suffix
            : organizationApi("/channels/" + encodeURIComponent(channelId) + suffix);
    }

    function stopChannelWechatPoll() {
        if (channelWechatTimer) { clearTimeout(channelWechatTimer); channelWechatTimer = null; }
    }

    function channelWechatStatusHtml(status) {
        if (status.state === "pending" || status.state === "scanned") {
            var hint = status.state === "scanned" ? "已扫码，请在手机上确认…" : "打开微信，扫描二维码连接";
            return (status.qr
                ? '<img class="wechat-qr" src="' + status.qr + '" alt="微信登录二维码">'
                : '<div class="wecom-qr-placeholder">正在生成二维码…</div>') +
                '<p class="wechat-connect-hint">' + hint + "</p>";
        }
        if (status.connected || status.state === "success") {
            return '<div class="wechat-connect-status">' +
                '<span class="badge badge-success">已连接</span>' +
                (status.bot_id ? '<span class="text-muted"> bot_id: ' + escapeHtml(status.bot_id) + "</span>" : "") +
                "</div>" +
                '<p class="wechat-connect-hint">若在别处重新绑定了该微信号导致掉线，可点此重新扫码夺回连接。</p>' +
                '<button type="button" class="btn-secondary" data-action="channel-wechat-login">重新扫码（换号/重连）</button>';
        }
        if (status.state === "failed") {
            return '<div class="wechat-connect-status">' +
                '<span class="badge badge-muted">机器人未连接</span></div>' +
                (status.error ? '<p class="wechat-connect-error">' + escapeHtml(status.error) + "</p>" : "") +
                '<button type="button" class="btn-primary" data-action="channel-wechat-login">刷新二维码</button>';
        }
        return '<div class="wechat-connect-status">' +
            '<span class="badge badge-muted">机器人未连接</span></div>' +
            '<button type="button" class="btn-primary" data-action="channel-wechat-login">扫码登录</button>';
    }

    function pollChannelWechatStatus(channelId, modalMode, onConnected, isPersonal) {
        stopChannelWechatPoll();
        var qrArea = document.getElementById("organization-channel-wechat-qr-area");
        request(channelEndpoint(channelId, isPersonal, "/wechat/status")).then(function (status) {
            if (!qrArea) return;
            if (status.qr) lastChannelWechatQr = status.qr;
            if (status.connected || status.state === "success") {
                stopChannelWechatPoll();
                if (onConnected) {
                    onConnected(channelId, status, isPersonal);
                    return;
                }
                qrArea.innerHTML = channelWechatStatusHtml(status);
                showToast("微信已连接", "success");
                return;
            }
            qrArea.innerHTML = channelWechatStatusHtml(status);
            if (status.state === "pending" || status.state === "scanned") {
                channelWechatTimer = setTimeout(function () {
                    pollChannelWechatStatus(channelId, modalMode, onConnected, isPersonal);
                }, 2000);
            }
        }).catch(function () {
            if (qrArea) qrArea.innerHTML = '<div class="tool-empty">连接状态获取失败</div>';
        });
    }

    function startChannelWechatLogin(channelId, modalMode, onConnected, isPersonal) {
        var qrArea = document.getElementById("organization-channel-wechat-qr-area");
        if (qrArea) qrArea.innerHTML = '<div class="wecom-qr-placeholder">正在生成二维码…</div>';
        request(channelEndpoint(channelId, isPersonal, "/wechat/login"), { method: "POST" })
            .then(function () { pollChannelWechatStatus(channelId, modalMode, onConnected, isPersonal); })
            .catch(function (error) { showToast("启动扫码失败：" + error.message, "error"); });
    }

    var channelFeishuTimer = null;
    var lastChannelFeishuQr = "";

    function stopChannelFeishuPoll() {
        if (channelFeishuTimer) { clearTimeout(channelFeishuTimer); channelFeishuTimer = null; }
    }

    function channelFeishuStatusHtml(status) {
        if (status.state === "pending") {
            return (status.qr
                ? '<img class="wechat-qr" src="' + status.qr + '" alt="飞书注册二维码">'
                : '<div class="wecom-qr-placeholder">正在生成二维码…</div>') +
                '<p class="wechat-connect-hint">用飞书扫描二维码，确认创建机器人应用…</p>';
        }
        if (status.state === "failed") {
            return '<div class="wechat-connect-status">' +
                '<span class="badge badge-muted">未连接</span></div>' +
                (status.error ? '<p class="wechat-connect-error">' + escapeHtml(status.error) + "</p>" : "") +
                '<button type="button" class="btn-primary" data-action="channel-feishu-login">刷新二维码</button>';
        }
        return '<div class="wechat-connect-status">' +
            '<span class="badge badge-muted">未创建</span></div>' +
            '<button type="button" class="btn-primary" data-action="channel-feishu-login">扫码创建</button>';
    }

    function pollChannelFeishuStatus(channelId, onConfirmCb, isPersonal) {
        stopChannelFeishuPoll();
        var qrArea = document.getElementById("organization-channel-feishu-qr-area");
        request(channelEndpoint(channelId, isPersonal, "/feishu/status")).then(function (status) {
            if (!qrArea) return;
            if (status.qr) lastChannelFeishuQr = status.qr;
            if (status.state === "success") {
                stopChannelFeishuPoll();
                if (onConfirmCb) {
                    onConfirmCb(channelId, status, isPersonal);
                    return;
                }
                qrArea.innerHTML = channelFeishuStatusHtml(status);
                return;
            }
            qrArea.innerHTML = channelFeishuStatusHtml(status);
            if (status.state === "pending") {
                channelFeishuTimer = setTimeout(function () {
                    pollChannelFeishuStatus(channelId, onConfirmCb, isPersonal);
                }, 2000);
            }
        }).catch(function () {
            if (qrArea) qrArea.innerHTML = '<div class="tool-empty">连接状态获取失败</div>';
        });
    }

    function startChannelFeishuLogin(channelId, onConfirmCb, isPersonal) {
        var qrArea = document.getElementById("organization-channel-feishu-qr-area");
        if (qrArea) qrArea.innerHTML = '<div class="wecom-qr-placeholder">正在生成二维码…</div>';
        request(channelEndpoint(channelId, isPersonal, "/feishu/login"), { method: "POST" })
            .then(function () { pollChannelFeishuStatus(channelId, onConfirmCb, isPersonal); })
            .catch(function (error) { showToast("启动扫码失败：" + error.message, "error"); });
    }

    function showChannelFeishuSuccess(qrArea, status) {
        if (qrArea) {
            qrArea.innerHTML =
                (lastChannelFeishuQr ? '<img class="wechat-qr" src="' + lastChannelFeishuQr + '" alt="飞书注册二维码">' : "") +
                '<div class="wechat-connect-status">' +
                '<span class="badge badge-success">连接成功</span>' +
                (status.app_id ? '<span class="text-muted"> app_id: ' + escapeHtml(status.app_id) + "</span>" : "") +
                "</div>" +
                '<p class="wechat-connect-hint">机器人已启用，可关闭弹窗开始使用。</p>';
        }
        var doneBtn = document.getElementById("organization-channel-done");
        if (doneBtn) {
            doneBtn.style.display = "";
            document.getElementById("organization-channel-step-next").style.display = "none";
            document.getElementById("organization-channel-step-back").style.display = "none";
            document.getElementById("organization-channel-bind-save").style.display = "none";
            document.getElementById("organization-channel-modal-cancel").style.display = "none";
        }
    }

    function channelDialog(current, creating) {
        current = current || {};
        return request(organizationApi("/agents")).then(function (data) {
            var modal = document.getElementById("organization-channel-modal");
            var form = document.getElementById("organization-channel-form");
            var typeSelect = document.getElementById("organization-channel-type");
            var agentSelect = document.getElementById("organization-channel-agent");
            var formPanel = document.getElementById("organization-channel-form-panel");
            var bindPanel = document.getElementById("organization-channel-bind-panel");
            var bindWechat = document.getElementById("organization-channel-bind-wechat");
            var bindWecom = document.getElementById("organization-channel-bind-wecom");
            var bindFeishu = document.getElementById("organization-channel-bind-feishu");
            var qrArea = document.getElementById("organization-channel-wechat-qr-area");
            var feishuQrArea = document.getElementById("organization-channel-feishu-qr-area");
            var doneBtn = document.getElementById("organization-channel-done");
            var stepNext = document.getElementById("organization-channel-step-next");
            var stepBack = document.getElementById("organization-channel-step-back");
            var bindSave = document.getElementById("organization-channel-bind-save");
            var submitBtn = form.querySelector('button[type="submit"]');
            document.getElementById("organization-channel-modal-title").textContent = creating ? "新建渠道" : "编辑渠道";
            var idInput = document.getElementById("organization-channel-id");
            var idEdited = !creating;
            typeSelect.disabled = false;
            agentSelect.disabled = false;
            idInput.value = current.id || suggestChannelId(current.type || "wecom_aibot");
            idInput.disabled = !creating;
            idInput.oninput = function () { idEdited = true; };
            typeSelect.value = current.type || "wecom_aibot";
            agentSelect.innerHTML = (data.items || []).map(function (item) {
                var payload = item.payload || {};
                return '<option value="' + escapeHtml(item.resource_id) + '">' +
                    escapeHtml(payload.name || item.resource_id) + "</option>";
            }).join("");
            var wantedAgent = current.agent_id || "general";
            if (!Array.prototype.some.call(agentSelect.options, function (option) { return option.value === wantedAgent; })) {
                agentSelect.insertAdjacentHTML("beforeend", '<option value="' + escapeHtml(wantedAgent) + '">' +
                    escapeHtml(wantedAgent) + "（已不可用）</option>");
            }
            agentSelect.value = wantedAgent;
            document.getElementById("organization-channel-enabled").checked = current.enabled === true;
            document.getElementById("organization-channel-group-policy").value =
                (current.settings || {}).group_policy || "private_only";
            document.getElementById("organization-channel-bot-id").value =
                (creating ? "" : (current.bot_account_id || ""));
            document.getElementById("organization-channel-secret").value = "";
            modal.style.display = "";
            var settled = false;
            var pendingChannelId = null;
            var credentialBound = false;
            return new Promise(function (resolve) {
                function unbind() {
                    modal.style.display = "none";
                    stopChannelWechatPoll();
                    stopChannelFeishuPoll();
                    lastChannelFeishuQr = "";
                    lastChannelWechatQr = "";
                    form.onsubmit = null;
                    typeSelect.onchange = null;
                    idInput.oninput = null;
                    stepNext.onclick = null;
                    stepBack.onclick = null;
                    bindSave.onclick = null;
                    if (doneBtn) doneBtn.onclick = null;
                    document.getElementById("organization-channel-modal-cancel").onclick = null;
                    document.getElementById("organization-channel-modal-close").onclick = null;
                }
                function close() {
                    var pendingId = pendingChannelId;
                    var bound = credentialBound;
                    pendingChannelId = null;
                    credentialBound = false;
                    unbind();
                    settle(null);
                    if (creating && pendingId && !bound) {
                        // The channel was saved but never bound; remove it so
                        // cancelling the dialog does not leave an orphan.
                        request(organizationApi("/channels/" + encodeURIComponent(pendingId)), { method: "DELETE" })
                            .then(function () { refresh(); })
                            .catch(function () {});
                    } else {
                        // Refresh once when the dialog closes (bound or
                        // cancelled) so a channel only shows up in the list
                        // after its credentials are configured.
                        refresh();
                    }
                }
                function settle(value) {
                    if (settled) return;
                    settled = true;
                    resolve(value);
                }
                function showStep(step) {
                    document.querySelectorAll("[data-channel-tab]").forEach(function (button) {
                        button.classList.toggle("active", button.getAttribute("data-channel-tab") === step);
                    });
                    formPanel.style.display = step === "form" ? "" : "none";
                    bindPanel.style.display = step === "bind" ? "" : "none";
                    var type = typeSelect.value;
                    if (step === "bind") {
                        bindWechat.style.display = type === "wechat_ilink" ? "" : "none";
                        bindWecom.style.display = type === "wecom_aibot" ? "" : "none";
                        bindFeishu.style.display = type === "feishu" ? "" : "none";
                    }
                    if (step === "form") {
                        if (doneBtn) doneBtn.style.display = "none";
                        document.getElementById("organization-channel-modal-cancel").style.display = "";
                        bindSave.style.display = "none";
                    }
                    stepNext.style.display = step === "form" ? "" : "none";
                    stepBack.style.display = step === "bind" ? "" : "none";
                    if (step === "bind") {
                        bindSave.style.display = type === "wecom_aibot" ? "" : "none";
                    }
                    if (step === "bind" && credentialBound) {
                        // Credentials are already bound; restore the success
                        // footer so the user can finish instead of having to
                        // re-scan after tabbing away and back.
                        if (doneBtn) doneBtn.style.display = "";
                        stepNext.style.display = "none";
                        stepBack.style.display = "none";
                        bindSave.style.display = "none";
                        document.getElementById("organization-channel-modal-cancel").style.display = "none";
                    }
                    if (step === "form") {
                        typeSelect.disabled = false;
                        agentSelect.disabled = false;
                        if (creating) idInput.disabled = false;
                        submitBtn.textContent = "保存";
                    }
                }
                function showConnectPhase(channelId, isPersonal) {
                    pendingChannelId = channelId;
                    credentialBound = false;
                    typeSelect.disabled = true;
                    agentSelect.disabled = true;
                    idInput.disabled = true;
                    showStep("bind");
                    if (typeSelect.value === "wechat_ilink") {
                        startChannelWechatLogin(channelId, true, autoConfirmWechat, isPersonal);
                    }
                    if (typeSelect.value === "feishu") {
                        startChannelFeishuLogin(channelId, autoConfirmFeishu, isPersonal);
                    }
                }
                function autoConfirmFeishu(channelId, status, isPersonal) {
                    stopChannelFeishuPoll();
                    request(channelEndpoint(channelId, isPersonal, "/feishu/confirm"), { method: "POST" })
                        .then(function () {
                            credentialBound = true;
                            showToast("飞书机器人已启用", "success");
                            showChannelFeishuSuccess(feishuQrArea, status || {});
                        })
                        .catch(function (error) { showToast("启用失败：" + error.message, "error"); });
                }
                function autoConfirmWechat(channelId, status, isPersonal) {
                    stopChannelWechatPoll();
                    request(channelEndpoint(channelId, isPersonal, "/wechat/confirm"), { method: "POST" })
                        .then(function () {
                            credentialBound = true;
                            showToast("微信机器人已启用", "success");
                            showChannelWechatSuccess(
                                document.getElementById("organization-channel-wechat-qr-area"),
                                status || {}
                            );
                        })
                        .catch(function (error) { showToast("启用失败：" + error.message, "error"); });
                }
                function showChannelWechatSuccess(qrArea, status) {
                    if (qrArea) {
                        qrArea.innerHTML =
                            (lastChannelWechatQr ? '<img class="wechat-qr" src="' + lastChannelWechatQr + '" alt="微信登录二维码">' : "") +
                            '<div class="wechat-connect-status">' +
                            '<span class="badge badge-success">连接成功</span>' +
                            (status.bot_id ? '<span class="text-muted"> bot_id: ' + escapeHtml(status.bot_id) + "</span>" : "") +
                            "</div>" +
                            '<p class="wechat-connect-hint">机器人已启用，可关闭弹窗开始使用。</p>';
                    }
                    if (doneBtn) {
                        doneBtn.style.display = "";
                        document.getElementById("organization-channel-step-next").style.display = "none";
                        document.getElementById("organization-channel-step-back").style.display = "none";
                        document.getElementById("organization-channel-bind-save").style.display = "none";
                        document.getElementById("organization-channel-modal-cancel").style.display = "none";
                    }
                }
                function submitWecom(channelId) {
                    var botId = document.getElementById("organization-channel-bot-id").value.trim();
                    var secret = document.getElementById("organization-channel-secret").value.trim();
                    if (!botId && !secret && !creating) {
                        // Editing without touching credentials: just finish.
                        close();
                        return;
                    }
                    if (!botId || !secret) {
                        showToast("请填写 Bot ID 和 Secret", "error");
                        return;
                    }
                    bindSave.disabled = true;
                    var originalText = bindSave.textContent;
                    bindSave.textContent = "正在保存…";
                    var credentialsUrl = isPersonalScope()
                        ? "/api/connections/" + encodeURIComponent(channelId) + "/wecom/credentials"
                        : organizationApi("/channels/" + encodeURIComponent(channelId) + "/credentials");
                    var credentialsBody = isPersonalScope()
                        ? { bot_id: botId, secret: secret }
                        : { credentials: { bot_id: botId, secret: secret } };
                    request(credentialsUrl, {
                        method: "PUT", headers: { "Content-Type": "application/json" },
                        body: JSON.stringify(credentialsBody)
                    }).then(function () {
                        credentialBound = true;
                        showToast("企微机器人凭证已保存", "success");
                        close();
                    }).catch(function (error) {
                        bindSave.disabled = false;
                        bindSave.textContent = originalText;
                        showToast("凭证保存失败：" + error.message, "error");
                    });
                }
                qrArea.addEventListener("click", function (event) {
                    var button = event.target.closest('[data-action="channel-wechat-login"]');
                    if (button && idInput.value) {
                        startChannelWechatLogin(idInput.value.trim(), true, autoConfirmWechat, isPersonalScope());
                    }
                });
                if (feishuQrArea) {
                    feishuQrArea.addEventListener("click", function (event) {
                        var button = event.target.closest('[data-action="channel-feishu-login"]');
                        if (button && idInput.value) {
                            startChannelFeishuLogin(idInput.value.trim(), autoConfirmFeishu, isPersonalScope());
                        }
                    });
                }
                function isPersonalScope() {
                    var scope = document.getElementById("organization-channel-scope");
                    return !!(scope && scope.value === "personal");
                }
                var scopeGroup = document.getElementById("organization-channel-scope-group");
                var scopeSelect = document.getElementById("organization-channel-scope");
                if (scopeGroup) {
                    scopeGroup.style.display = creating ? "" : "none";
                    if (creating && scopeSelect) scopeSelect.value = "organization";
                }
                if (scopeSelect) {
                    scopeSelect.onchange = function () {
                        var idGroup = document.getElementById("organization-channel-id-group");
                        if (idGroup) idGroup.style.display = isPersonalScope() ? "none" : "";
                    };
                }
                typeSelect.onchange = function () {
                    if (creating && !idEdited) idInput.value = suggestChannelId(typeSelect.value);
                };
                document.getElementById("organization-channel-modal-cancel").onclick = close;
                document.getElementById("organization-channel-modal-close").onclick = close;
                function goNext() {
                    if (pendingChannelId) {
                        showStep("bind");
                        return;
                    }
                    if (!agentSelect.options.length) {
                        showToast("智能体列表加载中或组织暂无可用智能体，请稍候重试", "error");
                        return;
                    }
                    if (!agentSelect.value) {
                        showToast("请选择关联智能体", "error");
                        return;
                    }
                    if (!form.checkValidity()) {
                        form.reportValidity();
                        showToast("请完善渠道配置（渠道实例 ID 需小写字母开头）", "error");
                        return;
                    }
                    settle({
                        payload: {
                            id: idInput.value.trim(),
                            type: typeSelect.value,
                            agent_id: agentSelect.value,
                            scope: isPersonalScope() ? "personal" : "organization",
                            enabled: document.getElementById("organization-channel-enabled").checked,
                            settings: Object.assign({}, current.settings || {}, {
                                group_policy: document.getElementById("organization-channel-group-policy").value
                            })
                        },
                        connect: showConnectPhase
                    });
                }
                stepNext.onclick = goNext;
                document.querySelectorAll("[data-channel-tab]").forEach(function (tab) {
                    // onclick (not addEventListener): the dialog is reopened
                    // repeatedly and handlers must not accumulate.
                    tab.onclick = function () {
                        var step = tab.getAttribute("data-channel-tab");
                        if (step === "bind") {
                            goNext();
                        } else {
                            showStep("form");
                        }
                    };
                });
                if (doneBtn) {
                    doneBtn.onclick = function () {
                        doneBtn.style.display = "none";
                        close();
                    };
                }
                stepBack.onclick = function () {
                    showStep("form");
                };
                form.onsubmit = function (event) {
                    event.preventDefault();
                    if (bindSave.style.display === "none") return;
                    var channelId = idInput.value.trim();
                    submitWecom(channelId);
                };
                showStep("form");
            });
        });
    }

    function scheduleDialog(current, creating) {
        current = current || {};
        var action = current.action || {};
        return loadScheduleOptions().then(function (options) {
            var modal = document.getElementById("organization-schedule-modal");
            var form = document.getElementById("organization-schedule-form");
            var typeSelect = document.getElementById("organization-schedule-action-type");
            var agentSelect = document.getElementById("organization-schedule-agent");
            var scriptSelect = document.getElementById("organization-schedule-script");
            var pluginSelect = document.getElementById("organization-schedule-plugin");
            var toolSelect = document.getElementById("organization-schedule-tool");
            var type = action.type || "text";
            document.getElementById("organization-schedule-modal-title").textContent = creating ? "新建定时任务" : "编辑定时任务";
            document.getElementById("organization-schedule-id").value = current.id || "";
            document.getElementById("organization-schedule-id").disabled = !creating;
            document.getElementById("organization-schedule-enabled").checked = current.enabled === true;
            document.getElementById("organization-schedule-crons").value = (current.crons || ["0 9 * * *"]).join("\n");
            document.getElementById("organization-schedule-timezone").textContent = "时区：" + (options.timezone || "平台统一时区");
            agentSelect.innerHTML = (options.agents || []).map(function (item) { return '<option value="' + escapeHtml(item.id) + '">' + escapeHtml(item.name || item.id) + '</option>'; }).join("");
            scriptSelect.innerHTML = (options.scripts || []).map(function (item) { return '<option value="' + escapeHtml(item.id) + '">' + escapeHtml(item.name || item.id) + '（' + escapeHtml(item.id) + '）</option>'; }).join("");
            pluginSelect.innerHTML = (options.plugins || []).map(function (item) { return '<option value="' + escapeHtml(item.id) + '">' + escapeHtml(item.name || item.id) + '</option>'; }).join("");
            function addUnavailable(select, value, label) {
                if (!value || Array.prototype.some.call(select.options, function (option) { return option.value === value; })) return;
                select.insertAdjacentHTML("beforeend", '<option value="' + escapeHtml(value) + '">' + escapeHtml(label || value) + '（已不可用）</option>');
            }
            function updateTools() {
                var plugin = (options.plugins || []).filter(function (item) { return item.id === pluginSelect.value; })[0];
                toolSelect.innerHTML = plugin ? (plugin.tools || []).map(function (item) { return '<option value="' + escapeHtml(item.name) + '">' + escapeHtml(item.name) + '</option>'; }).join("") : "";
                addUnavailable(toolSelect, action.tool_name, action.tool_name);
                toolSelect.value = action.tool_name || (toolSelect.options[0] && toolSelect.options[0].value) || "";
            }
            function updateFields() {
                var selected = typeSelect.value;
                document.getElementById("organization-schedule-text-group").hidden = selected !== "text";
                document.getElementById("organization-schedule-agent-group").hidden = selected !== "agent_prompt";
                document.getElementById("organization-schedule-script-group").hidden = selected !== "script";
                document.getElementById("organization-schedule-plugin-group").hidden = selected !== "plugin";
                document.getElementById("organization-schedule-parameters-group").hidden = selected !== "script" && selected !== "plugin";
                if (selected === "plugin") updateTools();
            }
            addUnavailable(agentSelect, action.agent_id, action.agent_id);
            addUnavailable(scriptSelect, action.script_id, action.script_id);
            addUnavailable(pluginSelect, action.plugin_id, action.plugin_id);
            agentSelect.value = action.agent_id || (agentSelect.options[0] && agentSelect.options[0].value) || "";
            scriptSelect.value = action.script_id || (scriptSelect.options[0] && scriptSelect.options[0].value) || "";
            pluginSelect.value = action.plugin_id || (pluginSelect.options[0] && pluginSelect.options[0].value) || "";
            document.getElementById("organization-schedule-content").value = action.content || "";
            document.getElementById("organization-schedule-prompt").value = action.prompt || "";
            document.getElementById("organization-schedule-parameters").value = JSON.stringify(action.parameters || {}, null, 2);
            typeSelect.value = type;
            updateFields();
            modal.style.display = "";
            return new Promise(function (resolve) {
                function close(value) { modal.style.display = "none"; form.onsubmit = null; document.getElementById("organization-schedule-modal-cancel").onclick = null; document.getElementById("organization-schedule-modal-close").onclick = null; resolve(value); }
                typeSelect.onchange = updateFields;
                pluginSelect.onchange = updateTools;
                document.getElementById("organization-schedule-modal-cancel").onclick = function () { close(null); };
                document.getElementById("organization-schedule-modal-close").onclick = function () { close(null); };
                form.onsubmit = function (event) {
                    event.preventDefault();
                    if (!form.checkValidity()) { form.reportValidity(); return; }
                    var parameters = {};
                    if (typeSelect.value === "script" || typeSelect.value === "plugin") {
                        try { parameters = JSON.parse(document.getElementById("organization-schedule-parameters").value || "{}"); }
                        catch (error) { showToast("动作参数必须是合法 JSON", "error"); return; }
                        if (!parameters || Array.isArray(parameters) || typeof parameters !== "object") { showToast("动作参数必须是 JSON 对象", "error"); return; }
                    }
                    var nextAction = { type: typeSelect.value };
                    if (nextAction.type === "text") nextAction.content = document.getElementById("organization-schedule-content").value.trim();
                    if (nextAction.type === "agent_prompt") { nextAction.agent_id = agentSelect.value; nextAction.prompt = document.getElementById("organization-schedule-prompt").value.trim(); }
                    if (nextAction.type === "script") { nextAction.script_id = scriptSelect.value; nextAction.parameters = parameters; }
                    if (nextAction.type === "plugin") { nextAction.plugin_id = pluginSelect.value; nextAction.tool_name = toolSelect.value; nextAction.parameters = parameters; }
                    close({
                        id: document.getElementById("organization-schedule-id").value.trim(),
                        enabled: document.getElementById("organization-schedule-enabled").checked,
                        crons: document.getElementById("organization-schedule-crons").value.split(/\r?\n/).map(function (entry) { return entry.trim(); }).filter(Boolean),
                        target: current.target || "last_active_user",
                        action: nextAction,
                        condition: current.condition || null
                    });
                };
            });
        });
    }

    function card(title, description, pills, actions) {
        return '<article class="organization-card"><h3>' + escapeHtml(title) + "</h3>" +
            "<p>" + escapeHtml(description || "暂无描述") + "</p>" +
            '<div class="organization-card-meta">' + (pills || []).map(function (pill) {
                return '<span class="organization-pill">' + escapeHtml(pill) + "</span>";
            }).join("") + "</div>" +
            '<div class="organization-card-actions">' + (actions || "") + "</div></article>";
    }

    function button(action, id, label, danger) {
        return '<button type="button" data-action="' + action + '" data-id="' +
            escapeHtml(id || "") + '"' + (danger ? ' data-danger="1"' : "") +
            ">" + escapeHtml(label) + "</button>";
    }

    function setItems(items, renderer) {
        summary.textContent = "共 " + items.length + " 项";
        list.innerHTML = items.length
            ? items.map(renderer).join("")
            : '<div class="organization-empty">暂无内容</div>';
    }

    function loadAgents() {
        return request(organizationApi("/agents")).then(function (data) {
            state.data = data;
            setItems(data.items || [], function (item) {
                var payload = item.payload || {};
                var scope = item.effective_scope || item.scope;
                var enabled = payload.enabled !== false && scope !== "disabled";
                var actions = "";
                if (canWriteOrganization()) {
                    actions += button("agent-default", item.resource_id, "设为默认");
                    actions += button("agent-toggle", item.resource_id, enabled ? "暂停" : "启用");
                    if (scope === "organization") {
                        actions += button("agent-edit", item.resource_id, "编辑");
                        actions += button("agent-delete", item.resource_id, "删除", true);
                    } else {
                        actions += button("agent-copy", item.resource_id, "复制");
                    }
                    actions += button("agent-knowledge", item.resource_id, "知识库授权");
                }
                return card(
                    payload.name || item.resource_id,
                    payload.description || payload.role,
                    [
                        scope === "organization" ? "组织智能体" : "公共模板",
                        enabled ? "已启用" : "已暂停",
                        data.default_agent_id === item.resource_id ? "默认" : "",
                        (payload.datasources || []).length
                            ? "数据源 " + payload.datasources.length
                            : ""
                    ].filter(Boolean),
                    actions
                );
            });
        });
    }

    function loadOverview() {
        return Promise.all([
            request(organizationApi("/members")),
            request(organizationApi("/analytics/overview"))
        ]).then(function (results) {
            var members = results[0].items || [];
            var analytics = results[1].overview || results[1] || {};
            setItems([
                { name: "成员", value: members.length },
                { name: "模型调用", value: analytics.total_runs || analytics.run_count || 0 },
                { name: "成功调用", value: analytics.success_runs || analytics.success_count || 0 }
            ], function (item) {
                return card(item.name, String(item.value), ["组织数据"], "");
            });
        });
    }

    function updateChannelWecomCredentials(current, isPersonal) {
        return showFormDialog({
            title: "配置企微凭证",
            fields: [
                { name: "bot_id", label: "Bot ID", value: current.bot_account_id || "", required: true },
                { name: "secret", label: "Secret（已配置则留空跳过校验）", type: "password" }
            ]
        }).then(function (value) {
            if (!value) return null;
            if (!value.bot_id && !value.secret) return null;
            var url = isPersonal
                ? "/api/connections/" + encodeURIComponent(current.connection_id) + "/wecom/credentials"
                : organizationApi("/channels/" + encodeURIComponent(current.id) + "/credentials");
            var body = isPersonal
                ? { bot_id: value.bot_id, secret: value.secret }
                : { credentials: { bot_id: value.bot_id, secret: value.secret } };
            return request(url, {
                method: "PUT", headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body)
            });
        }).then(function (result) {
            if (result) showToast("企业微信凭证已保存", "success");
            return result;
        });
    }

    function editPersonalChannelAgent(current) {
        return request(organizationApi("/agents")).then(function (data) {
            var options = (data.items || []).map(function (item) {
                var payload = item.payload || {};
                return { value: item.resource_id, label: payload.name || item.resource_id };
            });
            if (!options.length) {
                showToast("组织暂无可用智能体", "error");
                return null;
            }
            return showFormDialog({
                title: "更换接待智能体",
                fields: [{
                    name: "agent_id", label: "接待智能体", type: "select",
                    value: current.agent_id, options: options
                }]
            }).then(function (value) {
                if (!value || value.agent_id === current.agent_id) return null;
                return request("/api/connections/" + encodeURIComponent(current.connection_id) + "/agent", {
                    method: "PUT", headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ agent_id: value.agent_id })
                });
            }).then(function (result) {
                if (result) {
                    showToast("接待智能体已更换", "success");
                    refresh();
                }
                return result;
            });
        });
    }

    function channelScanHtml(kind, status) {
        var label = kind === "feishu" ? "飞书" : "微信";
        if (status.state === "pending" || status.state === "scanned") {
            var hint = kind === "feishu"
                ? "用飞书扫描二维码，确认创建机器人应用…"
                : (status.state === "scanned" ? "已扫码，请在手机上确认…" : "打开微信，扫描二维码连接");
            return (status.qr
                ? '<img class="wechat-qr" src="' + status.qr + '" alt="' + label + '登录二维码">'
                : '<div class="wecom-qr-placeholder">正在生成二维码…</div>') +
                '<p class="wechat-connect-hint">' + hint + "</p>";
        }
        if (status.state === "failed") {
            return '<div class="wechat-connect-status">' +
                '<span class="badge badge-muted">未连接</span></div>' +
                (status.error ? '<p class="wechat-connect-error">' + escapeHtml(status.error) + "</p>" : "") +
                '<button type="button" class="btn-primary" data-action="channel-scan-retry">刷新二维码</button>';
        }
        return '<div class="wechat-connect-status">' +
            '<span class="badge badge-muted">未连接</span></div>' +
            '<button type="button" class="btn-primary" data-action="channel-scan-retry">扫码连接</button>';
    }

    function showChannelScanSuccess(qrArea, status, kind, overlay) {
        var label = kind === "feishu" ? "飞书" : "微信";
        var qr = kind === "feishu" ? lastChannelFeishuQr : lastChannelWechatQr;
        var idLabel = kind === "feishu" ? status.app_id : status.bot_id;
        qrArea.innerHTML =
            (qr ? '<img class="wechat-qr" src="' + qr + '" alt="' + label + '登录二维码">' : "") +
            '<div class="wechat-connect-status">' +
            '<span class="badge badge-success">连接成功</span>' +
            (idLabel ? '<span class="text-muted"> ' + (kind === "feishu" ? "app_id: " : "bot_id: ") + escapeHtml(idLabel) + "</span>" : "") +
            "</div>" +
            '<p class="wechat-connect-hint">机器人已启用，可关闭弹窗开始使用。</p>';
        var done = document.createElement("button");
        done.type = "button";
        done.className = "btn-primary";
        done.textContent = "完成";
        done.style.marginTop = "12px";
        done.onclick = function () {
            overlay.remove();
            refresh();
        };
        qrArea.appendChild(done);
    }

    function openChannelScan(channelId, kind, isPersonal) {
        var label = kind === "feishu" ? "飞书扫码创建机器人" : "微信扫码连接";
        var overlay = document.createElement("div");
        overlay.className = "modal-overlay";
        overlay.style.display = "";
        overlay.innerHTML = '<div class="modal"><div class="modal-header"><h3>' + label +
            '</h3><button type="button" class="modal-close-btn" onclick="this.closest(\'.modal-overlay\').remove()">&times;</button></div>' +
            '<div class="modal-body"><div id="channel-scan-qr-area" class="wechat-connect"><div class="wecom-qr-placeholder">正在生成二维码…</div></div></div></div>';
        document.body.appendChild(overlay);
        var qrArea = overlay.querySelector("#channel-scan-qr-area");
        var endpoint = kind === "feishu" ? "feishu" : "wechat";

        function startScan() {
            qrArea.innerHTML = '<div class="wecom-qr-placeholder">正在生成二维码…</div>';
            request(channelEndpoint(channelId, isPersonal, "/" + endpoint + "/login"), { method: "POST" })
                .then(function () { poll(); })
                .catch(function (error) { showToast("启动扫码失败：" + error.message, "error"); });
        }

        function poll() {
            request(channelEndpoint(channelId, isPersonal, "/" + endpoint + "/status")).then(function (status) {
                if (status.qr) {
                    if (kind === "feishu") lastChannelFeishuQr = status.qr;
                    else lastChannelWechatQr = status.qr;
                }
                // Only "success" means the fresh scan completed; "connected"
                // stays true for already-configured channels and must not
                // trigger a confirm without a pending credential.
                if (status.state === "success") {
                    request(channelEndpoint(channelId, isPersonal, "/" + endpoint + "/confirm"), { method: "POST" })
                        .then(function () {
                            showToast(label + "已启用", "success");
                            showChannelScanSuccess(qrArea, status, kind, overlay);
                        })
                        .catch(function (error) { showToast("启用失败：" + error.message, "error"); });
                    return;
                }
                qrArea.innerHTML = channelScanHtml(kind, status);
                if (status.state === "pending" || status.state === "scanned") {
                    setTimeout(poll, 2000);
                }
            }).catch(function () {
                qrArea.innerHTML = '<div class="tool-empty">连接状态获取失败</div>';
            });
        }

        qrArea.addEventListener("click", function (event) {
            var button = event.target.closest('[data-action="channel-scan-retry"]');
            if (button) startScan();
        });
        startScan();
    }

    function loadChannels() {
        return request(organizationApi("/channels")).then(function (data) {
            state.data = data;
            setItems(data.items || [], function (item) {
                var personal = !!item.personal;
                var actions = "";
                if (canWriteOrganization() || personal) {
                    actions += button("channel-toggle", item.id, item.enabled ? "暂停" : "启用");
                    actions += button("channel-edit", item.id, "编辑");
                    if (!personal) actions += button("channel-test", item.id, "测试");
                    actions += button("channel-delete", item.id, "删除", true);
                }
                if (canManageOrganization() || personal) {
                    actions += button("channel-credentials", item.id, "配置凭据");
                }
                return card(
                    item.id + (personal ? "（个人）" : ""),
                    item.migration_error || (item.type + " · 智能体 " + item.agent_id),
                    [item.state, item.credential_configured ? "凭据已配置" : "缺少凭据", "v" + item.revision],
                    actions
                );
            });
        });
    }

    function loadSchedules() {
        return request(organizationApi("/schedules")).then(function (data) {
            state.data = data;
            notice.hidden = false;
            notice.textContent = "平台统一时区：" + data.timezone + "；无有效收件人时任务会安全跳过。";
            setItems(data.items || [], function (item) {
                var actions = "";
                if (canWriteOrganization()) {
                    actions += button("schedule-run", item.id, "执行");
                    actions += button("schedule-toggle", item.id, item.enabled ? "暂停" : "启用");
                    actions += button("schedule-edit", item.id, "编辑");
                    actions += button("schedule-delete", item.id, "删除", true);
                }
                return card(item.id, (item.crons || []).join("；"),
                    [item.enabled ? "已启用" : "已暂停", item.action.type, item.target], actions);
            });
        });
    }

    /* ---- schedule runs tab ---- */

    var RUN_STATUS_LABELS = {
        running: "运行中", succeeded: "成功", failed: "失败", skipped: "已跳过"
    };
    var RUN_STATUS_BADGES = {
        running: "badge-warning", succeeded: "badge-success",
        failed: "badge-danger", skipped: "badge-muted"
    };
    var RUN_ACTION_LABELS = {
        text: "文本消息", agent_prompt: "智能体生成",
        script: "平台脚本", plugin: "平台插件工具"
    };

    // Timestamps are stored as datetime.now(timezone.utc).isoformat(); the
    // six-digit microseconds are not standard ES, so fall back to raw text.
    function runTime(value) {
        if (!value) return "—";
        var date = new Date(value);
        if (isNaN(date.getTime())) return String(value);
        return date.toLocaleString("zh-CN", { hour12: false });
    }

    function runsCell(row, text, className) {
        var cell = document.createElement("td");
        cell.textContent = text;
        if (className) cell.className = className;
        row.appendChild(cell);
        return cell;
    }

    function renderRuns(items) {
        var body = document.getElementById("organization-runs-body");
        body.innerHTML = "";
        if (!items.length) {
            var emptyRow = document.createElement("tr");
            var emptyCell = document.createElement("td");
            emptyCell.colSpan = 6;
            emptyCell.textContent = "暂无执行记录";
            emptyRow.appendChild(emptyCell);
            body.appendChild(emptyRow);
            return;
        }
        items.forEach(function (item) {
            var row = document.createElement("tr");
            runsCell(row, runTime(item.started_at));
            runsCell(row, item.schedule_key || "—");
            runsCell(row, RUN_ACTION_LABELS[item.action_type] || item.action_type || "—");
            var statusCell = runsCell(row, "");
            var badge = document.createElement("span");
            badge.className = "badge " + (RUN_STATUS_BADGES[item.status] || "badge-muted");
            badge.textContent = RUN_STATUS_LABELS[item.status] || item.status || "—";
            statusCell.appendChild(badge);
            runsCell(row, item.detail || "—", "organization-runs-detail");
            runsCell(row, runTime(item.finished_at));
            body.appendChild(row);
        });
    }

    function loadScheduleRuns() {
        if (!runsPanel) return Promise.resolve();
        var status = document.getElementById("organization-runs-status").value;
        var url = organizationApi("/schedule-runs") + "?limit=" + runs.limit +
            "&offset=" + runs.offset +
            (status ? "&status=" + encodeURIComponent(status) : "");
        return request(url).then(function (data) {
            runs.total = data.total || 0;
            renderRuns(data.items || []);
            var page = Math.floor(runs.offset / runs.limit) + 1;
            var pages = Math.max(1, Math.ceil(runs.total / runs.limit));
            document.getElementById("organization-runs-page").textContent =
                "第 " + page + " / " + pages + " 页，共 " + runs.total + " 条";
            document.getElementById("organization-runs-prev").disabled = runs.offset <= 0;
            document.getElementById("organization-runs-next").disabled =
                runs.offset + runs.limit >= runs.total;
        }).catch(function (error) {
            renderRuns([]);
            showToast(error.message, "error");
        });
    }

    // Keep the schedule widgets and the runs panel mutually exclusive. The
    // primary button also depends on permissions, so never force it visible.
    function applyScheduleTab() {
        if (!runsPanel) return;
        var runsMode = runs.tab === "runs";
        runsPanel.hidden = !runsMode;
        summary.hidden = runsMode;
        list.hidden = runsMode;
        notice.hidden = runsMode || !notice.textContent;
        primary.hidden = runsMode || !runs.primaryAllowed;
    }

    function activateScheduleTab(button) {
        var target = button.getAttribute("data-schedule-tab");
        Array.prototype.forEach.call(
            scheduleTabs.querySelectorAll(".tab-btn"),
            function (item) { item.classList.toggle("active", item === button); }
        );
        runs.tab = target;
        applyScheduleTab();
        if (target !== "runs") {
            refresh();
            return;
        }
        runs.offset = 0;
        loadScheduleRuns();
    }

    function loadCapabilities(type) {
        return request(organizationApi("/capabilities/" + type)).then(function (data) {
            state.data = data;
            setItems(data.items || [], function (item) {
                var payload = item.payload || {};
                return card(payload.name || payload.id || item.resource_id,
                    payload.description || payload.model || payload.type,
                    ["平台发布", item.status || "published", "v" + (item.revision || 1)], "");
            });
        });
    }

    function loadTools() {
        return Promise.all([
            request(organizationApi("/capabilities/tools")),
            request(organizationApi("/capabilities/skills")),
            request(organizationApi("/capabilities/mcp"))
        ]).then(function (results) {
            var items = [];
            ["工具", "Skill", "MCP"].forEach(function (label, index) {
                (results[index].items || []).forEach(function (item) {
                    item._kind = label;
                    item._resourceType = ["tools", "skills", "mcp"][index];
                    items.push(item);
                });
            });
            state.data = { items: items };
            setItems(items, function (item) {
                var payload = item.payload || {};
                return card(payload.name || payload.id || item.resource_id,
                    payload.description || payload.type,
                    [item._kind, "平台只读"], "");
            });
        });
    }

    function loadMembers() {
        return request(organizationApi("/members")).then(function (data) {
            state.data = data;
            setItems(data.items || [], function (item) {
                var actions = "";
                if (canManageOrganization() && item.user_id && item.role !== "owner") {
                    actions += button("member-role", String(item.user_id), "调整角色");
                    actions += button("member-remove", String(item.user_id), "移除", true);
                    if (selectedOrganization() && selectedOrganization().role === "owner") {
                        actions += button("member-owner", String(item.user_id), "转移所有权", true);
                    }
                }
                return card(item.display_name || item.legacy_subject_id || "待认领成员", "",
                    [item.role, item.status], actions);
            });
        });
    }

    function loadKnowledge() {
        return request(organizationApi("/knowledge/categories")).then(function (data) {
            setItems(data.items || [], function (item) {
                return card(item.name, item.description, [item.scope, (item.source_count || 0) + " 个来源"], "");
            });
        });
    }

    function loadDrive() {
        return request(organizationApi("/drive/entries?path=")).then(function (data) {
            setItems(data.entries || [], function (item) {
                return card(item.name, item.path, [item.type], "");
            });
        });
    }

    function refresh() {
        list.innerHTML = '<div class="organization-empty">加载中…</div>';
        var loaders = {
            overview: loadOverview,
            agents: loadAgents, channels: loadChannels, schedules: loadSchedules,
            models: function () { return loadCapabilities("models"); },
            tools: loadTools,
            plugins: function () { return loadCapabilities("plugins"); },
            scripts: function () { return loadCapabilities("scripts"); },
            members: loadMembers, knowledge: loadKnowledge, drive: loadDrive
        };
        return loaders[module]().catch(function (error) {
            list.innerHTML = '<div class="organization-empty">' + escapeHtml(error.message) + "</div>";
        });
    }

    function createAgent() {
        return agentDialog({
            id: "", name: "", role: "assistant", description: "",
            system_prompt: "你是一个有帮助的助手。", capabilities: [],
            tools: [], plugin_tools: {}, skills: [], mcp_servers: [],
            datasources: [], enabled: true
        }, true).then(function (payload) {
            if (!payload) return null;
            return request(organizationApi("/agents/" + encodeURIComponent(payload.payload.id)), {
            method: "PUT", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
            });
        }).then(function (result) { return result ? refresh() : null; });
    }

    function channelPlatform(scope, type) {
        // Personal connections use the /api/connections platform vocabulary.
        if (scope === "personal") {
            return { wechat_ilink: "wechat", feishu: "feishu", wecom_aibot: "wecom" }[type] || type;
        }
        return type;
    }

    function createChannel(existing) {
        return channelDialog(existing || {
            type: "wecom_aibot", agent_id: "general", enabled: false,
            settings: { group_policy: "private_only" }
        }, !existing).then(function (result) {
            if (!result) return null;
            var payload = result.payload;
            if (payload.scope === "personal") {
                return request("/api/connections", {
                    method: "POST", headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        platform: channelPlatform("personal", payload.type),
                        organization_id: activeOrganizationId(),
                        agent_id: payload.agent_id
                    })
                }).then(function (created) {
                    result.connect(created.connection_id, true);
                    return created;
                }).catch(function (error) {
                    showToast("创建个人连接失败：" + error.message, "error");
                    return null;
                });
            }
            return request(organizationApi("/channels/" + encodeURIComponent(payload.id)), {
                method: "PUT", headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload)
            }).then(function (saved) {
                if (!saved) return null;
                result.connect(saved.id || payload.id, false);
                return saved;
            }).catch(function (error) {
                showToast("保存渠道失败：" + error.message, "error");
                return null;
            });
        });
    }

    function createSchedule(existing) {
        return scheduleDialog(existing || {
            enabled: false, crons: ["0 9 * * *"], target: "last_active_user",
            action: { type: "text", content: "提醒内容" }, condition: null
        }, !existing).then(function (payload) {
            if (!payload) return null;
            return request(organizationApi("/schedules/" + encodeURIComponent(payload.id)), {
            method: "PUT", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
            });
        }).then(function (result) { return result ? refresh() : null; });
    }

    function primaryAction() {
        if (module === "agents") return createAgent();
        if (module === "channels") return createChannel(null);
        if (module === "schedules") return createSchedule(null);
        if (module === "members") {
            return showFormDialog({ title: "邀请成员", fields: [{ name: "role", label: "成员角色", type: "select", value: "member", options: [{ value: "member", label: "成员" }, { value: "admin", label: "管理员" }] }] }).then(function (value) {
                if (!value) return null;
                return request(organizationApi("/invitations"), {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify(value)
                });
            }).then(function (data) {
                if (data) showNoticeDialog("邀请码（请安全发送）", data.invitation_token);
            });
        }
        if (module === "knowledge") {
            return showFormDialog({ title: "添加文本知识", fields: [{ name: "name", label: "名称", required: true }, { name: "content", label: "内容", type: "textarea", rows: 8, required: true }] }).then(function (value) {
                if (!value) return null;
                return request(organizationApi("/knowledge/text"), {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify(value)
                });
            }).then(function (result) { return result ? refresh() : null; });
        }
        if (module === "drive") {
            var input = document.createElement("input");
            input.type = "file";
            input.onchange = function () {
                if (!input.files[0]) return;
                var data = new FormData(); data.append("file", input.files[0]); data.append("path", "");
                request(organizationApi("/drive/upload"), { method: "POST", body: data }).then(refresh);
            };
            input.click();
        }
        return Promise.resolve();
    }

    list.addEventListener("click", function (event) {
        var target = event.target.closest("[data-action]");
        if (!target) return;
        var action = target.getAttribute("data-action");
        var id = target.getAttribute("data-id");
        var current;
        if (module === "agents") current = (state.data.items || []).filter(function (item) { return item.resource_id === id; })[0];
        if (module === "channels") current = (state.data.items || []).filter(function (item) { return item.id === id; })[0];
        if (module === "schedules") current = (state.data.items || []).filter(function (item) { return item.id === id; })[0];
        if (module === "members") current = (state.data.items || []).filter(function (item) { return String(item.user_id) === id; })[0];
        var promise = Promise.resolve();
        try {
        if (action === "agent-toggle") promise = request(organizationApi("/agents/" + encodeURIComponent(id) + "/status"), {
            method: "PATCH", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ enabled: !(current.payload.enabled !== false && current.effective_scope !== "disabled") })
        });
        else if (action === "agent-default") promise = request(organizationApi("/agent-settings/default"), {
            method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ agent_id: id })
        });
        else if (action === "agent-copy") {
            promise = showFormDialog({ title: "复制智能体模板", fields: [{ name: "id", label: "新智能体 ID", value: id + "_copy", required: true }] }).then(function (value) {
                return value ? request(organizationApi("/agents/" + encodeURIComponent(id) + "/copy"), {
                    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(value)
                }) : null;
            });
        } else if (action === "agent-edit") {
            promise = agentDialog(current, false).then(function (payload) {
                return payload ? request(organizationApi("/agents/" + encodeURIComponent(id)), {
                    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload)
                }) : null;
            });
        } else if (action === "agent-knowledge") {
            promise = Promise.all([
                request(organizationApi("/agents/" + encodeURIComponent(id) + "/knowledge-categories")),
                request(organizationApi("/knowledge/categories"))
            ]).then(function (results) {
                var allowed = (results[1].items || []).map(function (item) {
                    return item.category_id;
                });
                return showFormDialog({ title: "知识库授权", fields: [{ name: "category_ids", label: "知识库分类 ID（用逗号分隔，可选：" + allowed.join("、") + "）", value: (results[0].category_ids || []).join(",") }] }).then(function (value) {
                    if (!value) return null;
                    var selected = value.category_ids ? value.category_ids.split(",").map(function (entry) { return entry.trim(); }).filter(Boolean) : [];
                return request(organizationApi("/agents/" + encodeURIComponent(id) + "/knowledge-categories"), {
                    method: "PUT", headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ category_ids: selected })
                });
                });
            });
        } else if (action === "agent-delete") promise = showConfirm("确定删除该组织智能体？").then(function (ok) {
            return ok ? request(organizationApi("/agents/" + encodeURIComponent(id)), { method: "DELETE" }) : null;
        });
        else if (action === "channel-edit") {
            if (current.personal) return editPersonalChannelAgent(current).catch(function (e) { showToast(e.message, "error"); });
            return createChannel(current).catch(function (e) { showToast(e.message, "error"); });
        }
        else if (action === "channel-toggle") {
            var toggleUrl = current.personal
                ? "/api/connections/" + encodeURIComponent(current.connection_id) + "/status"
                : organizationApi("/channels/" + encodeURIComponent(id) + "/status");
            promise = request(toggleUrl, {
                method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: !current.enabled })
            });
        }
        else if (action === "channel-credentials") {
            if (current.personal) {
                if (current.type === "wecom_aibot") {
                    promise = updateChannelWecomCredentials(current, true);
                } else {
                    openChannelScan(current.connection_id, current.type === "feishu" ? "feishu" : "wechat", true);
                }
            } else if (current.type === "wechat_ilink") {
                openChannelScan(current.id, "wechat", false);
            } else if (current.type === "feishu") {
                openChannelScan(current.id, "feishu", false);
            } else {
                promise = updateChannelWecomCredentials(current, false);
            }
        } else if (action === "channel-test") promise = request(organizationApi("/channels/" + encodeURIComponent(id) + "/test"), { method: "POST" }).then(function (result) {
            showToast((result && result.detail) || "渠道测试通过", "success");
            return result;
        });
        else if (action === "channel-delete") {
            if (current.personal) {
                promise = showConfirm("确定删除该个人连接？删除后对应的微信/企微/飞书接入会立即断开。").then(function (ok) {
                    return ok ? request("/api/connections/" + encodeURIComponent(current.connection_id), { method: "DELETE" }) : null;
                });
            } else {
                promise = showConfirm("确定删除该渠道及其组织凭据？").then(function (ok) {
                    return ok ? request(organizationApi("/channels/" + encodeURIComponent(id)), { method: "DELETE" }) : null;
                });
            }
        }
        else if (action === "schedule-edit") return createSchedule(current).catch(function (e) { showToast(e.message, "error"); });
        else if (action === "schedule-run") promise = request(organizationApi("/schedules/" + encodeURIComponent(id) + "/run"), {
            method: "POST"
        }).then(function (result) {
            showToast("已触发执行", "success");
            return result;
        });
        else if (action === "schedule-toggle") promise = request(organizationApi("/schedules/" + encodeURIComponent(id) + "/status"), {
            method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: !current.enabled })
        });
        else if (action === "schedule-delete") promise = showConfirm("确定删除该定时任务？").then(function (ok) {
            return ok ? request(organizationApi("/schedules/" + encodeURIComponent(id)), { method: "DELETE" }) : null;
        });
        else if (action === "member-role") {
            promise = showFormDialog({ title: "调整成员角色", fields: [{ name: "role", label: "成员角色", type: "select", value: current.role, options: [{ value: "member", label: "成员" }, { value: "admin", label: "管理员" }] }] }).then(function (value) {
                return value ? request(organizationApi("/members/" + encodeURIComponent(id)), {
                    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(value)
                }) : null;
            });
        } else if (action === "member-remove") promise = showConfirm("确定移除该组织成员？").then(function (ok) {
            return ok ? request(organizationApi("/members/" + encodeURIComponent(id)), { method: "DELETE" }) : null;
        });
        else if (action === "member-owner") promise = showConfirm("转移后你将变为组织管理员，确定继续？").then(function (ok) {
            return ok ? request(organizationApi("/ownership"), {
                method: "PUT", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ new_owner_user_id: Number(id) })
            }) : null;
        });
        } catch (error) {
            promise = Promise.reject(error);
        }
        promise.then(refresh).catch(function (error) { showToast(error.message, "error"); });
    });

    if (scheduleTabs) {
        Array.prototype.forEach.call(
            scheduleTabs.querySelectorAll(".tab-btn"),
            function (tab) {
                tab.addEventListener("click", function () { activateScheduleTab(tab); });
            }
        );
        document.getElementById("organization-runs-status").addEventListener("change", function () {
            runs.offset = 0;
            loadScheduleRuns();
        });
        document.getElementById("organization-runs-prev").addEventListener("click", function () {
            if (runs.offset < runs.limit) return;
            runs.offset -= runs.limit;
            loadScheduleRuns();
        });
        document.getElementById("organization-runs-next").addEventListener("click", function () {
            if (runs.offset + runs.limit >= runs.total) return;
            runs.offset += runs.limit;
            loadScheduleRuns();
        });
    }

    // Refresh follows the active sub tab; other modules keep runs.tab default.
    document.getElementById("organization-refresh").addEventListener("click", function () {
        if (runs.tab === "runs") {
            loadScheduleRuns();
            return;
        }
        refresh();
    });
    primary.addEventListener("click", function () {
        primaryAction().catch(function (error) { showToast(error.message, "error"); });
    });

    (window.BP_CONTEXT_READY || Promise.resolve()).then(function (me) {
        var definition = definitions[module];
        document.getElementById("organization-module-title").textContent = definition[0];
        document.getElementById("organization-module-description").textContent = definition[1];
        if (!activeOrganizationId()) {
            document.getElementById("organization-name").textContent = "未选择组织";
            document.getElementById("organization-module-description").textContent =
                "加入或选择组织后，可管理该组织的" + definition[0] + "。";
            primary.hidden = true;
            document.getElementById("organization-refresh").disabled = true;
            // Without an organization there is no data source behind the tabs.
            if (scheduleTabs) scheduleTabs.hidden = true;
            summary.textContent = "";
            list.innerHTML = '<div class="organization-empty">当前账号尚未加入组织。请联系组织管理员邀请你加入。</div>';
            return;
        }
        var organization = (me.organizations || []).filter(function (item) {
            return item.organization_id === activeOrganizationId();
        })[0];
        document.getElementById("organization-name").textContent = organization ? organization.name : "组织工作台";
        if (definition[2] &&
                (module === "members" ? canManageOrganization() : canWriteOrganization())) {
            // Remember the permission fact so tab switches can restore the
            // button without re-deriving it.
            runs.primaryAllowed = true;
            primary.hidden = false;
            primary.textContent = definition[2];
        }
        refresh();
    }).catch(function (error) {
        console.error("organization module error:", error, error && error.stack);
        list.innerHTML = '<div class="organization-empty">' + escapeHtml(error.message) + "</div>";
    });
}
