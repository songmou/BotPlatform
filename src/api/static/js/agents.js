/* ===== Agents page ===== */
function initAgents() {
    var listEl = document.getElementById("agent-list");
    var modal = document.getElementById("agent-modal");
    var modalTitle = document.getElementById("modal-title");
    var form = document.getElementById("agent-form");
    var idGroup = document.getElementById("form-id-group");
    var editingId = null;

    loadAgents();
    loadModelOptions();

    function loadModelOptions() {
        return fetch("/api/models")
            .then(function (r) { return r.json(); })
            .then(function (models) {
                var select = document.getElementById("agent-model");
                var options = '<option value="">跟随默认模型</option>';
                models.forEach(function (m) {
                    if (!m.enabled) return;
                    options += '<option value="' + m.id + '">' + m.id + "（" + m.model + "）</option>";
                });
                select.innerHTML = options;
            });
    }

    var tempSlider = document.getElementById("agent-temperature");
    var tempLabel = document.getElementById("temp-value");
    tempSlider.addEventListener("input", function () {
        tempLabel.textContent = tempSlider.value === "" ? "默认" : tempSlider.value;
    });

    function resetTempSlider() {
        tempSlider.value = "";
        tempLabel.textContent = "默认";
    }

    var toolContainers = {
        builtin: document.getElementById("tools-builtin"),
        plugin: document.getElementById("tools-plugin"),
        skill: document.getElementById("tools-skill"),
        mcp: document.getElementById("tools-mcp")
    };
    var toolKinds = ["builtin", "plugin", "skill", "mcp"];

    function toolCardHtml(value, label, description, kind) {
        var desc = description
            ? '<span class="tool-desc">' + escapeHtml(description) + "</span>"
            : "";
        return '<label class="tool-check">' +
            '<input type="checkbox" data-kind="' + kind + '" value="' + escapeHtml(value) + '">' +
            '<span class="tool-info">' +
            '<span class="tool-name">' + escapeHtml(label) + "</span>" +
            desc +
            "</span></label>";
    }

    function renderCheckboxes(container, items, kind) {
        if (!items.length) {
            container.innerHTML = '<div class="tool-empty">暂无可选项</div>';
            return;
        }
        container.innerHTML = items.map(function (it) {
            return toolCardHtml(it.value, it.label, it.description, kind);
        }).join("");
    }

    function renderPluginGroups(container, plugins) {
        var enabled = plugins.filter(function (p) {
            return p.enabled && (p.tools || []).length;
        });
        if (!enabled.length) {
            container.innerHTML = '<div class="tool-empty">暂无可选项</div>';
            return;
        }
        container.innerHTML = enabled.map(function (p) {
            var cards = p.tools.map(function (t) {
                return toolCardHtml(t.name, t.name, t.description, "plugin");
            }).join("");
            return '<div class="tool-plugin-group">' +
                '<div class="tool-plugin-name">' + escapeHtml(p.id) + "</div>" +
                '<div class="tool-checkboxes tool-checkboxes-nested">' + cards + "</div>" +
                "</div>";
        }).join("");
    }

    function updateCount(kind) {
        var checked = toolContainers[kind].querySelectorAll("input:checked").length;
        document.getElementById("tools-" + kind + "-count").textContent =
            checked ? "（已选 " + checked + "）" : "";
    }

    function loadToolOptions() {
        return Promise.all([
            fetch("/api/tools").then(function (r) { return r.json(); }),
            fetch("/api/plugins").then(function (r) { return r.json(); }),
            fetch("/api/skills").then(function (r) { return r.json(); }),
            fetch("/api/mcp").then(function (r) { return r.json(); })
        ]).then(function (results) {
            var builtinTools = results[0] || [];
            var plugins = results[1] || [];
            var skills = results[2] || [];
            var servers = results[3] || [];

            renderCheckboxes(toolContainers.builtin, builtinTools.map(function (t) {
                return { value: t.name, label: t.name, description: t.description };
            }), "builtin");

            renderPluginGroups(toolContainers.plugin, plugins);

            renderCheckboxes(toolContainers.skill, skills.map(function (s) {
                return { value: s.id, label: s.name + (s.enabled ? "" : "（已禁用）"), description: s.description };
            }), "skill");

            renderCheckboxes(toolContainers.mcp, servers.map(function (m) {
                return { value: m.id, label: m.name + (m.enabled ? "" : "（已禁用）"), description: mcpTransportLabel(m.transport) };
            }), "mcp");

            toolKinds.forEach(updateCount);
        });
    }

    function setToolSelection(agent) {
        var toolSet = {}, skillSet = {}, mcpSet = {};
        (agent.tools || []).forEach(function (n) { toolSet[n] = true; });
        (agent.skills || []).forEach(function (n) { skillSet[n] = true; });
        (agent.mcp_servers || []).forEach(function (n) { mcpSet[n] = true; });
        toolKinds.forEach(function (kind) {
            toolContainers[kind].querySelectorAll("input").forEach(function (box) {
                if (kind === "skill") box.checked = !!skillSet[box.value];
                else if (kind === "mcp") box.checked = !!mcpSet[box.value];
                else box.checked = !!toolSet[box.value];
            });
            updateCount(kind);
        });
    }

    function collectSelection() {
        var tools = [];
        ["builtin", "plugin"].forEach(function (kind) {
            toolContainers[kind].querySelectorAll("input:checked").forEach(function (b) {
                tools.push(b.value);
            });
        });
        var skills = [];
        toolContainers.skill.querySelectorAll("input:checked").forEach(function (b) { skills.push(b.value); });
        var mcpServers = [];
        toolContainers.mcp.querySelectorAll("input:checked").forEach(function (b) { mcpServers.push(b.value); });
        return { tools: tools, skills: skills, mcp_servers: mcpServers };
    }

    document.getElementById("create-agent-btn").addEventListener("click", function () {
        editingId = null;
        modalTitle.textContent = "新建智能体";
        idGroup.style.display = "";
        form.reset();
        document.getElementById("agent-id").disabled = false;
        document.getElementById("agent-model").value = "";
        document.getElementById("agent-greeting").value = "";
        document.getElementById("agent-hints").value = "";
        document.getElementById("agent-max-tokens").value = "";
        resetTempSlider();
        loadModelOptions();
        loadToolOptions();
        openModal();
    });

    document.getElementById("modal-close").addEventListener("click", closeModal);
    document.getElementById("modal-cancel").addEventListener("click", closeModal);
    modal.addEventListener("click", function (e) {
        if (e.target === modal) closeModal();
    });

    function openModal() { modal.style.display = ""; }
    function closeModal() { modal.style.display = "none"; }

    function loadAgents() {
        fetch("/api/agents")
            .then(function (r) { return r.json(); })
            .then(function (agents) {
                listEl.innerHTML = agents.map(function (a) {
                    var caps = a.capabilities.map(function (c) {
                        return "<li><strong>" + c.name + "</strong>：" + c.description + "</li>";
                    }).join("");
                    var tools = a.tools.length ? a.tools.join("、") : "无";
                    var skills = (a.skills && a.skills.length) ? a.skills.join("、") : "无";
                    var mcpServers = (a.mcp_servers && a.mcp_servers.length) ? a.mcp_servers.join("、") : "无";
                    var modelInfo = a.model ? a.model : "跟随默认模型";
                    return '<details class="agent-card" data-id="' + a.id + '">' +
                        "<summary>" + a.name + " <small>" + a.role + "</small>" +
                        '<span class="agent-actions">' +
                        '<button class="btn-edit" data-action="edit" data-id="' + a.id + '">编辑</button>' +
                        '<button class="btn-danger" data-action="delete" data-id="' + a.id + '">删除</button>' +
                        "</span></summary>" +
                        '<div class="agent-detail">' +
                        "<p>" + escapeHtml(a.description) + "</p>" +
                        "<p><strong>模型：</strong>" + escapeHtml(modelInfo) + "</p>" +
                        (caps ? "<ul>" + caps + "</ul>" : "") +
                        "<p><strong>工具：</strong>" + escapeHtml(tools) + "</p>" +
                        "<p><strong>技能：</strong>" + escapeHtml(skills) + "</p>" +
                        "<p><strong>MCP 服务：</strong>" + escapeHtml(mcpServers) + "</p>" +
                        "<p><strong>系统提示词：</strong></p>" +
                        "<pre>" + escapeHtml(a.system_prompt) + "</pre>" +
                        "</div></details>";
                }).join("");
            });
    }

    listEl.addEventListener("click", function (e) {
        var btn = e.target.closest("[data-action]");
        if (!btn) return;
        e.preventDefault();
        e.stopPropagation();
        var action = btn.getAttribute("data-action");
        var id = btn.getAttribute("data-id");

        if (action === "delete") {
            showConfirm("确定要删除智能体「" + id + "」吗？").then(function (ok) {
                if (!ok) return;
                fetch("/api/agents/" + id, { method: "DELETE" })
                    .then(function (r) {
                        if (!r.ok) return r.json().then(function (d) { throw new Error(d.detail); });
                        showToast("已删除智能体 " + id, "success");
                        loadAgents();
                    })
                    .catch(function (err) { showToast("删除失败：" + err.message, "error"); });
            });
        }

        if (action === "edit") {
            fetch("/api/agents/" + id)
                .then(function (r) { return r.json(); })
                .then(function (a) {
                    editingId = id;
                    modalTitle.textContent = "编辑智能体";
                    idGroup.style.display = "none";
                    document.getElementById("agent-id").value = a.id;
                    document.getElementById("agent-name").value = a.name;
                    document.getElementById("agent-role").value = a.role;
                    document.getElementById("agent-desc").value = a.description;
                    document.getElementById("agent-prompt").value = a.system_prompt;
                    document.getElementById("agent-greeting").value = a.greeting || "";
                    document.getElementById("agent-hints").value = (a.greeting_hints || []).join("；");
                    if (a.temperature != null) {
                        tempSlider.value = a.temperature;
                        tempLabel.textContent = String(a.temperature);
                    } else {
                        resetTempSlider();
                    }
                    document.getElementById("agent-max-tokens").value = a.max_tokens || "";
                    loadModelOptions().then(function () {
                        document.getElementById("agent-model").value = a.model || "";
                    });
                    loadToolOptions().then(function () {
                        setToolSelection(a);
                    });
                    openModal();
                });
        }
    });

    form.addEventListener("submit", function (e) {
        e.preventDefault();

        var hintsRaw = document.getElementById("agent-hints").value.trim();
        var hints = hintsRaw ? hintsRaw.split(/[;；]/).map(function (s) { return s.trim(); }).filter(Boolean) : [];
        var tempVal = document.getElementById("agent-temperature").value;
        var maxTokVal = document.getElementById("agent-max-tokens").value;
        var selection = collectSelection();

        var payload = {
            name: document.getElementById("agent-name").value,
            role: document.getElementById("agent-role").value,
            description: document.getElementById("agent-desc").value,
            system_prompt: document.getElementById("agent-prompt").value,
            model: document.getElementById("agent-model").value || null,
            greeting: document.getElementById("agent-greeting").value.trim() || null,
            greeting_hints: hints,
            temperature: tempVal !== "" ? parseFloat(tempVal) : null,
            max_tokens: maxTokVal ? parseInt(maxTokVal, 10) : null,
            tools: selection.tools,
            skills: selection.skills,
            mcp_servers: selection.mcp_servers,
            capabilities: []
        };

        var url, method;
        if (editingId) {
            url = "/api/agents/" + editingId;
            method = "PUT";
        } else {
            payload.id = document.getElementById("agent-id").value;
            url = "/api/agents";
            method = "POST";
        }

        fetch(url, {
            method: method,
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        })
            .then(function (r) {
                if (!r.ok) return r.json().then(function (d) { throw new Error(d.detail); });
                showToast(editingId ? "已保存修改" : "已创建智能体", "success");
                closeModal();
                loadAgents();
            })
            .catch(function (err) { showToast("保存失败：" + err.message, "error"); });
    });
}

