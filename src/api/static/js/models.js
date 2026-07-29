/* ===== Models page ===== */
function initModels() {
    var statusEl = document.getElementById("model-status");
    var listEl = document.getElementById("model-list");
    var modal = document.getElementById("model-modal");
    var modalTitle = document.getElementById("model-modal-title");
    var form = document.getElementById("model-form");
    var idGroup = document.getElementById("model-id-group");
    var editingId = null;
    var allModels = [];
    var analyticsCurrency = "CNY";

    loadModels();
    initAnalyticsTabs();

    document.getElementById("create-model-btn").addEventListener("click", function () {
        editingId = null;
        modalTitle.textContent = "添加模型";
        idGroup.style.display = "";
        form.reset();
        document.getElementById("model-enabled").checked = true;
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

    function openModal() { modal.style.display = ""; }
    function closeModal() { modal.style.display = "none"; }

    function loadModels() {
        fetch("/api/models/status")
            .then(function (r) { return r.json(); })
            .then(function (s) {
                statusEl.innerHTML =
                    "<strong>路由状态</strong><br>" +
                    "主模型：" + s.primary_profile_id +
                    (s.cooling_down ? " <mark>冷却中</mark>" : " ✓") + "<br>" +
                    "兜底模型：" + s.fallback_profile_id + "<br>" +
                    (s.local_profile_id ? "本地模型：" + s.local_profile_id + "<br>" : "") +
                    (s.last_primary_error ? "<br><small>最近错误：" + s.last_primary_error + "</small>" : "");
            });

        fetch("/api/models")
            .then(function (r) { return r.json(); })
            .then(function (models) {
                allModels = models;
                var profileSelect = document.getElementById("analytics-profile");
                profileSelect.innerHTML = '<option value="">全部模型</option>' +
                    models.map(function (m) {
                        return '<option value="' + escapeHtml(m.id) + '">' + escapeHtml(m.id) + '</option>';
                    }).join("");
                listEl.innerHTML = models.map(function (m) {
                    var badges = "";
                    if (m.is_primary) badges += '<span class="badge badge-primary">主模型</span>';
                    if (m.is_fallback) badges += '<span class="badge badge-fallback">兜底</span>';
                    if (!m.enabled) badges += '<span class="badge badge-fallback">已禁用</span>';
                    var actions = '<div class="model-card-footer">';
                    if (m.enabled && !m.is_primary) {
                        actions += '<button class="btn-primary btn-switch" data-id="' + m.id + '">设为主模型</button> ';
                    }
                    actions += '<button class="btn-edit" data-action="edit-model" data-id="' + m.id + '">编辑</button> ';
                    if (!m.is_primary) {
                        actions += '<button class="btn-danger" data-action="delete-model" data-id="' + m.id + '">删除</button>';
                    }
                    actions += "</div>";
                    return '<div class="model-card">' +
                        "<h5>" + m.id + " " + badges + "</h5>" +
                        "<p>" + m.provider + " / " + m.model + "</p>" +
                        "<p>" + m.type + " · " + m.temperature + " · " + m.max_tokens + " tokens · " + m.timeout_seconds + "s</p>" +
                        "<p>" + (m.pricing ? "已配置 " + m.billing_currency + " 计价" : "未计价") + "</p>" +
                        actions +
                        "</div>";
                }).join("");
            });
    }

    listEl.addEventListener("click", function (e) {
        var switchBtn = e.target.closest(".btn-switch");
        if (switchBtn) {
            var id = switchBtn.getAttribute("data-id");
            switchBtn.disabled = true;
            switchBtn.textContent = "切换中...";
            fetch("/api/models/switch", {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ profile_id: id }),
            }).then(function (r) {
                if (!r.ok) return r.json().then(function (d) { throw new Error(d.detail); });
                showToast("已切换主模型为 " + id, "success");
                loadModels();
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
                fetch("/api/models/" + mid, { method: "DELETE" })
                    .then(function (r) {
                        if (!r.ok) return r.json().then(function (d) { throw new Error(d.detail); });
                        showToast("已删除模型 " + mid, "success");
                        loadModels();
                    })
                    .catch(function (err) { showToast("删除失败：" + err.message, "error"); });
            });
        }

        if (action === "edit-model") {
            fetch("/api/models/" + mid)
                .then(function (r) { return r.json(); })
                .then(function (m) {
                    editingId = mid;
                    modalTitle.textContent = "编辑模型";
                    idGroup.style.display = "none";
                    document.getElementById("model-id").value = m.id;
                    document.getElementById("model-provider").value = m.provider;
                    document.getElementById("model-type").value = m.type;
                    document.getElementById("model-base-url").value = m.base_url || "";
                    document.getElementById("model-name").value = m.model;
                    document.getElementById("model-api-key-env").value = m.api_key_env || "";
                    document.getElementById("model-temperature").value = m.temperature;
                    document.getElementById("model-max-tokens").value = m.max_tokens;
                    document.getElementById("model-timeout").value = m.timeout_seconds;
                    document.getElementById("model-enabled").checked = m.enabled;
                    document.getElementById("price-input").value = m.pricing ? m.pricing.input_per_million : "";
                    document.getElementById("price-cached").value = m.pricing ? (m.pricing.cached_input_per_million || "") : "";
                    document.getElementById("price-output").value = m.pricing ? m.pricing.output_per_million : "";
                    document.getElementById("price-reasoning").value = m.pricing ? (m.pricing.reasoning_output_per_million || "") : "";
                    openModal();
                });
        }
    });

    form.addEventListener("submit", function (e) {
        e.preventDefault();
        var payload = {
            type: document.getElementById("model-type").value,
            provider: document.getElementById("model-provider").value,
            base_url: document.getElementById("model-base-url").value,
            model: document.getElementById("model-name").value,
            api_key_env: document.getElementById("model-api-key-env").value || null,
            temperature: parseFloat(document.getElementById("model-temperature").value),
            max_tokens: parseInt(document.getElementById("model-max-tokens").value),
            timeout_seconds: parseFloat(document.getElementById("model-timeout").value),
            enabled: document.getElementById("model-enabled").checked,
        };
        var inputPrice = document.getElementById("price-input").value.trim();
        var outputPrice = document.getElementById("price-output").value.trim();
        if ((inputPrice && !outputPrice) || (!inputPrice && outputPrice)) {
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

        var url, method;
        if (editingId) {
            url = "/api/models/" + editingId;
            method = "PUT";
        } else {
            payload.id = document.getElementById("model-id").value;
            url = "/api/models";
            method = "POST";
        }

        fetch(url, {
            method: method,
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        })
            .then(function (r) {
                if (!r.ok) return r.json().then(function (d) { throw new Error(d.detail); });
                showToast(editingId ? "已保存修改" : "已添加模型", "success");
                closeModal();
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
                document.getElementById("create-model-btn").style.display = tab === "config" ? "" : "none";
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
                escapeHtml(r.source) + '</td><td>' + escapeHtml(r.agent_id || "—") + '</td><td>' +
                escapeHtml(r.status) + '</td><td>' + r.call_count + '</td><td>' +
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

    function openRunDetail(runId) {
        fetchJson("/api/model-analytics/runs/" + encodeURIComponent(runId)).then(function (run) {
            var calls = run.calls || [];
            var html = '<div class="run-meta"><span>运行：' + escapeHtml(run.run_id) + '</span>' +
                '<span>来源：' + escapeHtml(run.source) + '</span><span>状态：' + escapeHtml(run.status) + '</span></div>' +
                '<table class="data-table"><thead><tr><th>#</th><th>操作</th><th>模型</th><th>状态</th><th>耗时</th><th>Token</th><th>成本状态</th></tr></thead><tbody>' +
                calls.map(function (c) {
                    return '<tr><td>' + c.sequence + (c.is_retry ? " 重试" : "") + (c.is_fallback ? " 切换" : "") +
                        '</td><td>' + escapeHtml(c.operation) + '</td><td>' + escapeHtml(c.profile_id + " / " + (c.actual_model || c.configured_model)) +
                        '</td><td>' + escapeHtml(c.status) + '</td><td>' + c.duration_ms + ' ms</td><td>' +
                        ((c.input_tokens || 0) + (c.output_tokens || 0)) + '</td><td>' + escapeHtml(c.cost_status) + '</td></tr>';
                }).join("") + '</tbody></table>';
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
