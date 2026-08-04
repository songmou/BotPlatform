/* ===== Schedules page ===== */
function initScheduleTabs() {
    var validTabs = ["tasks", "automation"];

    function tabFromHash() {
        var value = (window.location.hash || "").replace(/^#/, "");
        return validTabs.indexOf(value) >= 0 ? value : "tasks";
    }

    function activateTab(name, updateHash) {
        var activeTab = validTabs.indexOf(name) >= 0 ? name : "tasks";
        document.querySelectorAll("[data-schedule-tab]").forEach(function (button) {
            var active = button.getAttribute("data-schedule-tab") === activeTab;
            button.classList.toggle("active", active);
            button.setAttribute("aria-selected", active ? "true" : "false");
            button.tabIndex = active ? 0 : -1;
        });
        document.querySelectorAll("[data-schedule-pane]").forEach(function (pane) {
            var active = pane.getAttribute("data-schedule-pane") === activeTab;
            pane.classList.toggle("active", active);
            pane.hidden = !active;
        });
        if (updateHash && window.location.hash !== "#" + activeTab) {
            window.history.replaceState(null, "", "#" + activeTab);
        }
    }

    var buttons = Array.prototype.slice.call(document.querySelectorAll("[data-schedule-tab]"));
    buttons.forEach(function (button, index) {
        button.addEventListener("click", function () {
            activateTab(button.getAttribute("data-schedule-tab"), true);
        });
        button.addEventListener("keydown", function (event) {
            var nextIndex = null;
            if (event.key === "ArrowRight") nextIndex = (index + 1) % buttons.length;
            if (event.key === "ArrowLeft") nextIndex = (index - 1 + buttons.length) % buttons.length;
            if (event.key === "Home") nextIndex = 0;
            if (event.key === "End") nextIndex = buttons.length - 1;
            if (nextIndex === null) return;
            event.preventDefault();
            buttons[nextIndex].focus();
            activateTab(buttons[nextIndex].getAttribute("data-schedule-tab"), true);
        });
    });
    window.addEventListener("hashchange", function () {
        activateTab(tabFromHash(), false);
    });
    activateTab(tabFromHash(), false);
}

function initSchedules() {
    var listEl = document.getElementById("schedule-list");
    var statusEl = document.getElementById("schedule-status");
    var modal = document.getElementById("schedule-modal");
    var modalTitle = document.getElementById("schedule-modal-title");
    var form = document.getElementById("schedule-form");
    var idGroup = document.getElementById("schedule-id-group");
    var editingId = null;

    loadSchedules();

    document.getElementById("create-schedule-btn").addEventListener("click", function () {
        editingId = null;
        modalTitle.textContent = "新建任务";
        idGroup.style.display = "";
        form.reset();
        document.getElementById("schedule-enabled").checked = true;
        document.getElementById("schedule-action-type").value = "text";
        document.getElementById("schedule-condition-enabled").checked = false;
        document.getElementById("condition-fields").style.display = "none";
        updateActionFields();
        openModal();
    });

    document.getElementById("schedule-modal-close").addEventListener("click", closeModal);
    document.getElementById("schedule-modal-cancel").addEventListener("click", closeModal);
    modal.addEventListener("click", function (e) { if (e.target === modal) closeModal(); });

    function openModal() { modal.style.display = ""; }
    function closeModal() { modal.style.display = "none"; }

    document.getElementById("schedule-action-type").addEventListener("change", updateActionFields);

    function updateActionFields() {
        var type = document.getElementById("schedule-action-type").value;
        document.getElementById("action-text-group").style.display = type === "text" ? "" : "none";
        document.getElementById("action-agent-group").style.display = type === "agent_prompt" ? "" : "none";
        document.getElementById("action-script-group").style.display = type === "script" ? "" : "none";
        document.getElementById("action-plugin-group").style.display = type === "plugin" ? "" : "none";
        document.getElementById("action-parameters-group").style.display =
            (type === "script" || type === "plugin") ? "" : "none";
    }

    document.getElementById("schedule-condition-enabled").addEventListener("change", function () {
        document.getElementById("condition-fields").style.display = this.checked ? "" : "none";
    });

    var actionLabels = {
        text: "文本消息",
        agent_prompt: "智能体生成",
        script: "脚本执行",
        plugin: "插件调用",
        image: "图片推送"
    };

    function loadSchedules() {
        fetch("/api/schedules")
            .then(function (r) { return r.json(); })
            .then(function (tasks) {
                var enabled = tasks.filter(function (t) { return t.enabled; }).length;
                statusEl.innerHTML = "<span>已启用 " + enabled + " / 共 " + tasks.length + " 项</span>";
                if (!tasks.length) {
                    listEl.innerHTML = '<div class="empty-state">暂无定时任务，点击「新建任务」创建</div>';
                    return;
                }
                listEl.innerHTML = tasks.map(function (t) {
                    var cronDisplay = (t.crons && t.crons.length) ? t.crons.join("<br>") : (t.cron || "—");
                    var condDisplay = t.condition
                        ? t.condition.type + " (" + t.condition.after_hours + "h~" + t.condition.before_hours + "h)"
                        : "无";
                    return '<div class="card schedule-card" data-id="' + t.id + '">' +
                        '<div class="card-header">' +
                        '<div><strong>' + escapeHtml(t.id) + '</strong>' +
                        (t.enabled ? '<span class="badge badge-success">启用</span>' : '<span class="badge badge-muted">禁用</span>') +
                        '</div></div>' +
                        '<div class="card-body">' +
                        '<p><strong>Cron：</strong><code>' + cronDisplay + '</code></p>' +
                        '<p><strong>动作：</strong>' + (actionLabels[t.action.type] || t.action.type) + '</p>' +
                        '<p><strong>目标：</strong>' + escapeHtml(t.target) + '</p>' +
                        '<p><strong>条件：</strong>' + escapeHtml(condDisplay) + '</p>' +
                        '</div><div class="card-footer card-actions">' +
                        '<button class="btn-edit" data-action="edit" data-id="' + t.id + '">编辑</button>' +
                        '<button class="btn-danger" data-action="delete" data-id="' + t.id + '">删除</button>' +
                        '</div></div>';
                }).join("");
            });
    }

    listEl.addEventListener("click", function (e) {
        var btn = e.target.closest("[data-action]");
        if (!btn) return;
        e.preventDefault();
        var action = btn.getAttribute("data-action");
        var id = btn.getAttribute("data-id");

        if (action === "delete") {
            showConfirm("确定要删除定时任务「" + id + "」吗？").then(function (ok) {
                if (!ok) return;
                fetch("/api/schedules/" + id, { method: "DELETE" })
                    .then(function (r) {
                        if (!r.ok) return r.json().then(function (d) { throw new Error(d.detail); });
                        showToast("已删除任务 " + id, "success");
                        loadSchedules();
                    })
                    .catch(function (err) { showToast("删除失败：" + err.message, "error"); });
            });
        }

        if (action === "edit") {
            fetch("/api/schedules/" + id)
                .then(function (r) { return r.json(); })
                .then(function (t) {
                    editingId = id;
                    modalTitle.textContent = "编辑任务";
                    idGroup.style.display = "none";
                    document.getElementById("schedule-id").value = t.id;
                    document.getElementById("schedule-enabled").checked = t.enabled;
                    var crons = t.crons && t.crons.length ? t.crons : (t.cron ? [t.cron] : []);
                    document.getElementById("schedule-crons").value = crons.join("\n");
                    document.getElementById("schedule-target").value = t.target;
                    document.getElementById("schedule-action-type").value = t.action.type;
                    document.getElementById("action-content").value = t.action.content || "";
                    document.getElementById("action-agent-id").value = t.action.agent_id || "";
                    document.getElementById("action-prompt").value = t.action.prompt || "";
                    document.getElementById("action-script-id").value = t.action.script_id || "";
                    document.getElementById("action-plugin-id").value = t.action.plugin_id || "";
                    document.getElementById("action-tool-name").value = t.action.tool_name || "";
                    document.getElementById("action-parameters").value =
                        JSON.stringify(t.action.parameters || {}, null, 2);
                    if (t.condition) {
                        document.getElementById("schedule-condition-enabled").checked = true;
                        document.getElementById("condition-fields").style.display = "";
                        document.getElementById("condition-after").value = t.condition.after_hours;
                        document.getElementById("condition-before").value = t.condition.before_hours;
                    } else {
                        document.getElementById("schedule-condition-enabled").checked = false;
                        document.getElementById("condition-fields").style.display = "none";
                    }
                    updateActionFields();
                    openModal();
                });
        }
    });

    form.addEventListener("submit", function (e) {
        e.preventDefault();

        var cronsRaw = document.getElementById("schedule-crons").value.trim();
        var crons = cronsRaw ? cronsRaw.split("\n").map(function (s) { return s.trim(); }).filter(Boolean) : [];
        var actionType = document.getElementById("schedule-action-type").value;
        var action = { type: actionType };

        if (actionType === "text") {
            action.content = document.getElementById("action-content").value.trim();
        } else if (actionType === "agent_prompt") {
            action.agent_id = document.getElementById("action-agent-id").value.trim();
            action.prompt = document.getElementById("action-prompt").value.trim();
        } else if (actionType === "script") {
            action.script_id = document.getElementById("action-script-id").value.trim();
            try {
                action.parameters = JSON.parse(document.getElementById("action-parameters").value || "{}");
            } catch (e) {
                showToast("动作参数不是有效 JSON", "error");
                return;
            }
        } else if (actionType === "plugin") {
            action.plugin_id = document.getElementById("action-plugin-id").value.trim();
            action.tool_name = document.getElementById("action-tool-name").value.trim();
            try {
                action.parameters = JSON.parse(document.getElementById("action-parameters").value || "{}");
            } catch (e) {
                showToast("动作参数不是有效 JSON", "error");
                return;
            }
        }

        var condition = null;
        if (document.getElementById("schedule-condition-enabled").checked) {
            condition = {
                type: "inactivity_once",
                after_hours: parseFloat(document.getElementById("condition-after").value) || 0,
                before_hours: parseFloat(document.getElementById("condition-before").value) || 24
            };
        }

        var payload = {
            enabled: document.getElementById("schedule-enabled").checked,
            crons: crons,
            target: document.getElementById("schedule-target").value,
            action: action,
            condition: condition
        };

        var url, method;
        if (editingId) {
            url = "/api/schedules/" + editingId;
            method = "PUT";
        } else {
            payload.id = document.getElementById("schedule-id").value;
            url = "/api/schedules";
            method = "POST";
        }

        fetch(url, {
            method: method,
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        })
            .then(function (r) {
                if (!r.ok) return r.json().then(function (d) { throw new Error(d.detail); });
                showToast(editingId ? "已保存修改" : "已创建任务", "success");
                closeModal();
                loadSchedules();
            })
            .catch(function (err) { showToast("保存失败：" + err.message, "error"); });
    });
}

function initScriptAutomation() {
    var tenants = [];
    var scripts = [];
    var editingId = null;
    var modal = document.getElementById("script-schedule-modal");
    var tenantSelect = document.getElementById("script-schedule-tenant");
    var scriptSelect = document.getElementById("script-schedule-script");

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

    function renderScriptScheduleEnv(scriptId) {
        if (!window.EnvPanel) return;
        var container = document.getElementById("script-schedule-env-bindings");
        if (container) {
            window.EnvPanel.loadGlobalEnvBindings("script", scriptId, container);
        }
        if (window.CredentialPanel) {
            var credContainer = document.getElementById("script-schedule-credential-bindings");
            var tenantId = (typeof tenantSelect !== "undefined" && tenantSelect) ? tenantSelect.value : null;
            window.CredentialPanel.loadScriptCredentials(scriptId, tenantId, credContainer);
        }
    }

    function renderParameters(values) {
        var selected = scripts.find(function (item) { return item.id === scriptSelect.value; });
        var specs = selected ? selected.parameters || {} : {};
        values = values || {};
        document.getElementById("script-schedule-parameters").innerHTML =
            Object.keys(specs).map(function (name) {
                var spec = specs[name];
                var id = "schedule-param-" + name;
                if (spec.type === "boolean") {
                    return '<div class="form-group"><label class="checkbox-label"><input id="' + id +
                        '" type="checkbox" data-schedule-param="' + escapeHtml(name) +
                        '" data-type="boolean"' + (values[name] ? " checked" : "") + "> " +
                        escapeHtml(name) + "</label></div>";
                }
                if (spec.choices && spec.choices.length) {
                    return '<div class="form-group"><label for="' + id + '">' + escapeHtml(name) +
                        '</label><select id="' + id + '" data-schedule-param="' + escapeHtml(name) +
                        '" data-type="' + escapeHtml(spec.type) + '"><option value="">不传</option>' +
                        spec.choices.map(function (choice) {
                            return '<option value="' + escapeHtml(choice) + '"' +
                                (values[name] === choice ? " selected" : "") + ">" +
                                escapeHtml(choice) + "</option>";
                        }).join("") + "</select></div>";
                }
                var inputType = spec.type === "date" ? "date" : (spec.type === "integer" ? "number" : "text");
                return '<div class="form-group"><label for="' + id + '">' + escapeHtml(name) +
                    '</label><input id="' + id + '" type="' + inputType +
                    '" data-schedule-param="' + escapeHtml(name) + '" data-type="' +
                    escapeHtml(spec.type) + '" value="' + escapeHtml(values[name] == null ? "" : String(values[name])) +
                    '"' + (spec.required ? " required" : "") + "></div>";
            }).join("");
    }

    function collectParameters() {
        var result = {};
        document.querySelectorAll("[data-schedule-param]").forEach(function (input) {
            var name = input.getAttribute("data-schedule-param");
            var type = input.getAttribute("data-type");
            if (type === "boolean") result[name] = input.checked;
            else if (input.value !== "") result[name] = type === "integer" ? parseInt(input.value, 10) : input.value;
        });
        return result;
    }

    function loadDependencies() {
        return Promise.all([request("/api/tenants"), request("/api/scripts")]).then(function (results) {
            tenants = results[0];
            scripts = results[1].scripts || [];
            tenantSelect.innerHTML = tenants.length
                ? tenants.map(function (item) {
                    return '<option value="' + escapeHtml(item.tenant_id) + '">' +
                        escapeHtml(item.user_id + " / " + item.bot_id) + "</option>";
                }).join("")
                : '<option value="">暂无机器人用户</option>';
            tenantSelect.disabled = !tenants.length;
            scriptSelect.innerHTML = scripts.length
                ? scripts.map(function (item) {
                    return '<option value="' + escapeHtml(item.id) + '">' +
                        escapeHtml(item.name + " (" + item.id + ")") + "</option>";
                }).join("")
                : '<option value="">暂无可用脚本</option>';
            scriptSelect.disabled = !scripts.length;
            renderParameters();
            loadSchedules();
        }).catch(function (error) {
            showToast("加载脚本计划数据失败：" + error.message, "error");
        });
    }

    function loadSchedules() {
        var tenantId = tenantSelect.value;
        var list = document.getElementById("script-schedule-list");
        if (!tenantId) {
            list.innerHTML = '<div class="empty-state">暂无机器人用户</div>';
            return;
        }
        request("/api/tenants/" + encodeURIComponent(tenantId) + "/script-schedules").then(function (items) {
            if (!items.length) {
                list.innerHTML = '<div class="empty-state">当前用户暂无脚本计划</div>';
                return;
            }
            list.innerHTML = items.map(function (item) {
                return '<div class="card"><div class="card-header"><div><strong>' +
                    escapeHtml(item.schedule_id) + '</strong><span class="badge ' +
                    (item.enabled ? "badge-success" : "badge-muted") + '">' +
                    (item.enabled ? "启用" : "停用") + '</span></div></div><div class="card-body"><p>脚本：<code>' +
                    escapeHtml(item.script_id) + '</code></p><p>Cron：<code>' +
                    escapeHtml(item.crons.join("；")) + '</code></p><p>授权版本：<code>' +
                    escapeHtml((item.authorized_sha256 || "").slice(0, 12)) +
                    '</code></p><p>最近状态：' + escapeHtml(item.last_status || "尚未运行") +
                    '</p></div><div class="card-footer card-actions">' +
                    '<button class="btn-edit" data-script-schedule-action="edit" data-id="' +
                    item.schedule_id + '">编辑</button><button class="btn-secondary" data-script-schedule-action="' +
                    (item.enabled ? "disable" : "enable") + '" data-id="' + item.schedule_id + '">' +
                    (item.enabled ? "停用" : "重新授权") + '</button><button class="btn-danger" data-script-schedule-action="delete" data-id="' +
                    item.schedule_id + '">删除</button></div></div>';
            }).join("");
            list._items = items;
        }).catch(function (error) {
            showToast("加载脚本计划失败：" + error.message, "error");
        });
    }

    function openEditor(item) {
        editingId = item ? item.schedule_id : null;
        document.getElementById("script-schedule-modal-title").textContent = item ? "编辑脚本计划" : "新建脚本计划";
        document.getElementById("script-schedule-id-group").style.display = item ? "none" : "";
        document.getElementById("script-schedule-id").value = item ? item.schedule_id : "";
        scriptSelect.value = item ? item.script_id : (scripts[0] ? scripts[0].id : "");
        document.getElementById("script-schedule-crons").value = item ? item.crons.join("\n") : "";
        document.getElementById("script-schedule-enabled").checked = item ? item.enabled : true;
        renderParameters(item ? item.parameters : {});
        renderScriptScheduleEnv(scriptSelect.value);
        modal.style.display = "";
    }

    tenantSelect.addEventListener("change", loadSchedules);
    scriptSelect.addEventListener("change", function () { renderParameters(); renderScriptScheduleEnv(scriptSelect.value); });
    document.getElementById("create-script-schedule-btn").addEventListener("click", function () {
        if (!tenants.length || !scripts.length) {
            showToast("需要先有机器人用户和已注册脚本", "error"); return;
        }
        openEditor(null);
    });
    document.getElementById("script-schedule-modal-close").addEventListener("click", function () { modal.style.display = "none"; });
    document.getElementById("script-schedule-modal-cancel").addEventListener("click", function () { modal.style.display = "none"; });

    document.getElementById("script-schedule-form").addEventListener("submit", function (event) {
        event.preventDefault();
        var tenantId = tenantSelect.value;
        var scheduleId = editingId || document.getElementById("script-schedule-id").value.trim();
        var payload = {
            schedule_id: scheduleId,
            script_id: scriptSelect.value,
            crons: document.getElementById("script-schedule-crons").value.split(/\r?\n/).map(function (item) { return item.trim(); }).filter(Boolean),
            parameters: collectParameters(),
            enabled: document.getElementById("script-schedule-enabled").checked
        };
        var selectedScript = scripts.find(function (item) { return item.id === payload.script_id; }) || {};
        var summary = "脚本：" + (selectedScript.name || payload.script_id) +
            "（" + payload.script_id + "）\n参数：" + JSON.stringify(payload.parameters) +
            "\n版本：" + (selectedScript.sha256_short || "内置") +
            "\n时间：" + payload.crons.join("；") + "（Asia/Shanghai）" +
            "\n是否无人值守：" + (payload.enabled ? "是" : "否");
        showConfirm(summary + "\n\n保存后触发时不再逐次确认。确定继续吗？").then(function (ok) {
            if (!ok) return;
            var base = "/api/tenants/" + encodeURIComponent(tenantId) + "/script-schedules";
            request(editingId ? base + "/" + encodeURIComponent(editingId) : base, {
                method: editingId ? "PUT" : "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify(payload)
            }).then(function () {
                modal.style.display = "none"; showToast("已保存并授权脚本计划", "success"); loadSchedules();
            }).catch(function (error) { showToast("保存失败：" + error.message, "error"); });
        });
    });

    document.getElementById("script-schedule-list").addEventListener("click", function (event) {
        var button = event.target.closest("[data-script-schedule-action]");
        if (!button) return;
        var action = button.getAttribute("data-script-schedule-action");
        var id = button.getAttribute("data-id");
        var items = document.getElementById("script-schedule-list")._items || [];
        var item = items.find(function (value) { return value.schedule_id === id; });
        if (action === "edit") { openEditor(item); return; }
        var tenantId = tenantSelect.value;
        var base = "/api/tenants/" + encodeURIComponent(tenantId) + "/script-schedules/" + encodeURIComponent(id);
        var message = action === "enable"
            ? "计划：" + id + "\n脚本：" + item.script_id +
                "\n参数：" + JSON.stringify(item.parameters || {}) +
                "\n时间：" + (item.crons || []).join("；") + "（Asia/Shanghai）" +
                "\n重新启用会按当前脚本版本授权无人值守执行，确定继续吗？"
            : (action === "disable" ? "确定停用该脚本计划吗？" : "确定删除该脚本计划吗？");
        showConfirm(message).then(function (ok) {
            if (!ok) return;
            request(base, action === "delete" ? {method: "DELETE"} : {
                method: "PUT", headers: {"Content-Type": "application/json"},
                body: JSON.stringify({action: action})
            }).then(function () { showToast("操作成功", "success"); loadSchedules(); })
              .catch(function (error) { showToast("操作失败：" + error.message, "error"); });
        });
    });

    loadDependencies();
}
