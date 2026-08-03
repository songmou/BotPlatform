/* ===== External script registry and runs ===== */
function initScripts() {
    var catalog = [];
    var external = {};
    var tenants = [];
    var editingId = null;
    var runningScript = null;
    var pollTimer = null;
    var runs = [];
    var expandedRunId = null;
    var currentTab = "catalog";
    var modal = document.getElementById("script-modal");
    var runModal = document.getElementById("script-run-modal");
    var settingsModal = document.getElementById("script-settings-modal");
    var paramsModal = document.getElementById("script-params-modal");

    var STATUS_LABELS = {
        running: "运行中",
        cancelling: "取消中",
        success: "成功",
        failed: "失败",
        skipped: "跳过",
        timed_out: "超时",
        cancelled: "已取消"
    };
    var ACTIVE_STATUSES = ["running", "cancelling"];
    var TRIGGER_LABELS = {web: "网页执行", schedule: "定时计划", bot: "机器人"};

    function request(url, options) {
        return fetch(url, options).then(function (response) {
            if (!response.ok) {
                return response.json().then(function (body) {
                    throw new Error(body.detail || "请求失败");
                });
            }
            return response.json();
        });
    }

    function closeModal(target) { target.style.display = "none"; }
    function openModal(target) { target.style.display = ""; }
    function setBusy(button, busy, busyText) {
        if (!button) return;
        if (busy) {
            button.dataset.originalText = button.textContent;
            button.textContent = busyText || "处理中…";
            button.disabled = true;
        } else {
            button.textContent = button.dataset.originalText || button.textContent;
            button.disabled = false;
            delete button.dataset.originalText;
        }
    }
    function lines(value) {
        return value.split(/\r?\n/).map(function (item) { return item.trim(); }).filter(Boolean);
    }

    function tabFromHash() {
        var value = (window.location.hash || "").replace("#", "");
        return value === "runs" ? "runs" : "catalog";
    }

    function activateTab(name, updateHash) {
        currentTab = name === "runs" ? "runs" : "catalog";
        document.querySelectorAll("[data-scripts-tab]").forEach(function (button) {
            var active = button.getAttribute("data-scripts-tab") === currentTab;
            button.classList.toggle("active", active);
            button.setAttribute("aria-selected", active ? "true" : "false");
        });
        document.querySelectorAll("[data-scripts-pane]").forEach(function (pane) {
            var active = pane.getAttribute("data-scripts-pane") === currentTab;
            pane.classList.toggle("active", active);
            pane.style.display = active ? "" : "none";
        });
        if (updateHash && window.location.hash !== "#" + currentTab) {
            window.history.replaceState(null, "", "#" + currentTab);
        }
        if (currentTab === "runs" && document.getElementById("script-run-tenant").value) {
            loadRuns();
        }
    }

    function statusBadge(status) {
        var known = STATUS_LABELS.hasOwnProperty(status);
        var cls = known ? status : "unknown";
        var label = known ? STATUS_LABELS[status] : status;
        return '<span class="run-status run-status-' + escapeHtml(cls) + '">' + escapeHtml(label) + "</span>";
    }

    function formatTime(value) {
        if (!value) return "-";
        var date = new Date(String(value).replace(" ", "T"));
        if (isNaN(date.getTime())) return String(value);
        function pad(n) { return n < 10 ? "0" + n : "" + n; }
        return pad(date.getMonth() + 1) + "-" + pad(date.getDate()) + " " +
            pad(date.getHours()) + ":" + pad(date.getMinutes()) + ":" + pad(date.getSeconds());
    }

    function formatDuration(start, end) {
        if (!start || !end) return "-";
        var from = new Date(String(start).replace(" ", "T"));
        var to = new Date(String(end).replace(" ", "T"));
        if (isNaN(from.getTime()) || isNaN(to.getTime())) return "-";
        var seconds = Math.max(0, Math.round((to - from) / 1000));
        if (seconds < 60) return seconds + " 秒";
        var minutes = Math.floor(seconds / 60);
        if (minutes < 60) return minutes + " 分 " + (seconds % 60) + " 秒";
        return Math.floor(minutes / 60) + " 时 " + (minutes % 60) + " 分";
    }

    function loadTenants() {
        return request("/api/tenants").then(function (items) {
            tenants = items;
            var select = document.getElementById("script-run-tenant");
            select.innerHTML = items.map(function (item) {
                return '<option value="' + escapeHtml(item.tenant_id) + '">' +
                    escapeHtml(item.user_id + " / " + item.bot_id) + "</option>";
            }).join("");
            if (!items.length) {
                document.getElementById("script-run-list").innerHTML =
                    '<div class="empty-state">暂无可查看运行记录的机器人用户</div>';
            } else if (currentTab === "runs") {
                loadRuns();
            }
        }).catch(function (error) {
            showToast("加载用户失败：" + error.message, "error");
        });
    }

    function paramChips(specs) {
        var names = Object.keys(specs || {});
        if (!names.length) return '<div class="param-chips-empty">无公开参数</div>';
        return '<div class="param-chips">' + names.map(function (name) {
            var spec = specs[name] || {};
            return '<span class="param-chip">' + escapeHtml(name) +
                '<span class="param-type">' + escapeHtml(spec.type || "text") + "</span>" +
                (spec.required ? '<span class="param-required">*</span>' : "") + "</span>";
        }).join("") + "</div>";
    }

    function scriptCard(item) {
        var badges = '<span class="badge ' + (item.enabled ? "badge-success" : "badge-muted") + '">' +
            (item.enabled ? "启用" : "禁用") + "</span>" +
            '<span class="badge badge-muted">' + (item.runtime === "python" ? "Python" : "可执行文件") + "</span>" +
            '<span class="badge badge-muted">' + (item.external ? "外部" : "内置") + "</span>";
        var version = item.sha256_short
            ? "<code>" + escapeHtml(item.sha256_short) + "</code>"
            : '<span>内置</span>';
        var itemId = escapeHtml(item.id);
        var actions = '<button class="btn-primary btn-sm" data-action="run" data-id="' +
            itemId + '"' + (item.enabled ? "" : " disabled") + '>执行</button>';
        if (item.external) {
            actions += '<span class="btn-spacer"></span>' +
                '<div class="script-action-menu">' +
                '<button class="btn-secondary btn-sm" data-action="menu" data-id="' + itemId +
                '" aria-expanded="false">更多</button>' +
                '<div class="script-action-menu-popover" data-menu-for="' + itemId + '" style="display:none">' +
                '<button data-action="edit" data-id="' + itemId + '">编辑定义</button>' +
                '<button data-action="trust" data-id="' + itemId + '">信任当前版本</button>' +
                '<button class="danger" data-action="delete" data-id="' + itemId + '">删除脚本</button>' +
                "</div></div>";
        }
        var hasParams = Object.keys(item.parameters || {}).length > 0;
        return '<article class="script-card" data-enabled="' +
            (item.enabled ? "enabled" : "disabled") + '">' +
            '<div class="script-card-head"><span class="script-card-name">' + escapeHtml(item.name) +
            "</span>" + badges + "</div>" +
            '<div class="script-card-desc">' + escapeHtml(item.description || "暂无描述") + "</div>" +
            '<div class="script-meta">' +
            '<div class="script-meta-row"><span class="script-meta-label">ID</span><code>' +
            escapeHtml(item.id) + "</code></div>" +
            '<div class="script-meta-row"><span class="script-meta-label">版本</span>' + version + "</div>" +
            "</div>" + paramChips(item.parameters) +
            (hasParams
                ? '<button class="script-params-link" data-action="params" data-id="' +
                    itemId + '">查看完整参数定义</button>'
                : "") +
            '<div class="script-card-footer">' + actions + "</div></article>";
    }

    function renderCatalog() {
        var list = document.getElementById("script-list");
        var query = document.getElementById("script-search").value.trim().toLowerCase();
        var status = document.getElementById("script-filter-status").value;
        var filtered = catalog.filter(function (item) {
            var haystack = (item.name + " " + item.id + " " + (item.description || "")).toLowerCase();
            return (!query || haystack.indexOf(query) !== -1) &&
                (!status || status === (item.enabled ? "enabled" : "disabled"));
        });
        list.innerHTML = filtered.length
            ? filtered.map(scriptCard).join("")
            : '<div class="empty-state">没有符合条件的脚本</div>';
    }

    function loadCatalog() {
        document.getElementById("script-list").innerHTML =
            '<div class="scripts-loading">正在加载脚本库…</div>';
        return request("/api/scripts").then(function (data) {
            catalog = data.scripts || [];
            external = {};
            (data.external_entries || []).forEach(function (item) { external[item.id] = item; });
            document.getElementById("script-roots").value = (data.allowed_roots || []).join("\n");
            if (!catalog.length) {
                document.getElementById("script-list").innerHTML =
                    '<div class="empty-state">暂无已注册脚本</div>';
                return;
            }
            renderCatalog();
        }).catch(function (error) {
            document.getElementById("script-list").innerHTML =
                '<div class="scripts-error">加载脚本失败：' + escapeHtml(error.message) + "</div>";
            showToast("加载脚本失败：" + error.message, "error");
        });
    }

    function openParameters(script) {
        document.getElementById("script-params-title").textContent = script.name + " · 参数定义";
        document.getElementById("script-params-content").textContent =
            JSON.stringify(script.parameters || {}, null, 2);
        openModal(paramsModal);
    }

    function openEditor(item) {
        editingId = item ? item.id : null;
        var value = item || {};
        document.getElementById("script-modal-title").textContent = item ? "编辑脚本" : "注册脚本";
        document.getElementById("script-save-button").textContent =
            item ? "保存定义（不变更信任版本）" : "保存并信任当前版本";
        document.getElementById("script-id-group").style.display = item ? "none" : "";
        document.getElementById("script-id").value = value.id || "";
        document.getElementById("script-name").value = value.name || "";
        document.getElementById("script-description").value = value.description || "";
        document.getElementById("script-entrypoint").value = value.entrypoint || "";
        document.getElementById("script-runtime").value = value.runtime || "executable";
        document.getElementById("script-timeout").value = value.timeout_seconds || 900;
        document.getElementById("script-working-directory").value = value.working_directory || "";
        document.getElementById("script-concurrency").value = value.concurrency_scope || "global";
        document.getElementById("script-env").value = (value.env_allowlist || []).join("\n");
        document.getElementById("script-parameters").value = JSON.stringify(value.parameters || {}, null, 2);
        document.getElementById("script-enabled").checked = value.enabled !== false;
        openModal(modal);
    }

    function parameterFields(script) {
        var specs = script.parameters || {};
        var names = Object.keys(specs);
        if (!names.length) return '<div class="run-no-params">该脚本没有公开参数，可直接执行。</div>';
        return names.map(function (name) {
            var spec = specs[name];
            var id = "run-param-" + name;
            var input;
            if (spec.type === "boolean") {
                input = '<label class="checkbox-label"><input type="checkbox" id="' + id +
                    '" data-param="' + escapeHtml(name) + '" data-type="boolean"> 启用</label>';
            } else if (spec.choices && spec.choices.length) {
                input = '<select id="' + id + '" data-param="' + escapeHtml(name) +
                    '" data-type="' + escapeHtml(spec.type) + '"><option value="">不传</option>' +
                    spec.choices.map(function (choice) {
                        return '<option value="' + escapeHtml(choice) + '">' + escapeHtml(choice) + "</option>";
                    }).join("") + "</select>";
            } else {
                var type = spec.type === "date" ? "date" : (spec.type === "integer" ? "number" : "text");
                input = '<input id="' + id + '" type="' + type + '" data-param="' +
                    escapeHtml(name) + '" data-type="' + escapeHtml(spec.type) + '"' +
                    (spec.required ? " required" : "") + ">";
            }
            return '<div class="form-group"><label for="' + id + '">' +
                escapeHtml(name) + (spec.required ? "（必填）" : "") + "</label>" + input + "</div>";
        }).join("");
    }

    function openRun(script) {
        if (!tenants.length) {
            showToast("没有可执行脚本的机器人用户", "error");
            return;
        }
        runningScript = script;
        document.getElementById("script-run-title").textContent = "执行 " + script.name;
        document.getElementById("script-run-fields").innerHTML =
            '<div class="form-group"><label for="run-tenant">机器人用户</label><select id="run-tenant" class="tenant-select">' +
            tenants.map(function (item) {
                return '<option value="' + escapeHtml(item.tenant_id) + '">' +
                    escapeHtml(item.user_id + " / " + item.bot_id) + "</option>";
            }).join("") + "</select></div>" + parameterFields(script);
        document.getElementById("script-run-hint").innerHTML =
            "将执行已审核版本 <code>" + escapeHtml(script.sha256_short || "内置") +
            "</code>。真实发布不会自动串联预检。";
        openModal(runModal);
    }

    function collectRunParameters(root) {
        var result = {};
        root.querySelectorAll("[data-param]").forEach(function (input) {
            var name = input.getAttribute("data-param");
            var type = input.getAttribute("data-type");
            if (type === "boolean") {
                result[name] = input.checked;
            } else if (input.value !== "") {
                result[name] = type === "integer" ? parseInt(input.value, 10) : input.value;
            }
        });
        return result;
    }

    function runRecord(run) {
        var cancellable = run.status === "running"
            ? '<button class="btn-danger btn-sm" data-run-action="cancel" data-id="' +
                escapeHtml(run.run_id) + '">取消</button>' : "";
        var start = run.started_at || run.created_at;
        var meta = '<div class="run-meta-grid">' +
            '<div class="run-meta-item"><span class="run-meta-label">触发方式</span>' +
            '<span class="run-meta-value">' + escapeHtml(TRIGGER_LABELS[run.trigger] || run.trigger || "-") + "</span></div>" +
            '<div class="run-meta-item"><span class="run-meta-label">创建时间</span>' +
            '<span class="run-meta-value">' + escapeHtml(formatTime(run.created_at)) + "</span></div>" +
            '<div class="run-meta-item"><span class="run-meta-label">结束时间</span>' +
            '<span class="run-meta-value">' + escapeHtml(formatTime(run.finished_at)) + "</span></div>" +
            '<div class="run-meta-item"><span class="run-meta-label">耗时</span>' +
            '<span class="run-meta-value">' + escapeHtml(formatDuration(start, run.finished_at)) + "</span></div>" +
            '<div class="run-meta-item"><span class="run-meta-label">退出码</span>' +
            '<span class="run-meta-value">' +
            escapeHtml(run.exit_code === null || run.exit_code === undefined ? "-" : String(run.exit_code)) +
            "</span></div></div>";
        var error = run.error
            ? '<div class="run-error">' + escapeHtml(run.error) + "</div>" : "";
        var log = run.log_tail
            ? '<pre class="run-log">' + escapeHtml(run.log_tail) + "</pre>"
            : '<pre class="run-log"><span class="run-log-empty">暂无日志输出</span></pre>';
        var expanded = run.run_id === expandedRunId;
        return '<article class="run-record' + (expanded ? " expanded" : "") +
            '" data-run-id="' + escapeHtml(run.run_id) + '">' +
            '<button class="run-record-summary" type="button" data-run-action="toggle" data-id="' +
            escapeHtml(run.run_id) + '" aria-expanded="' + (expanded ? "true" : "false") + '">' +
            '<span class="run-record-name">' + escapeHtml(run.script_name) + "</span>" +
            statusBadge(run.status) +
            '<span class="run-record-cell run-record-time">' + escapeHtml(formatTime(run.created_at)) + "</span>" +
            '<span class="run-record-cell run-record-trigger">' +
            escapeHtml(TRIGGER_LABELS[run.trigger] || run.trigger || "-") + "</span>" +
            '<span class="run-record-cell run-record-duration">' +
            escapeHtml(formatDuration(start, run.finished_at)) + "</span>" +
            '<span class="run-record-toggle">›</span></button>' +
            '<div class="run-record-detail"><div class="run-detail-head">' +
            '<div class="run-card-id">' + escapeHtml(run.run_id) + "</div>" + cancellable + "</div>" + meta +
            (run.summary ? '<div class="run-summary">' + escapeHtml(run.summary) + "</div>" : "") +
            error + '<details class="run-log-detail"><summary>日志尾部</summary>' + log +
            "</details></div></article>";
    }

    function renderRuns() {
        var status = document.getElementById("script-run-status").value;
        var filtered = status
            ? runs.filter(function (run) { return run.status === status; })
            : runs;
        var list = document.getElementById("script-run-list");
        if (!filtered.length) {
            list.innerHTML = '<div class="empty-state">' +
                (runs.length ? "没有符合当前筛选的运行记录" : "暂无运行记录") + "</div>";
            return;
        }
        list.innerHTML = filtered.map(runRecord).join("");
    }

    function schedulePoll(items) {
        if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
        var active = items.some(function (run) {
            return ACTIVE_STATUSES.indexOf(run.status) !== -1;
        });
        if (!active || currentTab !== "runs") return;
        pollTimer = setTimeout(function () {
            pollTimer = null;
            if (document.hidden) { schedulePoll(items); return; }
            loadRuns(true);
        }, 5000);
    }

    function loadRuns(silent) {
        var tenantId = document.getElementById("script-run-tenant").value;
        if (!tenantId) {
            runs = [];
            renderRuns();
            return Promise.resolve();
        }
        var list = document.getElementById("script-run-list");
        if (!silent) list.innerHTML = '<div class="scripts-loading">正在加载运行记录…</div>';
        return request("/api/script-runs?tenant_id=" + encodeURIComponent(tenantId)).then(function (items) {
            runs = items;
            if (expandedRunId && !runs.some(function (run) { return run.run_id === expandedRunId; })) {
                expandedRunId = null;
            }
            renderRuns();
            schedulePoll(runs);
        }).catch(function (error) {
            list.innerHTML = '<div class="scripts-error">加载运行记录失败：' +
                escapeHtml(error.message) + "</div>";
            showToast("加载运行记录失败：" + error.message, "error");
        });
    }

    document.addEventListener("visibilitychange", function () {
        // Resume polling immediately when the page becomes visible again.
        if (!document.hidden && pollTimer) {
            clearTimeout(pollTimer);
            pollTimer = null;
            loadRuns(true);
        }
    });

    document.getElementById("create-script-btn").addEventListener("click", function () { openEditor(null); });
    document.getElementById("script-settings-btn").addEventListener("click", function () {
        openModal(settingsModal);
    });
    document.getElementById("script-modal-close").addEventListener("click", function () { closeModal(modal); });
    document.getElementById("script-modal-cancel").addEventListener("click", function () { closeModal(modal); });
    document.getElementById("script-run-close").addEventListener("click", function () { closeModal(runModal); });
    document.getElementById("script-run-cancel").addEventListener("click", function () { closeModal(runModal); });
    document.getElementById("script-settings-close").addEventListener("click", function () {
        closeModal(settingsModal);
    });
    document.getElementById("script-settings-cancel").addEventListener("click", function () {
        closeModal(settingsModal);
    });
    document.getElementById("script-params-close").addEventListener("click", function () {
        closeModal(paramsModal);
    });
    [modal, runModal, settingsModal, paramsModal].forEach(function (target) {
        target.addEventListener("click", function (event) {
            if (event.target === target) closeModal(target);
        });
    });
    document.addEventListener("keydown", function (event) {
        if (event.key !== "Escape") return;
        [paramsModal, runModal, modal, settingsModal].some(function (target) {
            if (target.style.display !== "none") {
                closeModal(target);
                return true;
            }
            return false;
        });
    });
    document.querySelectorAll("[data-scripts-tab]").forEach(function (button) {
        button.addEventListener("click", function () {
            activateTab(button.getAttribute("data-scripts-tab"), true);
        });
    });
    window.addEventListener("hashchange", function () { activateTab(tabFromHash(), false); });
    document.getElementById("script-search").addEventListener("input", renderCatalog);
    document.getElementById("script-filter-status").addEventListener("change", renderCatalog);
    document.getElementById("script-run-status").addEventListener("change", renderRuns);
    document.getElementById("script-run-tenant").addEventListener("change", function () {
        expandedRunId = null;
        loadRuns();
    });
    document.getElementById("refresh-runs-btn").addEventListener("click", function () {
        var button = this;
        setBusy(button, true, "刷新中…");
        loadRuns().finally(function () { setBusy(button, false); });
    });

    document.getElementById("save-script-roots").addEventListener("click", function () {
        var button = this;
        var roots = lines(document.getElementById("script-roots").value);
        showConfirm("保存允许根目录会改变可注册脚本的安全边界，确定继续吗？").then(function (ok) {
            if (!ok) return;
            setBusy(button, true, "保存中…");
            request("/api/scripts/roots", {
                method: "PUT", headers: {"Content-Type": "application/json"},
                body: JSON.stringify({allowed_roots: roots})
            }).then(function () {
                closeModal(settingsModal);
                showToast("已保存允许根目录", "success");
                loadCatalog();
            }).catch(function (error) {
                showToast("保存失败：" + error.message, "error");
            }).finally(function () { setBusy(button, false); });
        });
    });

    document.getElementById("script-form").addEventListener("submit", function (event) {
        event.preventDefault();
        var parameters;
        try { parameters = JSON.parse(document.getElementById("script-parameters").value || "{}"); }
        catch (error) { showToast("参数定义不是有效 JSON", "error"); return; }
        var payload = {
            id: document.getElementById("script-id").value.trim(),
            name: document.getElementById("script-name").value.trim(),
            description: document.getElementById("script-description").value.trim(),
            entrypoint: document.getElementById("script-entrypoint").value.trim(),
            runtime: document.getElementById("script-runtime").value,
            timeout_seconds: parseInt(document.getElementById("script-timeout").value, 10),
            working_directory: document.getElementById("script-working-directory").value.trim(),
            concurrency_scope: document.getElementById("script-concurrency").value,
            env_allowlist: lines(document.getElementById("script-env").value),
            parameters: parameters,
            enabled: document.getElementById("script-enabled").checked
        };
        if (!payload.working_directory) delete payload.working_directory;
        var url = editingId ? "/api/scripts/" + encodeURIComponent(editingId) : "/api/scripts";
        var saveButton = document.getElementById("script-save-button");
        setBusy(saveButton, true, "保存中…");
        request(url, {
            method: editingId ? "PUT" : "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify(payload)
        }).then(function () {
            closeModal(modal); showToast(editingId ? "已更新脚本" : "已注册并信任脚本", "success"); loadCatalog();
        }).catch(function (error) {
            showToast("保存失败：" + error.message, "error");
        }).finally(function () { setBusy(saveButton, false); });
    });

    document.getElementById("script-list").addEventListener("click", function (event) {
        var button = event.target.closest("[data-action]");
        if (!button) return;
        var id = button.getAttribute("data-id");
        var action = button.getAttribute("data-action");
        var script = catalog.find(function (item) { return item.id === id; });
        if (!script) return;
        if (action === "menu") {
            var menu = button.parentElement.querySelector(".script-action-menu-popover");
            var open = menu.style.display !== "none";
            document.querySelectorAll(".script-action-menu-popover").forEach(function (item) {
                item.style.display = "none";
            });
            menu.style.display = open ? "none" : "";
            button.setAttribute("aria-expanded", open ? "false" : "true");
            return;
        }
        if (action === "params") { openParameters(script); return; }
        if (action === "run") { openRun(script); return; }
        if (action === "edit") { openEditor(external[id]); return; }
        var entry = external[id] || {};
        var message = action === "trust"
            ? "重新信任脚本：" + script.name + "\n路径：" + (entry.entrypoint || "-") +
                "\n原版本：" + (script.sha256_short || "-") +
                "\n系统将重新计算 SHA-256，旧定时授权会立即失效。确定继续吗？"
            : "确定删除脚本 " + id + " 吗？";
        showConfirm(message).then(function (ok) {
            if (!ok) return;
            var url = "/api/scripts/" + encodeURIComponent(id) + (action === "trust" ? "/trust-current" : "");
            request(url, {method: action === "trust" ? "POST" : "DELETE"}).then(function () {
                showToast(action === "trust" ? "已信任当前版本" : "已删除脚本", "success"); loadCatalog();
            }).catch(function (error) { showToast("操作失败：" + error.message, "error"); });
        });
    });
    document.addEventListener("click", function (event) {
        if (event.target.closest(".script-action-menu")) return;
        document.querySelectorAll(".script-action-menu-popover").forEach(function (menu) {
            menu.style.display = "none";
        });
        document.querySelectorAll("[data-action='menu']").forEach(function (button) {
            button.setAttribute("aria-expanded", "false");
        });
    });

    document.getElementById("script-run-form").addEventListener("submit", function (event) {
        event.preventDefault();
        var tenantId = document.getElementById("run-tenant").value;
        var params = collectRunParameters(document.getElementById("script-run-fields"));
        var summary = "脚本：" + runningScript.name + "（" + runningScript.id + "）" +
            "\n参数：" + JSON.stringify(params) +
            "\n版本：" + (runningScript.sha256_short || "内置") +
            "\n执行时间：立即执行\n是否无人值守：否";
        showConfirm(summary + "\n\n确定执行吗？").then(function (ok) {
            if (!ok) return;
            var submitButton = document.querySelector("#script-run-form button[type='submit']");
            setBusy(submitButton, true, "提交中…");
            request("/api/scripts/" + encodeURIComponent(runningScript.id) + "/runs", {
                method: "POST", headers: {"Content-Type": "application/json"},
                body: JSON.stringify({tenant_id: tenantId, parameters: params})
            }).then(function (run) {
                closeModal(runModal); showToast("已提交：" + run.run_id, "success");
                document.getElementById("script-run-tenant").value = tenantId;
                expandedRunId = run.run_id;
                activateTab("runs", true);
            }).catch(function (error) {
                showToast("执行失败：" + error.message, "error");
            }).finally(function () { setBusy(submitButton, false); });
        });
    });

    document.getElementById("script-run-list").addEventListener("click", function (event) {
        var button = event.target.closest("[data-run-action]");
        if (!button) return;
        if (button.getAttribute("data-run-action") === "toggle") {
            var toggleId = button.getAttribute("data-id");
            expandedRunId = expandedRunId === toggleId ? null : toggleId;
            renderRuns();
            return;
        }
        var tenantId = document.getElementById("script-run-tenant").value;
        var runId = button.getAttribute("data-id");
        showConfirm("确定取消脚本任务 " + runId + " 吗？").then(function (ok) {
            if (!ok) return;
            request("/api/script-runs/" + encodeURIComponent(runId) + "/cancel", {
                method: "POST", headers: {"Content-Type": "application/json"},
                body: JSON.stringify({tenant_id: tenantId})
            }).then(function () { showToast("已请求取消", "success"); loadRuns(); })
              .catch(function (error) { showToast("取消失败：" + error.message, "error"); });
        });
    });

    activateTab(tabFromHash(), false);
    Promise.all([loadCatalog(), loadTenants()]);
}
