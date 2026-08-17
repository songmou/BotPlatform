/* ===== Models page ===== */
function initModels() {
    var statusEl = document.getElementById("model-status");
    var listEl = document.getElementById("model-list");
    var rolesPane = document.getElementById("model-roles-pane");
    var createBtn = document.getElementById("create-model-btn");
    var modal = document.getElementById("model-modal");
    var modalTitle = document.getElementById("model-modal-title");
    var form = document.getElementById("model-form");
    var idGroup = document.getElementById("model-id-group");
    var editingId = null;
    var allModels = [];
    // Runtime model bindings live in settings/runtime, not in the model
    // payloads, so the cards join against this snapshot for their badges.
    var routing = {};
    var ROUTING_URL = "/api/v2/platform/model-routing";
    var analyticsCurrency = "CNY";
    var currentCat = "chat";
    var modalityLabels = { chat: "\u5bf9\u8bdd", embedding: "\u5411\u91cf", rerank: "\u91cd\u6392" };

    loadModels();
    initAnalyticsTabs();
    initCategoryNav();
    initEditorTabs();

    createBtn.addEventListener("click", function () {
        editingId = null;
        modalTitle.textContent = "\u6dfb\u52a0\u6a21\u578b";
        idGroup.style.display = "";
        form.reset();
        document.getElementById("model-enabled").checked = true;
        // Pre-select the modality matching the active category so creating a
        // model from the "\u5411\u91cf/\u91cd\u6392" tabs opens the right form.
        document.getElementById("model-modality").value =
            (currentCat === "embedding" || currentCat === "rerank") ? currentCat : "chat";
        document.getElementById("model-api-key").value = "";
        var akStatus = document.getElementById("model-api-key-status");
        akStatus.textContent = "";
        akStatus.className = "badge";
        document.getElementById("model-api-key").placeholder = "初次录入密钥；编辑时留空表示保持不变";
        applyModalityVisibility();
        openModal();
    });

    document.getElementById("model-modal-close").addEventListener("click", closeModal);
    document.getElementById("model-modal-cancel").addEventListener("click", closeModal);
    modal.addEventListener("click", function (e) { if (e.target === modal) closeModal(); });

    var providerInput = document.getElementById("model-provider");
    var providerDropdown = document.getElementById("provider-dropdown");
    var providerItems = providerDropdown.querySelectorAll(".dropdown-item");

    providerInput.addEventListener("focus", function () {
        filterProviders(this.value);
        providerDropdown.style.display = "";
    });
    providerInput.addEventListener("input", function () {
        filterProviders(this.value);
        providerDropdown.style.display = "";
    });
    providerDropdown.addEventListener("mousedown", function (e) {
        var item = e.target.closest(".dropdown-item");
        if (!item) return;
        e.preventDefault();
        providerInput.value = item.getAttribute("data-value");
        providerDropdown.style.display = "none";
    });
    document.addEventListener("mousedown", function (e) {
        if (!e.target.closest(".dropdown-wrap")) {
            providerDropdown.style.display = "none";
        }
    });

    function filterProviders(query) {
        var q = query.trim().toLowerCase();
        providerItems.forEach(function (item) {
            var val = item.getAttribute("data-value");
            item.style.display = (!q || val.indexOf(q) !== -1) ? "" : "none";
        });
    }

    function openModal() {
        activateEditorTab("basic");
        modal.style.display = "";
    }
    function closeModal() { modal.style.display = "none"; }

    // Editor modal sub-tabs: switch panels, keep footer always visible.
    function initEditorTabs() {
        form.querySelectorAll("[data-editor-tab]").forEach(function (btn) {
            btn.addEventListener("click", function () {
                activateEditorTab(btn.getAttribute("data-editor-tab"));
            });
        });
    }

    function activateEditorTab(name) {
        form.querySelectorAll("[data-editor-tab]").forEach(function (btn) {
            var active = btn.getAttribute("data-editor-tab") === name;
            btn.classList.toggle("active", active);
            btn.setAttribute("aria-selected", active ? "true" : "false");
        });
        form.querySelectorAll("[data-editor-panel]").forEach(function (panel) {
            panel.hidden = panel.getAttribute("data-editor-panel") !== name;
        });
    }

    var modalityEl = document.getElementById("model-modality");
    modalityEl.addEventListener("change", applyModalityVisibility);

    function applyModalityVisibility() {
        var modality = modalityEl.value;
        var localTransformersOption = document.querySelector('#model-type option[value="local_transformers"]');
        if (localTransformersOption) {
            localTransformersOption.disabled = modality !== "rerank";
            if (modality !== "rerank" && document.getElementById("model-type").value === "local_transformers") {
                document.getElementById("model-type").value = "openai_compatible";
            }
        }
        var chatOn = modality === "chat";
        form.querySelectorAll(".chat-only").forEach(function (el) {
            el.style.display = chatOn ? "" : "none";
            // Disabled controls are exempt from constraint validation, so a
            // hidden (display:none) chat field can no longer block submit.
            el.querySelectorAll("input, select, textarea").forEach(function (f) { f.disabled = !chatOn; });
        });
        var embedOn = modality === "embedding";
        form.querySelectorAll(".embedding-only").forEach(function (el) {
            el.style.display = embedOn ? "" : "none";
            el.querySelectorAll("input, select, textarea").forEach(function (f) { f.disabled = !embedOn; });
        });
        // Rerank models have no tunable parameters; pricing only applies to chat.
        var tabVisibility = {
            basic: true,
            connection: true,
            params: modality !== "rerank",
            pricing: modality === "chat",
        };
        var activeHidden = false;
        form.querySelectorAll("[data-editor-tab]").forEach(function (btn) {
            var tab = btn.getAttribute("data-editor-tab");
            btn.style.display = tabVisibility[tab] ? "" : "none";
            if (!tabVisibility[tab] && btn.classList.contains("active")) activeHidden = true;
        });
        if (activeHidden) activateEditorTab("basic");
    }

    var roleVision = document.getElementById("role-vision");
    var roleEmbedding = document.getElementById("role-embedding");
    var roleRerank = document.getElementById("role-rerank");
    document.getElementById("save-roles-btn").addEventListener("click", saveRoles);

    function roleOptions(candidates, current) {
        var opts = '<option value="">（未绑定）</option>';
        (candidates || []).forEach(function (c) {
            var label = c.id + (c.enabled ? "" : "（已禁用）");
            opts += '<option value="' + escapeHtml(c.id) + '"' +
                (c.id === current ? " selected" : "") + ">" + escapeHtml(label) + "</option>";
        });
        return opts;
    }

    function readJson(response) {
        if (response.ok) return response.json();
        return response.json().then(
            function (data) { throw new Error((data && data.detail) || "请求失败"); },
            function () { throw new Error("请求失败(" + response.status + ")"); }
        );
    }

    function applyRouting() {
        statusEl.innerHTML =
            "<strong>路由状态</strong><br>" +
            "主模型：" + escapeHtml(routing.primary_profile_id || "—") +
            (routing.cooling_down ? " <mark>冷却中</mark>" : " ✓") + "<br>" +
            "兜底模型：" + escapeHtml(routing.fallback_profile_id || "—") + "<br>" +
            (routing.local_profile_id
                ? "本地模型：" + escapeHtml(routing.local_profile_id) + "<br>" : "") +
            (routing.last_primary_error
                ? "<br><small>最近错误：" + escapeHtml(routing.last_primary_error) + "</small>" : "");
        roleVision.innerHTML = roleOptions(routing.vision_candidates, routing.vision_model);
        roleEmbedding.innerHTML = roleOptions(routing.embedding_candidates, routing.embedding_model);
        roleRerank.innerHTML = roleOptions(routing.rerank_candidates, routing.rerank_model);
    }

    function loadRouting() {
        return fetch(ROUTING_URL).then(readJson).then(function (data) {
            routing = data || {};
            applyRouting();
            return routing;
        });
    }

    function saveRoles() {
        fetch(ROUTING_URL, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                vision_model: roleVision.value,
                embedding_model: roleEmbedding.value,
                rerank_model: roleRerank.value,
            }),
        }).then(readJson).then(function (d) {
            showToast(
                d.restart_required
                    ? "已保存，向量 / 重排绑定需完整重启后生效"
                    : "角色绑定已保存",
                "success"
            );
            return loadModels();
        }).catch(function (err) { showToast("保存失败：" + err.message, "error"); });
    }

    function loadModels() {
        return Promise.all([
            loadRouting(),
            CatalogApi.list("models"),
        ]).then(function (results) {
            allModels = results[1] || [];
            var profileSelect = document.getElementById("analytics-profile");
            profileSelect.innerHTML = '<option value="">全部模型</option>' +
                allModels.map(function (m) {
                    return '<option value="' + escapeHtml(m.id) + '">' + escapeHtml(m.id) + '</option>';
                }).join("");
            renderModels();
        }).catch(function (err) {
            showToast("加载模型失败：" + err.message, "error");
        });
    }

    function initCategoryNav() {
        document.querySelectorAll("[data-model-cat]").forEach(function (btn) {
            btn.addEventListener("click", function () {
                activateCategory(btn.getAttribute("data-model-cat"));
            });
        });
    }

    function activateCategory(cat) {
        currentCat = cat;
        document.querySelectorAll("[data-model-cat]").forEach(function (item) {
            item.classList.toggle("active", item.getAttribute("data-model-cat") === cat);
        });
        createBtn.style.display = cat === "roles" ? "none" : "";
        renderModels();
    }

    // Older running processes may not include modality yet. Treat those
    // profiles as chat models so the card never renders "undefined".
    function modelModality(m) {
        return m.modality || "chat";
    }

    function renderModels() {
        var counts = { chat: 0, embedding: 0, rerank: 0 };
        allModels.forEach(function (m) {
            var mod = modelModality(m);
            if (counts[mod] !== undefined) counts[mod] += 1;
        });
        Object.keys(counts).forEach(function (cat) {
            var badge = document.querySelector('[data-cat-count="' + cat + '"]');
            if (badge) badge.textContent = counts[cat];
        });

        if (currentCat === "roles") {
            // .card-grid sets display:grid, which overrides the [hidden] UA rule,
            // so toggle inline display explicitly to hide the model list.
            listEl.style.display = "none";
            rolesPane.hidden = false;
            return;
        }
        rolesPane.hidden = true;
        listEl.style.display = "";

        var items = allModels.filter(function (m) { return modelModality(m) === currentCat; });
        if (!items.length) {
            listEl.innerHTML = '<div class="empty-state">暂无' +
                (modalityLabels[currentCat] || "") +
                '模型，点击右上角“添加模型”创建</div>';
            return;
        }
        listEl.innerHTML = items.map(modelCardHtml).join("");
    }

    function modelCardHtml(m) {
        var modality = modelModality(m);
        var modalityLabel = modalityLabels[modality] || "其他";
        var isPrimary = !!m.id && m.id === routing.active_model;
        var isFallback = !!m.id && m.id === routing.fallback_model;
        var roleTags = [];
        if (m.id === routing.local_model) roleTags.push("本地");
        if (m.id === routing.flash_model) roleTags.push("Flash");
        if (m.id === routing.pro_model) roleTags.push("Pro");
        if (m.id === routing.vision_model) roleTags.push("视觉");
        if (m.id === routing.embedding_model) roleTags.push("向量");
        if (m.id === routing.rerank_model) roleTags.push("重排");
        var badges = '<span class="badge badge-modality badge-modality-' + modality + '">' + modalityLabel + '</span>';
        if (isPrimary) badges += '<span class="badge badge-primary">主模型</span>';
        if (isFallback) badges += '<span class="badge badge-fallback">兜底</span>';
        roleTags.forEach(function (tag) {
            badges += '<span class="badge badge-modality">' + tag + '</span>';
        });
        if (!m.enabled) badges += '<span class="badge badge-fallback">已禁用</span>';
        var actions = '<div class="model-card-footer">';
        if (modality === "chat" && m.enabled && !isPrimary) {
            actions += '<button class="btn-primary btn-switch" data-id="' + m.id + '">设为主模型</button> ';
        }
        actions += '<button class="btn-edit" data-action="edit-model" data-id="' + m.id + '">编辑</button> ';
        if (!isPrimary) {
            actions += '<button class="btn-danger" data-action="delete-model" data-id="' + m.id + '">删除</button>';
        }
        actions += "</div>";
        var detail;
        var modelType = m.type || "未指定类型";
        var timeout = m.timeout_seconds != null ? m.timeout_seconds + "s" : "未设置超时";
        if (modality === "embedding") {
            detail = modelType + " · " + (m.dimensions || "?") + " 维 · " + timeout;
        } else if (modality === "rerank") {
            detail = modelType + " · " + timeout;
        } else {
            var temperature = m.temperature != null ? m.temperature : "—";
            var maxTokens = m.max_tokens != null ? m.max_tokens + " tokens" : "未设置 Token";
            detail = modelType + " · " + temperature + " · " + maxTokens + " · " + timeout;
        }
        var extra = "";
        if (modality === "chat") {
            extra = "<p>" + (m.pricing
                ? "已配置 " + (m.billing_currency || "CNY") + " 计价"
                : "未计价") + "</p>";
        }
        return '<div class="model-card' + (isPrimary ? ' is-primary' : '') + '">' +
            "<h5>" + escapeHtml(m.id || "未命名档案") + " " + badges + "</h5>" +
            "<p>" + escapeHtml(m.provider || "未指定厂商") + " / " +
            escapeHtml(m.model || "未指定模型") + "</p>" +
            "<p>" + escapeHtml(detail) + "</p>" +
            extra +
            actions +
            "</div>";
    }

    listEl.addEventListener("click", function (e) {
        var switchBtn = e.target.closest(".btn-switch");
        if (switchBtn) {
            var id = switchBtn.getAttribute("data-id");
            switchBtn.disabled = true;
            switchBtn.textContent = "切换中...";
            fetch(ROUTING_URL, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ active_model: id }),
            }).then(readJson).then(function () {
                showToast("已切换主模型为 " + id, "success");
                return loadModels();
            }).catch(function (err) {
                showToast("切换失败：" + err.message, "error");
                switchBtn.disabled = false;
                switchBtn.textContent = "设为主模型";
            });
            return;
        }

        var btn = e.target.closest("[data-action]");
        if (!btn) return;
        var action = btn.getAttribute("data-action");
        var mid = btn.getAttribute("data-id");

        if (action === "delete-model") {
            showConfirm("确定要删除模型「" + mid + "」吗？").then(function (ok) {
                if (!ok) return;
                CatalogApi.remove("models", mid)
                    .then(function () {
                        showToast("已删除模型 " + mid, "success");
                        loadModels();
                    })
                    .catch(function (err) { showToast("删除失败：" + err.message, "error"); });
            });
        }

        if (action === "edit-model") {
            CatalogApi.get("models", mid)
                .then(function (m) {
                    editingId = mid;
                    modalTitle.textContent = "编辑模型";
                    idGroup.style.display = "none";
                    document.getElementById("model-id").value = m.id;
                    document.getElementById("model-modality").value = m.modality || "chat";
                    document.getElementById("model-provider").value = m.provider;
                    document.getElementById("model-type").value = m.type;
                    document.getElementById("model-base-url").value = m.base_url || "";
                    document.getElementById("model-name").value = m.model;
                    document.getElementById("model-api-key").value = "";
                    var akStatus = document.getElementById("model-api-key-status");
                    if (m.api_key_set) {
                        akStatus.textContent = "已配置";
                        akStatus.className = "badge badge-success";
                        document.getElementById("model-api-key").placeholder = "留空表示保持不变";
                    } else {
                        akStatus.textContent = "未配置";
                        akStatus.className = "badge badge-muted";
                        document.getElementById("model-api-key").placeholder = "初次录入密钥";
                    }
                    document.getElementById("model-dimensions").value = m.dimensions || "";
                    // Non-chat profiles carry sentinel max_tokens=1 / temperature=0
                    // from the backend; only backfill them for chat so a hidden
                    // field never holds an out-of-range value.
                    var isChat = (m.modality || "chat") === "chat";
                    document.getElementById("model-temperature").value = isChat ? m.temperature : 0.7;
                    document.getElementById("model-max-tokens").value = isChat ? m.max_tokens : 2048;
                    document.getElementById("model-timeout").value = m.timeout_seconds;
                    document.getElementById("model-enabled").checked = m.enabled;
                    var caps = m.capabilities || {};
                    document.getElementById("model-cap-tools").checked = !!caps.tools;
                    document.getElementById("model-cap-vision").checked = !!caps.vision;
                    document.getElementById("model-cap-reasoning").checked = !!caps.reasoning;
                    document.getElementById("price-input").value = m.pricing ? m.pricing.input_per_million : "";
                    document.getElementById("price-cached").value = m.pricing ? (m.pricing.cached_input_per_million || "") : "";
                    document.getElementById("price-output").value = m.pricing ? m.pricing.output_per_million : "";
                    document.getElementById("price-reasoning").value = m.pricing ? (m.pricing.reasoning_output_per_million || "") : "";
                    applyModalityVisibility();
                    openModal();
                });
        }
    });

    form.addEventListener("submit", function (e) {
        e.preventDefault();
        // The form is novalidate so hidden tab panels never block submit
        // silently: jump to the panel owning the first invalid field.
        if (!form.checkValidity()) {
            var invalid = form.querySelector(":invalid");
            if (invalid) {
                var panel = invalid.closest("[data-editor-panel]");
                if (panel) activateEditorTab(panel.getAttribute("data-editor-panel"));
                invalid.reportValidity();
                showToast("表单校验未通过：" + (invalid.validationMessage || invalid.id || "请检查标红字段"), "error");
            }
            return;
        }
        var modality = document.getElementById("model-modality").value;
        var payload = {
            modality: modality,
            type: document.getElementById("model-type").value,
            provider: document.getElementById("model-provider").value,
            base_url: document.getElementById("model-base-url").value,
            model: document.getElementById("model-name").value,
            api_key: document.getElementById("model-api-key").value || null,
            timeout_seconds: parseFloat(document.getElementById("model-timeout").value),
            enabled: document.getElementById("model-enabled").checked,
        };
        if (modality === "chat") {
            payload.temperature = parseFloat(document.getElementById("model-temperature").value);
            payload.max_tokens = parseInt(document.getElementById("model-max-tokens").value);
            payload.capabilities = {
                tools: document.getElementById("model-cap-tools").checked,
                vision: document.getElementById("model-cap-vision").checked,
                reasoning: document.getElementById("model-cap-reasoning").checked,
            };
            var inputPrice = document.getElementById("price-input").value.trim();
            var outputPrice = document.getElementById("price-output").value.trim();
            if ((inputPrice && !outputPrice) || (!inputPrice && outputPrice)) {
                activateEditorTab("pricing");
                showToast("普通输入与普通输出价格必须同时填写", "error");
                return;
            }
            if (inputPrice && outputPrice) {
                payload.pricing = {
                    input_per_million: inputPrice,
                    cached_input_per_million: document.getElementById("price-cached").value.trim() || null,
                    output_per_million: outputPrice,
                    reasoning_output_per_million: document.getElementById("price-reasoning").value.trim() || null,
                };
            } else if (editingId) {
                payload.pricing = null;
            }
        } else if (modality === "embedding") {
            var dims = document.getElementById("model-dimensions").value.trim();
            if (!dims) {
                activateEditorTab("params");
                document.getElementById("model-dimensions").focus();
                showToast("向量模型必须填写向量维度", "error");
                return;
            }
            payload.dimensions = parseInt(dims);
        }

        var resourceId = editingId || document.getElementById("model-id").value;
        if (!editingId) payload.id = resourceId;

        CatalogApi.save("models", resourceId, payload)
            .then(function (d) {
                var restart = d && d.restart_required;
                showToast(
                    (editingId ? "已保存修改" : "已添加模型") +
                        (restart ? "，向量 / 重排模型需完整重启后生效" : ""),
                    "success"
                );
                closeModal();
                // Jump to the category matching the saved model so it is visible.
                activateCategory(modality);
                loadModels();
            })
            .catch(function (err) { showToast("保存失败：" + err.message, "error"); });
    });

    function initAnalyticsTabs() {
        document.querySelectorAll("[data-model-tab]").forEach(function (btn) {
            btn.addEventListener("click", function () {
                var tab = btn.getAttribute("data-model-tab");
                document.querySelectorAll("[data-model-tab]").forEach(function (item) {
                    item.classList.toggle("active", item === btn);
                });
                document.querySelectorAll("[data-model-pane]").forEach(function (pane) {
                    pane.classList.toggle("active", pane.getAttribute("data-model-pane") === tab);
                });
                document.getElementById("create-model-btn").style.display =
                    (tab === "config" && currentCat !== "roles") ? "" : "none";
                if (tab === "usage" || tab === "quality") loadAnalytics();
                if (tab === "budgets") loadBudgets();
            });
        });
        document.getElementById("analytics-refresh").addEventListener("click", loadAnalytics);
        ["analytics-days", "analytics-profile", "analytics-source"].forEach(function (id) {
            document.getElementById(id).addEventListener("change", loadAnalytics);
        });
        document.getElementById("budget-scope-type").addEventListener("change", function () {
            var global = this.value === "global";
            document.getElementById("budget-scope-id").disabled = global;
            if (global) document.getElementById("budget-scope-id").value = "";
        });
        document.getElementById("budget-form").addEventListener("submit", saveBudget);
        document.getElementById("run-detail-close").addEventListener("click", closeRunDetail);
        document.getElementById("run-detail-modal").addEventListener("click", function (e) {
            if (e.target === this) closeRunDetail();
        });
        document.getElementById("model-runs-body").addEventListener("click", function (e) {
            var row = e.target.closest("[data-run-id]");
            if (row) openRunDetail(row.getAttribute("data-run-id"));
        });
    }

    function analyticsQuery() {
        var days = parseInt(document.getElementById("analytics-days").value, 10) || 7;
        var from = new Date(Date.now() - days * 86400000).toISOString();
        var params = new URLSearchParams({ from: from, to: new Date().toISOString() });
        var profile = document.getElementById("analytics-profile").value;
        var source = document.getElementById("analytics-source").value;
        if (profile) params.set("profile_id", profile);
        if (source) params.set("source", source);
        document.getElementById("analytics-export").href = "/api/model-analytics/export.csv?" + params.toString();
        return params.toString();
    }

    function fetchJson(url) {
        return fetch(url).then(function (r) {
            if (!r.ok) return r.json().then(function (d) { throw new Error(d.detail || "请求失败"); });
            return r.json();
        });
    }

    function pct(value) {
        return value === null || value === undefined ? "—" : (value * 100).toFixed(1) + "%";
    }

    function formatCost(micros) {
        return analyticsCurrency + " " + ((micros || 0) / 1000000).toFixed(6);
    }

    function sourceLabel(source) {
        return {
            web: "Web",
            wechat: "微信",
            wecom: "企业微信",
            feishu: "飞书",
            schedule: "定时任务",
            internal: "内部任务",
        }[source] || source || "未知";
    }

    function statusLabel(status) {
        return {
            running: "运行中",
            success: "成功",
            partial: "部分成功",
            failed: "失败",
            cancelled: "已取消",
        }[status] || status || "未知";
    }

    function statCard(label, value, hint) {
        return '<div class="analytics-stat"><span>' + escapeHtml(label) + '</span><strong>' +
            escapeHtml(String(value)) + '</strong><small>' + escapeHtml(hint || "") + '</small></div>';
    }

    function loadAnalytics() {
        var query = analyticsQuery();
        Promise.all([
            fetchJson("/api/model-analytics/overview?" + query),
            fetchJson("/api/model-analytics/timeseries?" + query + "&bucket=day"),
            fetchJson("/api/model-analytics/breakdown?" + query + "&dimension=profile"),
            fetchJson("/api/model-analytics/runs?" + query + "&limit=100"),
        ]).then(function (values) {
            var overview = values[0];
            analyticsCurrency = overview.currency || "CNY";
            renderUsage(overview, values[1].items, values[2].items, values[3].items);
            renderQuality(overview);
        }).catch(function (err) {
            showToast("加载模型分析失败：" + err.message, "error");
        });
    }

    function renderUsage(o, series, breakdown, runs) {
        document.getElementById("usage-stats").innerHTML =
            statCard("逻辑运行", o.run_count, "一次用户或系统任务") +
            statCard("物理调用", o.call_count, "含重试与工具循环") +
            statCard("输入 Token", o.input_tokens, "缓存 " + o.cached_input_tokens) +
            statCard("输出 Token", o.output_tokens, "") +
            statCard("总成本", formatCost(o.cost_micros), "") +
            statCard("平均运行成本", formatCost(o.run_count ? o.cost_micros / o.run_count : 0), "") +
            statCard("成功率", pct(o.success_rate), "") +
            statCard("未完整计价", o.unpriced_calls, "不能按零成本处理");

        var maxCalls = Math.max.apply(null, series.map(function (x) { return x.call_count; }).concat([1]));
        document.getElementById("usage-trend").innerHTML = series.length ? series.map(function (x) {
            return '<div class="trend-row"><span>' + escapeHtml(x.bucket) + '</span>' +
                '<div class="trend-track"><i style="width:' + (x.call_count / maxCalls * 100) + '%"></i></div>' +
                '<b>' + x.call_count + ' / ' + escapeHtml(formatCost(x.cost_micros)) + '</b></div>';
        }).join("") : '<div class="empty-state">当前范围暂无调用数据</div>';

        document.getElementById("usage-breakdown").innerHTML = breakdown.length ?
            '<table class="data-table"><thead><tr><th>模型</th><th>调用</th><th>成功率</th><th>成本</th></tr></thead><tbody>' +
            breakdown.map(function (x) {
                return '<tr><td>' + escapeHtml(x.name) + '</td><td>' + x.call_count + '</td><td>' +
                    pct(x.call_count ? x.success_count / x.call_count : null) + '</td><td>' +
                    escapeHtml(formatCost(x.cost_micros)) + '</td></tr>';
            }).join("") + '</tbody></table>' : '<div class="empty-state">暂无模型分布数据</div>';

        document.getElementById("model-runs-body").innerHTML = runs.length ? runs.map(function (r) {
            return '<tr data-run-id="' + escapeHtml(r.run_id) + '"><td>' +
                escapeHtml(new Date(r.started_at).toLocaleString()) + '</td><td>' +
                escapeHtml(sourceLabel(r.source)) + '</td><td>' + escapeHtml(r.agent_id || "—") + '</td><td>' +
                escapeHtml(statusLabel(r.status)) + '</td><td>' + r.call_count + '</td><td>' +
                (r.input_tokens + r.output_tokens) + '</td><td>' +
                escapeHtml(formatCost(r.cost_micros)) + (r.unpriced_calls ? " ⚠" : "") + '</td></tr>';
        }).join("") : '<tr><td colspan="7" class="empty-state">暂无运行记录</td></tr>';
    }

    function renderQuality(o) {
        document.getElementById("quality-stats").innerHTML =
            statCard("好评率", pct(o.positive_rate), o.feedback_count + " 条反馈") +
            statCard("反馈覆盖率", pct(o.feedback_coverage), "") +
            statCard("调用成功率", pct(o.success_rate), "") +
            statCard("P50 延迟", o.duration_p50_ms === null ? "—" : o.duration_p50_ms + " ms", "") +
            statCard("P95 延迟", o.duration_p95_ms === null ? "—" : o.duration_p95_ms + " ms", "") +
            statCard("截断率", pct(o.truncation_rate), "") +
            statCard("重试率", pct(o.retry_rate), "") +
            statCard("模型切换率", pct(o.fallback_rate), "");
        document.getElementById("quality-notes").innerHTML =
            '<span>工具失败：<strong>' + o.tool_failure_count + '</strong></span>' +
            '<span>未完整计价：<strong>' + o.unpriced_calls + '</strong></span>' +
            '<span>反馈样本：<strong>' + o.feedback_count + '</strong></span>';
    }

    function traceJson(value) {
        var serialized;
        try {
            serialized = JSON.stringify(value, null, 2);
        } catch (_err) {
            serialized = String(value);
        }
        return '<pre class="trace-pre">' + escapeHtml(serialized || "") + '</pre>';
    }

    function traceMessage(message) {
        var role = {
            system: "系统",
            user: "用户",
            assistant: "模型",
            tool: "工具",
        }[message.role] || message.role || "未知";
        var html = '<div class="trace-message"><div class="trace-message-head"><strong>' +
            escapeHtml(role) + '</strong>';
        if (message.tool_call_id) {
            html += '<span>调用 ID：' + escapeHtml(message.tool_call_id) + '</span>';
        }
        html += '</div>';
        if (message.content) {
            html += '<pre class="trace-pre trace-content">' + escapeHtml(message.content) + '</pre>';
        } else {
            html += '<div class="trace-empty">无文字内容</div>';
        }
        if (message.tool_calls && message.tool_calls.length) {
            html += '<div class="trace-subsection"><strong>工具调用</strong>' +
                traceJson(message.tool_calls) + '</div>';
        }
        if (message.extensions && Object.keys(message.extensions).length) {
            html += '<div class="trace-subsection"><strong>扩展字段</strong>' +
                traceJson(message.extensions) + '</div>';
        }
        return html + '</div>';
    }

    function traceInput(request) {
        if (!request) {
            return '<div class="trace-legacy">该记录创建于内容采集启用前，无法还原模型输入。</div>';
        }
        var messages = request.messages || [];
        var html = messages.length ? messages.map(traceMessage).join("") :
            '<div class="trace-empty">没有消息内容</div>';
        if (request.tools && request.tools.length) {
            html += '<div class="trace-subsection"><strong>可用工具定义</strong>' +
                traceJson(request.tools) + '</div>';
        }
        html += '<div class="trace-subsection trace-inline-meta"><strong>生成参数</strong>' +
            traceJson(request.generation || {}) + '</div>';
        if (request.image && request.image.present) {
            html += '<div class="trace-image-meta">附带图片 · ' +
                escapeHtml(String(request.image.size_bytes || 0)) + ' 字节（未保存图片内容）</div>';
        }
        return html;
    }

    function traceOutput(response, errorMessage, hasRequest) {
        var html = "";
        if (response) {
            html += traceMessage(response.message || {});
            html += '<div class="trace-response-meta"><span>实际模型：' +
                escapeHtml(response.actual_model || "—") + '</span><span>结束原因：' +
                escapeHtml(response.finish_reason || "—") + '</span><span>请求 ID：' +
                escapeHtml(response.request_id || "—") + '</span></div>';
        } else if (!hasRequest) {
            html += '<div class="trace-legacy">该记录创建于内容采集启用前，无法还原模型输出。</div>';
        } else {
            html += '<div class="trace-empty">模型未返回可记录的响应内容</div>';
        }
        if (errorMessage) {
            html += '<div class="trace-error"><strong>调用错误</strong><pre class="trace-pre">' +
                escapeHtml(errorMessage) + '</pre></div>';
        }
        return html;
    }

    function traceCall(call, index, total) {
        var flags = [];
        if (call.is_retry) flags.push("重试");
        if (call.is_fallback) flags.push("模型切换");
        var open = index === 0 || index === total - 1 ? " open" : "";
        var cost = call.cost_micros === null || call.cost_micros === undefined ?
            (call.cost_status || "未计价") :
            (formatCost(call.cost_micros) + " · " + (call.cost_status || "已计价"));
        return '<details class="model-call-card"' + open + '><summary>' +
            '<span class="call-sequence">#' + call.sequence + '</span>' +
            '<span class="call-model">' + escapeHtml(call.profile_id + " / " +
                (call.actual_model || call.configured_model)) + '</span>' +
            '<span>' + escapeHtml(call.operation) + '</span>' +
            (flags.length ? '<span class="call-flags">' + escapeHtml(flags.join(" · ")) + '</span>' : "") +
            '<span class="call-status call-status-' + escapeHtml(call.status) + '">' +
                escapeHtml(statusLabel(call.status)) + '</span>' +
            '<span>' + call.duration_ms + ' ms</span><span>' +
                ((call.input_tokens || 0) + (call.output_tokens || 0)) + ' Token</span><span>' +
                escapeHtml(cost) + '</span></summary>' +
            '<div class="model-call-content"><section><h4>模型输入</h4>' +
                traceInput(call.request) + '</section><section><h4>模型输出</h4>' +
                traceOutput(call.response, call.error_message, !!call.request) +
            '</section></div></details>';
    }

    function openRunDetail(runId) {
        fetchJson("/api/model-analytics/runs/" + encodeURIComponent(runId)).then(function (run) {
            var calls = run.calls || [];
            var html = '<div class="run-meta"><span>运行：' + escapeHtml(run.run_id) + '</span>' +
                '<span>来源：' + escapeHtml(sourceLabel(run.source)) + '</span><span>状态：' +
                escapeHtml(statusLabel(run.status)) + '</span><span>智能体：' +
                escapeHtml(run.agent_id || "—") + '</span></div>' +
                (calls.length ? '<div class="model-call-chain">' + calls.map(function (call, index) {
                    return traceCall(call, index, calls.length);
                }).join("") + '</div>' : '<div class="empty-state">该运行没有模型调用记录</div>');
            document.getElementById("run-detail-content").innerHTML = html;
            document.getElementById("run-detail-modal").style.display = "";
        }).catch(function (err) { showToast(err.message, "error"); });
    }

    function closeRunDetail() {
        document.getElementById("run-detail-modal").style.display = "none";
    }

    function loadBudgets() {
        fetchJson("/api/model-budgets").then(function (data) {
            analyticsCurrency = data.currency || "CNY";
            document.getElementById("budget-list").innerHTML = data.items.length ? data.items.map(function (b) {
                var ratio = Math.min(100, b.usage_ratio * 100);
                return '<div class="budget-item"><div><strong>' + escapeHtml(b.scope_type + (b.scope_id ? " / " + b.scope_id : "")) +
                    '</strong><button class="budget-delete" data-id="' + b.budget_id + '">删除</button></div>' +
                    '<div class="budget-track"><i style="width:' + ratio + '%"></i></div><small>' +
                    escapeHtml(formatCost(b.spent_micros)) + ' / ' + escapeHtml(formatCost(b.monthly_limit_micros)) +
                    '（' + (b.usage_ratio * 100).toFixed(1) + '%）</small></div>';
            }).join("") : '<div class="empty-state">尚未配置预算</div>';
            document.getElementById("budget-alerts").innerHTML = data.alerts.length ? data.alerts.map(function (a) {
                return '<div class="alert-item"><strong>' + a.threshold + '% 预警</strong><span>' +
                    escapeHtml(a.period + " · " + a.scope_type + (a.scope_id ? " / " + a.scope_id : "")) +
                    '</span><small>' + escapeHtml(formatCost(a.spent_micros)) + '</small></div>';
            }).join("") : '<div class="empty-state">暂无预算预警</div>';
            document.querySelectorAll(".budget-delete").forEach(function (btn) {
                btn.addEventListener("click", function () {
                    showConfirm("确定删除该预算吗？").then(function (ok) {
                        if (!ok) return;
                        fetch("/api/model-budgets/" + btn.getAttribute("data-id"), { method: "DELETE" })
                            .then(function (r) { if (!r.ok) throw new Error("删除失败"); loadBudgets(); });
                    });
                });
            });
        }).catch(function (err) { showToast("加载预算失败：" + err.message, "error"); });
    }

    function saveBudget(e) {
        e.preventDefault();
        var amount = parseFloat(document.getElementById("budget-amount").value);
        var payload = {
            scope_type: document.getElementById("budget-scope-type").value,
            scope_id: document.getElementById("budget-scope-id").value.trim(),
            monthly_limit_micros: Math.round(amount * 1000000),
            enabled: true,
        };
        fetch("/api/model-budgets", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        }).then(function (r) {
            if (!r.ok) return r.json().then(function (d) { throw new Error(d.detail); });
            document.getElementById("budget-form").reset();
            showToast("预算已保存", "success");
            loadBudgets();
        }).catch(function (err) { showToast("保存预算失败：" + err.message, "error"); });
    }
}
