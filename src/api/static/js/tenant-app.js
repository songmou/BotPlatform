(function () {
    "use strict";

    var state = {
        me: null,
        organizationId: "",
        resource: "agents",
        view: "resources"
    };
    var titles = {
        agents: "智能体",
        models: "模型",
        skills: "Skill",
        plugins: "插件",
        mcp: "MCP",
        channels: "渠道",
        schedules: "自动化",
        knowledge: "知识库",
        drive: "文件",
        members: "成员"
    };

    function request(url, options) {
        return fetch(url, options).then(function (response) {
            if (response.status === 401) {
                window.location.href = "/login?next=/app";
                throw new Error("未登录");
            }
            return response.json().then(function (body) {
                if (!response.ok) throw new Error(body.detail || "请求失败");
                return body;
            });
        });
    }

    function escapeHtml(value) {
        var div = document.createElement("div");
        div.textContent = value == null ? "" : String(value);
        return div.innerHTML;
    }

    function setCards(items, render) {
        var container = document.getElementById("tenant-resource-list");
        document.getElementById("tenant-summary").textContent =
            "共 " + items.length + " 项";
        if (!items.length) {
            container.innerHTML = '<div class="tenant-empty">暂无可用内容</div>';
            return;
        }
        container.innerHTML = items.map(render).join("");
    }

    function resourceCard(item) {
        var payload = item.payload || {};
        var name = payload.name || payload.id || item.resource_id;
        var scope = item.effective_scope || item.scope;
        var labels = {
            public: "公共",
            public_override: "公共 · 已覆盖",
            organization: "组织自有"
        };
        return '<article class="tenant-resource-card">' +
            "<h3>" + escapeHtml(name) + "</h3>" +
            "<p>" + escapeHtml(payload.description || payload.role || "暂无描述") + "</p>" +
            '<div class="tenant-resource-meta">' +
            '<span class="tenant-badge">' + escapeHtml(labels[scope] || scope) + "</span>" +
            '<span class="tenant-badge">v' + escapeHtml(item.revision || 1) + "</span>" +
            '</div><div class="tenant-resource-actions">' +
            (scope === "organization"
                ? '<button data-resource-action="edit" data-id="' + escapeHtml(item.resource_id) + '">编辑</button>' +
                  '<button data-resource-action="delete" data-id="' + escapeHtml(item.resource_id) + '">删除</button>'
                : '<button data-resource-action="override" data-id="' + escapeHtml(item.resource_id) + '">覆盖</button>' +
                  (scope === "public_override"
                      ? '<button data-resource-action="reset" data-id="' + escapeHtml(item.resource_id) + '">恢复公共默认</button>'
                      : "")) +
            "</div></article>";
    }

    function loadResources() {
        return request(
            "/api/v2/orgs/" + encodeURIComponent(state.organizationId) +
            "/resources/" + encodeURIComponent(state.resource)
        ).then(function (data) {
            setCards(data.items || [], resourceCard);
        });
    }

    function loadMembers() {
        return request(
            "/api/v2/orgs/" + encodeURIComponent(state.organizationId) + "/members"
        ).then(function (data) {
            setCards(data.items || [], function (item) {
                return '<article class="tenant-resource-card"><h3>' +
                    escapeHtml(item.display_name || item.legacy_subject_id || "待认领成员") +
                    '</h3><div class="tenant-resource-meta"><span class="tenant-badge">' +
                    escapeHtml(item.role) + '</span><span class="tenant-badge">' +
                    escapeHtml(item.status) + "</span></div></article>";
            });
        });
    }

    function loadKnowledge() {
        return request(
            "/api/v2/orgs/" + encodeURIComponent(state.organizationId) +
            "/knowledge/categories"
        ).then(function (data) {
            setCards(data.items || [], function (item) {
                return '<article class="tenant-resource-card"><h3>' +
                    escapeHtml(item.name) + "</h3><p>" +
                    escapeHtml(item.description || "暂无描述") +
                    '</p><div class="tenant-resource-meta"><span class="tenant-badge">' +
                    (item.scope === "public" ? "公共只读" : "组织共享") +
                    '</span><span class="tenant-badge">' +
                    escapeHtml(item.source_count || 0) + " 个来源</span></div></article>";
            });
        });
    }

    function loadDrive() {
        return request(
            "/api/v2/orgs/" + encodeURIComponent(state.organizationId) +
            "/drive/entries?path="
        ).then(function (data) {
            setCards(data.entries || [], function (item) {
                return '<article class="tenant-resource-card"><h3>' +
                    escapeHtml(item.name) + "</h3><p>" +
                    escapeHtml(item.path) +
                    '</p><div class="tenant-resource-meta"><span class="tenant-badge">' +
                    escapeHtml(item.type) + "</span></div></article>";
            });
        });
    }

    function refresh() {
        if (!state.organizationId) return Promise.resolve();
        document.getElementById("tenant-view-title").textContent =
            titles[state.view === "resources" ? state.resource : state.view];
        var action = document.getElementById("tenant-primary-action");
        action.style.display = "";
        action.textContent = state.view === "members" ? "邀请成员" :
            state.view === "knowledge" ? "添加文本" :
            state.view === "drive" ? "上传文件" : "新建组织资源";
        if (state.view === "resources" &&
            ["plugins", "tools", "scripts"].indexOf(state.resource) >= 0) {
            action.style.display = "none";
        }
        if (state.view === "members") return loadMembers();
        if (state.view === "knowledge") return loadKnowledge();
        if (state.view === "drive") return loadDrive();
        return loadResources();
    }

    function selectNavigation(button) {
        document.querySelectorAll(".tenant-nav-item").forEach(function (item) {
            item.classList.toggle("active", item === button);
        });
        if (button.dataset.resource) {
            state.view = "resources";
            state.resource = button.dataset.resource;
        } else {
            state.view = button.dataset.view;
        }
        refresh().catch(showError);
    }

    function showError(error) {
        document.getElementById("tenant-resource-list").innerHTML =
            '<div class="tenant-empty">' + escapeHtml(error.message) + "</div>";
    }

    function jsonPrompt(title, initial) {
        var raw = window.prompt(title, JSON.stringify(initial || {}, null, 2));
        if (raw === null) return null;
        try {
            return JSON.parse(raw);
        } catch (error) {
            throw new Error("JSON 格式无效");
        }
    }

    function createResource() {
        var id = window.prompt("资源编号（字母开头）");
        if (!id) return Promise.resolve();
        var payload = jsonPrompt("输入资源 JSON 配置", {id: id, name: id});
        if (payload === null) return Promise.resolve();
        return request(
            "/api/v2/orgs/" + encodeURIComponent(state.organizationId) +
            "/resources/" + encodeURIComponent(state.resource) + "/" +
            encodeURIComponent(id),
            {
                method: "PUT",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({payload: payload})
            }
        ).then(refresh);
    }

    function inviteMember() {
        var role = (window.prompt("成员角色：member 或 admin", "member") || "").trim();
        if (!role) return Promise.resolve();
        return request(
            "/api/v2/orgs/" + encodeURIComponent(state.organizationId) + "/invitations",
            {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({role: role})
            }
        ).then(function (data) {
            window.prompt("邀请码（请安全发送给成员）", data.invitation_token);
        });
    }

    function addKnowledgeText() {
        var name = window.prompt("知识名称");
        if (!name) return Promise.resolve();
        var content = window.prompt("知识内容");
        if (!content) return Promise.resolve();
        return request(
            "/api/v2/orgs/" + encodeURIComponent(state.organizationId) + "/knowledge/text",
            {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({name: name, content: content})
            }
        ).then(refresh);
    }

    function uploadDriveFile() {
        var input = document.createElement("input");
        input.type = "file";
        input.addEventListener("change", function () {
            if (!input.files || !input.files[0]) return;
            var data = new FormData();
            data.append("file", input.files[0]);
            data.append("path", "");
            request(
                "/api/v2/orgs/" + encodeURIComponent(state.organizationId) + "/drive/upload",
                {method: "POST", body: data}
            ).then(refresh).catch(showError);
        });
        input.click();
        return Promise.resolve();
    }

    function primaryAction() {
        if (state.view === "members") return inviteMember();
        if (state.view === "knowledge") return addKnowledgeText();
        if (state.view === "drive") return uploadDriveFile();
        return createResource();
    }

    function resourceAction(button) {
        var id = button.dataset.id;
        var action = button.dataset.resourceAction;
        var base = "/api/v2/orgs/" + encodeURIComponent(state.organizationId) +
            "/resources/" + encodeURIComponent(state.resource) + "/" +
            encodeURIComponent(id);
        if (action === "delete") {
            if (!window.confirm("确定删除该组织资源？")) return Promise.resolve();
            return request(base, {method: "DELETE"}).then(refresh);
        }
        if (action === "reset") {
            return request(base + "/override", {method: "DELETE"}).then(refresh);
        }
        var payload = jsonPrompt(
            action === "override" ? "输入字段覆盖 JSON" : "输入完整资源 JSON",
            {}
        );
        if (payload === null) return Promise.resolve();
        if (action === "override") {
            return request(base + "/override", {
                method: "PUT",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({enabled: true, patch: payload, list_modes: {}})
            }).then(refresh);
        }
        return request(base, {
            method: "PUT",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({payload: payload})
        }).then(refresh);
    }

    function initialize() {
        return request("/api/v2/me").then(function (me) {
            state.me = me;
            document.getElementById("tenant-current-user").textContent =
                me.user.username;
            var select = document.getElementById("organization-switch");
            select.innerHTML = (me.organizations || []).map(function (item) {
                return '<option value="' + escapeHtml(item.organization_id) + '">' +
                    escapeHtml(item.name) + " · " + escapeHtml(item.role) +
                    "</option>";
            }).join("");
            state.organizationId = me.active_organization_id ||
                (me.organizations[0] && me.organizations[0].organization_id) || "";
            select.value = state.organizationId;
            if (!state.organizationId) {
                showError(new Error("当前账号尚未加入组织，请联系平台管理员发送邀请。"));
                return;
            }
            return refresh();
        });
    }

    document.querySelectorAll(".tenant-nav-item").forEach(function (button) {
        button.addEventListener("click", function () { selectNavigation(button); });
    });
    document.getElementById("tenant-refresh").addEventListener("click", function () {
        refresh().catch(showError);
    });
    document.getElementById("tenant-primary-action").addEventListener("click", function () {
        primaryAction().catch(showError);
    });
    document.getElementById("tenant-resource-list").addEventListener("click", function (event) {
        var button = event.target.closest("[data-resource-action]");
        if (button) resourceAction(button).catch(showError);
    });
    document.getElementById("organization-switch").addEventListener("change", function (event) {
        state.organizationId = event.target.value;
        request("/api/v2/me/active-organization", {
            method: "PUT",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({organization_id: state.organizationId})
        }).then(refresh).catch(showError);
    });
    document.getElementById("tenant-logout").addEventListener("click", function () {
        request("/api/auth/logout", {method: "POST"}).finally(function () {
            window.location.href = "/login";
        });
    });

    initialize().catch(showError);
})();
