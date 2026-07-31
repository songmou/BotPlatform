function initPlugins() {
    var listEl = document.getElementById("plugin-list");
    var modal = document.getElementById("plugin-modal");
    var installModal = document.getElementById("plugin-install-modal");
    var plugins = [];
    var tenants = [];
    var editing = null;
    var packageUpdateId = null;

    function escapeAttribute(value) {
        return escapeHtml(String(value))
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function pluginColor(value) {
        var color = String(value || "");
        return /^#[0-9a-f]{3,8}$/i.test(color) ? color : "#6b7280";
    }

    function request(url, options) {
        return fetch(url, options).then(function (response) {
            if (response.ok) return response.json();
            return response.json().then(function (data) {
                throw new Error(data.detail || "请求失败");
            });
        });
    }

    function load() {
        return Promise.all([
            request("/api/plugins"),
            request("/api/tenants").catch(function () { return []; })
        ]).then(function (results) {
            plugins = results[0] || [];
            tenants = results[1] || [];
            render();
        }).catch(function (error) {
            listEl.innerHTML = '<div class="empty-state">加载插件失败：' +
                escapeHtml(error.message) + "</div>";
        });
    }

    function statusLabel(plugin) {
        if (!plugin.installed || plugin.runtime_status === "missing") return "插件包缺失";
        if (plugin.missing_dependencies && plugin.missing_dependencies.length) return "依赖缺失";
        if (plugin.runtime_status === "running") return "运行中";
        if (plugin.runtime_status === "error") return "加载失败";
        if (plugin.enabled) return "待重启";
        return "已禁用";
    }

    function render() {
        var query = document.getElementById("plugin-search").value.trim().toLowerCase();
        var filter = document.getElementById("plugin-filter-status").value;
        var summary = document.getElementById("plugin-result-summary");
        var filtered = plugins.filter(function (plugin) {
            var matches = !query || (plugin.id + " " + plugin.name + " " +
                plugin.description).toLowerCase().indexOf(query) !== -1;
            if (!matches || !filter) return matches;
            if (filter === "running") return plugin.runtime_status === "running";
            if (filter === "disabled") return !plugin.enabled;
            return plugin.runtime_status === "error" ||
                plugin.runtime_status === "dependency_missing";
        });
        summary.textContent = filtered.length === plugins.length
            ? "共 " + plugins.length + " 个插件"
            : "显示 " + filtered.length + " / " + plugins.length + " 个插件";
        if (!filtered.length) {
            listEl.innerHTML = '<div class="empty-state">' +
                (plugins.length ? "没有符合当前条件的插件" : "暂无已安装插件") +
                "</div>";
            return;
        }
        listEl.innerHTML = filtered.map(function (plugin) {
            var badgeClass = plugin.runtime_status === "running"
                ? "badge-success"
                : plugin.enabled ? "badge-warning" : "badge-muted";
            var color = pluginColor(plugin.color);
            return '<div class="plugin-tile" data-id="' + escapeAttribute(plugin.id) +
                '" tabindex="0" role="button" aria-label="查看插件 ' +
                escapeAttribute(plugin.name) + '" style="--plugin-color:' +
                color + '">' +
                '<div class="plugin-tile-header">' +
                '<div class="plugin-avatar" style="background:' + color + '">' +
                escapeHtml(plugin.icon) + "</div>" +
                '<div class="plugin-tile-info"><div class="plugin-tile-name">' +
                escapeHtml(plugin.name) + '</div><div class="plugin-tile-meta">' +
                '<span class="badge ' + badgeClass + '">' + escapeHtml(statusLabel(plugin)) +
                '</span><span class="text-muted">v' + escapeHtml(plugin.version) +
                " · " + plugin.tool_count + " 个工具</span></div></div></div>" +
                '<p class="plugin-tile-desc">' + escapeHtml(plugin.description) + "</p>" +
                '<div class="plugin-tile-tags"><span class="tag">' +
                (plugin.source === "bundled" ? "内置额外插件" : "本地插件") +
                "</span></div></div>";
        }).join("");
    }

    function renderSettings(plugin) {
        var schema = plugin.settings_schema || {};
        var properties = schema.properties || {};
        var required = schema.required || [];
        var settings = plugin.settings || {};
        var form = document.getElementById("plugin-settings-form");
        var names = Object.keys(properties);
        if (!names.length) {
            form.innerHTML = '<div class="plugin-setting-empty">该插件没有可配置项。</div>';
            return;
        }
        form.innerHTML = names.map(function (name) {
            var field = properties[name] || {};
            var value = Object.prototype.hasOwnProperty.call(settings, name)
                ? settings[name] : field.default;
            var isRequired = required.indexOf(name) !== -1;
            var displayName = field.title || name;
            var controlLabel = escapeAttribute(displayName);
            var label = '<span class="plugin-setting-name">' +
                escapeHtml(displayName) + "</span>" +
                (displayName !== name
                    ? '<code class="plugin-setting-key">' + escapeHtml(name) + "</code>"
                    : "") +
                (isRequired ? ' <span class="plugin-setting-required">*</span>' : "");
            var typeLabels = {
                string: "文本",
                boolean: "开关",
                integer: "整数",
                number: "数值",
                array: "数组",
                object: "对象"
            };
            var typeLabel = field["x-ui"] === "tenant-list"
                ? "用户列表"
                : field.enum ? "选项" : (typeLabels[field.type] || "文本");
            var control;
            if (field["x-ui"] === "tenant-list") {
                var selected = {};
                (value || []).forEach(function (id) { selected[id] = true; });
                control = '<div class="plugin-setting-select plugin-setting-select-multiple">' +
                    '<select multiple data-setting="' + escapeAttribute(name) +
                    '" data-type="tenant-list" size="5" aria-label="' + controlLabel + '">' +
                    tenants.map(function (tenant) {
                        return '<option value="' + escapeAttribute(tenant.tenant_id) + '"' +
                            (selected[tenant.tenant_id] ? " selected" : "") + ">" +
                            escapeHtml(tenant.user_id + "（" + tenant.tenant_id.slice(0, 8) + "）") +
                            "</option>";
                    }).join("") + "</select></div>";
            } else if (field.type === "boolean") {
                control = '<div class="plugin-setting-boolean"><label class="switch-label">' +
                    '<input type="checkbox" data-setting="' +
                    escapeAttribute(name) + '" data-type="boolean" aria-label="' + controlLabel +
                    '"' + (value ? " checked" : "") +
                    '><span class="switch" aria-hidden="true"></span><span class="plugin-setting-state">' +
                    (value ? "已开启" : "已关闭") + "</span></label></div>";
            } else if (field.enum) {
                control = '<div class="plugin-setting-select"><select data-setting="' +
                    escapeAttribute(name) + '" data-type="string" aria-label="' +
                    controlLabel + '">' +
                    field.enum.map(function (item) {
                        return '<option value="' + escapeAttribute(item) + '"' +
                            (item === value ? " selected" : "") + ">" +
                            escapeHtml(String(item)) + "</option>";
                    }).join("") + "</select></div>";
            } else if (field.type === "integer" || field.type === "number") {
                control = '<input type="number" data-setting="' + escapeAttribute(name) +
                    '" data-type="' + field.type + '" aria-label="' + controlLabel +
                    '" value="' +
                    (value == null ? "" : escapeAttribute(value)) + '"' +
                    (field.minimum != null
                        ? ' min="' + escapeAttribute(field.minimum) + '"'
                        : "") +
                    (field.maximum != null
                        ? ' max="' + escapeAttribute(field.maximum) + '"'
                        : "") + ">";
            } else if (field.type === "array" || field.type === "object") {
                control = '<textarea rows="5" data-setting="' + escapeAttribute(name) +
                    '" data-type="json" aria-label="' + controlLabel + '">' +
                    escapeHtml(JSON.stringify(value == null ? (field.type === "array" ? [] : {}) : value, null, 2)) +
                    "</textarea>";
            } else {
                control = '<input type="text" data-setting="' + escapeAttribute(name) +
                    '" data-type="string" aria-label="' + controlLabel + '" value="' +
                    escapeAttribute(value == null ? "" : value) + '"' +
                    (field["x-ui"] === "path" ? ' placeholder="本机路径"' : "") + ">";
            }
            return '<div class="form-group plugin-setting-field">' +
                '<div class="plugin-setting-header"><label>' + label +
                '</label><span class="plugin-setting-type">' +
                escapeHtml(typeLabel) + "</span></div>" +
                (field.description
                    ? '<p class="plugin-setting-description">' +
                        escapeHtml(field.description) + "</p>"
                    : (field.minimum != null || field.maximum != null)
                        ? '<p class="plugin-setting-description">' +
                            (field.minimum != null && field.maximum != null
                                ? "允许范围：" + escapeHtml(field.minimum) + "–" +
                                    escapeHtml(field.maximum)
                                : field.minimum != null
                                    ? "最小值：" + escapeHtml(field.minimum)
                                    : "最大值：" + escapeHtml(field.maximum)) +
                            "</p>"
                    : "") +
                control + "</div>";
        }).join("");
    }

    function collectSettings() {
        var result = {};
        document.querySelectorAll("#plugin-settings-form [data-setting]").forEach(function (control) {
            var name = control.getAttribute("data-setting");
            var type = control.getAttribute("data-type");
            if (type === "boolean") result[name] = control.checked;
            else if (type === "integer") {
                if (control.value !== "") result[name] = parseInt(control.value, 10);
            } else if (type === "number") {
                if (control.value !== "") result[name] = parseFloat(control.value);
            } else if (type === "json") {
                try {
                    result[name] = JSON.parse(control.value || "null");
                } catch (error) {
                    throw new Error(name + " 的 JSON 格式错误：" + error.message);
                }
            } else if (type === "tenant-list") {
                result[name] = Array.prototype.slice.call(control.selectedOptions)
                    .map(function (option) { return option.value; });
            } else if (control.value !== "") result[name] = control.value;
        });
        return result;
    }

    function openDetail(plugin) {
        editing = plugin;
        modal.style.display = "";
        document.getElementById("plugin-modal-icon").textContent = plugin.icon;
        document.getElementById("plugin-modal-icon").style.background =
            pluginColor(plugin.color);
        document.getElementById("plugin-modal-title").textContent = plugin.name;
        document.getElementById("plugin-modal-subtitle").textContent =
            plugin.id + " · v" + plugin.version;
        document.getElementById("plugin-enabled").checked = plugin.enabled;
        document.getElementById("plugin-status-text").textContent =
            plugin.enabled ? "已配置启用" : "已配置禁用";
        var message = document.getElementById("plugin-runtime-message");
        var details = [];
        if (plugin.restart_required) details.push("配置已变化，需要完整重启");
        if (plugin.missing_dependencies && plugin.missing_dependencies.length) {
            details.push("缺少依赖：" + plugin.missing_dependencies.join("、"));
        }
        if (plugin.load_error) details.push("加载错误：" + plugin.load_error);
        message.textContent = details.join("；");
        message.style.display = details.length ? "" : "none";
        document.getElementById("plugin-tools-table").innerHTML =
            (plugin.tools || []).map(function (tool) {
                return '<div class="tool-def-item"><div class="tool-def-header"><code>' +
                    escapeHtml(tool.name) + '</code><span class="badge ' +
                    (tool.approval_policy === "required" ? "badge-warning" : "badge-muted") +
                    '">' + (tool.approval_policy === "required" ? "强制审批" : "自动") +
                    '</span></div><p class="tool-def-desc">' +
                    escapeHtml(tool.description) + "</p></div>";
            }).join("") || '<p class="text-muted">无工具定义</p>';
        renderSettings(plugin);
        document.getElementById("plugin-settings-json").textContent =
            JSON.stringify(plugin.settings || {}, null, 2);
        document.getElementById("plugin-remove-btn").style.display =
            plugin.source === "external" ? "" : "none";
        document.getElementById("plugin-update-package-btn").style.display =
            plugin.source === "external" ? "" : "none";
    }

    listEl.addEventListener("click", function (event) {
        var tile = event.target.closest(".plugin-tile");
        if (!tile) return;
        var plugin = plugins.find(function (item) {
            return item.id === tile.getAttribute("data-id");
        });
        if (plugin) openDetail(plugin);
    });
    listEl.addEventListener("keydown", function (event) {
        if (event.key !== "Enter" && event.key !== " ") return;
        var tile = event.target.closest(".plugin-tile");
        if (!tile) return;
        event.preventDefault();
        tile.click();
    });

    document.getElementById("plugin-save-btn").addEventListener("click", function () {
        if (!editing) return;
        var settings;
        try {
            settings = collectSettings();
        } catch (error) {
            showToast(error.message, "error");
            return;
        }
        request("/api/plugins/" + encodeURIComponent(editing.id), {
            method: "PUT",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({
                enabled: document.getElementById("plugin-enabled").checked,
                settings: settings
            })
        }).then(function () {
            showToast("配置已保存，完整重启后生效", "success");
            modal.style.display = "none";
            load();
        }).catch(function (error) { showToast("保存失败：" + error.message, "error"); });
    });

    document.getElementById("plugin-remove-btn").addEventListener("click", function () {
        if (!editing) return;
        showConfirm("移除插件包但保留插件数据？请先确保插件已禁用并重启。").then(function (ok) {
            if (!ok) return;
            request("/api/plugins/" + encodeURIComponent(editing.id), {method: "DELETE"})
                .then(function () {
                    showToast("插件已移至可恢复目录，重启后生效", "success");
                    modal.style.display = "none";
                    load();
                })
                .catch(function (error) { showToast("移除失败：" + error.message, "error"); });
        });
    });
    document.getElementById("plugin-update-package-btn").addEventListener("click", function () {
        if (!editing) return;
        packageUpdateId = editing.id;
        document.getElementById("plugin-install-title").textContent = "更新插件包";
        document.getElementById("plugin-source-path").value = "";
        installModal.style.display = "";
    });
    document.getElementById("plugin-clear-data-btn").addEventListener("click", function () {
        if (!editing) return;
        var confirmation = window.prompt(
            "此操作不可恢复。请输入插件 ID “" + editing.id + "”确认清除数据：",
            ""
        );
        if (confirmation === null) return;
        request("/api/plugins/" + encodeURIComponent(editing.id) + "/data", {
            method: "DELETE",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({confirmation: confirmation})
        }).then(function () {
            showToast("插件数据已清除", "success");
            modal.style.display = "none";
        }).catch(function (error) {
            showToast("清除失败：" + error.message, "error");
        });
    });

    function closeModals() {
        modal.style.display = "none";
        installModal.style.display = "none";
    }
    document.getElementById("plugin-modal-close").onclick = closeModals;
    document.getElementById("plugin-modal-cancel").onclick = closeModals;
    document.getElementById("plugin-install-close").onclick = closeModals;
    document.getElementById("plugin-install-cancel").onclick = closeModals;
    document.getElementById("plugin-install-btn").onclick = function () {
        packageUpdateId = null;
        document.getElementById("plugin-install-title").textContent = "安装本地插件";
        document.getElementById("plugin-source-path").value = "";
        installModal.style.display = "";
    };
    document.getElementById("plugin-install-submit").onclick = function () {
        var path = document.getElementById("plugin-source-path").value.trim();
        if (!path) {
            showToast("请输入插件目录", "error");
            return;
        }
        var url = packageUpdateId
            ? "/api/plugins/" + encodeURIComponent(packageUpdateId) + "/package"
            : "/api/plugins/install";
        request(url, {
            method: packageUpdateId ? "PUT" : "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({source_path: path})
        }).then(function () {
            showToast(packageUpdateId
                ? "插件包已更新，重启后生效"
                : "插件已安装，配置并重启后生效", "success");
            packageUpdateId = null;
            installModal.style.display = "none";
            load();
        }).catch(function (error) { showToast("安装失败：" + error.message, "error"); });
    };
    document.getElementById("plugin-search").addEventListener("input", render);
    document.getElementById("plugin-filter-status").addEventListener("change", render);
    document.getElementById("plugin-enabled").addEventListener("change", function () {
        document.getElementById("plugin-status-text").textContent =
            this.checked ? "已配置启用" : "已配置禁用";
    });
    document.getElementById("plugin-settings-form").addEventListener("change", function (event) {
        var input = event.target;
        if (input.getAttribute("data-type") !== "boolean") return;
        var label = input.closest(".switch-label");
        var state = label && label.querySelector(".plugin-setting-state");
        if (state) state.textContent = input.checked ? "已开启" : "已关闭";
    });
    load();
}
