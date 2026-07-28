
/* ===== Plugins page ===== */
var PLUGIN_META = {
    browser_automation: { icon: "B", color: "#4285f4", desc: "Playwright 驱动的浏览器自动化，支持网页快照与交互" },
    codex_tasks: { icon: "C", color: "#10a37f", desc: "Codex 编码任务管理，支持创建、继续和审批" },
    todo: { icon: "T", color: "#f59e0b", desc: "私人待办事项管理，支持增删改查与提醒" }
};

function initTools() {
    var listEl = document.getElementById("plugin-list");
    var modal = document.getElementById("plugin-modal");
    var searchInput = document.getElementById("plugin-search");
    var filterSelect = document.getElementById("plugin-filter-status");
    var allPlugins = [];

    var validTabs = ["skills", "mcp", "plugins", "builtin", "audit"];
    function switchTab() {
        var hash = location.hash.replace("#", "");
        if (validTabs.indexOf(hash) === -1) hash = "builtin";
        var detailPane = document.getElementById("tools-pane-mcp-detail");
        if (detailPane) detailPane.style.display = "none";
        validTabs.forEach(function (t) {
            var pane = document.getElementById("tools-pane-" + t);
            if (pane) pane.style.display = t === hash ? "" : "none";
        });
        document.querySelectorAll(".nav-sub-item").forEach(function (el) {
            el.classList.toggle("active", el.getAttribute("data-tab") === hash);
        });
    }
    switchTab();
    window.addEventListener("hashchange", switchTab);

    loadPlugins();
    loadBuiltinTools();
    loadAuditLogs();
    loadSkills();
    loadMcpServers();

    searchInput.addEventListener("input", renderPlugins);
    filterSelect.addEventListener("change", renderPlugins);

    document.getElementById("plugin-modal-close").addEventListener("click", closeModal);
    document.getElementById("plugin-modal-cancel").addEventListener("click", closeModal);
    modal.addEventListener("click", function (e) { if (e.target === modal) closeModal(); });

    document.getElementById("plugin-edit-settings-btn").addEventListener("click", function () {
        var wrap = document.getElementById("plugin-settings-edit-wrap");
        var view = document.getElementById("plugin-settings-view");
        if (wrap.style.display === "none") {
            wrap.style.display = "";
            view.style.display = "none";
            this.textContent = "取消编辑";
        } else {
            wrap.style.display = "none";
            view.style.display = "";
            this.textContent = "编辑";
        }
    });

    document.getElementById("plugin-save-btn").addEventListener("click", function () {
        var editingId = modal.getAttribute("data-plugin-id");
        var settingsWrap = document.getElementById("plugin-settings-edit-wrap");
        var settings;

        if (settingsWrap.style.display !== "none") {
            var settingsText = document.getElementById("plugin-settings").value.trim();
            settings = {};
            if (settingsText) {
                try {
                    settings = JSON.parse(settingsText);
                } catch (err) {
                    showToast("设置 JSON 格式错误：" + err.message, "error");
                    return;
                }
            }
        } else {
            settings = JSON.parse(document.getElementById("plugin-settings-view").textContent || "{}");
        }

        var payload = {
            enabled: document.getElementById("plugin-enabled").checked,
            settings: settings
        };

        fetch("/api/plugins/" + editingId, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        })
            .then(function (r) {
                if (!r.ok) return r.json().then(function (d) { throw new Error(d.detail); });
                showToast("已保存修改", "success");
                closeModal();
                loadPlugins();
            })
            .catch(function (err) { showToast("保存失败：" + err.message, "error"); });
    });

    function openModal() { modal.style.display = ""; }
    function closeModal() { modal.style.display = "none"; }

    function getMeta(id) {
        return PLUGIN_META[id] || { icon: id.charAt(0).toUpperCase(), color: "#6b7280", desc: "" };
    }

    function loadPlugins() {
        fetch("/api/plugins")
            .then(function (r) { return r.json(); })
            .then(function (plugins) {
                allPlugins = plugins;
                renderPlugins();
            });
    }

    function renderPlugins() {
        var query = searchInput.value.trim().toLowerCase();
        var statusFilter = filterSelect.value;

        var filtered = allPlugins.filter(function (p) {
            if (query && p.id.toLowerCase().indexOf(query) === -1) return false;
            if (statusFilter === "enabled" && !p.enabled) return false;
            if (statusFilter === "disabled" && p.enabled) return false;
            return true;
        });

        if (!filtered.length) {
            listEl.innerHTML = '<div class="empty-state">' +
                (allPlugins.length ? "未找到匹配的插件" : "暂无已注册插件") + "</div>";
            return;
        }

        listEl.innerHTML = filtered.map(function (p) {
            var meta = getMeta(p.id);
            var statusBadge = p.enabled
                ? '<span class="badge badge-success">已启用</span>'
                : '<span class="badge badge-muted">已禁用</span>';

            return '<div class="plugin-tile" data-id="' + p.id + '">' +
                '<div class="plugin-tile-header">' +
                    '<div class="plugin-avatar" style="background:' + meta.color + '">' + meta.icon + "</div>" +
                    '<div class="plugin-tile-info">' +
                        '<div class="plugin-tile-name">' + escapeHtml(p.id) + "</div>" +
                        '<div class="plugin-tile-meta">' + statusBadge +
                        '<span class="text-muted">' + p.tool_count + " 个工具</span></div>" +
                    "</div>" +
                "</div>" +
                '<p class="plugin-tile-desc">' + escapeHtml(meta.desc) + "</p>" +
                '<div class="plugin-tile-tags">' +
                    p.tools.map(function (t) {
                        return '<span class="tag' + (t.requires_approval ? " tag-warning" : "") + '">' +
                            escapeHtml(t.name) + "</span>";
                    }).join("") +
                "</div>" +
            "</div>";
        }).join("");
    }

    listEl.addEventListener("click", function (e) {
        var tile = e.target.closest(".plugin-tile");
        if (!tile) return;
        var id = tile.getAttribute("data-id");
        openPluginDetail(id);
    });

    function openPluginDetail(id) {
        var p = allPlugins.find(function (x) { return x.id === id; });
        if (!p) return;
        var meta = getMeta(p.id);

        modal.setAttribute("data-plugin-id", id);
        document.getElementById("plugin-modal-icon").textContent = meta.icon;
        document.getElementById("plugin-modal-icon").style.background = meta.color;
        document.getElementById("plugin-modal-title").textContent = p.id;
        document.getElementById("plugin-modal-subtitle").textContent = meta.desc;
        document.getElementById("plugin-enabled").checked = p.enabled;
        document.getElementById("plugin-status-text").textContent = p.enabled ? "启用" : "禁用";
        document.getElementById("plugin-tool-count").textContent = p.tool_count;

        var toolsHtml = p.tools.map(function (t) {
            var approvalBadge = t.requires_approval
                ? '<span class="badge badge-warning">需审批</span>'
                : '<span class="badge badge-muted">自动</span>';
            return '<div class="tool-def-item">' +
                '<div class="tool-def-header">' +
                    '<code class="tool-def-name">' + escapeHtml(t.name) + "</code>" +
                    approvalBadge +
                "</div>" +
                '<p class="tool-def-desc">' + escapeHtml(t.description) + "</p>" +
                '<details class="tool-def-params"><summary>参数定义</summary>' +
                "<pre>" + escapeHtml(JSON.stringify(t.parameters, null, 2)) + "</pre></details>" +
            "</div>";
        }).join("");

        document.getElementById("plugin-tools-table").innerHTML = toolsHtml || '<p class="text-muted">无工具定义</p>';

        var settingsJson = JSON.stringify(p.settings, null, 2);
        document.getElementById("plugin-settings-view").textContent = settingsJson;
        document.getElementById("plugin-settings-view").style.display = "";
        document.getElementById("plugin-settings-edit-wrap").style.display = "none";
        document.getElementById("plugin-settings").value = settingsJson;
        document.getElementById("plugin-edit-settings-btn").textContent = "编辑";

        var enabledCheckbox = document.getElementById("plugin-enabled");
        enabledCheckbox.onchange = function () {
            document.getElementById("plugin-status-text").textContent = this.checked ? "启用" : "禁用";
        };

        openModal();
    }

    function loadBuiltinTools() {
        fetch("/api/tools")
            .then(function (r) { return r.json(); })
            .then(function (tools) {
                var container = document.getElementById("builtin-tools-list");
                var countEl = document.getElementById("builtin-tools-count");
                if (!tools.length) {
                    container.innerHTML = '<p class="text-muted">暂无内置工具</p>';
                    return;
                }
                countEl.textContent = "（" + tools.length + "）";

                var categories = {};
                tools.forEach(function (t) {
                    if (!categories[t.category]) categories[t.category] = [];
                    categories[t.category].push(t);
                });

                container.innerHTML = Object.keys(categories).map(function (cat) {
                    var items = categories[cat].map(function (t) {
                        var badges = t.available
                            ? '<span class="badge badge-success">可用</span>'
                            : '<span class="badge badge-muted">不可用</span>';
                        if (t.requires_approval) {
                            badges += ' <span class="badge badge-warning">需审批</span>';
                        }
                        var toggle = '<label class="switch-label">' +
                            '<input type="checkbox" class="tool-toggle" data-tool="' + escapeHtml(t.name) + '"' +
                            (t.enabled ? " checked" : "") + ">" +
                            '<span class="switch switch-sm"></span>' +
                            "</label>";
                        return '<div class="builtin-tool-item">' +
                            '<div class="builtin-tool-header">' +
                                '<code class="builtin-tool-name">' + escapeHtml(t.name) + "</code>" +
                                '<div class="builtin-tool-badges">' + toggle + badges + "</div>" +
                            "</div>" +
                            '<p class="builtin-tool-desc">' + escapeHtml(t.description) + "</p>" +
                        "</div>";
                    }).join("");
                    return '<div class="builtin-tool-category">' +
                        '<div class="builtin-tool-category-title">' + escapeHtml(cat) + "</div>" +
                        '<div class="builtin-tool-items">' + items + "</div>" +
                    "</div>";
                }).join("");

                container.querySelectorAll(".tool-toggle").forEach(function (cb) {
                    cb.addEventListener("change", function () {
                        var toolName = this.getAttribute("data-tool");
                        var enabled = this.checked;
                        fetch("/api/tools/" + encodeURIComponent(toolName), {
                            method: "PATCH",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ enabled: enabled }),
                        })
                            .then(function (r) {
                                if (!r.ok) return r.json().then(function (d) { throw new Error(d.detail); });
                                showToast(enabled ? "已启用 " + toolName : "已禁用 " + toolName, "success");
                                loadBuiltinTools();
                            })
                            .catch(function (err) { showToast("操作失败：" + err.message, "error"); });
                    });
                });
            });
    }

    var auditOffset = 0;
    var auditLimit = 20;

    function loadAuditLogs(append) {
        fetch("/api/tools/audit?limit=" + auditLimit + "&offset=" + auditOffset)
            .then(function (r) { return r.json(); })
            .then(function (data) {
                var container = document.getElementById("tool-audit-list");
                var loadMoreWrap = document.getElementById("audit-load-more-wrap");
                var items = data.items || [];
                if (!items.length && !append) {
                    container.innerHTML = '<p class="text-muted">暂无审计记录</p>';
                    loadMoreWrap.style.display = "none";
                    return;
                }
                var html = items.map(function (item) {
                    var statusBadge = item.status === "成功"
                        ? '<span class="badge badge-success">成功</span>'
                        : '<span class="badge badge-warning">失败</span>';
                    var ts = item.ts ? item.ts.replace("T", " ").substring(0, 19) : "";
                    return '<div class="tool-audit-row' + (item.status !== "成功" ? " audit-row-fail" : "") + '">' +
                        '<span class="audit-ts">' + escapeHtml(ts) + "</span>" +
                        '<code class="audit-tool">' + escapeHtml(item.tool_name) + "</code>" +
                        statusBadge +
                        '<span class="audit-duration">' + (item.duration_ms || 0) + "ms</span>" +
                        (item.error ? '<span class="audit-error">' + escapeHtml(item.error) + "</span>" : "") +
                    "</div>";
                }).join("");
                if (append) {
                    container.innerHTML += html;
                } else {
                    container.innerHTML = html;
                }
                loadMoreWrap.style.display = (auditOffset + items.length < data.total) ? "" : "none";
            });
    }

    var auditLoadMoreBtn = document.getElementById("audit-load-more");
    if (auditLoadMoreBtn) {
        auditLoadMoreBtn.addEventListener("click", function () {
            auditOffset += auditLimit;
            loadAuditLogs(true);
        });
    }

    /* ---- Skill 技能 ---- */
    var skillModal = document.getElementById("skill-modal");
    var skillEditingId = null;

    document.getElementById("create-skill-btn").addEventListener("click", function () {
        skillEditingId = null;
        document.getElementById("skill-modal-title").textContent = "新建技能";
        document.getElementById("skill-id-group").style.display = "";
        document.getElementById("skill-form").reset();
        document.getElementById("skill-enabled").checked = true;
        document.getElementById("skill-desc-count").textContent = "0";
        document.getElementById("skill-submit-btn").textContent = "立即创建";
        skillModal.style.display = "";
    });

    document.getElementById("skill-modal-close").addEventListener("click", function () { skillModal.style.display = "none"; });
    document.getElementById("skill-modal-cancel").addEventListener("click", function () { skillModal.style.display = "none"; });
    skillModal.addEventListener("click", function (e) { if (e.target === skillModal) skillModal.style.display = "none"; });

    document.getElementById("skill-description").addEventListener("input", function () {
        document.getElementById("skill-desc-count").textContent = this.value.length;
    });

    document.getElementById("skill-fill-example").addEventListener("click", function () {
        document.getElementById("skill-prompt").value =
            "你是一个多语言翻译专家。\n\n" +
            "## 任务\n当用户给出文本时，将其翻译为目标语言。\n\n" +
            "## 规则\n- 保持原文的语气和格式\n- 专有名词保留原文\n- 若未指定目标语言，默认翻译为英文\n\n" +
            "## 输出\n仅输出翻译结果，不要附加解释。";
    });

    document.getElementById("skill-form").addEventListener("submit", function (e) {
        e.preventDefault();
        var id = document.getElementById("skill-id").value.trim();
        var name = document.getElementById("skill-name").value.trim();
        var description = document.getElementById("skill-description").value.trim();
        var prompt = document.getElementById("skill-prompt").value.trim();
        var enabled = document.getElementById("skill-enabled").checked;
        if (!name || !prompt) { showToast("名称和指令不能为空", "error"); return; }

        var payload = { name: name, description: description, prompt: prompt, enabled: enabled };
        var method, url;
        if (skillEditingId) {
            method = "PUT"; url = "/api/skills/" + encodeURIComponent(skillEditingId);
        } else {
            method = "POST"; url = "/api/skills"; payload.id = id;
        }
        fetch(url, { method: method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) })
            .then(function (r) { if (!r.ok) return r.json().then(function (d) { throw new Error(d.detail); }); return r.json(); })
            .then(function () { showToast(skillEditingId ? "已更新技能" : "已创建技能", "success"); skillModal.style.display = "none"; loadSkills(); })
            .catch(function (err) { showToast("操作失败：" + err.message, "error"); });
    });

    function loadSkills() {
        fetch("/api/skills").then(function (r) { return r.json(); }).then(function (skills) {
            var container = document.getElementById("skill-list");
            if (!skills.length) { container.innerHTML = '<div class="empty-state">暂无技能，点击"新建技能"创建</div>'; return; }
            container.innerHTML = skills.map(function (s) {
                var badge = s.enabled ? '<span class="badge badge-success">已启用</span>' : '<span class="badge badge-muted">已禁用</span>';
                return '<div class="plugin-tile" data-skill-id="' + escapeHtml(s.id) + '">' +
                    '<div class="plugin-tile-header">' +
                        '<div class="plugin-avatar" style="background:#6366f1">S</div>' +
                        '<div class="plugin-tile-info">' +
                            '<div class="plugin-tile-name">' + escapeHtml(s.name) + "</div>" +
                            '<div class="plugin-tile-meta">' + badge +
                            '<span class="text-muted">' + escapeHtml(s.id) + "</span></div>" +
                        "</div>" +
                    "</div>" +
                    '<p class="plugin-tile-desc">' + escapeHtml(s.description || "") + "</p>" +
                    '<div class="plugin-tile-tags"><span class="tag">prompt</span></div>' +
                "</div>";
            }).join("");
        });
    }

    document.addEventListener("click", function (e) {
        var tile = e.target.closest("[data-skill-id]");
        if (!tile) return;
        var id = tile.getAttribute("data-skill-id");
        fetch("/api/skills").then(function (r) { return r.json(); }).then(function (skills) {
            var s = skills.find(function (x) { return x.id === id; });
            if (!s) return;
            skillEditingId = id;
            document.getElementById("skill-modal-title").textContent = "编辑技能";
            document.getElementById("skill-id-group").style.display = "none";
            document.getElementById("skill-name").value = s.name;
            document.getElementById("skill-description").value = s.description;
            document.getElementById("skill-prompt").value = s.prompt;
            document.getElementById("skill-enabled").checked = s.enabled;
            document.getElementById("skill-desc-count").textContent = (s.description || "").length;
            document.getElementById("skill-submit-btn").textContent = "保存";
            skillModal.style.display = "";
        });
    });

    /* ---- MCP 服务 ---- */
    var mcpModal = document.getElementById("mcp-modal");
    var mcpEditingId = null;

    document.getElementById("create-mcp-btn").addEventListener("click", function () {
        mcpEditingId = null;
        document.getElementById("mcp-modal-title").textContent = "添加 MCP 服务";
        document.getElementById("mcp-id-group").style.display = "";
        document.getElementById("mcp-form").reset();
        document.getElementById("mcp-enabled").checked = true;
        document.getElementById("mcp-transport").value = "stdio";
        document.getElementById("mcp-submit-btn").textContent = "立即创建";
        toggleMcpTransport();
        mcpModal.style.display = "";
    });

    function toggleMcpTransport() {
        var transport = document.getElementById("mcp-transport").value;
        var isStdio = transport === "stdio";
        document.getElementById("mcp-command-group").style.display = isStdio ? "" : "none";
        document.getElementById("mcp-args-group").style.display = isStdio ? "" : "none";
        document.getElementById("mcp-url-group").style.display = isStdio ? "none" : "";
        document.getElementById("mcp-headers-group").style.display = isStdio ? "none" : "";
        document.querySelectorAll("#mcp-transport-selector .transport-option").forEach(function (btn) {
            btn.classList.toggle("active", btn.getAttribute("data-transport") === transport);
        });
    }
    document.querySelectorAll("#mcp-transport-selector .transport-option").forEach(function (btn) {
        btn.addEventListener("click", function () {
            document.getElementById("mcp-transport").value = this.getAttribute("data-transport");
            toggleMcpTransport();
        });
    });

    document.getElementById("mcp-modal-close").addEventListener("click", function () { mcpModal.style.display = "none"; });
    document.getElementById("mcp-modal-cancel").addEventListener("click", function () { mcpModal.style.display = "none"; });
    mcpModal.addEventListener("click", function (e) { if (e.target === mcpModal) mcpModal.style.display = "none"; });

    document.getElementById("mcp-form").addEventListener("submit", function (e) {
        e.preventDefault();
        var id = document.getElementById("mcp-id").value.trim();
        var name = document.getElementById("mcp-name").value.trim();
        var transport = document.getElementById("mcp-transport").value;
        var command = document.getElementById("mcp-command").value.trim();
        var argsText = document.getElementById("mcp-args").value.trim();
        var url = document.getElementById("mcp-url").value.trim();
        var headersText = document.getElementById("mcp-headers").value.trim();
        var enabled = document.getElementById("mcp-enabled").checked;
        if (!name) { showToast("名称不能为空", "error"); return; }
        var headers = {};
        if (headersText) {
            try { headers = JSON.parse(headersText); }
            catch (err) { showToast("请求头必须是合法的 JSON 键值对", "error"); return; }
        }
        var args = argsText ? argsText.split(/\s+/) : [];
        var payload = { name: name, transport: transport, enabled: enabled };
        if (transport === "stdio") { payload.command = command; payload.args = args; }
        else { payload.url = url; payload.headers = headers; }
        var method, apiUrl;
        if (mcpEditingId) { method = "PUT"; apiUrl = "/api/mcp/" + encodeURIComponent(mcpEditingId); }
        else { method = "POST"; apiUrl = "/api/mcp"; payload.id = id; }
        fetch(apiUrl, { method: method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) })
            .then(function (r) { if (!r.ok) return r.json().then(function (d) { throw new Error(d.detail); }); return r.json(); })
            .then(function () { showToast(mcpEditingId ? "已更新 MCP 服务" : "已添加 MCP 服务", "success"); mcpModal.style.display = "none"; loadMcpServers(); })
            .catch(function (err) { showToast("操作失败：" + err.message, "error"); });
    });

    function loadMcpServers() {
        fetch("/api/mcp").then(function (r) { return r.json(); }).then(function (servers) {
            var container = document.getElementById("mcp-list");
            if (!servers.length) { container.innerHTML = '<div class="empty-state">暂无 MCP 服务，点击"添加服务"创建</div>'; return; }
            container.innerHTML = servers.map(function (s) {
                var badge = s.enabled ? '<span class="badge badge-success">已启用</span>' : '<span class="badge badge-muted">已禁用</span>';
                var transportTag = s.transport === "stdio"
                    ? '<span class="tag">' + escapeHtml(s.command || "") + "</span>"
                    : '<span class="tag">' + escapeHtml(s.url || "") + "</span>";
                return '<div class="plugin-tile" data-mcp-id="' + escapeHtml(s.id) + '">' +
                    '<div class="plugin-tile-header">' +
                        '<div class="plugin-avatar" style="background:#0ea5e9">M</div>' +
                        '<div class="plugin-tile-info">' +
                            '<div class="plugin-tile-name">' + escapeHtml(s.name) + "</div>" +
                            '<div class="plugin-tile-meta">' + badge +
                            '<span class="text-muted">' + escapeHtml(mcpTransportLabel(s.transport)) + "</span></div>" +
                        "</div>" +
                    "</div>" +
                    '<div class="plugin-tile-tags">' + transportTag + "</div>" +
                "</div>";
            }).join("");
        });
    }

    function openMcpEdit(s) {
        mcpEditingId = s.id;
        document.getElementById("mcp-modal-title").textContent = "编辑 MCP 服务";
        document.getElementById("mcp-id-group").style.display = "none";
        document.getElementById("mcp-name").value = s.name;
        document.getElementById("mcp-transport").value = s.transport;
        document.getElementById("mcp-command").value = s.command || "";
        document.getElementById("mcp-args").value = (s.args || []).join(" ");
        document.getElementById("mcp-url").value = s.url || "";
        var hdrs = s.headers || {};
        document.getElementById("mcp-headers").value = Object.keys(hdrs).length ? JSON.stringify(hdrs) : "";
        document.getElementById("mcp-enabled").checked = s.enabled;
        document.getElementById("mcp-submit-btn").textContent = "保存";
        toggleMcpTransport();
        mcpModal.style.display = "";
    }

    var currentMcpServer = null;
    var mcpDetailTools = {};
    var mcpListPane = document.getElementById("tools-pane-mcp");
    var mcpDetailPane = document.getElementById("tools-pane-mcp-detail");

    function showMcpList() {
        mcpDetailPane.style.display = "none";
        mcpListPane.style.display = "";
        loadMcpServers();
    }

    function switchMcpDetailTab(tab) {
        document.getElementById("mcp-detail-overview").style.display = tab === "overview" ? "" : "none";
        document.getElementById("mcp-detail-tools").style.display = tab === "tools" ? "" : "none";
        document.querySelectorAll(".mcp-detail-subtab").forEach(function (btn) {
            btn.classList.toggle("active", btn.getAttribute("data-detail-tab") === tab);
        });
    }

    function buildMcpConfigJson(s) {
        var entry = { transportType: s.transport };
        if (s.transport === "stdio") {
            entry.command = s.command || "";
            if (s.args && s.args.length) entry.args = s.args;
        } else {
            entry.url = s.url || "";
            if (s.headers && Object.keys(s.headers).length) entry.headers = s.headers;
        }
        var obj = { mcpServers: {} };
        obj.mcpServers[s.id] = entry;
        return JSON.stringify(obj, null, 2);
    }

    function openMcpDetail(id) {
        fetch("/api/mcp").then(function (r) { return r.json(); }).then(function (servers) {
            var s = servers.find(function (x) { return x.id === id; });
            if (!s) return;
            currentMcpServer = s;
            document.getElementById("mcp-detail-title").textContent = s.name;
            var status = s.enabled
                ? '<span class="badge badge-success">已启用</span>'
                : '<span class="badge badge-muted">已禁用</span>';
            var rows = [
                ["名称", escapeHtml(s.name)],
                ["ID", escapeHtml(s.id)],
                ["连接类型", escapeHtml(mcpTransportLabel(s.transport))],
                ["状态", status]
            ];
            if (s.transport === "stdio") rows.push(["命令", escapeHtml(s.command || "")]);
            else rows.push(["服务地址", escapeHtml(s.url || "")]);
            document.getElementById("mcp-detail-info").innerHTML = rows.map(function (r) {
                return '<div class="mcp-info-item"><span class="mcp-info-label">' + r[0] +
                    '</span><span class="mcp-info-value">' + r[1] + "</span></div>";
            }).join("");
            document.getElementById("mcp-detail-config").textContent = buildMcpConfigJson(s);
            mcpListPane.style.display = "none";
            mcpDetailPane.style.display = "";
            switchMcpDetailTab("overview");
            loadMcpDetailTools(id);
        });
    }

    function renderMcpToolFields(params) {
        params = params || {};
        var props = params.properties || {};
        var required = params.required || [];
        var keys = Object.keys(props);
        if (!keys.length) {
            return '<div class="mcp-field-empty">此工具无需参数</div>';
        }
        return keys.map(function (key) {
            var spec = props[key] || {};
            var type = spec.type || "string";
            var isRequired = required.indexOf(key) !== -1;
            var reqMark = isRequired ? '<span class="mcp-field-req">*</span>' : "";
            var desc = spec.description
                ? '<div class="mcp-field-desc">' + escapeHtml(spec.description) + "</div>" : "";
            var control;
            if (type === "boolean") {
                control = '<select class="mcp-field-input"><option value=""></option>' +
                    '<option value="true">true</option><option value="false">false</option></select>';
            } else if (type === "number" || type === "integer") {
                control = '<input type="number" class="mcp-field-input" placeholder="' + escapeHtml(type) + '">';
            } else if (type === "array" || type === "object") {
                control = '<textarea class="mcp-field-input" rows="2" placeholder="' +
                    (type === "array" ? "[...]（JSON）" : "{...}（JSON）") + '"></textarea>';
            } else {
                control = '<input type="text" class="mcp-field-input" placeholder="' + escapeHtml(type) + '">';
            }
            return '<div class="mcp-field" data-key="' + escapeHtml(key) + '" data-type="' + escapeHtml(type) + '">' +
                '<label class="mcp-field-label"><span class="mcp-field-name">' + escapeHtml(key) + "</span>" +
                reqMark + '<span class="mcp-field-type">' + escapeHtml(type) + "</span></label>" +
                desc + control +
            "</div>";
        }).join("");
    }

    function loadMcpDetailTools(id) {
        var listEl = document.getElementById("mcp-detail-tools-list");
        var countEl = document.getElementById("mcp-detail-tools-count");
        listEl.innerHTML = '<p class="text-muted">加载中…</p>';
        countEl.textContent = "";
        fetch("/api/mcp/" + encodeURIComponent(id) + "/tools")
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data.error && (!data.tools || !data.tools.length)) {
                    listEl.innerHTML = '<div class="empty-state">连接失败：' + escapeHtml(data.error) + "</div>";
                    return;
                }
                var tools = data.tools || [];
                countEl.textContent = "共 " + tools.length + " 个工具";
                mcpDetailTools = {};
                document.getElementById("mcp-tool-debug").innerHTML =
                    '<div class="mcp-debug-empty">选择左侧工具进行调试</div>';
                if (!tools.length) {
                    listEl.innerHTML = '<div class="empty-state">该服务未暴露工具</div>';
                    return;
                }
                listEl.innerHTML = tools.map(function (t) {
                    mcpDetailTools[t.name] = t;
                    return '<div class="mcp-tool-item" data-tool="' + escapeHtml(t.name) + '">' +
                        '<code class="mcp-tool-item-name">' + escapeHtml(t.name) + "</code>" +
                        '<span class="mcp-tool-desc">' + escapeHtml(t.description || "") + "</span>" +
                    "</div>";
                }).join("");
            })
            .catch(function () {
                listEl.innerHTML = '<div class="empty-state">加载工具失败</div>';
            });
    }

    function runMcpTool(toolName, fieldsEl, outputEl, btn) {
        var args = {};
        var fields = fieldsEl ? fieldsEl.querySelectorAll(".mcp-field") : [];
        for (var i = 0; i < fields.length; i++) {
            var field = fields[i];
            var key = field.getAttribute("data-key");
            var type = field.getAttribute("data-type");
            var input = field.querySelector(".mcp-field-input");
            var raw = (input.value || "").trim();
            if (raw === "") continue;
            if (type === "boolean") {
                args[key] = raw === "true";
            } else if (type === "number" || type === "integer") {
                var num = Number(raw);
                if (isNaN(num)) { outputEl.textContent = "参数 " + key + " 必须是数字"; return; }
                args[key] = num;
            } else if (type === "array" || type === "object") {
                try { args[key] = JSON.parse(raw); }
                catch (err) { outputEl.textContent = "参数 " + key + " 必须是合法 JSON"; return; }
            } else {
                args[key] = raw;
            }
        }
        btn.disabled = true;
        outputEl.textContent = "运行中…";
        fetch("/api/mcp/" + encodeURIComponent(currentMcpServer.id) + "/tools/" +
              encodeURIComponent(toolName) + "/invoke", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ arguments: args })
        })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data.ok) {
                    outputEl.textContent = typeof data.result === "string"
                        ? data.result : JSON.stringify(data.result, null, 2);
                } else {
                    outputEl.textContent = "错误：" + (data.error || "调用失败");
                }
            })
            .catch(function () { outputEl.textContent = "请求失败"; })
            .finally(function () { btn.disabled = false; });
    }

    function openMcpDebug(toolName) {
        var t = mcpDetailTools[toolName];
        if (!t) return;
        document.getElementById("mcp-tool-debug").innerHTML =
            '<div class="mcp-debug-title">调试工具 · <code>' + escapeHtml(t.name) + "</code></div>" +
            (t.description ? '<div class="mcp-debug-desc">' + escapeHtml(t.description) + "</div>" : "") +
            '<div class="mcp-tool-label">参数</div>' +
            '<div class="mcp-tool-fields">' + renderMcpToolFields(t.parameters) + "</div>" +
            '<div class="mcp-tool-run-row"><button class="btn-primary btn-sm mcp-tool-run">运行</button></div>' +
            '<div class="mcp-tool-label">输出</div>' +
            "<pre class=\"mcp-tool-output\">--</pre>";
    }

    document.getElementById("mcp-detail-back").addEventListener("click", showMcpList);
    document.querySelectorAll(".mcp-detail-subtab").forEach(function (btn) {
        btn.addEventListener("click", function () {
            switchMcpDetailTab(this.getAttribute("data-detail-tab"));
        });
    });
    document.getElementById("mcp-detail-edit").addEventListener("click", function () {
        if (currentMcpServer) openMcpEdit(currentMcpServer);
    });
    document.getElementById("mcp-detail-copy").addEventListener("click", function () {
        var text = document.getElementById("mcp-detail-config").textContent;
        navigator.clipboard.writeText(text).then(function () {
            showToast("已复制配置", "success");
        }, function () { showToast("复制失败", "error"); });
    });
    var mcpToolsListEl = document.getElementById("mcp-detail-tools-list");
    mcpToolsListEl.addEventListener("click", function (e) {
        var item = e.target.closest(".mcp-tool-item");
        if (!item) return;
        mcpToolsListEl.querySelectorAll(".mcp-tool-item").forEach(function (el) {
            el.classList.remove("active");
        });
        item.classList.add("active");
        openMcpDebug(item.getAttribute("data-tool"));
    });
    document.getElementById("mcp-tool-debug").addEventListener("click", function (e) {
        if (!e.target.classList.contains("mcp-tool-run")) return;
        var panel = document.getElementById("mcp-tool-debug");
        var active = mcpToolsListEl.querySelector(".mcp-tool-item.active");
        if (!active) return;
        runMcpTool(
            active.getAttribute("data-tool"),
            panel.querySelector(".mcp-tool-fields"),
            panel.querySelector(".mcp-tool-output"),
            e.target
        );
    });

    document.addEventListener("click", function (e) {
        var tile = e.target.closest("[data-mcp-id]");
        if (!tile) return;
        openMcpDetail(tile.getAttribute("data-mcp-id"));
    });
}
