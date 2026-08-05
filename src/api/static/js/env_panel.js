/* Shared read-only renderer for declared environment variable bindings.

Scripts and plugins declare the variable *names* they need; the actual values
come from the platform global layer or (per tenant) the organization layer.
This module renders the resolved binding table used across the script, plugin,
and schedule popups. The organization values themselves are managed on the
tenant management page and are never displayed in plaintext here.
*/
(function () {
    "use strict";

    function escapeHtml(value) {
        return String(value == null ? "" : value)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;");
    }

    function sourceLabel(source) {
        if (source === "global") return "全局";
        if (source === "tenant") return "组织";
        if (source === "reserved") return "保留名";
        return "未配置";
    }

    function renderEnvBindings(container, bindings) {
        if (!container) return;
        if (!bindings || !bindings.length) {
            container.innerHTML = '<div class="env-empty">未声明环境变量</div>';
            return;
        }
        var rows = bindings.map(function (b) {
            var cls = b.defined ? "env-ok" : "env-missing";
            var value = b.defined ? (b.masked || "****") : "—";
            return '<tr class="' + cls + '"><td><code>' + escapeHtml(b.name) + "</code></td><td>" +
                escapeHtml(sourceLabel(b.source)) + '</td><td class="env-value">' +
                escapeHtml(value) + "</td></tr>";
        }).join("");
        container.innerHTML =
            '<table class="env-table"><thead><tr><th>变量名</th><th>来源</th>' +
            "<th>值(脱敏)</th></tr></thead><tbody>" + rows + "</tbody></table>";
    }

    function loadGlobalEnvBindings(kind, id, container) {
        if (!container) return;
        if (!id) { container.innerHTML = ""; return; }
        var qs = kind === "plugin"
            ? ("plugin_id=" + encodeURIComponent(id))
            : ("script_id=" + encodeURIComponent(id));
        fetch("/api/env/global/resolve?" + qs, { headers: { "Accept": "application/json" } })
            .then(function (r) { return r.json(); })
            .then(function (data) { renderEnvBindings(container, data.bindings || []); })
            .catch(function () { container.innerHTML = ""; });
    }

    window.EnvPanel = {
        renderEnvBindings: renderEnvBindings,
        loadGlobalEnvBindings: loadGlobalEnvBindings
    };
})();
