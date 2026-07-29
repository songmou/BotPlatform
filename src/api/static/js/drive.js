/* Network drive page: browse, upload/download, manage entries and audit log. */

function initDrive() {
    var state = {
        scope: "public",
        tenantId: "",
        path: "",
        auditOffset: 0,
        auditLimit: 20,
        auditTotal: 0
    };

    var scopeSelect = document.getElementById("drive-scope");
    var tenantSelect = document.getElementById("drive-tenant");
    var breadcrumbsEl = document.getElementById("drive-breadcrumbs");
    var tableBody = document.getElementById("drive-table-body");
    var fileInput = document.getElementById("drive-file-input");

    function query(params) {
        var pairs = [];
        Object.keys(params).forEach(function (key) {
            if (params[key] !== "" && params[key] !== null && params[key] !== undefined) {
                pairs.push(encodeURIComponent(key) + "=" + encodeURIComponent(params[key]));
            }
        });
        return pairs.length ? "?" + pairs.join("&") : "";
    }

    function scopeParams() {
        return { scope: state.scope, tenant_id: state.scope === "tenant" ? state.tenantId : "" };
    }

    function handleError(response) {
        return response.json().catch(function () { return {}; }).then(function (body) {
            throw new Error(body.detail || "请求失败（" + response.status + "）");
        });
    }

    function formatSize(entry) {
        if (entry.type === "folder") return "—";
        var size = entry.size;
        if (size < 1024) return size + " B";
        if (size < 1024 * 1024) return (size / 1024).toFixed(1) + " KB";
        if (size < 1024 * 1024 * 1024) return (size / 1024 / 1024).toFixed(1) + " MB";
        return (size / 1024 / 1024 / 1024).toFixed(2) + " GB";
    }

    function formatTime(seconds) {
        if (!seconds) return "—";
        return new Date(seconds * 1000).toLocaleString("zh-CN", { hour12: false });
    }

    /* ---- tenants ---- */

    function loadTenants() {
        return fetch("/api/drive/tenants")
            .then(function (r) { return r.ok ? r.json() : handleError(r); })
            .then(function (items) {
                tenantSelect.innerHTML = "";
                items.forEach(function (item) {
                    var option = document.createElement("option");
                    option.value = item.tenant_id;
                    option.textContent = item.user_id + "（" + item.bot_id + "）";
                    tenantSelect.appendChild(option);
                });
                if (items.length) state.tenantId = items[0].tenant_id;
            });
    }

    /* ---- file browsing ---- */

    function renderBreadcrumbs(breadcrumbs) {
        breadcrumbsEl.innerHTML = "";
        var rootLink = document.createElement("a");
        rootLink.textContent = state.scope === "public" ? "公共文件区" : "租户文件区";
        rootLink.addEventListener("click", function () { navigate(""); });
        breadcrumbsEl.appendChild(rootLink);
        breadcrumbs.forEach(function (crumb) {
            var sep = document.createElement("span");
            sep.className = "sep";
            sep.textContent = "/";
            breadcrumbsEl.appendChild(sep);
            var link = document.createElement("a");
            link.textContent = crumb.name;
            link.addEventListener("click", function () { navigate(crumb.path); });
            breadcrumbsEl.appendChild(link);
        });
    }

    function entryActions(entry) {
        var cell = document.createElement("td");
        cell.className = "drive-actions";
        var manage = hasPermission("drive.manage");
        if (entry.type === "file") {
            var downloadBtn = document.createElement("button");
            downloadBtn.className = "btn-secondary";
            downloadBtn.textContent = "下载";
            downloadBtn.addEventListener("click", function () {
                var params = scopeParams();
                params.path = entry.path;
                window.open("/api/drive/download" + query(params), "_blank");
            });
            cell.appendChild(downloadBtn);

            var previewBtn = document.createElement("button");
            previewBtn.className = "btn-secondary";
            previewBtn.textContent = "预览";
            previewBtn.addEventListener("click", function () { previewFile(entry); });
            cell.appendChild(previewBtn);
        }
        if (manage) {
            var renameBtn = document.createElement("button");
            renameBtn.className = "btn-secondary";
            renameBtn.textContent = "重命名";
            renameBtn.addEventListener("click", function () { renameEntry(entry); });
            cell.appendChild(renameBtn);

            var moveBtn = document.createElement("button");
            moveBtn.className = "btn-secondary";
            moveBtn.textContent = "移动";
            moveBtn.addEventListener("click", function () { moveEntry(entry); });
            cell.appendChild(moveBtn);

            var deleteBtn = document.createElement("button");
            deleteBtn.className = "btn-secondary";
            deleteBtn.textContent = "删除";
            deleteBtn.addEventListener("click", function () { deleteEntry(entry); });
            cell.appendChild(deleteBtn);
        }
        return cell;
    }

    function renderEntries(listing) {
        renderBreadcrumbs(listing.breadcrumbs);
        tableBody.innerHTML = "";
        if (!listing.entries.length) {
            var emptyRow = document.createElement("tr");
            var emptyCell = document.createElement("td");
            emptyCell.colSpan = 4;
            emptyCell.textContent = "该目录为空";
            emptyRow.appendChild(emptyCell);
            tableBody.appendChild(emptyRow);
            return;
        }
        listing.entries.forEach(function (entry) {
            var row = document.createElement("tr");
            var nameCell = document.createElement("td");
            var nameSpan = document.createElement("span");
            nameSpan.className = "drive-entry-name " + entry.type;
            nameSpan.textContent = (entry.type === "folder" ? "📁 " : "📄 ") + entry.name;
            if (entry.type === "folder") {
                nameSpan.addEventListener("click", function () { navigate(entry.path); });
            }
            nameCell.appendChild(nameSpan);
            row.appendChild(nameCell);

            var sizeCell = document.createElement("td");
            sizeCell.textContent = formatSize(entry);
            row.appendChild(sizeCell);

            var timeCell = document.createElement("td");
            timeCell.textContent = formatTime(entry.modified_at);
            row.appendChild(timeCell);

            row.appendChild(entryActions(entry));
            tableBody.appendChild(row);
        });
    }

    function loadEntries() {
        var params = scopeParams();
        params.path = state.path;
        return fetch("/api/drive/entries" + query(params))
            .then(function (r) { return r.ok ? r.json() : handleError(r); })
            .then(renderEntries)
            .catch(function (err) { showToast(err.message, "error"); });
    }

    function navigate(path) {
        state.path = path;
        loadEntries();
    }

    /* ---- write operations ---- */

    function createFolder() {
        var name = window.prompt("请输入新文件夹名称");
        if (!name) return;
        fetch("/api/drive/folders", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                scope: state.scope,
                tenant_id: state.scope === "tenant" ? state.tenantId : null,
                path: state.path,
                name: name.trim()
            })
        })
            .then(function (r) { return r.ok ? r.json() : handleError(r); })
            .then(function () {
                showToast("文件夹创建成功", "success");
                loadEntries();
            })
            .catch(function (err) { showToast(err.message, "error"); });
    }

    function uploadFile(file, overwrite) {
        var form = new FormData();
        form.append("scope", state.scope);
        if (state.scope === "tenant") form.append("tenant_id", state.tenantId);
        form.append("path", state.path);
        form.append("overwrite", overwrite ? "true" : "false");
        form.append("file", file);
        fetch("/api/drive/upload", { method: "POST", body: form })
            .then(function (r) {
                if (r.ok) return r.json();
                return r.json().catch(function () { return {}; }).then(function (body) {
                    var detail = body.detail || "上传失败（" + r.status + "）";
                    if (!overwrite && detail.indexOf("同名文件已存在") !== -1) {
                        return showConfirm("同名文件已存在，是否覆盖 " + file.name + "？").then(function (yes) {
                            if (yes) uploadFile(file, true);
                            return null;
                        });
                    }
                    throw new Error(detail);
                });
            })
            .then(function (result) {
                if (result) {
                    showToast("上传成功：" + result.path, "success");
                    loadEntries();
                }
            })
            .catch(function (err) { showToast(err.message, "error"); });
    }

    function entryAction(action, entry, target) {
        return fetch("/api/drive/entries", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                scope: state.scope,
                tenant_id: state.scope === "tenant" ? state.tenantId : null,
                action: action,
                path: entry.path,
                target: target
            })
        }).then(function (r) { return r.ok ? r.json() : handleError(r); });
    }

    function renameEntry(entry) {
        var name = window.prompt("请输入新名称", entry.name);
        if (!name || name === entry.name) return;
        entryAction("rename", entry, name.trim())
            .then(function () {
                showToast("重命名成功", "success");
                loadEntries();
            })
            .catch(function (err) { showToast(err.message, "error"); });
    }

    function moveEntry(entry) {
        var target = window.prompt("请输入目标目录（相对路径，根目录留空）", state.path);
        if (target === null) return;
        entryAction("move", entry, target.trim())
            .then(function () {
                showToast("移动成功", "success");
                loadEntries();
            })
            .catch(function (err) { showToast(err.message, "error"); });
    }

    function deleteEntry(entry) {
        var label = entry.type === "folder" ? "文件夹" : "文件";
        showConfirm("确定删除" + label + " " + entry.name + " 吗？文件夹将递归删除其全部内容。")
            .then(function (yes) {
                if (!yes) return;
                var params = scopeParams();
                params.path = entry.path;
                if (entry.type === "folder") params.recursive = "true";
                fetch("/api/drive/entries" + query(params), { method: "DELETE" })
                    .then(function (r) { return r.ok ? r.json() : handleError(r); })
                    .then(function () {
                        showToast("删除成功", "success");
                        loadEntries();
                    })
                    .catch(function (err) { showToast(err.message, "error"); });
            });
    }

    /* ---- preview ---- */

    function previewFile(entry) {
        var params = scopeParams();
        params.path = entry.path;
        fetch("/api/drive/preview" + query(params))
            .then(function (r) { return r.ok ? r.json() : handleError(r); })
            .then(function (result) {
                document.getElementById("drive-preview-title").textContent = entry.name +
                    (result.truncated ? "（内容过长，仅显示开头部分）" : "");
                document.getElementById("drive-preview-content").textContent = result.content;
                document.getElementById("drive-preview-modal").style.display = "";
            })
            .catch(function (err) { showToast(err.message, "error"); });
    }

    /* ---- audit log ---- */

    function loadAudit() {
        var action = document.getElementById("drive-audit-action").value;
        var params = {
            action: action,
            limit: state.auditLimit,
            offset: state.auditOffset
        };
        fetch("/api/drive/audit" + query(params))
            .then(function (r) { return r.ok ? r.json() : handleError(r); })
            .then(function (result) {
                state.auditTotal = result.total;
                var body = document.getElementById("drive-audit-body");
                body.innerHTML = "";
                if (!result.items.length) {
                    var row = document.createElement("tr");
                    var cell = document.createElement("td");
                    cell.colSpan = 7;
                    cell.textContent = "暂无操作日志";
                    row.appendChild(cell);
                    body.appendChild(row);
                }
                result.items.forEach(function (item) {
                    var row = document.createElement("tr");
                    [
                        new Date(item.ts).toLocaleString("zh-CN", { hour12: false }),
                        item.operator,
                        item.source === "web" ? "管理面板" : "智能体",
                        item.scope === "public" ? "公共区" : "租户区",
                        item.action,
                        item.path + (item.target_path ? " → " + item.target_path : ""),
                        item.status + (item.error ? "：" + item.error : "")
                    ].forEach(function (text) {
                        var cell = document.createElement("td");
                        cell.textContent = text;
                        row.appendChild(cell);
                    });
                    body.appendChild(row);
                });
                var page = Math.floor(state.auditOffset / state.auditLimit) + 1;
                var pages = Math.max(1, Math.ceil(state.auditTotal / state.auditLimit));
                document.getElementById("drive-audit-page").textContent =
                    "第 " + page + " / " + pages + " 页，共 " + state.auditTotal + " 条";
            })
            .catch(function (err) { showToast(err.message, "error"); });
    }

    /* ---- wiring ---- */

    scopeSelect.addEventListener("change", function () {
        state.scope = scopeSelect.value;
        state.path = "";
        tenantSelect.style.display = state.scope === "tenant" ? "" : "none";
        loadEntries();
    });
    tenantSelect.addEventListener("change", function () {
        state.tenantId = tenantSelect.value;
        state.path = "";
        loadEntries();
    });
    document.getElementById("drive-mkdir-btn").addEventListener("click", createFolder);
    document.getElementById("drive-upload-btn").addEventListener("click", function () {
        fileInput.click();
    });
    fileInput.addEventListener("change", function () {
        if (fileInput.files.length) uploadFile(fileInput.files[0], false);
        fileInput.value = "";
    });
    document.getElementById("drive-preview-close").addEventListener("click", function () {
        document.getElementById("drive-preview-modal").style.display = "none";
    });
    document.querySelectorAll(".drive-tab").forEach(function (tab) {
        tab.addEventListener("click", function () {
            document.querySelectorAll(".drive-tab").forEach(function (item) {
                item.classList.toggle("active", item === tab);
            });
            var showFiles = tab.getAttribute("data-tab") === "files";
            document.getElementById("drive-files-panel").style.display = showFiles ? "" : "none";
            document.getElementById("drive-audit-panel").style.display = showFiles ? "none" : "";
            if (!showFiles) loadAudit();
        });
    });
    document.getElementById("drive-audit-refresh").addEventListener("click", function () {
        state.auditOffset = 0;
        loadAudit();
    });
    document.getElementById("drive-audit-action").addEventListener("change", function () {
        state.auditOffset = 0;
        loadAudit();
    });
    document.getElementById("drive-audit-prev").addEventListener("click", function () {
        if (state.auditOffset >= state.auditLimit) {
            state.auditOffset -= state.auditLimit;
            loadAudit();
        }
    });
    document.getElementById("drive-audit-next").addEventListener("click", function () {
        if (state.auditOffset + state.auditLimit < state.auditTotal) {
            state.auditOffset += state.auditLimit;
            loadAudit();
        }
    });

    loadMe()
        .then(function () {
            if (!hasPermission("drive.manage")) {
                document.querySelectorAll("[data-manage='1']").forEach(function (el) {
                    el.style.display = "none";
                });
            }
            return loadTenants();
        })
        .then(loadEntries)
        .catch(function (err) { showToast(err.message || "加载失败", "error"); });
}
