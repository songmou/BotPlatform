/* ===== Agents page ===== */
// Brand icon file per platform (WeCom/Feishu ship as PNG, others as SVG).
var PUBLISH_ICON_EXT = { wechat: "svg", wecom: "png", dingtalk: "svg", feishu: "png" };
function publishIconSrc(platform) {
    return "/static/img/publish/" + platform + "." + (PUBLISH_ICON_EXT[platform] || "svg");
}
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
    var capabilityKinds = toolKinds.concat(["knowledge"]);
    var knowledgeContainer = document.getElementById("tools-knowledge");
    var bulkSelectionButtons = document.querySelectorAll(".agent-tool-toggle-selection");

    function toolContainer(kind) {
        return kind === "knowledge"
            ? knowledgeContainer
            : toolContainers[kind];
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

    function loadToolOptions() {
        return Promise.all([
            fetch("/api/tools").then(function (r) { return r.json(); }),
            fetch("/api/plugins").then(function (r) { return r.json(); }),
            fetch("/api/skills").then(function (r) { return r.json(); }),
            fetch("/api/mcp").then(function (r) { return r.json(); }),
            fetch("/api/knowledge/categories").then(function (r) {
                return r.ok ? r.json() : { categories: [] };
            })
        ]).then(function (results) {
            var builtinTools = results[0] || [];
            var plugins = results[1] || [];
            var skills = results[2] || [];
            var servers = results[3] || [];
            var categories = (results[4] && results[4].categories) || [];

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

            toolKinds.forEach(updateCount);
            updateKnowledgeCount();
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
        capabilityKinds.forEach(updateBulkSelectionControl);
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
        return {
            tools: tools, plugin_tools: pluginTools,
            skills: skills, mcp_servers: mcpServers,
            knowledge_category_ids: knowledgeCategories
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
            fetch("/api/agents").then(function (r) { return r.json(); }),
            fetch("/api/agents/active").then(function (r) { return r.json(); })
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

        var publishRow = '<div class="agent-publish-row"><span class="agent-publish-label">发布：</span>' +
            ["wechat", "wecom", "feishu", "dingtalk"].map(function (p) {
                return '<button class="agent-publish-icon" data-action="publish-platform" data-id="' + a.id +
                    '" data-platform="' + p + '" title="' +
                    ({ wechat: "微信", wecom: "企业微信", feishu: "飞书", dingtalk: "钉钉" })[p] + '">' +
                    '<img src="' + publishIconSrc(p) + '" alt="' + p + '"></button>';
            }).join("") + "</div>";

        return '<div class="agent-card' + (enabled ? "" : " disabled") + '" data-id="' + a.id + '">' +
            "<h5>" + escapeHtml(a.name) + " " + badges + "</h5>" +
            '<p class="agent-card-role">' + escapeHtml(a.role || "") + "</p>" +
            '<p class="agent-card-desc">' + escapeHtml(a.description || "") + "</p>" +
            (capTags ? '<div class="agent-cap-tags">' + capTags + "</div>" : "") +
            "<p>模型：" + escapeHtml(modelInfo) + "</p>" +
            "<p>" + counts + "</p>" +
            actions +
            publishRow +
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
            fetch("/api/agents/" + id, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ enabled: nextEnabled }),
            })
                .then(function (r) {
                    if (!r.ok) return r.json().then(function (d) { throw new Error(d.detail); });
                    showToast((nextEnabled ? "已启用智能体 " : "已禁用智能体 ") + id, "success");
                    loadAgents();
                })
                .catch(function (err) { showToast("操作失败：" + err.message, "error"); });
        }

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

        if (action === "publish-platform") {
            var platform = btn.getAttribute("data-platform");
            if (platform === "dingtalk" || platform === "feishu") {
                showToast(({ dingtalk: "钉钉", feishu: "飞书" })[platform] + "发布暂未开放，敬请期待", "info");
                return;
            }
            openPublishModal(id, platform);
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
                    document.getElementById("agent-enabled").checked = a.enabled !== false;
                    loadModelOptions().then(function () {
                        document.getElementById("agent-model").value = a.model || "";
                    });
                    Promise.all([
                        loadToolOptions(),
                        fetch("/api/agents/" + encodeURIComponent(id) + "/knowledge-categories")
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
                return r.json();
            })
            .then(function (saved) {
                return fetch("/api/agents/" + encodeURIComponent(saved.id) + "/knowledge-categories", {
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

    /* ===== Publish ===== */
    var publishModal = document.getElementById("publish-modal");
    var publishPlatforms = document.getElementById("publish-platforms");
    var publishTitle = document.getElementById("publish-modal-title");
    var publishAgentId = null;
    var publishPlatform = null;
    // Per-open-session guard so auto-association fires at most once per
    // platform after a successful scan/config, avoiding render loops.
    var autoAssocDone = {};
    var boundByPlatform = {};
    // Guard so the WeChat panel auto-starts a login (to show a QR) only once
    // per open session, not on every 2s status poll.
    var wechatAutoStarted = false;

    document.getElementById("publish-modal-close").addEventListener("click", closePublishModal);
    document.getElementById("publish-modal-cancel").addEventListener("click", closePublishModal);
    publishModal.addEventListener("click", function (e) {
        if (e.target === publishModal) closePublishModal();
    });

    function closePublishModal() {
        publishModal.style.display = "none";
        stopWechatPoll();
    }

    function openPublishModal(agentId, platform) {
        publishAgentId = agentId;
        publishPlatform = platform || null;
        autoAssocDone = {};
        boundByPlatform = {};
        wechatAutoStarted = false;
        publishTitle.textContent = "发布智能体「" + agentId + "」到" + platformName(platform);
        renderPublish();
        publishModal.style.display = "";
    }

    // Associate the current agent to a platform once its bot connection is
    // ready (WeChat scanned / WeCom configured). Guarded to fire once per
    // open session so re-renders do not loop.
    function autoAssociate(platform) {
        if (autoAssocDone[platform]) return;
        autoAssocDone[platform] = true;
        fetch("/api/publish/" + platform + "/agents", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ agent_id: publishAgentId }),
        }).then(handleRes).then(function () {
            showToast("已关联到" + platformName(platform), "success");
            loadAgents();
            renderPublish();
        }).catch(function (err) {
            showToast("关联失败：" + err.message, "error");
        });
    }

    function renderPublish() {
        publishPlatforms.innerHTML = '<div class="tool-empty">加载中…</div>';
        fetch("/api/publish")
            .then(function (r) { return r.json(); })
            .then(function (data) {
                var platforms = (data && data.platforms) || [];
                if (publishPlatform) {
                    platforms = platforms.filter(function (p) {
                        return p.platform === publishPlatform;
                    });
                }
                platforms.forEach(function (p) {
                    boundByPlatform[p.platform] = p.agent ? p.agent.agent_id : null;
                });
                publishPlatforms.innerHTML = platforms.map(platformCardHtml).join("");
                if (publishPlatforms.querySelector('[data-role="wechat-connect"]')) {
                    refreshWechatConnect();
                }
                // WeCom: once credentials are configured, auto-associate only
                // when the platform has no binding yet. If it is bound to a
                // different agent, switching requires explicit confirmation.
                platforms.forEach(function (p) {
                    if (p.platform === "wecom" && p.config && p.config.configured &&
                        !boundByPlatform.wecom) {
                        autoAssociate("wecom");
                    }
                });
            })
            .catch(function () {
                publishPlatforms.innerHTML = '<div class="tool-empty">加载失败</div>';
            });
    }

    var wechatPollTimer = null;

    function stopWechatPoll() {
        if (wechatPollTimer) {
            clearTimeout(wechatPollTimer);
            wechatPollTimer = null;
        }
    }

    function refreshWechatConnect() {
        stopWechatPoll();
        var box = publishPlatforms.querySelector('[data-role="wechat-connect"]');
        if (!box || publishModal.style.display === "none") return;
        fetch("/api/publish/wechat/status")
            .then(function (r) { return r.json(); })
            .then(function (s) {
                box = publishPlatforms.querySelector('[data-role="wechat-connect"]');
                if (!box) return;
                renderWechatConnect(box, s);
                if (s.state === "pending" || s.state === "scanned") {
                    wechatPollTimer = setTimeout(refreshWechatConnect, 2000);
                }
            })
            .catch(function () {
                if (box) box.innerHTML = '<div class="tool-empty">连接状态获取失败</div>';
            });
    }

    function renderWechatConnect(box, s) {
        // A re-scan keeps the old credentials until confirmed, so `connected`
        // may still be true while a new login is in progress. Show the QR
        // first whenever a login is pending/scanning.
        if (s.state === "pending" || s.state === "scanned") {
            var hint = s.state === "scanned"
                ? "已扫码，请在手机上确认…"
                : "打开微信，扫描二维码连接机器人";
            box.innerHTML = (s.qr
                ? '<img class="wechat-qr" src="' + s.qr + '" alt="微信登录二维码">'
                : '<div class="wecom-qr-placeholder">正在生成二维码…</div>') +
                '<p class="wechat-connect-hint">' + hint + "</p>";
            return;
        }
        if (s.connected) {
            box.innerHTML = '<div class="wechat-connect-status">' +
                '<span class="badge badge-success">机器人已连接</span>' +
                (s.bot_id ? '<span class="text-muted"> bot_id: ' + escapeHtml(s.bot_id) + "</span>" : "") +
                "</div>" +
                '<p class="wechat-connect-hint">若在别处重新绑定了该微信号导致掉线，可点此重新扫码夺回连接。</p>' +
                '<button type="button" class="btn-secondary" data-pub-action="wechat-login">重新扫码（换号/重连）</button>';
            // Auto-associate only when the platform has no binding yet;
            // switching from another agent needs explicit confirmation.
            if (!boundByPlatform.wechat) {
                autoAssociate("wechat");
            }
            return;
        }
        if (s.state === "failed") {
            box.innerHTML = '<div class="wechat-connect-status">' +
                '<span class="badge badge-muted">机器人未连接</span></div>' +
                (s.error ? '<p class="wechat-connect-error">' + escapeHtml(s.error) + "</p>" : "") +
                '<button type="button" class="btn-primary" data-pub-action="wechat-login">刷新二维码</button>';
            return;
        }
        // idle & not connected: auto-start a login so a QR is always shown.
        box.innerHTML = '<div class="wechat-connect-status">' +
            '<span class="badge badge-muted">机器人未连接</span></div>' +
            '<div class="wecom-qr-placeholder">正在生成二维码…</div>';
        if (!wechatAutoStarted) {
            wechatAutoStarted = true;
            fetch("/api/publish/wechat/login", { method: "POST" })
                .then(handleRes)
                .then(function () { refreshWechatConnect(); })
                .catch(function (err) { showToast("生成二维码失败：" + err.message, "error"); });
        }
    }

    function platformCardHtml(platform) {
        var header = '<div class="plugin-tile-header">' +
            '<div class="plugin-avatar plugin-avatar-img">' +
            '<img src="' + publishIconSrc(platform.platform) + '" alt="' + platform.platform + '"></div>' +
            '<div class="plugin-tile-info"><div class="plugin-tile-name">' + escapeHtml(platform.name) + "</div>";

        if (!platform.supported) {
            return '<div class="plugin-tile" style="opacity:0.55" data-placeholder="' + platform.platform + '">' +
                header + '<div class="plugin-tile-meta"><span class="badge badge-muted">暂未开放</span></div></div></div>' +
                '<button type="button" class="btn-secondary" data-pub-action="placeholder">敬请期待</button></div>';
        }

        var bound = platform.agent || null;
        var isThis = bound && bound.agent_id === publishAgentId;
        var statusBadge;
        if (isThis) {
            statusBadge = bound.enabled
                ? '<span class="badge badge-success">已关联本智能体</span>'
                : '<span class="badge badge-fallback">已禁用</span>';
        } else if (bound) {
            statusBadge = '<span class="badge badge-muted">已关联：' +
                escapeHtml(bound.agent_name || bound.agent_id) + "</span>";
        } else {
            statusBadge = '<span class="badge badge-muted">未关联</span>';
        }
        header += '<div class="plugin-tile-meta">' + statusBadge + "</div></div></div>";

        var body = "";
        if (platform.platform === "wechat") {
            body += '<div class="wechat-connect" data-role="wechat-connect">' +
                '<div class="tool-empty">正在获取连接状态…</div></div>';
        }
        if (platform.platform === "wecom") {
            var cfg = platform.config || {};
            if (cfg.configured) {
                body += '<div class="wechat-connect-status wecom-configured">' +
                    '<span class="badge badge-success">机器人已配置</span>' +
                    '<span class="text-muted">Bot ID: ' + escapeHtml(cfg.bot_id || "") + "</span>" +
                    '<button type="button" class="btn-link" data-pub-action="wecom-reconfig">重新配置</button>' +
                    "</div>";
                body += '<div class="wecom-setup" style="display:none">';
            } else {
                body += '<div class="wecom-setup">';
            }
            body += '<div class="wecom-methods">' +
                '<label class="wecom-method"><input type="radio" name="wecom-method" value="quick" checked> 快捷绑定（推荐）</label>' +
                '<label class="wecom-method"><input type="radio" name="wecom-method" value="manual"> 手动配置</label>' +
                "</div>" +
                '<div class="wecom-panel" data-panel="quick">' +
                '<div class="wecom-qr-placeholder">扫码创建机器人暂未开放<br>请先使用「手动配置」填入 Bot ID / Secret</div>' +
                "</div>" +
                '<div class="wecom-panel" data-panel="manual" style="display:none">' +
                '<p class="wechat-connect-hint">在企微后台为智能机器人开启「API 模式 - 长连接」后，填入凭证；保存并发布后机器人进程会自动建立连接。</p>' +
                '<div class="form-group"><label>Bot ID</label>' +
                '<input type="text" data-field="bot_id" value="' + escapeHtml(cfg.bot_id || "") + '" placeholder="企业微信智能机器人 Bot ID"></div>' +
                '<div class="form-group"><label>Secret</label>' +
                '<input type="password" data-field="secret" placeholder="' +
                (cfg.configured ? "已配置，如需修改请重新填写" : "机器人 Secret") + '"></div>' +
                '<button type="button" class="btn-secondary" data-pub-action="wecom-config">保存配置</button>' +
                "</div></div>";
        }

        var actions = "";
        if (isThis) {
            actions = '<div class="model-card-footer">' +
                '<button type="button" class="btn-secondary" data-pub-action="toggle" data-enabled="' +
                (bound.enabled ? "1" : "0") + '">' + (bound.enabled ? "禁用" : "启用") + "</button> " +
                '<button type="button" class="btn-danger" data-pub-action="delete" data-agent="' +
                escapeHtml(bound.agent_id) + '">取消关联</button></div>';
        } else if (bound) {
            actions = '<div class="model-card-footer"><button type="button" class="btn-primary" ' +
                'data-pub-action="publish">改为关联本智能体（替换当前）</button></div>';
        }

        return '<div class="plugin-tile" data-platform="' + platform.platform +
            '" data-bound="' + (bound ? escapeHtml(bound.agent_id) : "") + '">' +
            header + body + actions + "</div>";
    }

    publishPlatforms.addEventListener("change", function (e) {
        var radio = e.target.closest('input[name="wecom-method"]');
        if (!radio) return;
        var tile = radio.closest("[data-platform]");
        if (!tile) return;
        tile.querySelectorAll(".wecom-panel").forEach(function (panel) {
            panel.style.display = panel.getAttribute("data-panel") === radio.value ? "" : "none";
        });
    });

    publishPlatforms.addEventListener("click", function (e) {
        var btn = e.target.closest("[data-pub-action]");
        if (!btn) return;
        var action = btn.getAttribute("data-pub-action");
        if (action === "placeholder") {
            showToast("该平台暂未开放，敬请期待", "info");
            return;
        }
        var tile = btn.closest("[data-platform]");
        if (!tile) return;
        var platform = tile.getAttribute("data-platform");

        function field(name) { return tile.querySelector('[data-field="' + name + '"]'); }

        if (action === "wechat-login") {
            fetch("/api/publish/wechat/login", { method: "POST" })
                .then(handleRes)
                .then(function () { refreshWechatConnect(); })
                .catch(function (err) { showToast("启动扫码失败：" + err.message, "error"); });
            return;
        }

        if (action === "wecom-reconfig") {
            var setup = tile.querySelector(".wecom-setup");
            if (setup) setup.style.display = setup.style.display === "none" ? "" : "none";
            return;
        }

        if (action === "wecom-config") {
            var payload = {
                bot_id: field("bot_id").value.trim(),
                secret: field("secret").value.trim(),
            };
            if (!payload.bot_id || !payload.secret) {
                showToast("请填写 Bot ID 和 Secret", "error");
                return;
            }
            var saveBtn = btn;
            var oldText = saveBtn.textContent;
            saveBtn.disabled = true;
            saveBtn.textContent = "校验中…";
            fetch("/api/publish/wecom/config", {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            }).then(handleRes).then(function () {
                showToast("企业微信凭证校验通过并已保存", "success");
                renderPublish();
            }).catch(function (err) {
                saveBtn.disabled = false;
                saveBtn.textContent = oldText;
                showToast("保存失败：" + err.message, "error");
            });
            return;
        }

        if (action === "publish") {
            showConfirm(
                "确定将「" + platformName(platform) + "」切换为关联本智能体吗？" +
                "切换后会清空该平台的会话上下文，新助手将从头开始。"
            ).then(function (ok) {
                if (!ok) return;
                fetch("/api/publish/" + platform + "/agents", {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ agent_id: publishAgentId }),
                }).then(handleRes).then(function () {
                    showToast("已关联到" + platformName(platform), "success");
                    closePublishModal();
                    loadAgents();
                }).catch(function (err) { showToast("关联失败：" + err.message, "error"); });
            });
            return;
        }

        if (action === "toggle") {
            var nextEnabled = btn.getAttribute("data-enabled") !== "1";
            fetch("/api/publish/" + platform + "/agents/" + encodeURIComponent(publishAgentId) + "/enabled", {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ enabled: nextEnabled }),
            }).then(handleRes).then(function () {
                showToast(nextEnabled ? "已启用" : "已禁用", "success");
                renderPublish();
            }).catch(function (err) { showToast("操作失败：" + err.message, "error"); });
            return;
        }

        if (action === "delete") {
            showConfirm("确定要取消" + platformName(platform) + "与本智能体的关联吗？").then(function (ok) {
                if (!ok) return;
                fetch("/api/publish/" + platform + "/agents/" + encodeURIComponent(publishAgentId), {
                    method: "DELETE",
                }).then(handleRes).then(function () {
                    showToast("已取消关联", "success");
                    renderPublish();
                }).catch(function (err) { showToast("取消失败：" + err.message, "error"); });
            });
        }
    });

    function handleRes(r) {
        if (!r.ok) return r.json().then(function (d) { throw new Error(d.detail || "请求失败"); });
        return r.json();
    }

    function platformName(platform) {
        return { wechat: "微信", wecom: "企业微信", dingtalk: "钉钉", feishu: "飞书" }[platform] || platform;
    }
}
