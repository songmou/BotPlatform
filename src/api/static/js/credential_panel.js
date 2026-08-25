/* Read-only renderer for integration credential status of scripts.

Scripts backed by a platform integration (ctsehr/ctsoa/autogen) receive their
account/password from the per-tenant integration store and the Keychain, not
from the org-env store. This module shows whether those credentials are
configured for the selected tenant before a run or schedule, and links the
operator to the right setup command.
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

    function badge(ok, label) {
        return '<span class="cred-badge ' + (ok ? "cred-ok" : "cred-missing") + '">' +
            (ok ? "✓ " : "✗ ") + escapeHtml(label) + "</span>";
    }

    function renderStatus(container, status) {
        if (!container) return;
        if (!status || !status.requires_credentials) {
            container.innerHTML = '<div class="cred-empty">该脚本不需要集成凭据</div>';
            return;
        }
        var ready = !!status.ready;
        var accountBadge = badge(status.account_set, "账户已配置");
        var secretBadge = badge(status.keychain_secret_set, "密钥链密钥已配置");
        var readiness = ready
            ? '<span class="cred-badge cred-ok">✓ 凭据就绪，可正常执行</span>'
            : '<span class="cred-badge cred-missing">✗ 凭据未就绪，执行将失败</span>';
        var hint = ready ? "" :
            '<p class="cred-hint">请使用 <code>/integration setup ' +
            escapeHtml(status.integration_id) + "</code> 配置账户与密钥链。</p>";
        container.innerHTML =
            '<div class="cred-section">' +
            '<div class="cred-heading">集成凭据：' + escapeHtml(status.integration_id) + "</div>" +
            '<div class="cred-badges">' + accountBadge + secretBadge + "</div>" +
            '<div class="cred-readiness">' + readiness + "</div>" +
            hint +
            "</div>";
    }

    function loadScriptCredentials(scriptId, tenantId, container) {
        if (!container || !scriptId) {
            if (container) container.innerHTML = "";
            return;
        }
        var url = "/api/scripts/" + encodeURIComponent(scriptId) + "/credentials" +
            (tenantId ? ("?tenant_id=" + encodeURIComponent(tenantId)) : "");
        fetch(url, { headers: { "Accept": "application/json" } })
            .then(function (r) { return r.json(); })
            .then(function (data) { renderStatus(container, data); })
            .catch(function () { container.innerHTML = ""; });
    }

    window.CredentialPanel = {
        renderStatus: renderStatus,
        loadScriptCredentials: loadScriptCredentials
    };
})();
