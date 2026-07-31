/* ===== Users page ===== */

function initUsers() {
    var roleCache = [];
    var VIEWS = {
        tenants: { title: "机器人用户", desc: "管理通过机器人接入的终端用户（租户）" },
        admins: { title: "管理员账号", desc: "管理可登录面板的管理员账号" },
        roles: { title: "角色权限", desc: "查看与编辑各角色的权限" }
    };

    function activate(name) {
        if ((name === "admins" || name === "roles") && !hasPermission("admins.manage")) {
            name = "tenants";
        }
        if (!VIEWS[name]) name = "tenants";
        ["tenants", "admins", "roles"].forEach(function (t) {
            var el = document.getElementById("users-tab-" + t);
            if (el) el.style.display = t === name ? "" : "none";
        });
        document.querySelectorAll(".nav-sub-item[data-tab]").forEach(function (a) {
            a.classList.toggle("active", a.getAttribute("data-tab") === name);
        });
        var titleEl = document.getElementById("users-title");
        var descEl = document.getElementById("users-desc");
        if (titleEl) titleEl.textContent = VIEWS[name].title;
        if (descEl) descEl.textContent = VIEWS[name].desc;
        var createBtn = document.getElementById("create-admin-btn");
        if (createBtn) createBtn.style.display = name === "admins" && hasPermission("admins.manage") ? "" : "none";
        if (name === "tenants") loadTenants();
        if (name === "admins") loadAdmins();
        if (name === "roles") loadRoles();
    }

    function currentView() {
        return (window.location.hash || "").replace("#", "") || "tenants";
    }

    window.addEventListener("hashchange", function () { activate(currentView()); });

    function fmtTime(value) {
        if (!value) return "-";
        try { return new Date(value).toLocaleString("zh-CN"); } catch (e) { return value; }
    }

    /* --- Tenants tab --- */

    function loadTenants() {
        fetch("/api/tenants")
            .then(function (r) { return r.ok ? r.json() : Promise.reject(r); })
            .then(function (items) {
                var table = document.getElementById("tenant-table");
                var empty = document.getElementById("tenant-empty");
                var tbody = document.getElementById("tenant-tbody");
                if (!items.length) {
                    empty.style.display = "";
                    table.style.display = "none";
                    return;
                }
                empty.style.display = "none";
                table.style.display = "";
                var canDelete = hasPermission("tenants.delete");
                tbody.innerHTML = items.map(function (t) {
                    return "<tr>" +
                        '<td><code class="mono">' + escapeHtml(t.tenant_id.slice(0, 8)) + "…</code></td>" +
                        "<td>" + escapeHtml(t.bot_id) + "</td>" +
                        "<td>" + escapeHtml(t.user_id) + "</td>" +
                        "<td>" + fmtTime(t.created_at) + "</td>" +
                        "<td>" + t.message_count + "</td>" +
                        "<td>" + fmtTime(t.last_active_at) + "</td>" +
                        "<td>" + escapeHtml(t.model_mode) + "</td>" +
                        '<td class="table-actions">' +
                            '<button class="btn-secondary btn-sm tenant-view" data-id="' + t.tenant_id + '">详情</button>' +
                            (canDelete ? '<button class="btn-danger btn-sm tenant-delete" data-id="' + t.tenant_id + '">删除</button>' : "") +
                        "</td></tr>";
                }).join("");
            })
            .catch(function () { showToast("加载机器人用户失败", "error"); });
    }

    document.getElementById("tenant-tbody").addEventListener("click", function (evt) {
        var viewBtn = evt.target.closest(".tenant-view");
        if (viewBtn) return showTenantDetail(viewBtn.getAttribute("data-id"));
        var delBtn = evt.target.closest(".tenant-delete");
        if (delBtn) {
            var id = delBtn.getAttribute("data-id");
            showConfirm("删除该租户将永久清除其全部数据（对话、记忆、文件），且不可恢复。确定删除？").then(function (ok) {
                if (!ok) return;
                fetch("/api/tenants/" + encodeURIComponent(id), { method: "DELETE" })
                    .then(function (r) {
                        if (r.ok) { showToast("已删除", "success"); loadTenants(); }
                        else showToast("删除失败", "error");
                    });
            });
        }
    });

    function showTenantDetail(id) {
        var detailModal = document.getElementById("tenant-detail-modal");
        var body = document.getElementById("tenant-detail-body");
        document.getElementById("tenant-detail-title").textContent = "机器人用户详情";
        body.innerHTML = '<div class="tenant-detail-loading">正在加载用户详情…</div>';
        detailModal.style.display = "";
        fetch("/api/tenants/" + encodeURIComponent(id))
            .then(function (r) { return r.ok ? r.json() : Promise.reject(r); })
            .then(function (t) {
                document.getElementById("tenant-detail-title").textContent =
                    "机器人用户 · " + t.user_id;
                var subscriptions = t.schedule_subscriptions.length
                    ? '<ul class="tenant-detail-list">' + t.schedule_subscriptions.map(function (s) {
                        return '<li><code>' + escapeHtml(s.task_id) + "</code>" +
                            '<span class="badge ' + (s.enabled ? "badge-success" : "badge-muted") + '">' +
                            (s.enabled ? "已开启" : "已关闭") + "</span></li>";
                    }).join("") + "</ul>"
                    : '<div class="tenant-section-empty">暂无定时任务订阅</div>';
                var integrations = t.integrations.length
                    ? '<ul class="tenant-detail-list">' + t.integrations.map(function (i) {
                        return "<li><code>" + escapeHtml(i.integration_id) + "</code>" +
                            "<span>" + escapeHtml(fmtTime(i.updated_at)) + "</span></li>";
                    }).join("") + "</ul>"
                    : '<div class="tenant-section-empty">暂无集成</div>';
                var conversations = t.recent_events.length
                    ? '<div class="chat-log">' + t.recent_events.map(function (e) {
                        var side = e.role === "user" ? "right" : (e.role === "assistant" ? "left" : "system");
                        var ts = escapeHtml(fmtTime(e.created_at));
                        if (side === "system") {
                            return '<div class="chat-sys">' + escapeHtml(e.content) + "</div>";
                        }
                        var who = side === "right" ? "用户" : "助手";
                        return '<div class="chat-msg chat-' + side + '">' +
                            '<div class="chat-bubble">' +
                                '<div class="chat-meta"><span class="chat-who">' + who + "</span>" +
                                '<span class="chat-time">' + ts + "</span></div>" +
                                '<div class="chat-text">' + escapeHtml(e.content) + "</div>" +
                            "</div></div>";
                    }).join("") + "</div>"
                    : '<div class="tenant-section-empty tenant-chat-empty">暂无最近对话</div>';
                body.innerHTML =
                    '<section class="tenant-summary">' +
                        '<div class="tenant-identity">' +
                            '<div><span>渠道</span><strong>' + escapeHtml(t.bot_id) + "</strong></div>" +
                            '<div><span>用户标识</span><strong>' + escapeHtml(t.user_id) + "</strong></div>" +
                            '<div class="tenant-id"><span>租户 ID</span><code>' +
                                escapeHtml(t.tenant_id) + "</code></div>" +
                        "</div>" +
                        '<div class="tenant-stat-grid">' +
                            '<div><span>模型模式</span><strong>' + escapeHtml(t.model_mode) + "</strong></div>" +
                            '<div><span>消息数</span><strong>' + t.message_count + "</strong></div>" +
                            '<div><span>创建时间</span><strong>' +
                                escapeHtml(fmtTime(t.created_at)) + "</strong></div>" +
                            '<div><span>最近活跃</span><strong>' +
                                escapeHtml(fmtTime(t.last_active_at)) + "</strong></div>" +
                        "</div>" +
                    "</section>" +
                    '<div class="tenant-detail-layout">' +
                        '<div class="tenant-side-sections">' +
                            '<section class="tenant-detail-section"><h4>定时任务订阅</h4>' +
                                subscriptions + "</section>" +
                            '<section class="tenant-detail-section"><h4>集成</h4>' +
                                integrations + "</section>" +
                        "</div>" +
                        '<section class="tenant-detail-section tenant-conversation-section">' +
                            '<div class="tenant-section-title"><h4>最近对话</h4><span>最近 ' +
                                t.recent_events.length + " 条</span></div>" +
                            conversations +
                        "</section>" +
                    "</div>";
            })
            .catch(function () {
                body.innerHTML = '<div class="tenant-detail-error">加载用户详情失败，请稍后重试</div>';
                showToast("加载详情失败", "error");
            });
    }

    document.getElementById("tenant-detail-close").addEventListener("click", function () {
        document.getElementById("tenant-detail-modal").style.display = "none";
    });
    document.getElementById("tenant-detail-modal").addEventListener("click", function (evt) {
        if (evt.target === this) this.style.display = "none";
    });
    document.addEventListener("keydown", function (evt) {
        if (evt.key === "Escape") {
            document.getElementById("tenant-detail-modal").style.display = "none";
        }
    });

    /* --- Admins tab --- */

    var editingAdminId = null;

    function loadRoleOptions() {
        return fetch("/api/admins/roles")
            .then(function (r) { return r.ok ? r.json() : []; })
            .then(function (roles) {
                roleCache = roles;
                var select = document.getElementById("admin-role");
                select.innerHTML = roles.map(function (role) {
                    return '<option value="' + role.role_id + '">' + escapeHtml(role.name) + "（" + escapeHtml(role.code) + "）</option>";
                }).join("");
                return roles;
            });
    }

    function loadAdmins() {
        fetch("/api/admins")
            .then(function (r) { return r.ok ? r.json() : Promise.reject(r); })
            .then(function (items) {
                var tbody = document.getElementById("admin-tbody");
                tbody.innerHTML = items.map(function (u) {
                    return "<tr>" +
                        "<td>" + escapeHtml(u.username) + "</td>" +
                        "<td>" + escapeHtml(u.role.name) + "</td>" +
                        "<td>" + (u.disabled ? '<span class="badge badge-muted">已禁用</span>' : '<span class="badge badge-success">正常</span>') + "</td>" +
                        "<td>" + fmtTime(u.created_at) + "</td>" +
                        "<td>" + fmtTime(u.last_login_at) + "</td>" +
                        '<td class="table-actions">' +
                            '<button class="btn-secondary btn-sm admin-edit" data-id="' + u.user_id + '">编辑</button>' +
                            '<button class="btn-secondary btn-sm admin-reset" data-id="' + u.user_id + '">重置密码</button>' +
                            '<button class="btn-danger btn-sm admin-delete" data-id="' + u.user_id + '">删除</button>' +
                        "</td></tr>";
                }).join("");
            })
            .catch(function () { showToast("加载管理员账号失败", "error"); });
    }

    document.getElementById("admin-tbody").addEventListener("click", function (evt) {
        var editBtn = evt.target.closest(".admin-edit");
        if (editBtn) return openAdminModal(parseInt(editBtn.getAttribute("data-id"), 10));
        var resetBtn = evt.target.closest(".admin-reset");
        if (resetBtn) {
            var rid = resetBtn.getAttribute("data-id");
            showConfirm("重置该账号密码？其所有登录会话将立即失效。").then(function (ok) {
                if (!ok) return;
                fetch("/api/admins/" + rid + "/password", { method: "POST" })
                    .then(function (r) { return r.ok ? r.json() : Promise.reject(r); })
                    .then(function (data) {
                        window.prompt("新密码（仅显示一次，请立即复制）：", data.new_password);
                    })
                    .catch(function () { showToast("重置失败", "error"); });
            });
            return;
        }
        var delBtn = evt.target.closest(".admin-delete");
        if (delBtn) {
            var did = delBtn.getAttribute("data-id");
            showConfirm("确定删除该管理员账号？").then(function (ok) {
                if (!ok) return;
                fetch("/api/admins/" + did, { method: "DELETE" })
                    .then(function (r) {
                        if (r.ok) { showToast("已删除", "success"); loadAdmins(); }
                        else r.json().then(function (d) { showToast(d.detail || "删除失败", "error"); });
                    });
            });
        }
    });

    function openAdminModal(userId) {
        editingAdminId = userId || null;
        document.getElementById("admin-modal-title").textContent = editingAdminId ? "编辑账号" : "新建账号";
        document.getElementById("admin-username-group").style.display = editingAdminId ? "none" : "";
        document.getElementById("admin-password-group").style.display = "none";
        document.getElementById("admin-disabled-group").style.display = editingAdminId ? "" : "none";
        document.getElementById("admin-form").reset();
        loadRoleOptions().then(function () {
            if (!editingAdminId) return;
            fetch("/api/admins").then(function (r) { return r.json(); }).then(function (items) {
                var user = items.find(function (u) { return u.user_id === editingAdminId; });
                if (!user) return;
                document.getElementById("admin-role").value = String(user.role.role_id);
                document.getElementById("admin-disabled").checked = user.disabled;
            });
        });
        document.getElementById("admin-modal").style.display = "";
    }

    function closeAdminModal() {
        document.getElementById("admin-modal").style.display = "none";
    }

    document.getElementById("create-admin-btn").addEventListener("click", function () { openAdminModal(null); });
    document.getElementById("admin-modal-close").addEventListener("click", closeAdminModal);
    document.getElementById("admin-modal-cancel").addEventListener("click", closeAdminModal);

    document.getElementById("admin-form").addEventListener("submit", function (evt) {
        evt.preventDefault();
        var roleId = parseInt(document.getElementById("admin-role").value, 10);
        var req;
        if (editingAdminId) {
            req = fetch("/api/admins/" + editingAdminId, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    role_id: roleId,
                    disabled: document.getElementById("admin-disabled").checked
                })
            });
        } else {
            req = fetch("/api/admins", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    username: document.getElementById("admin-username").value.trim(),
                    role_id: roleId
                })
            });
        }
        req.then(function (r) {
            if (r.ok) {
                showToast(editingAdminId ? "已保存" : "已创建，初始密码为 12345", "success");
                closeAdminModal();
                loadAdmins();
            } else r.json().then(function (d) { showToast(d.detail || "保存失败", "error"); });
        });
    });

    /* --- Roles tab --- */

    var ALL_PERMISSIONS = [
        { key: "tenants.read", label: "查看机器人用户", desc: "浏览租户列表与详情" },
        { key: "tenants.delete", label: "删除机器人用户", desc: "清除租户及其全部数据" },
        { key: "panel.read", label: "查看面板配置", desc: "读取模型、智能体等配置" },
        { key: "panel.write", label: "修改面板配置", desc: "编辑模型、智能体、定时任务等" },
        { key: "plugins.read", label: "查看插件", desc: "浏览已安装插件及运行状态" },
        { key: "plugins.manage", label: "管理插件", desc: "安装、配置、禁用和移除可信插件" },
        { key: "model_analytics.read", label: "查看模型分析", desc: "查看用量、成本、质量与预算进度" },
        { key: "model_analytics.manage", label: "管理模型预算", desc: "创建、修改和删除月度预算" },
        { key: "admins.manage", label: "管理账号与角色", desc: "增删管理员并分配角色权限" }
    ];

    var ROLE_COLORS = { admin: "#4353ff", editor: "#0ea5e9", viewer: "#10b981" };
    var editingRoleId = null;

    function permLabel(key) {
        for (var i = 0; i < ALL_PERMISSIONS.length; i++) {
            if (ALL_PERMISSIONS[i].key === key) return ALL_PERMISSIONS[i].label;
        }
        return key;
    }

    function loadRoles() {
        fetch("/api/admins/roles")
            .then(function (r) { return r.ok ? r.json() : Promise.reject(r); })
            .then(function (roles) {
                roleCache = roles;
                var container = document.getElementById("roles-container");
                container.innerHTML = roles.map(function (role) {
                    var isAdmin = role.builtin && role.code === "admin";
                    var color = ROLE_COLORS[role.code] || "#6b7280";
                    var initial = escapeHtml((role.name || role.code || "?").charAt(0));
                    var count = isAdmin ? ALL_PERMISSIONS.length : role.permissions.length;
                    var chips = isAdmin
                        ? '<span class="perm-chip perm-chip-all">全部权限</span>'
                        : (role.permissions.length
                            ? role.permissions.map(function (key) {
                                return '<span class="perm-chip">' + escapeHtml(permLabel(key)) + "</span>";
                            }).join("")
                            : '<span class="perm-empty">无任何权限</span>');
                    return '<div class="role-row' + (isAdmin ? " role-row-locked" : "") + '" data-role-id="' + role.role_id + '"' + (isAdmin ? "" : ' tabindex="0"') + '>' +
                        '<span class="role-avatar" style="background:' + color + '">' + initial + "</span>" +
                        '<div class="role-body">' +
                            '<div class="role-line"><span class="role-name">' + escapeHtml(role.name) + "</span>" +
                            '<span class="role-code">' + escapeHtml(role.code) + "</span>" +
                            '<span class="role-count">' + count + " 项权限</span></div>" +
                            '<div class="role-chips">' + chips + "</div>" +
                        "</div>" +
                        (isAdmin
                            ? '<span class="role-lock" title="内置角色不可修改"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="4" y="11" width="16" height="9" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg></span>'
                            : '<span class="role-edit-hint"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>编辑</span>') +
                        "</div>";
                }).join("");
            })
            .catch(function () { showToast("加载角色失败", "error"); });
    }

    function findRole(roleId) {
        for (var i = 0; i < roleCache.length; i++) {
            if (String(roleCache[i].role_id) === String(roleId)) return roleCache[i];
        }
        return null;
    }

    function openRoleModal(roleId) {
        var role = findRole(roleId);
        if (!role) return;
        editingRoleId = roleId;
        document.getElementById("role-modal-title").textContent = "编辑「" + role.name + "」权限";
        document.getElementById("role-modal-body").innerHTML =
            '<div class="role-perms">' + ALL_PERMISSIONS.map(function (p) {
                var checked = role.permissions.indexOf(p.key) !== -1 ? " checked" : "";
                return '<label class="perm-row">' +
                    '<span class="perm-text"><span class="perm-label">' + p.label + "</span>" +
                    '<span class="perm-desc">' + p.desc + "</span></span>" +
                    '<span class="switch"><input type="checkbox" class="role-perm" data-perm="' + p.key + '"' + checked + '><span class="slider"></span></span>' +
                    "</label>";
            }).join("") + "</div>";
        document.getElementById("role-modal").style.display = "";
    }

    function closeRoleModal() {
        editingRoleId = null;
        document.getElementById("role-modal").style.display = "none";
    }

    document.getElementById("roles-container").addEventListener("click", function (evt) {
        var row = evt.target.closest(".role-row");
        if (!row || row.classList.contains("role-row-locked")) return;
        openRoleModal(row.getAttribute("data-role-id"));
    });

    document.getElementById("role-modal-close").addEventListener("click", closeRoleModal);
    document.getElementById("role-modal-cancel").addEventListener("click", closeRoleModal);

    document.getElementById("role-modal-save").addEventListener("click", function () {
        if (editingRoleId === null) return;
        var permissions = [];
        document.querySelectorAll("#role-modal-body .role-perm:checked").forEach(function (cb) {
            permissions.push(cb.getAttribute("data-perm"));
        });
        fetch("/api/admins/roles/" + editingRoleId, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ permissions: permissions })
        }).then(function (r) {
            if (r.ok) { showToast("角色权限已更新", "success"); closeRoleModal(); loadRoles(); }
            else r.json().then(function (d) { showToast(d.detail || "保存失败", "error"); });
        });
    });

    /* --- bootstrap --- */

    loadMe().then(function () {
        activate(currentView());
    }).catch(function () {
        window.location.href = "/login";
    });
}
