(function () {
    "use strict";
    var page = document.getElementById("governance-page");
    if (!page) return;
    var module = page.getAttribute("data-module");
    var title = document.getElementById("governance-title");
    var eyebrow = document.getElementById("governance-eyebrow");
    var description = document.getElementById("governance-description");
    var summary = document.getElementById("governance-summary");
    var list = document.getElementById("governance-list");
    var refreshButton = document.getElementById("governance-refresh");
    var filters = document.getElementById("governance-filters");
    var platform = module.indexOf("platform-") === 0;
    var audit = module === "audit" || module === "platform-audit";

    var icons = {
        records: '<svg viewBox="0 0 24 24"><path d="M5 4h14v16H5zM8 8h8M8 12h8M8 16h5"/></svg>',
        success: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/></svg>',
        warning: '<svg viewBox="0 0 24 24"><path d="M12 3 2 21h20zM12 9v5M12 18h.01"/></svg>',
        calls: '<svg viewBox="0 0 24 24"><path d="M5 12h14M14 7l5 5-5 5"/></svg>',
        tokens: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8"/><path d="M9 9h6M9 12h6M9 15h4"/></svg>',
        cost: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M15 8.5c-.7-.6-1.6-1-2.8-1-1.5 0-2.7.8-2.7 2s1 1.8 2.8 2.2c1.8.4 2.7 1 2.7 2.3s-1.2 2.3-3 2.3c-1.2 0-2.4-.4-3.2-1.2M12 5v14"/></svg>'
    };

    function request(url) {
        return fetch(url).then(function (response) {
            return response.json().catch(function () { return {}; }).then(function (body) {
                if (!response.ok) throw new Error(body.detail || "加载失败");
                return body;
            });
        });
    }

    function number(value) {
        return Number(value || 0).toLocaleString("zh-CN");
    }

    function percent(value) {
        return value === null || value === undefined ? "—" : (Number(value) * 100).toFixed(1) + "%";
    }

    function stat(label, value, hint, tone, icon) {
        return '<article class="stat-card stat-card-' + (tone || "indigo") + '">' +
            '<div class="stat-icon" aria-hidden="true">' + (icons[icon] || icons.records) + '</div>' +
            '<div class="stat-content"><div class="stat-label">' + escapeHtml(label) +
            '</div><div class="stat-value">' + escapeHtml(String(value)) +
            '</div><div class="stat-hint">' + escapeHtml(hint || "") + '</div></div></article>';
    }

    function metric(label, value, detail) {
        return '<div class="analytics-metric"><div><span>' + escapeHtml(label) +
            '</span><strong>' + escapeHtml(String(value)) + '</strong></div><small>' +
            escapeHtml(detail) + '</small></div>';
    }

    function progress(label, value, tone) {
        var normalized = value === null || value === undefined ? 0 : Math.max(0, Math.min(1, Number(value)));
        return '<div class="health-row"><div><span>' + escapeHtml(label) + '</span><strong>' +
            percent(value) + '</strong></div><div class="health-track"><span class="health-fill health-fill-' +
            tone + '" style="width:' + (normalized * 100).toFixed(1) + '%"></span></div></div>';
    }

    function formatTime(value) {
        if (!value) return "—";
        var date = new Date(value);
        if (Number.isNaN(date.getTime())) return String(value);
        return date.toLocaleString("zh-CN", { hour12: false });
    }

    function sourceLabel(value) {
        var labels = {
            platform: "平台操作", organization: "组织操作",
            platform_delegation: "平台代管", api: "API"
        };
        return labels[value] || value || "—";
    }

    function renderAudit(items) {
        var successCount = items.filter(function (item) {
            return Number(item.status_code) >= 200 && Number(item.status_code) < 400;
        }).length;
        var failureCount = items.length - successCount;
        var delegatedCount = items.filter(function (item) {
            return item.source === "platform_delegation";
        }).length;
        summary.innerHTML = stat("审计记录", number(items.length), "最近返回的操作记录", "indigo", "records") +
            stat("成功操作", number(successCount), "HTTP 状态正常", "cyan", "success") +
            stat("异常操作", number(failureCount), "需要关注的失败请求", "amber", "warning") +
            stat("平台代管", number(delegatedCount), "保留真实操作者身份", "violet", "calls");

        var rows = items.map(function (item) {
            var ok = Number(item.status_code) >= 200 && Number(item.status_code) < 400;
            return '<tr><td class="audit-time">' + escapeHtml(formatTime(item.occurred_at)) + '</td>' +
                '<td><span class="audit-action">' + escapeHtml(item.action || "操作") + '</span></td>' +
                '<td class="audit-resource"><code>' + escapeHtml(item.resource || "—") + '</code>' +
                (item.detail ? '<small>' + escapeHtml(item.detail) + '</small>' : '') + '</td>' +
                (platform ? '<td><span class="table-secondary">' + escapeHtml(item.organization_id || "平台级") + '</span></td>' : '') +
                '<td><span class="source-badge">' + escapeHtml(sourceLabel(item.source)) + '</span></td>' +
                '<td><span class="status-badge ' + (ok ? "status-badge-success" : "status-badge-danger") +
                '"><span></span>' + escapeHtml(String(item.status_code || "—")) + '</span></td></tr>';
        }).join("");
        list.innerHTML = '<section class="governance-table-panel"><div class="overview-panel-heading">' +
            '<div><span class="overview-panel-kicker">操作明细</span><h3>审计事件</h3></div>' +
            '<span class="table-count">共 ' + number(items.length) + ' 条</span></div>' +
            '<div class="table-scroll"><table class="data-table governance-audit-table"><thead><tr>' +
            '<th>发生时间</th><th>操作</th><th>资源</th>' + (platform ? '<th>组织</th>' : '') +
            '<th>来源</th><th>状态</th></tr></thead><tbody>' +
            (rows || '<tr><td class="table-empty" colspan="' + (platform ? "6" : "5") + '">暂无审计记录</td></tr>') +
            '</tbody></table></div></section>';
    }

    function renderAnalytics(data) {
        var overview = data.overview || data || {};
        var inputTokens = Number(overview.input_tokens || 0);
        var outputTokens = Number(overview.output_tokens || 0);
        var totalTokens = inputTokens + outputTokens;
        var currency = overview.currency || "CNY";
        var cost = (Number(overview.cost_micros || 0) / 1000000).toFixed(4) + " " + currency;
        summary.innerHTML = stat("运行次数", number(overview.run_count || overview.total_runs), "智能体完整运行", "indigo", "calls") +
            stat("模型调用", number(overview.call_count), "包含重试与回退", "violet", "records") +
            stat("总 Token", number(totalTokens), "输入与输出合计", "cyan", "tokens") +
            stat("估算成本", cost, overview.unpriced_calls ? number(overview.unpriced_calls) + " 次调用未计价" : "全部调用已计价", "amber", "cost");

        list.innerHTML = '<div class="analytics-grid"><section class="overview-panel">' +
            '<div class="overview-panel-heading"><div><span class="overview-panel-kicker">运行质量</span><h3>服务健康度</h3></div>' +
            '<span class="status-badge status-badge-success"><span></span>实时汇总</span></div>' +
            '<div class="health-list">' + progress("调用成功率", overview.success_rate, "success") +
            progress("重试率", overview.retry_rate, "warning") +
            progress("回退率", overview.fallback_rate, "violet") +
            progress("反馈覆盖率", overview.feedback_coverage, "indigo") + '</div></section>' +
            '<section class="overview-panel"><div class="overview-panel-heading"><div>' +
            '<span class="overview-panel-kicker">性能与质量</span><h3>关键指标</h3></div></div>' +
            '<div class="analytics-metrics">' +
            metric("P50 延迟", number(overview.duration_p50_ms) + " ms", "半数调用在此时间内完成") +
            metric("P95 延迟", number(overview.duration_p95_ms) + " ms", "长尾调用性能") +
            metric("正向反馈", percent(overview.positive_rate), number(overview.feedback_count) + " 条有效反馈") +
            metric("工具失败", number(overview.tool_failure_count), "运行关联的失败工具调用") +
            '</div></section><section class="overview-panel token-panel"><div class="overview-panel-heading"><div>' +
            '<span class="overview-panel-kicker">用量构成</span><h3>Token 分布</h3></div></div>' +
            '<div class="token-visual"><div class="token-ring" style="--input-ratio:' +
            (totalTokens ? (inputTokens / totalTokens * 100).toFixed(1) : "0") + '%;--ring-end:' +
            (totalTokens ? "100" : "0") + '%"><span>' + number(totalTokens) +
            '<small>总量</small></span></div><div class="token-legend"><div><i class="legend-input"></i><span>输入 Token</span><strong>' +
            number(inputTokens) + '</strong></div><div><i class="legend-output"></i><span>输出 Token</span><strong>' +
            number(outputTokens) + '</strong></div><div><i class="legend-cache"></i><span>缓存命中</span><strong>' +
            number(overview.cached_input_tokens) + '</strong></div></div></div></section></div>';
    }

    function load() {
        if (!platform && !activeOrganizationId()) {
            title.textContent = audit ? "组织审计" : "组织分析";
            description.textContent = "加入或选择组织后，可查看该组织的数据与操作记录。";
            summary.innerHTML = "";
            list.innerHTML = '<div class="status-card">当前账号尚未加入组织。请联系组织管理员邀请你加入。</div>';
            return Promise.resolve();
        }
        title.textContent = audit ? (platform ? "平台审计" : "组织审计") : (platform ? "聚合分析" : "组织分析");
        eyebrow.textContent = audit ? "AUDIT TRAIL" : "ANALYTICS OVERVIEW";
        description.textContent = audit ?
            (platform ? "查看跨组织的平台管理操作与代管记录。" : "查看当前组织内的管理操作；平台代管操作保留真实操作者。") :
            (platform ? "查看全平台模型调用、成本和质量概览。" : "查看当前组织的模型调用、成本和质量概览。");
        list.innerHTML = '<div class="governance-loading"><span></span>正在加载数据…</div>';
        summary.innerHTML = "";
        if (filters) filters.hidden = audit;
        var url = audit ? (platform ? "/api/v2/platform/audit" : organizationApi("/audit")) :
            (platform ? "/api/v2/platform/analytics/overview" : organizationApi("/analytics/overview"));
        if (!audit && filters) {
            var params = new URLSearchParams();
            [["from", "governance-from"], ["to", "governance-to"], ["profile_id", "governance-profile"], ["agent_id", "governance-agent"], ["source", "governance-source"], ["status", "governance-status"]].forEach(function (pair) {
                var value = document.getElementById(pair[1]).value.trim();
                if (value) params.set(pair[0], value);
            });
            if (params.toString()) url += "?" + params.toString();
        }
        return request(url).then(function (data) {
            if (audit) renderAudit(data.items || []);
            else renderAnalytics(data);
        }).catch(function (error) {
            list.innerHTML = '<div class="status-card">' + escapeHtml(error.message) + '</div>';
        });
    }

    refreshButton.addEventListener("click", function () {
        refreshButton.disabled = true;
        load().finally(function () { refreshButton.disabled = false; });
    });
    if (filters) {
        filters.addEventListener("submit", function (event) { event.preventDefault(); load(); });
        document.getElementById("governance-clear").addEventListener("click", function () { filters.reset(); load(); });
    }
    (window.BP_CONTEXT_READY || Promise.resolve()).then(load);
})();
