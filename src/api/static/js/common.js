/* Auto-split from app.js. Shared globals + auth + cross-page helpers. */

/* ===== Global utilities ===== */

var ICON_COPY = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
var ICON_REGEN = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>';

/* Theme toggle */
document.getElementById("theme-toggle").addEventListener("click", function () {
    var html = document.documentElement;
    var next = html.getAttribute("data-theme") === "dark" ? "light" : "dark";
    html.setAttribute("data-theme", next);
    try { localStorage.setItem("bp-theme", next); } catch (e) {}
});

/* Work-in-progress menu placeholder */
document.addEventListener("click", function (evt) {
    var el = evt.target.closest && evt.target.closest("[data-wip='1']");
    if (!el) return;
    evt.preventDefault();
    var name = el.getAttribute("data-wip-name") || "该功能";
    showToast(name + " 正在开发中，敬请期待", "info");
});

/* Sidebar nav group expand/collapse */
document.addEventListener("click", function (evt) {
    var toggle = evt.target.closest && evt.target.closest(".nav-group-toggle");
    if (!toggle) return;
    var group = toggle.closest(".nav-group");
    if (group) group.classList.toggle("open");
});

function showToast(message, type) {
    type = type || "info";
    var container = document.getElementById("toast-container");
    if (!container) return;
    var toast = document.createElement("div");
    toast.className = "toast toast-" + type;
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(function () { toast.classList.add("show"); }, 10);
    setTimeout(function () {
        toast.classList.remove("show");
        setTimeout(function () { toast.remove(); }, 300);
    }, 2600);
}

function showConfirm(message) {
    return new Promise(function (resolve) {
        var overlay = document.getElementById("confirm-overlay");
        var okBtn = document.getElementById("confirm-ok");
        var cancelBtn = document.getElementById("confirm-cancel");
        document.getElementById("confirm-message").textContent = message;
        overlay.style.display = "";
        function cleanup(result) {
            overlay.style.display = "none";
            okBtn.removeEventListener("click", onOk);
            cancelBtn.removeEventListener("click", onCancel);
            resolve(result);
        }
        function onOk() { cleanup(true); }
        function onCancel() { cleanup(false); }
        okBtn.addEventListener("click", onOk);
        cancelBtn.addEventListener("click", onCancel);
    });
}

function showNoticeDialog(title, value) {
    var overlay = document.getElementById("notice-dialog-overlay");
    var input = document.getElementById("notice-dialog-value");
    document.getElementById("notice-dialog-title").textContent = title;
    input.value = value || "";
    overlay.style.display = "";
    function close() { overlay.style.display = "none"; }
    document.getElementById("notice-dialog-close").onclick = close;
    document.getElementById("notice-dialog-ok").onclick = close;
    document.getElementById("notice-dialog-copy").onclick = function () {
        copyText(input.value).then(function () { showToast("已复制", "success"); });
    };
}

/* Small reusable HTML form dialog for scoped management pages. */
function showFormDialog(options) {
    return new Promise(function (resolve) {
        var overlay = document.getElementById("form-dialog-overlay");
        var form = document.getElementById("form-dialog-form");
        var fields = options.fields || [];
        document.getElementById("form-dialog-title").textContent = options.title || "编辑";
        document.getElementById("form-dialog-fields").innerHTML = fields.map(function (field) {
            var id = "dialog-field-" + field.name;
            var label = '<label for="' + id + '">' + escapeHtml(field.label || field.name) + "</label>";
            var value = field.value === undefined || field.value === null ? "" : String(field.value);
            if (field.type === "select") return '<div class="form-group">' + label + '<select id="' + id + '" name="' + escapeHtml(field.name) + '">' + (field.options || []).map(function (item) { return '<option value="' + escapeHtml(item.value) + '"' + (String(item.value) === value ? " selected" : "") + ">" + escapeHtml(item.label) + "</option>"; }).join("") + "</select></div>";
            if (field.type === "checkbox") return '<div class="form-group"><label class="checkbox-label"><input id="' + id + '" name="' + escapeHtml(field.name) + '" type="checkbox"' + (field.value ? " checked" : "") + "> " + escapeHtml(field.label || field.name) + "</label></div>";
            if (field.type === "textarea") return '<div class="form-group">' + label + '<textarea id="' + id + '" name="' + escapeHtml(field.name) + '" rows="' + (field.rows || 4) + '"' + (field.required ? " required" : "") + ">" + escapeHtml(value) + "</textarea></div>";
            return '<div class="form-group">' + label + '<input id="' + id + '" name="' + escapeHtml(field.name) + '" type="' + (field.type || "text") + '" value="' + escapeHtml(value) + '"' + (field.required ? " required" : "") + (field.placeholder ? ' placeholder="' + escapeHtml(field.placeholder) + '"' : "") + "></div>";
        }).join("");
        overlay.style.display = "";
        function finish(value) {
            overlay.style.display = "none";
            form.onsubmit = null;
            document.getElementById("form-dialog-cancel").onclick = null;
            document.getElementById("form-dialog-close").onclick = null;
            resolve(value);
        }
        form.onsubmit = function (event) {
            event.preventDefault();
            var result = {};
            fields.forEach(function (field) {
                var element = document.getElementById("dialog-field-" + field.name);
                result[field.name] = field.type === "checkbox" ? element.checked : element.value.trim();
            });
            finish(result);
        };
        document.getElementById("form-dialog-cancel").onclick = function () { finish(null); };
        document.getElementById("form-dialog-close").onclick = function () { finish(null); };
    });
}

function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
        return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve) {
        var ta = document.createElement("textarea");
        ta.value = text;
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        ta.remove();
        resolve();
    });
}

function escapeHtml(text) {
    var div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
}


function mcpTransportLabel(transport) {
    var map = {
        stdio: "本地命令（stdio）",
        sse: "远程服务（SSE）",
        streamablehttp: "远程服务（Streamable HTTP）"
    };
    return map[transport] || transport;
}

/* ===== Auth (current user + logout) ===== */

var CURRENT_PERMISSIONS = null;
var BP_CONTEXT = null;
var BP_CONTEXT_READY = null;

function hasPermission(perm) {
    if (!CURRENT_PERMISSIONS) return false;
    return CURRENT_PERMISSIONS.indexOf("*") !== -1 || CURRENT_PERMISSIONS.indexOf(perm) !== -1;
}

function loadMe() {
    return fetch("/api/v2/me")
        .then(function (r) {
            if (!r.ok) throw new Error("unauthorized");
            return r.json();
        })
        .then(function (me) {
            CURRENT_PERMISSIONS = (me.user && me.user.platform_permissions) || [];
            BP_CONTEXT = me;
            var el = document.getElementById("current-user");
            if (el) {
                el.textContent = me.user.username + " · " +
                    (hasPermission("admins.manage") ? "平台管理员" : "组织成员");
            }
            renderOrganizationPageSwitch(me);
            return me;
        });
}

function selectedOrganizationId() {
    return new URLSearchParams(window.location.search).get("organization_id") || "";
}

function activeOrganizationId() {
    return selectedOrganizationId();
}

function selectedOrganization() {
    var id = selectedOrganizationId();
    var organizations = (BP_CONTEXT && BP_CONTEXT.organizations) || [];
    return organizations.filter(function (item) {
        return item.organization_id === id;
    })[0] || null;
}

function organizationApi(path) {
    var organizationId = activeOrganizationId();
    if (!organizationId) throw new Error("当前未选择组织");
    return "/api/v2/orgs/" + encodeURIComponent(organizationId) + path;
}

function canWriteOrganization() {
    var organization = selectedOrganization();
    return !!(organization && organization.permissions &&
        organization.permissions.collaborate);
}

function canManageOrganization() {
    var organization = selectedOrganization();
    return !!(organization && organization.permissions &&
        organization.permissions.manage_sensitive);
}

function updateOrganizationLinks(organizationId) {
    Array.prototype.forEach.call(document.querySelectorAll("[data-organization-link]"), function (link) {
        var url = new URL(link.getAttribute("href"), window.location.origin);
        if (organizationId) url.searchParams.set("organization_id", organizationId);
        else url.searchParams.delete("organization_id");
        link.setAttribute("href", url.pathname + url.search);
    });
}

function renderOrganizationPageSwitch(me) {
    var select = document.getElementById("organization-page-switch");
    var currentId = selectedOrganizationId();
    var organizations = me.organizations || [];
    var rememberedId = "";
    try { rememberedId = localStorage.getItem("bp-last-organization-id") || ""; }
    catch (error) {}
    var remembered = organizations.some(function (item) {
        return item.organization_id === rememberedId;
    });
    if (!currentId && remembered) currentId = rememberedId;
    if (!currentId && organizations.length === 1) currentId = organizations[0].organization_id;
    if (currentId) {
        try { localStorage.setItem("bp-last-organization-id", currentId); }
        catch (error) {}
    }
    updateOrganizationLinks(currentId);
    if (!select) return;
    if (!selectedOrganizationId() && currentId) {
        var singleUrl = new URL(window.location.href);
        singleUrl.searchParams.set("organization_id", currentId);
        window.history.replaceState({}, "", singleUrl.pathname + singleUrl.search + singleUrl.hash);
    }
    var options = [{ value: "", label: organizations.length ? "请选择组织" : "暂无可用组织" }];
    organizations.forEach(function (item) {
        options.push({ value: item.organization_id, label: item.name + " · " + item.role });
    });
    select.innerHTML = options.map(function (item) {
        return '<option value="' + escapeHtml(item.value) + '">' +
            escapeHtml(item.label) + "</option>";
    }).join("");
    select.value = currentId;
    select.disabled = organizations.length === 0;
    var selected = selectedOrganization();
    var role = document.getElementById("organization-context-role");
    if (role) role.textContent = selected ? selected.role : "未选择";
    var delegated = selected && selected.role === "platform_delegation";
    var banner = document.getElementById("delegation-banner");
    if (banner) banner.hidden = !delegated;
    var required = document.getElementById("organization-selection-required");
    var root = document.getElementById("page-content-root");
    if (required) required.hidden = !!currentId;
    if (root) root.hidden = !currentId;
    select.addEventListener("change", function (event) {
        var url = new URL(window.location.href);
        if (event.target.value) url.searchParams.set("organization_id", event.target.value);
        else url.searchParams.delete("organization_id");
        window.location.href = url.pathname + url.search + url.hash;
    });
}

function runScopedModule(moduleName, initializer) {
    var ready = window.BP_CONTEXT_READY || loadMe();
    return ready.then(function () {
        var organizationPage = document.body.getAttribute("data-organization-page") === "1";
        if (organizationPage && !selectedOrganizationId()) return null;
        return initializer({
            module: moduleName,
            mode: organizationPage ? "organization" : "platform-public",
            organizationId: organizationPage ? selectedOrganizationId() : ""
        });
    }).catch(function (error) {
        showToast(error.message || "页面加载失败", "error");
        return null;
    });
}

(function () {
    var logoutBtn = document.getElementById("logout-btn");
    if (logoutBtn) {
        logoutBtn.addEventListener("click", function () {
            fetch("/api/auth/logout", { method: "POST" }).then(function () {
                window.location.href = "/login";
            });
        });
    }
    if (document.getElementById("current-user")) {
        BP_CONTEXT_READY = loadMe();
        window.BP_CONTEXT_READY = BP_CONTEXT_READY;
        BP_CONTEXT_READY.catch(function () {});
    }
})();
