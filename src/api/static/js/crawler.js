(function () {
    "use strict";

    var state = { sources: [], runs: [], pages: [], records: [], categories: [] };

    function api(path, options) {
        return fetch(organizationApi(path), options || {}).then(function (response) {
            return response.json().catch(function () { return {}; }).then(function (body) {
                if (!response.ok) throw new Error(body.detail || "请求失败");
                return body;
            });
        });
    }

    function lines(value) {
        return String(value || "").split(/\r?\n/).map(function (item) { return item.trim(); }).filter(Boolean);
    }

    function formatTime(value) {
        if (!value) return "—";
        try { return new Date(value).toLocaleString(); } catch (error) { return value; }
    }

    function badge(value) {
        return '<span class="crawler-badge ' + escapeHtml(value || "") + '">' + escapeHtml(value || "未知") + "</span>";
    }

    function button(label, action, id, secondary) {
        return '<button type="button" class="' + (secondary ? "btn-secondary" : "btn-primary") +
            '" data-action="' + action + '" data-id="' + escapeHtml(id) + '">' + label + "</button>";
    }

    function renderSummary() {
        var changed = state.runs.reduce(function (sum, item) { return sum + Number(item.pages_changed || 0); }, 0);
        var failed = state.runs.filter(function (item) { return item.status === "failed"; }).length;
        document.getElementById("crawler-summary").innerHTML = [
            [state.sources.length, "抓取源"], [state.pages.length, "已收录页面"],
            [changed, "最近变更"], [failed, "失败运行"]
        ].map(function (item) {
            return '<div class="crawler-stat"><strong>' + item[0] + '</strong><span>' + item[1] + "</span></div>";
        }).join("");
    }

    function renderSources() {
        var target = document.getElementById("crawler-sources");
        if (!state.sources.length) {
            target.innerHTML = '<div class="crawler-card crawler-muted">暂无抓取源。Owner 或 Admin 可以新建 HTTP/HTTPS 抓取任务。</div>';
            return;
        }
        target.innerHTML = state.sources.map(function (source) {
            var config = source.config || {};
            return '<article class="crawler-card"><h3>' + escapeHtml(source.name) + " " +
                badge(source.enabled ? "已启用" : "已停用") + '</h3><div class="crawler-card-meta">' +
                '<div>种子：<code>' + escapeHtml((config.seed_urls || [])[0] || "—") + "</code></div>" +
                '<div>范围：' + escapeHtml((config.allowed_domains || []).join("、")) + "</div>" +
                '<div>上限：深度 ' + Number(config.max_depth || 0) + " / " + Number(config.max_pages || 0) + " 页</div>" +
                '<div>计划：' + escapeHtml(config.schedule_cron || "仅手动") + "（平台时区）</div>" +
                '<div>知识库：' + escapeHtml(config.knowledge_category_id || "不写入") + "</div></div>" +
                '<div class="crawler-card-actions">' + button("立即抓取", "run", source.source_id, false) +
                (canManageOrganization() ? button("编辑", "edit", source.source_id, true) + button("删除", "delete", source.source_id, true) : "") +
                "</div></article>";
        }).join("");
    }

    function renderRuns() {
        document.getElementById("crawler-runs").innerHTML = state.runs.map(function (run) {
            var actions = "";
            if (["queued", "running"].indexOf(run.status) !== -1) actions += button("取消", "cancel-run", run.run_id, true);
            if (["failed", "canceled"].indexOf(run.status) !== -1) actions += button("重试", "retry-run", run.run_id, true);
            return "<tr><td>" + badge(run.status) + "</td><td>" + escapeHtml(run.trigger_type) + "</td><td>" +
                Number(run.pages_fetched || 0) + " / " + Number(run.pages_queued || 0) + "</td><td>" +
                Number(run.pages_changed || 0) + "（失败 " + Number(run.pages_failed || 0) + "）</td><td>" +
                Number(run.records_created || 0) + "</td><td>" + formatTime(run.created_at) + "</td><td>" + actions + "</td></tr>";
        }).join("") || '<tr><td colspan="7" class="crawler-muted">暂无运行记录</td></tr>';
    }

    function renderPages() {
        document.getElementById("crawler-pages").innerHTML = state.pages.map(function (page) {
            return "<tr><td>" + escapeHtml(page.title || "未命名") + '</td><td><a href="' + escapeHtml(page.canonical_url) +
                '" target="_blank" rel="noopener">' + escapeHtml(page.canonical_url) + "</a></td><td>" +
                escapeHtml(page.content_type || "—") + "</td><td>" + badge(page.status) + "</td><td>" +
                formatTime(page.last_fetched_at) + "</td><td>" + button("版本差异", "diff", page.page_id, true) + "</td></tr>";
        }).join("") || '<tr><td colspan="6" class="crawler-muted">暂无抓取页面</td></tr>';
    }

    function renderChart() {
        var chart = document.getElementById("crawler-chart");
        var numericKey = "";
        state.records.some(function (record) {
            return Object.keys(record.data || {}).some(function (key) {
                if (typeof record.data[key] === "number") { numericKey = key; return true; }
                return false;
            });
        });
        var values = numericKey ? state.records.slice().reverse().map(function (record) {
            return { value: Number((record.data || {})[numericKey]), time: record.fetched_at };
        }).filter(function (item) { return isFinite(item.value); }) : [];
        if (values.length < 2) {
            chart.innerHTML = '<div class="crawler-muted">至少积累两个数值记录后显示趋势图。</div>';
            return;
        }
        var min = Math.min.apply(null, values.map(function (item) { return item.value; }));
        var max = Math.max.apply(null, values.map(function (item) { return item.value; }));
        var span = max - min || 1;
        var points = values.map(function (item, index) {
            var x = values.length === 1 ? 20 : 20 + index * 760 / (values.length - 1);
            var y = 150 - (item.value - min) * 120 / span;
            return x.toFixed(1) + "," + y.toFixed(1);
        }).join(" ");
        chart.innerHTML = '<strong>' + escapeHtml(numericKey) + ' 趋势</strong><svg viewBox="0 0 800 180" preserveAspectRatio="none">' +
            '<polyline points="' + points + '"></polyline><text x="5" y="22">' + escapeHtml(String(max)) +
            '</text><text x="5" y="165">' + escapeHtml(String(min)) + "</text></svg>";
    }

    function renderRecords() {
        document.getElementById("crawler-records").innerHTML = state.records.map(function (record) {
            return "<tr><td>" + escapeHtml(record.template_name) + "</td><td><code>" +
                escapeHtml(JSON.stringify(record.data, null, 2)) + "</code></td><td>" + badge(record.extraction_method) +
                (record.error ? '<div class="crawler-muted">' + escapeHtml(record.error) + "</div>" : "") +
                '</td><td><a href="' + escapeHtml(record.canonical_url) + '" target="_blank" rel="noopener">' +
                escapeHtml(record.title || record.canonical_url) + "</a></td><td>" + formatTime(record.fetched_at) + "</td></tr>";
        }).join("") || '<tr><td colspan="5" class="crawler-muted">暂无结构化记录</td></tr>';
        renderChart();
    }

    function render() {
        renderSummary(); renderSources(); renderRuns(); renderPages(); renderRecords();
        document.getElementById("crawler-add").hidden = !canManageOrganization();
    }

    function refresh() {
        var notice = document.getElementById("crawler-notice");
        return Promise.all([
            api("/crawl-sources"), api("/crawl-runs?limit=100"), api("/crawl-pages?limit=100"),
            api("/crawl-records?limit=200"), api("/knowledge/categories")
        ]).then(function (result) {
            state.sources = result[0].items || []; state.runs = result[1].items || [];
            state.pages = result[2].items || []; state.records = result[3].items || [];
            state.categories = result[4].items || result[4].categories || [];
            notice.hidden = true; render();
        }).catch(function (error) {
            notice.textContent = error.message; notice.hidden = false;
            document.getElementById("crawler-add").hidden = !canManageOrganization();
            if (error.message.indexOf("插件未启用") !== -1) {
                notice.innerHTML = escapeHtml(error.message) + '。请到<a href="/platform/plugins">插件中心</a>启用 web_crawler。';
            }
        });
    }

    function fillEditor(source) {
        var config = source ? source.config || {} : {};
        document.getElementById("crawler-editor-title").textContent = source ? "编辑抓取源" : "新建抓取源";
        document.getElementById("crawler-source-id").value = source ? source.source_id : "";
        document.getElementById("crawler-name").value = source ? source.name : "";
        document.getElementById("crawler-render").value = config.render_mode || "auto";
        document.getElementById("crawler-seeds").value = (config.seed_urls || []).join("\n");
        document.getElementById("crawler-domains").value = (config.allowed_domains || []).join("\n");
        document.getElementById("crawler-depth").value = config.max_depth === undefined ? 2 : config.max_depth;
        document.getElementById("crawler-max-pages").value = config.max_pages || 100;
        document.getElementById("crawler-retention").value = config.retention_versions || 5;
        document.getElementById("crawler-cron").value = config.schedule_cron || "";
        document.getElementById("crawler-includes").value = (config.include_patterns || []).join("\n");
        document.getElementById("crawler-excludes").value = (config.exclude_patterns || []).join("\n");
        document.getElementById("crawler-templates").value = JSON.stringify(config.templates || [], null, 2);
        document.getElementById("crawler-enabled").checked = config.enabled !== false;
        var category = document.getElementById("crawler-category");
        category.innerHTML = '<option value="">不写入知识库</option>' + state.categories.map(function (item) {
            return '<option value="' + escapeHtml(item.category_id) + '">' + escapeHtml(item.name) + "</option>";
        }).join("");
        category.value = config.knowledge_category_id || "";
        document.getElementById("crawler-editor").showModal();
    }

    function saveEditor(event) {
        event.preventDefault();
        var templates;
        try { templates = JSON.parse(document.getElementById("crawler-templates").value || "[]"); }
        catch (error) { showToast("提取模板必须是有效 JSON", "error"); return; }
        var sourceId = document.getElementById("crawler-source-id").value;
        var body = {
            name: document.getElementById("crawler-name").value.trim(),
            seed_urls: lines(document.getElementById("crawler-seeds").value),
            allowed_domains: lines(document.getElementById("crawler-domains").value),
            include_patterns: lines(document.getElementById("crawler-includes").value),
            exclude_patterns: lines(document.getElementById("crawler-excludes").value),
            max_depth: Number(document.getElementById("crawler-depth").value),
            max_pages: Number(document.getElementById("crawler-max-pages").value),
            retention_versions: Number(document.getElementById("crawler-retention").value),
            schedule_cron: document.getElementById("crawler-cron").value.trim(),
            render_mode: document.getElementById("crawler-render").value,
            knowledge_category_id: document.getElementById("crawler-category").value,
            templates: templates, enabled: document.getElementById("crawler-enabled").checked
        };
        api("/crawl-sources" + (sourceId ? "/" + encodeURIComponent(sourceId) : ""), {
            method: sourceId ? "PUT" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
        }).then(function () { document.getElementById("crawler-editor").close(); showToast("抓取源已保存", "success"); refresh(); })
            .catch(function (error) { showToast(error.message, "error"); });
    }

    function showDiff(pageId) {
        api("/crawl-pages/" + encodeURIComponent(pageId)).then(function (page) {
            var snapshots = page.snapshots || [];
            if (snapshots.length < 2) throw new Error("该页面尚无两个可比较版本");
            return api("/crawl-pages/" + encodeURIComponent(pageId) + "/diff?older=" +
                encodeURIComponent(snapshots[1].snapshot_id) + "&newer=" + encodeURIComponent(snapshots[0].snapshot_id));
        }).then(function (result) {
            document.getElementById("crawler-diff").textContent = result.diff || "两个版本的正文没有差异";
            document.getElementById("crawler-detail").showModal();
        }).catch(function (error) { showToast(error.message, "error"); });
    }

    function action(event) {
        var target = event.target.closest("[data-action]");
        if (!target) return;
        var id = target.getAttribute("data-id"), name = target.getAttribute("data-action"), promise;
        if (name === "edit") { fillEditor(state.sources.filter(function (item) { return item.source_id === id; })[0]); return; }
        if (name === "diff") { showDiff(id); return; }
        if (name === "run") promise = api("/crawl-sources/" + encodeURIComponent(id) + "/runs", { method: "POST" });
        if (name === "cancel-run") promise = api("/crawl-runs/" + encodeURIComponent(id) + "/cancel", { method: "POST" });
        if (name === "retry-run") promise = api("/crawl-runs/" + encodeURIComponent(id) + "/retry", { method: "POST" });
        if (name === "delete") {
            if (!window.confirm("确认删除该抓取源及全部页面快照和记录？此操作不可恢复。")) return;
            promise = api("/crawl-sources/" + encodeURIComponent(id), { method: "DELETE" });
        }
        if (promise) promise.then(function () { showToast("操作已提交", "success"); refresh(); })
            .catch(function (error) { showToast(error.message, "error"); });
    }

    runScopedModule("crawler", function () {
        document.getElementById("crawler-add").hidden = !canManageOrganization();
        document.getElementById("crawler-refresh").addEventListener("click", refresh);
        document.getElementById("crawler-add").addEventListener("click", function () { fillEditor(null); });
        document.getElementById("crawler-form").addEventListener("submit", saveEditor);
        document.getElementById("crawler-editor-close").addEventListener("click", function () { document.getElementById("crawler-editor").close(); });
        document.getElementById("crawler-cancel").addEventListener("click", function () { document.getElementById("crawler-editor").close(); });
        document.getElementById("crawler-cron-example").addEventListener("click", function () {
            document.getElementById("crawler-cron").value = "0 1 * * *";
            document.getElementById("crawler-cron").focus();
        });
        document.getElementById("crawler-detail-close").addEventListener("click", function () { document.getElementById("crawler-detail").close(); });
        document.getElementById("crawler-page").addEventListener("click", action);
        document.querySelectorAll("[data-crawler-tab]").forEach(function (tab) {
            tab.addEventListener("click", function () {
                document.querySelectorAll("[data-crawler-tab]").forEach(function (item) { item.classList.toggle("active", item === tab); });
                document.querySelectorAll("[data-crawler-panel]").forEach(function (panel) { panel.hidden = panel.getAttribute("data-crawler-panel") !== tab.getAttribute("data-crawler-tab"); });
            });
        });
        refresh();
    });
}());
