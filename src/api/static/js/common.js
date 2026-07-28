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

function hasPermission(perm) {
    if (!CURRENT_PERMISSIONS) return false;
    return CURRENT_PERMISSIONS.indexOf("*") !== -1 || CURRENT_PERMISSIONS.indexOf(perm) !== -1;
}

function loadMe() {
    return fetch("/api/auth/me")
        .then(function (r) {
            if (!r.ok) throw new Error("unauthorized");
            return r.json();
        })
        .then(function (me) {
            CURRENT_PERMISSIONS = me.permissions || [];
            var el = document.getElementById("current-user");
            if (el) el.textContent = me.user.username + " · " + me.user.role.name;
            if (!hasPermission("admins.manage")) {
                ["nav-sub-admins", "nav-sub-roles"].forEach(function (id) {
                    var item = document.getElementById(id);
                    if (item) item.style.display = "none";
                });
            }
            return me;
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
    if (document.getElementById("current-user")) loadMe().catch(function () {});
})();

