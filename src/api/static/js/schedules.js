/* ===== Schedules page ===== */
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
                        '</div>' +
                        '<div class="card-actions">' +
                        '<button class="btn-edit" data-action="edit" data-id="' + t.id + '">编辑</button>' +
                        '<button class="btn-danger" data-action="delete" data-id="' + t.id + '">删除</button>' +
                        '</div></div>' +
                        '<div class="card-body">' +
                        '<p><strong>Cron：</strong><code>' + cronDisplay + '</code></p>' +
                        '<p><strong>动作：</strong>' + (actionLabels[t.action.type] || t.action.type) + '</p>' +
                        '<p><strong>目标：</strong>' + escapeHtml(t.target) + '</p>' +
                        '<p><strong>条件：</strong>' + escapeHtml(condDisplay) + '</p>' +
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
        } else if (actionType === "plugin") {
            action.plugin_id = document.getElementById("action-plugin-id").value.trim();
            action.tool_name = document.getElementById("action-tool-name").value.trim();
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
