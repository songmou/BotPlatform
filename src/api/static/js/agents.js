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
    var knowledgeContainer = document.getElementById("tools-knowledge");

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
        });
    }

    function updateKnowledgeCount() {
        var checked = knowledgeContainer.querySelectorAll("input:checked").length;
        document.getElementById("tools-knowledge-count").textContent =
            checked ? "（已选 " + checked + "）" : "";
    }

    knowledgeContainer.addEventListener("change", updateKnowledgeCount);

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
        resetChannelSection(null);
        loadModelOptions();
        loadToolOptions();
        openModal();
    });

    document.getElementById("modal-close").addEventListener("click", closeModal);
    document.getElementById("modal-cancel").addEventListener("click", closeModal);
    modal.addEventListener("click", function (e) {
        if (e.target === modal) closeModal();
    });

    function openModal() { activateModalTab("basic"); modal.style.display = ""; }
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

        if (action === "edit") {
            fetch("/api/agents/" + id)
                .then(function (r) { return r.json(); })
                .then(function (a) {
                    editingId = id;
                    modalTitle.textContent = "编辑智能体";
                    resetChannelSection(id);
                    loadAgentChannels(id);
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

    /* ===== Channel instances (bound to this agent) ===== */
    var CHANNEL_STATE_LABELS = {
        connected: "已连接", running: "运行中", connecting: "连接中",
        failed: "连接失败", authentication_required: "需重新登录",
        missing_credentials: "缺少凭据", disabled: "已禁用",
        stopped: "已停止", restart_required: "待重启", unknown: "状态未知"
    };
    var CHANNEL_CREDENTIAL_LABELS = {
        token: "Token", base_url: "服务地址", bot_id: "Bot ID",
        user_id: "User ID（可选）", secret: "Secret",
        app_id: "App ID", app_secret: "App Secret"
    };
    var channelModal = document.getElementById("channel-modal");
    var channelForm = document.getElementById("channel-form");
    var channelTypeSelect = document.getElementById("channel-type");
    var channelProviders = [];
    var agentChannelInstances = [];
    var editingInstance = null;

    function findProvider(type) {
        return channelProviders.find(function (p) { return p.type === type; });
    }

    function channelJson(url, options) {
        return fetch(url, options || {}).then(function (r) {
            return r.json().catch(function () { return {}; }).then(function (d) {
                if (!r.ok) throw new Error(d.detail || "请求失败");
                return d;
            });
        });
    }

    function renderChannelCredentialFields() {
        var provider = findProvider(channelTypeSelect.value);
        var container = document.getElementById("channel-credential-fields");
        if (!provider) { container.innerHTML = ""; return; }
        container.innerHTML = provider.credential_fields.map(function (field) {
            var secret = provider.secret_fields.indexOf(field) !== -1;
            return '<div class="form-group"><label for="credential-' + escapeHtml(field) + '">' +
                escapeHtml(CHANNEL_CREDENTIAL_LABELS[field] || field) + '</label><input id="credential-' +
                escapeHtml(field) + '" data-credential="' + escapeHtml(field) + '" type="' +
                (secret ? "password" : "text") + '" autocomplete="off"></div>';
        }).join("");
    }

    function renderAgentChannels() {
        var container = document.getElementById("agent-channel-list");
        if (!agentChannelInstances.length) {
            container.innerHTML = '<p class="text-muted agent-channel-empty">尚未绑定渠道实例。</p>';
            return;
        }
        container.innerHTML = agentChannelInstances.map(function (channel) {
            var policy = channel.settings.group_policy === "mention_only" ? "群聊 @ 响应" : "仅私聊";
            var credential = channel.credential_configured ? "凭据已配置" : "凭据未配置";
            var stateLabel = CHANNEL_STATE_LABELS[channel.state] || channel.state || "未知";
            return '<div class="agent-channel-row" data-channel-id="' + escapeHtml(channel.id) + '">' +
                '<div class="agent-channel-row-main"><div class="agent-channel-row-title">' +
                escapeHtml(channel.id) +
                '<span class="badge badge-muted">' + escapeHtml(stateLabel) + '</span></div>' +
                '<div class="agent-channel-row-meta">' + escapeHtml(channel.name) + ' · ' + policy +
                ' · ' + credential + '</div></div>' +
                '<div class="agent-channel-row-actions">' +
                '<button type="button" class="btn-secondary" data-channel-action="edit">编辑</button>' +
                '<button type="button" class="btn-danger" data-channel-action="delete">删除</button>' +
                '</div></div>';
        }).join("");
    }

    function loadAgentChannels(agentId) {
        return fetch("/api/channels")
            .then(function (r) { return r.ok ? r.json() : { channels: [], providers: [] }; })
            .then(function (data) {
                channelProviders = data.providers || [];
                agentChannelInstances = (data.channels || []).filter(function (c) {
                    return c.agent_id === agentId;
                });
                renderAgentChannels();
            });
    }

    function resetChannelSection(agentId) {
        var addBtn = document.getElementById("agent-add-channel-btn");
        var hint = document.getElementById("agent-channel-hint");
        var list = document.getElementById("agent-channel-list");
        agentChannelInstances = [];
        list.innerHTML = "";
        if (!agentId) {
            addBtn.disabled = true;
            hint.textContent = "保存智能体后可添加渠道实例。";
            return;
        }
        addBtn.disabled = false;
        hint.textContent = "为该智能体绑定微信 iLink、企业微信或飞书渠道实例；保存后需重启机器人进程生效。";
    }

    function openChannelInstanceModal(instance) {
        editingInstance = instance || null;
        channelForm.reset();
        document.getElementById("channel-modal-title").textContent =
            instance ? "配置渠道实例" : "添加渠道实例";
        channelTypeSelect.innerHTML = channelProviders.map(function (p) {
            return '<option value="' + escapeHtml(p.type) + '">' + escapeHtml(p.name) + '</option>';
        }).join("");
        document.getElementById("channel-id").value = instance ? instance.id : "";
        document.getElementById("channel-id").readOnly = !!instance;
        channelTypeSelect.value = instance ? instance.type :
            (channelTypeSelect.options[0] ? channelTypeSelect.options[0].value : "");
        channelTypeSelect.disabled = !!instance;
        document.getElementById("channel-enabled").checked = instance ? instance.enabled : true;
        document.getElementById("channel-group-policy").value = instance
            ? instance.settings.group_policy
            : (channelTypeSelect.value === "wechat_ilink" ? "private_only" : "mention_only");
        document.getElementById("keep-credentials-row").style.display =
            instance && instance.credential_configured ? "" : "none";
        document.getElementById("keep-credentials").checked = true;
        renderChannelCredentialFields();
        channelModal.style.display = "";
    }

    function closeChannelInstanceModal() {
        channelModal.style.display = "none";
        editingInstance = null;
    }

    function collectChannelCredentials() {
        var result = {};
        document.querySelectorAll("#channel-credential-fields [data-credential]").forEach(function (input) {
            if (input.value.trim()) result[input.dataset.credential] = input.value.trim();
        });
        return result;
    }

    function saveChannelInstance(e) {
        e.preventDefault();
        if (!editingId) { showToast("请先保存智能体", "error"); return; }
        var id = document.getElementById("channel-id").value.trim();
        var body = {
            type: channelTypeSelect.value,
            enabled: document.getElementById("channel-enabled").checked,
            agent_id: editingId,
            settings: { group_policy: document.getElementById("channel-group-policy").value }
        };
        var credentials = collectChannelCredentials();
        var keep = document.getElementById("keep-credentials").checked;
        var wasConfigured = editingInstance && editingInstance.credential_configured;
        channelJson("/api/channels/" + encodeURIComponent(id), {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body)
        }).then(function () {
            if (Object.keys(credentials).length) {
                return channelJson("/api/channels/" + encodeURIComponent(id) + "/credentials", {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ credentials: credentials })
                });
            }
            if (wasConfigured && !keep) {
                return channelJson("/api/channels/" + encodeURIComponent(id) + "/credentials", {
                    method: "DELETE"
                });
            }
        }).then(function () {
            closeChannelInstanceModal();
            showToast("渠道实例已保存，请重启机器人进程生效", "success");
            return loadAgentChannels(editingId);
        }).catch(function (error) {
            showToast(error.message, "error");
        });
    }

    function deleteChannelInstance(id) {
        showConfirm("确定删除渠道实例「" + id + "」吗？").then(function (ok) {
            if (!ok) return;
            channelJson("/api/channels/" + encodeURIComponent(id), { method: "DELETE" })
                .then(function () {
                    showToast("渠道实例已删除，请重启机器人进程生效", "success");
                    return loadAgentChannels(editingId);
                })
                .catch(function (error) { showToast(error.message, "error"); });
        });
    }

    document.getElementById("agent-add-channel-btn").addEventListener("click", function () {
        if (!editingId) { showToast("请先保存智能体", "error"); return; }
        openChannelInstanceModal(null);
    });
    document.getElementById("channel-modal-close").addEventListener("click", closeChannelInstanceModal);
    document.getElementById("channel-modal-cancel").addEventListener("click", closeChannelInstanceModal);
    channelForm.addEventListener("submit", saveChannelInstance);
    channelTypeSelect.addEventListener("change", function () {
        document.getElementById("channel-group-policy").value =
            this.value === "wechat_ilink" ? "private_only" : "mention_only";
        renderChannelCredentialFields();
    });
    document.getElementById("agent-channel-list").addEventListener("click", function (event) {
        var row = event.target.closest(".agent-channel-row");
        var action = event.target.getAttribute("data-channel-action");
        if (!row || !action) return;
        var id = row.getAttribute("data-channel-id");
        if (action === "edit") {
            var instance = agentChannelInstances.find(function (c) { return c.id === id; });
            if (instance) openChannelInstanceModal(instance);
        }
        if (action === "delete") deleteChannelInstance(id);
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
}
