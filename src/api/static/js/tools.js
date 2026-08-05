
/* ===== Plugins page ===== */
function initTools() {
    if (location.hash === "#plugins") {
        location.replace("/plugins");
        return;
    }
    var auditOffset = 0;
    var auditLimit = 20;
    var auditSearchTimer = null;
    var auditRequestSeq = 0;
    var allBuiltinTools = [];
    var builtinCategory = "全部";
    var builtinTabsEl = document.getElementById("builtin-category-tabs");
    var builtinListEl = document.getElementById("builtin-tools-list");

    var validTabs = ["skills", "mcp", "builtin", "audit"];
    function switchTab() {
        var hash = location.hash.replace("#", "") || window.BP_INITIAL_TOOLS_TAB;
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
        // Audit data is loaded lazily and refreshed on every tab entry.
        if (hash === "audit") {
            auditOffset = 0;
            loadAuditLogs(false);
        }
    }
    switchTab();
    window.addEventListener("hashchange", switchTab);

    loadBuiltinTools();
    loadSkills();
    loadMcpServers();

    builtinTabsEl.addEventListener("click", function (event) {
        var tab = event.target.closest("[data-tool-category]");
        if (!tab) return;
        builtinCategory = tab.getAttribute("data-tool-category");
        renderBuiltinCategoryTabs();
        renderBuiltinTools();
    });
    builtinTabsEl.addEventListener("keydown", function (event) {
        if (["ArrowLeft", "ArrowRight", "Home", "End"].indexOf(event.key) === -1) return;
        var tabs = Array.prototype.slice.call(builtinTabsEl.querySelectorAll("[role='tab']"));
        var current = tabs.indexOf(document.activeElement);
        if (current === -1) return;
        event.preventDefault();
        var next = current;
        if (event.key === "ArrowLeft") next = (current - 1 + tabs.length) % tabs.length;
        if (event.key === "ArrowRight") next = (current + 1) % tabs.length;
        if (event.key === "Home") next = 0;
        if (event.key === "End") next = tabs.length - 1;
        tabs[next].focus();
        tabs[next].click();
    });

    function builtinCategories() {
        var seen = {};
        return allBuiltinTools.reduce(function (categories, tool) {
            if (!seen[tool.category]) {
                seen[tool.category] = true;
                categories.push(tool.category);
            }
            return categories;
        }, []);
    }

    function renderBuiltinCategoryTabs() {
        var categories = ["全部"].concat(builtinCategories());
        if (categories.indexOf(builtinCategory) === -1) builtinCategory = "全部";
        builtinTabsEl.innerHTML = categories.map(function (category, index) {
            var count = category === "全部"
                ? allBuiltinTools.length
                : allBuiltinTools.filter(function (tool) { return tool.category === category; }).length;
            var active = category === builtinCategory;
            return '<button id="builtin-category-tab-' + index + '" class="tab-btn' +
                (active ? " active" : "") + '" type="button" role="tab" data-tool-category="' +
                escapeHtml(category) + '" aria-selected="' + (active ? "true" : "false") +
                '" aria-controls="builtin-tools-list" tabindex="' + (active ? "0" : "-1") + '">' +
                escapeHtml(category) + '<span class="builtin-tab-count">' + count + "</span></button>";
        }).join("");
        var activeIndex = categories.indexOf(builtinCategory);
        builtinListEl.setAttribute("aria-labelledby", "builtin-category-tab-" + activeIndex);
    }

    function renderBuiltinTools() {
        var countEl = document.getElementById("builtin-tools-count");
        countEl.textContent = "（" + allBuiltinTools.length + "）";
        if (!allBuiltinTools.length) {
            builtinListEl.innerHTML = '<p class="text-muted">暂无内置工具</p>';
            return;
        }

        var visibleTools = builtinCategory === "全部"
            ? allBuiltinTools
            : allBuiltinTools.filter(function (tool) { return tool.category === builtinCategory; });
        var categories = {};
        visibleTools.forEach(function (tool) {
            if (!categories[tool.category]) categories[tool.category] = [];
            categories[tool.category].push(tool);
        });

        builtinListEl.innerHTML = Object.keys(categories).map(function (category) {
            var items = categories[category].map(function (tool) {
                var badges = tool.available
                    ? '<span class="badge badge-success">可用</span>'
                    : '<span class="badge badge-muted">不可用</span>';
                if (tool.requires_approval) {
                    badges += ' <span class="badge badge-warning">需审批</span>';
                }
                if (tool.source_type === "plugin") {
                    badges += ' <span class="badge badge-muted">来源：' +
                        escapeHtml(tool.source_id || "plugin") + "</span>";
                }
                var toggle = '<label class="switch-label" title="启用/禁用">' +
                    '<input type="checkbox" class="tool-toggle" data-tool="' + escapeHtml(tool.name) + '"' +
                    (tool.enabled ? " checked" : "") + ">" +
                    '<span class="switch switch-sm"></span>' +
                    '<span class="text-muted">启用</span>' +
                    "</label>";
                var approvalToggle = '<label class="switch-label" title="要求审批">' +
                    '<input type="checkbox" class="tool-approval-toggle" data-tool="' +
                    escapeHtml(tool.name) + '"' +
                    (tool.requires_approval ? " checked" : "") +
                    (tool.approval_policy === "required" ? " disabled" : "") + ">" +
                    '<span class="switch switch-sm"></span>' +
                    '<span class="text-muted">审批</span></label>';
                return '<div class="builtin-tool-item">' +
                    '<div class="builtin-tool-header">' +
                        '<code class="builtin-tool-name">' + escapeHtml(tool.name) + "</code>" +
                        '<div class="builtin-tool-badges">' + toggle +
                        approvalToggle + badges + "</div>" +
                    "</div>" +
                    '<p class="builtin-tool-desc">' + escapeHtml(tool.description) + "</p>" +
                "</div>";
            }).join("");
            var title = builtinCategory === "全部"
                ? '<div class="builtin-tool-category-title">' + escapeHtml(category) + "</div>"
                : "";
            return '<div class="builtin-tool-category">' + title +
                '<div class="builtin-tool-items">' + items + "</div></div>";
        }).join("");

        builtinListEl.querySelectorAll(".tool-toggle").forEach(function (checkbox) {
            checkbox.addEventListener("change", function () {
                var toolName = this.getAttribute("data-tool");
                var enabled = this.checked;
                fetch("/api/tools/" + encodeURIComponent(toolName), {
                    method: "PATCH",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ enabled: enabled }),
                })
                    .then(function (response) {
                        if (!response.ok) {
                            return response.json().then(function (data) { throw new Error(data.detail); });
                        }
                        showToast(enabled ? "已启用 " + toolName : "已禁用 " + toolName, "success");
                        loadBuiltinTools();
                    })
                    .catch(function (error) {
                        showToast("操作失败：" + error.message, "error");
                        renderBuiltinTools();
                    });
            });
        });
        builtinListEl.querySelectorAll(".tool-approval-toggle").forEach(function (checkbox) {
            checkbox.addEventListener("change", function () {
                var toolName = this.getAttribute("data-tool");
                var requireApproval = this.checked;
                fetch("/api/tools/" + encodeURIComponent(toolName), {
                    method: "PATCH",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ require_approval: requireApproval }),
                })
                    .then(function (response) {
                        if (!response.ok) {
                            return response.json().then(function (data) {
                                throw new Error(data.detail);
                            });
                        }
                        showToast("已更新 " + toolName + " 的审批策略", "success");
                        loadBuiltinTools();
                    })
                    .catch(function (error) {
                        showToast("操作失败：" + error.message, "error");
                        renderBuiltinTools();
                    });
            });
        });
    }

    function loadBuiltinTools() {
        fetch("/api/tools")
            .then(function (response) {
                if (!response.ok) throw new Error("请求失败");
                return response.json();
            })
            .then(function (tools) {
                allBuiltinTools = tools;
                renderBuiltinCategoryTabs();
                renderBuiltinTools();
            })
            .catch(function (error) {
                showToast("加载内置工具失败：" + error.message, "error");
            });
    }

    function formatAuditTime(ts) {
        if (!ts) return "";
        var date = new Date(ts);
        if (isNaN(date.getTime())) return ts;
        function pad(n) { return n < 10 ? "0" + n : "" + n; }
        return date.getFullYear() + "-" + pad(date.getMonth() + 1) + "-" + pad(date.getDate()) +
            " " + pad(date.getHours()) + ":" + pad(date.getMinutes()) + ":" + pad(date.getSeconds());
    }

    function renderAuditEntry(item) {
        var statusBadge = item.status === "成功"
            ? '<span class="badge badge-success">成功</span>'
            : '<span class="badge badge-danger">失败</span>';
        var owner = [item.tenant_id, item.agent_id].filter(Boolean).join(" / ") || "系统";
        return '<details class="tool-audit-entry' +
            (item.status !== "成功" ? " audit-row-fail" : "") + '">' +
            '<summary class="tool-audit-row">' +
                '<span class="audit-ts">' + escapeHtml(formatAuditTime(item.ts)) + "</span>" +
                '<code class="audit-tool">' + escapeHtml(item.tool_name) + "</code>" +
                statusBadge +
                '<span class="audit-owner">' + escapeHtml(owner) + "</span>" +
                '<span class="audit-duration">' + (item.duration_ms || 0) + "ms</span>" +
                '<span class="audit-chevron">›</span>' +
            "</summary>" +
            '<div class="audit-detail">' +
                '<div><span>机器人用户</span><code>' + escapeHtml(item.tenant_id || "-") + "</code></div>" +
                '<div><span>智能体</span><code>' + escapeHtml(item.agent_id || "-") + "</code></div>" +
                '<div><span>会话</span><code>' + escapeHtml(item.session_id || "-") + "</code></div>" +
                '<div><span>参数哈希</span><code>' + escapeHtml(item.args_hash || "-") + "</code></div>" +
                '<div><span>输出大小</span><strong>' + (item.output_bytes || 0) + " B</strong></div>" +
                '<div><span>错误信息</span><strong class="audit-detail-error">' +
                    escapeHtml(item.error || "-") + "</strong></div>" +
            "</div></details>";
    }

    function loadAuditLogs(append) {
        var tool = document.getElementById("audit-filter-tool").value.trim();
        var status = document.getElementById("audit-filter-status").value;
        var requestOffset = append ? auditOffset : 0;
        var query = "?limit=" + encodeURIComponent(auditLimit) +
            "&offset=" + encodeURIComponent(requestOffset);
        if (tool) query += "&tool=" + encodeURIComponent(tool);
        if (status) query += "&status=" + encodeURIComponent(status);
        var container = document.getElementById("tool-audit-list");
        var loadMoreWrap = document.getElementById("audit-load-more-wrap");
        var totalEl = document.getElementById("audit-total");
        // Stale-response guard: only the latest request may touch the DOM.
        var seq = ++auditRequestSeq;
        if (!append) container.innerHTML = '<div class="audit-loading">正在加载审计记录…</div>';
        fetch("/api/tools/audit" + query)
            .then(function (r) {
                if (!r.ok) {
                    return r.json().then(function (body) {
                        throw new Error(body.detail || "请求失败");
                    });
                }
                return r.json();
            })
            .then(function (data) {
                if (seq !== auditRequestSeq) return;
                var items = data.items || [];
                totalEl.textContent = "共 " + (data.total || 0) + " 条";
                if (!items.length && !append) {
                    container.innerHTML = '<div class="empty-state">暂无符合条件的工具执行记录</div>';
                    loadMoreWrap.style.display = "none";
                    return;
                }
                var html = items.map(renderAuditEntry).join("");
                if (append) {
                    // insertAdjacentHTML keeps already-expanded entries open.
                    container.insertAdjacentHTML("beforeend", html);
                } else {
                    container.innerHTML = html;
                }
                // Advance the offset only after a successful load so a failed
                // "load more" can simply be retried without skipping a page.
                auditOffset = requestOffset + items.length;
                loadMoreWrap.style.display = auditOffset < data.total ? "" : "none";
            })
            .catch(function (error) {
                if (seq !== auditRequestSeq) return;
                if (!append) {
                    container.innerHTML = '<div class="audit-error-state">加载审计记录失败：' +
                        escapeHtml(error.message) + "</div>";
                    loadMoreWrap.style.display = "none";
                }
                showToast("加载审计记录失败：" + error.message, "error");
            });
    }

    var auditLoadMoreBtn = document.getElementById("audit-load-more");
    if (auditLoadMoreBtn) {
        auditLoadMoreBtn.addEventListener("click", function () {
            loadAuditLogs(true);
        });
    }
    document.getElementById("audit-filter-status").addEventListener("change", function () {
        loadAuditLogs(false);
    });
    document.getElementById("audit-filter-tool").addEventListener("input", function () {
        if (auditSearchTimer) clearTimeout(auditSearchTimer);
        auditSearchTimer = setTimeout(function () {
            loadAuditLogs(false);
        }, 300);
    });
    document.getElementById("audit-refresh").addEventListener("click", function () {
        loadAuditLogs(false);
    });

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
        document.getElementById("skill-delete-btn").style.display = "none";
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
            document.getElementById("skill-delete-btn").style.display = "";
            skillModal.style.display = "";
        });
    });
    document.getElementById("skill-delete-btn").addEventListener("click", function () {
        if (!skillEditingId) return;
        var id = skillEditingId;
        showConfirm("确定删除技能“" + id + "”吗？").then(function (ok) {
            if (!ok) return null;
            return fetch("/api/skills/" + encodeURIComponent(id), { method: "DELETE" });
        }).then(function (response) {
            if (!response) return;
            if (!response.ok) return response.json().then(function (body) { throw new Error(body.detail || "删除失败"); });
            skillModal.style.display = "none";
            showToast("技能已删除", "success");
            loadSkills();
        }).catch(function (error) { showToast("删除失败：" + error.message, "error"); });
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
            .then(function () {
                var savedId = mcpEditingId;
                showToast(mcpEditingId ? "已更新 MCP 服务" : "已添加 MCP 服务", "success");
                mcpModal.style.display = "none";
                loadMcpServers();
                if (savedId && mcpDetailPane.style.display !== "none" &&
                    currentMcpServer && currentMcpServer.id === savedId) {
                    openMcpDetail(savedId);
                }
            })
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
        document.getElementById("mcp-id").value = s.id;
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
        if (tab === "tools" && currentMcpServer) loadMcpDetailTools(currentMcpServer.id);
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
    document.getElementById("mcp-detail-delete").addEventListener("click", function () {
        if (!currentMcpServer) return;
        var id = currentMcpServer.id;
        showConfirm("确定删除 MCP 服务“" + id + "”吗？").then(function (ok) {
            if (!ok) return null;
            return fetch("/api/mcp/" + encodeURIComponent(id), { method: "DELETE" });
        }).then(function (response) {
            if (!response) return;
            if (!response.ok) return response.json().then(function (body) { throw new Error(body.detail || "删除失败"); });
            showToast("MCP 服务已删除", "success");
            showMcpList();
        }).catch(function (error) { showToast("删除失败：" + error.message, "error"); });
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
