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

    /* ===== Modal sub-tabs ===== */
    function activateModalTab(target) {
        document.querySelectorAll(".agent-modal-tabs .tab-btn").forEach(function (tab) {
            var active = tab.getAttribute("data-agent-tab") === target;
            tab.classList.toggle("active", active);
            tab.setAttribute("aria-selected", active ? "true" : "false");
        });
        document.querySelectorAll(".agent-tab-panel").forEach(function (panel) {
            panel.hidden = panel.getAttribute("data-agent-panel") !== target;
        });
        updateBulkSelectionControl(target);
    }
    document.querySelectorAll(".agent-modal-tabs .tab-btn").forEach(function (tab) {
        tab.addEventListener("click", function () {
            activateModalTab(tab.getAttribute("data-agent-tab"));
        });
    });

    function loadModelOptions() {
        return CatalogApi.list("models")
            .then(function (models) {
                var select = document.getElementById("agent-model");
                var options = '<option value="">跟随默认模型</option>';
                models.forEach(function (m) {
                    if (!m.enabled) return;
                    options += '<option value="' + m.id + '">' + m.id + "（" + m.model + "）</option>";
                });
                select.innerHTML = options;
            })
            .catch(function (err) {
                showToast("加载模型列表失败：" + err.message, "error");
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
    var capabilityKinds = toolKinds.concat(["knowledge", "datasource"]);
    var knowledgeContainer = document.getElementById("tools-knowledge");
    var datasourceContainer = document.getElementById("tools-datasource");
    var bulkSelectionButtons = document.querySelectorAll(".agent-tool-toggle-selection");

    function toolContainer(kind) {
        if (kind === "knowledge") return knowledgeContainer;
        if (kind === "datasource") return datasourceContainer;
        return toolContainers[kind];
    }

    function updateBulkSelectionControl(kind) {
        if (capabilityKinds.indexOf(kind) === -1) return;
        var bulkSelectionButton = document.querySelector(
            '.agent-tool-toggle-selection[data-tool-kind="' + kind + '"]'
        );
        if (!bulkSelectionButton) return;
        var container = toolContainer(kind);
        var boxes = container ? container.querySelectorAll("input[type=checkbox]") : [];
        var allSelected = boxes.length && Array.prototype.every.call(boxes, function (box) {
            return box.checked;
        });
        bulkSelectionButton.disabled = !boxes.length;
        bulkSelectionButton.textContent = allSelected ? "取消全选" : "全选";
    }

    bulkSelectionButtons.forEach(function (bulkSelectionButton) {
        bulkSelectionButton.addEventListener("click", function () {
            var kind = bulkSelectionButton.getAttribute("data-tool-kind");
            var container = toolContainer(kind);
            if (!container) return;
            var boxes = container.querySelectorAll("input[type=checkbox]");
            var selectAll = !boxes.length || !Array.prototype.every.call(boxes, function (box) {
                return box.checked;
            });
            boxes.forEach(function (box) { box.checked = selectAll; });
            if (kind === "knowledge") updateKnowledgeCount();
            else if (kind === "datasource") updateDatasourceCount();
            else updateCount(kind);
            updateBulkSelectionControl(kind);
        });
    });

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
                return toolCardHtml(t.name, t.name, t.description, "plugin")
                    .replace("<input ", '<input data-plugin-id="' + escapeHtml(p.id) + '" ');
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

    /* ===== Datasource binding ===== */
    function datasourceDescription(item) {
        var parts = [];
        if (item.engine) parts.push(String(item.engine).toUpperCase());
        if (item.database) parts.push(item.database);
        var tableCount = (item.tables || []).length;
        parts.push(tableCount ? "授权 " + tableCount + " 张表" : "未限定表（授权范围为整库）");
        if (item.driver_ready === false) parts.push("驱动未安装");
        return parts.join(" · ");
    }

    function renderDatasources(items) {
        var usable = (items || []).filter(function (item) {
            return item && item.id && item.enabled !== false;
        });
        if (!usable.length) {
            datasourceContainer.innerHTML =
                '<div class="tool-empty">暂无已启用的数据源，请先在「系统工具 → 数据库」中新增并启用。</div>';
            return;
        }
        datasourceContainer.innerHTML =
            '<div class="tool-hint">绑定后，该智能体在对话中会自动获得只读检索能力' +
            '（db_list_tables / db_describe_table / db_query），并把授权表结构注入系统提示词。' +
            '写操作（db_execute）不会被自动开启。</div>' +
            usable.map(function (item) {
                return toolCardHtml(
                    item.id,
                    item.name || item.id,
                    datasourceDescription(item),
                    "datasource"
                );
            }).join("");
    }

    function updateDatasourceCount() {
        var checked = datasourceContainer.querySelectorAll("input:checked").length;
        document.getElementById("tools-datasource-count").textContent =
            checked ? "（已选 " + checked + "）" : "";
    }

    function loadToolOptions() {
        return Promise.all([
            fetch("/api/tools").then(function (r) { return r.json(); }),
            fetch("/api/plugins").then(function (r) { return r.json(); }),
            CatalogApi.list("skills"),
            CatalogApi.list("mcp"),
            fetch("/api/knowledge/categories").then(function (r) {
                return r.ok ? r.json() : { categories: [] };
            }),
            fetch("/api/datasources").then(function (r) {
                return r.ok ? r.json() : [];
            }).catch(function () { return []; })
        ]).then(function (results) {
            var builtinTools = results[0] || [];
            var plugins = results[1] || [];
            var skills = results[2] || [];
            var servers = results[3] || [];
            var categories = (results[4] && results[4].categories) || [];
            var datasources = results[5] || [];

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

            var groups = {};
            categories.forEach(function (category) {
                var key = category.scope === "public"
                    ? "公共知识库"
                    : "租户 " + String(category.tenant_id || "").slice(0, 8);
                if (!groups[key]) groups[key] = [];
                groups[key].push(category);
            });
            var groupNames = Object.keys(groups);
            knowledgeContainer.innerHTML = groupNames.length ? groupNames.map(function (name) {
                return '<div class="tool-plugin-group"><div class="tool-plugin-name">' +
                    escapeHtml(name) + '</div><div class="tool-checkboxes tool-checkboxes-nested">' +
                    groups[name].map(function (category) {
                        return toolCardHtml(
                            category.category_id,
                            category.name,
                            category.description || (category.scope === "public" ? "公共" : "租户私有"),
                            "knowledge"
                        );
                    }).join("") + "</div></div>";
            }).join("") : '<div class="tool-empty">暂无知识库</div>';

            renderDatasources(datasources);

            toolKinds.forEach(updateCount);
            updateKnowledgeCount();
            updateDatasourceCount();
            capabilityKinds.forEach(updateBulkSelectionControl);
        });
    }

    function updateKnowledgeCount() {
        var checked = knowledgeContainer.querySelectorAll("input:checked").length;
        document.getElementById("tools-knowledge-count").textContent =
            checked ? "（已选 " + checked + "）" : "";
    }

    knowledgeContainer.addEventListener("change", function () {
        updateKnowledgeCount();
        updateBulkSelectionControl("knowledge");
    });
    datasourceContainer.addEventListener("change", function () {
        updateDatasourceCount();
        updateBulkSelectionControl("datasource");
    });
    toolKinds.forEach(function (kind) {
        toolContainers[kind].addEventListener("change", function () {
            updateCount(kind);
            updateBulkSelectionControl(kind);
        });
    });

    function setToolSelection(agent, categoryIds) {
        var toolSet = {}, pluginToolSet = {}, skillSet = {}, mcpSet = {};
        (agent.tools || []).forEach(function (n) { toolSet[n] = true; });
        Object.keys(agent.plugin_tools || {}).forEach(function (pluginId) {
            (agent.plugin_tools[pluginId] || []).forEach(function (n) {
                pluginToolSet[pluginId + ":" + n] = true;
            });
        });
        (agent.skills || []).forEach(function (n) { skillSet[n] = true; });
        (agent.mcp_servers || []).forEach(function (n) { mcpSet[n] = true; });
        toolKinds.forEach(function (kind) {
            toolContainers[kind].querySelectorAll("input").forEach(function (box) {
                if (kind === "skill") box.checked = !!skillSet[box.value];
                else if (kind === "mcp") box.checked = !!mcpSet[box.value];
                else if (kind === "plugin") {
                    box.checked = !!pluginToolSet[
                        box.getAttribute("data-plugin-id") + ":" + box.value
                    ];
                } else box.checked = !!toolSet[box.value];
            });
            updateCount(kind);
        });
        var unresolved = [];
        Object.keys(agent.plugin_tools || {}).forEach(function (pluginId) {
            (agent.plugin_tools[pluginId] || []).forEach(function (toolName) {
                var found = Array.prototype.some.call(
                    toolContainers.plugin.querySelectorAll("input"),
                    function (box) {
                        return box.getAttribute("data-plugin-id") === pluginId &&
                            box.value === toolName;
                    }
                );
                if (!found) {
                    unresolved.push({
                        plugin_id: pluginId,
                        tool_name: toolName
                    });
                }
            });
        });
        if (unresolved.length) {
            toolContainers.plugin.insertAdjacentHTML(
                "beforeend",
                '<div class="tool-plugin-group unresolved-plugin-binding">' +
                '<div class="tool-plugin-name">未解析的插件绑定</div>' +
                '<div class="tool-checkboxes tool-checkboxes-nested">' +
                unresolved.map(function (item) {
                    return toolCardHtml(
                        item.tool_name,
                        item.tool_name + "（插件缺失）",
                        "插件 " + item.plugin_id +
                            " 当前未安装；保留绑定但不会阻止核心启动。",
                        "plugin"
                    ).replace(
                        "<input ",
                        '<input checked data-plugin-id="' +
                            escapeHtml(item.plugin_id) + '" '
                    );
                }).join("") + "</div></div>"
            );
            updateCount("plugin");
        }
        var categorySet = {};
        (categoryIds || []).forEach(function (id) { categorySet[id] = true; });
        knowledgeContainer.querySelectorAll("input").forEach(function (box) {
            box.checked = !!categorySet[box.value];
        });
        updateKnowledgeCount();
        setDatasourceSelection(agent.datasources || []);
        capabilityKinds.forEach(updateBulkSelectionControl);
    }

    function setDatasourceSelection(datasourceIds) {
        var bound = {};
        (datasourceIds || []).forEach(function (id) { bound[id] = true; });
        var known = {};
        datasourceContainer.querySelectorAll("input").forEach(function (box) {
            known[box.value] = true;
            box.checked = !!bound[box.value];
        });
        var missing = (datasourceIds || []).filter(function (id) { return !known[id]; });
        if (missing.length) {
            datasourceContainer.insertAdjacentHTML(
                "beforeend",
                '<div class="tool-warning">以下已绑定的数据源当前不可用（已停用或已删除），保存后会自动解除绑定：' +
                escapeHtml(missing.join("、")) + "</div>"
            );
        }
        updateDatasourceCount();
    }

    function collectSelection() {
        var tools = [];
        toolContainers.builtin.querySelectorAll("input:checked").forEach(function (b) {
            tools.push(b.value);
        });
        var pluginTools = {};
        toolContainers.plugin.querySelectorAll("input:checked").forEach(function (b) {
            var pluginId = b.getAttribute("data-plugin-id");
            if (!pluginTools[pluginId]) pluginTools[pluginId] = [];
            pluginTools[pluginId].push(b.value);
        });
        var skills = [];
        toolContainers.skill.querySelectorAll("input:checked").forEach(function (b) { skills.push(b.value); });
        var mcpServers = [];
        toolContainers.mcp.querySelectorAll("input:checked").forEach(function (b) { mcpServers.push(b.value); });
        var knowledgeCategories = [];
        knowledgeContainer.querySelectorAll("input:checked").forEach(function (b) {
            knowledgeCategories.push(b.value);
        });
        var datasources = [];
        datasourceContainer.querySelectorAll("input:checked").forEach(function (b) {
            datasources.push(b.value);
        });
        return {
            tools: tools, plugin_tools: pluginTools,
            skills: skills, mcp_servers: mcpServers,
            knowledge_category_ids: knowledgeCategories,
            datasources: datasources
        };
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
        document.getElementById("agent-enabled").checked = true;
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

    function openModal() {
        activateModalTab("basic");
        modal.style.display = "";
    }
    function closeModal() { modal.style.display = "none"; }

    function loadAgents() {
        Promise.all([
            CatalogApi.list("agents"),
            fetch("/api/v2/platform/agents/active").then(function (r) { return r.json(); })
        ]).then(function (results) {
            var agents = results[0];
            var defaultId = results[1].id;
            listEl.innerHTML = agents.map(function (a) {
                return agentCardHtml(a, a.id === defaultId);
            }).join("");
        });
    }

    function agentCardHtml(a, isDefault) {
        var enabled = a.enabled !== false;
        var badges = "";
        if (isDefault) badges += '<span class="badge badge-primary">默认</span>';
        if (!enabled) badges += '<span class="badge badge-fallback">已禁用</span>';

        var caps = a.capabilities || [];
        var capTags = caps.slice(0, 4).map(function (c) {
            return '<span class="agent-cap-pill">' + escapeHtml(c.name) + "</span>";
        }).join("");
        if (caps.length > 4) {
            capTags += '<span class="agent-cap-pill agent-cap-more">+' + (caps.length - 4) + "</span>";
        }

        var modelInfo = a.model ? a.model : "跟随默认模型";
        var pluginToolCount = Object.keys(a.plugin_tools || {}).reduce(function (total, id) {
            return total + (a.plugin_tools[id] || []).length;
        }, 0);
        var counts = "工具 " + ((a.tools || []).length + pluginToolCount) +
            " · 技能 " + (a.skills || []).length +
            " · MCP " + (a.mcp_servers || []).length;
        if ((a.datasources || []).length) {
            counts += " · 数据源 " + a.datasources.length;
        }

        var actions = '<div class="model-card-footer agent-card-actions">';
        if (!isDefault) {
            actions += '<button class="btn-secondary" data-action="toggle" data-id="' + a.id + '" data-enabled="' + (enabled ? "1" : "0") + '">' +
                (enabled ? "禁用" : "启用") + "</button> ";
        }
        actions += '<button class="btn-edit" data-action="edit" data-id="' + a.id + '">编辑</button> ';
        if (!isDefault) {
            actions += '<button class="btn-danger" data-action="delete" data-id="' + a.id + '">删除</button>';
        }
        actions += "</div>";

        return '<div class="agent-card' + (enabled ? "" : " disabled") + '" data-id="' + a.id + '">' +
            "<h5>" + escapeHtml(a.name) + " " + badges + "</h5>" +
            '<p class="agent-card-role">' + escapeHtml(a.role || "") + "</p>" +
            '<p class="agent-card-desc">' + escapeHtml(a.description || "") + "</p>" +
            (capTags ? '<div class="agent-cap-tags">' + capTags + "</div>" : "") +
            "<p>模型：" + escapeHtml(modelInfo) + "</p>" +
            "<p>" + counts + "</p>" +
            actions +
            "</div>";
    }

    listEl.addEventListener("click", function (e) {
        var btn = e.target.closest("[data-action]");
        if (!btn) return;
        e.preventDefault();
        e.stopPropagation();
        var action = btn.getAttribute("data-action");
        var id = btn.getAttribute("data-id");

        if (action === "toggle") {
            var nextEnabled = btn.getAttribute("data-enabled") !== "1";
            CatalogApi.patch("agents", id, { enabled: nextEnabled })
                .then(function () {
                    showToast((nextEnabled ? "已启用智能体 " : "已禁用智能体 ") + id, "success");
                    loadAgents();
                })
                .catch(function (err) { showToast("操作失败：" + err.message, "error"); });
        }

        if (action === "delete") {
            showConfirm("确定要删除智能体「" + id + "」吗？").then(function (ok) {
                if (!ok) return;
                CatalogApi.remove("agents", id)
                    .then(function () {
                        showToast("已删除智能体 " + id, "success");
                        loadAgents();
                    })
                    .catch(function (err) { showToast("删除失败：" + err.message, "error"); });
            });
        }

        if (action === "edit") {
            CatalogApi.get("agents", id)
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
                    document.getElementById("agent-enabled").checked = a.enabled !== false;
                    loadModelOptions().then(function () {
                        document.getElementById("agent-model").value = a.model || "";
                    });
                    Promise.all([
                        loadToolOptions(),
                        fetch("/api/v2/platform/agents/" + encodeURIComponent(id) + "/knowledge-categories")
                            .then(function (r) { return r.ok ? r.json() : { category_ids: [] }; })
                    ]).then(function (results) {
                        setToolSelection(a, results[1].category_ids || []);
                    });
                    openModal();
                });
        }
    });

    form.addEventListener("submit", function (e) {
        e.preventDefault();

        if (!form.checkValidity()) {
            var invalid = form.querySelector(":invalid");
            var panel = invalid && invalid.closest(".agent-tab-panel");
            if (panel) activateModalTab(panel.getAttribute("data-agent-panel"));
            form.reportValidity();
            return;
        }

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
            enabled: document.getElementById("agent-enabled").checked,
            tools: selection.tools,
            plugin_tools: selection.plugin_tools,
            skills: selection.skills,
            mcp_servers: selection.mcp_servers,
            datasources: selection.datasources,
            capabilities: []
        };

        var resourceId = editingId || document.getElementById("agent-id").value;
        if (!editingId) payload.id = resourceId;

        CatalogApi.save("agents", resourceId, payload)
            .then(function (saved) {
                return fetch("/api/v2/platform/agents/" + encodeURIComponent(resourceId) + "/knowledge-categories", {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ category_ids: selection.knowledge_category_ids })
                }).then(function (r) {
                    if (!r.ok) return r.json().then(function (d) { throw new Error(d.detail); });
                    return saved;
                });
            })
            .then(function () {
                showToast(editingId ? "已保存修改" : "已创建智能体", "success");
                closeModal();
                loadAgents();
            })
            .catch(function (err) { showToast("保存失败：" + err.message, "error"); });
    });
}
