/* Network drive page: tabbed scopes, lazy folder tree, uploads and audit log. */

function initDrive() {
    var state = {
        scope: "public",
        tenantId: "",
        path: "",
        auditOffset: 0,
        auditLimit: 20,
        auditTotal: 0
    };

    var tenantSelect = document.getElementById("drive-tenant");
    var breadcrumbsEl = document.getElementById("drive-breadcrumbs");
    var tableBody = document.getElementById("drive-table-body");
    var fileInput = document.getElementById("drive-file-input");
    var folderInput = document.getElementById("drive-folder-input");
    var filesPanel = document.getElementById("drive-files-panel");
    var auditPanel = document.getElementById("drive-audit-panel");
    var selectAllBox = document.getElementById("drive-select-all");
    var selection = {};
    var currentEntries = [];

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

    function formatBytes(size) {
        if (size < 1024) return size + " B";
        if (size < 1024 * 1024) return (size / 1024).toFixed(1) + " KB";
        if (size < 1024 * 1024 * 1024) return (size / 1024 / 1024).toFixed(1) + " MB";
        return (size / 1024 / 1024 / 1024).toFixed(2) + " GB";
    }

    function formatSize(entry) {
        if (entry.type === "folder") return "—";
        return formatBytes(entry.size);
    }

    function formatTime(seconds) {
        if (!seconds) return "—";
        return new Date(seconds * 1000).toLocaleString("zh-CN", { hour12: false });
    }

    function parentPath(path) {
        var index = path.lastIndexOf("/");
        return index === -1 ? "" : path.slice(0, index);
    }

    /* ---- icons & file-type helpers ---- */

    // Stroke icons matching the sidebar nav style. Static markup only.
    var ICON_SVGS = {
        folder: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>',
        file: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>'
    };

    function createIcon(kind) {
        var wrapper = document.createElement("span");
        wrapper.className = "drive-icon";
        wrapper.innerHTML = ICON_SVGS[kind];
        return wrapper;
    }

    var IMAGE_EXTS = ["png", "jpg", "jpeg", "gif", "webp", "bmp", "svg", "ico"];
    // Text formats that can be edited in place (must stay UTF-8 friendly).
    var TEXT_EXTS = [
        "txt", "md", "markdown", "json", "log", "csv", "yaml", "yml", "xml",
        "ini", "conf", "cfg", "toml", "py", "js", "ts", "css", "html", "htm",
        "sh", "bat", "sql", "env"
    ];
    var EDIT_MAX_BYTES = 256 * 1024;

    function extOf(name) {
        var index = name.lastIndexOf(".");
        return index === -1 ? "" : name.slice(index + 1).toLowerCase();
    }

    function isTextFile(name) {
        return TEXT_EXTS.indexOf(extOf(name)) !== -1;
    }

    function isMarkdownFile(name) {
        var ext = extOf(name);
        return ext === "md" || ext === "markdown";
    }

    function fetchFolders(path) {
        var params = scopeParams();
        params.path = path;
        return fetch("/api/drive/entries" + query(params))
            .then(function (r) { return r.ok ? r.json() : handleError(r); })
            .then(function (listing) {
                return listing.entries.filter(function (entry) { return entry.type === "folder"; });
            });
    }

    /* ---- reusable lazy folder tree ----
       opts: rootLabel, onSelect(path), excludePaths (subtrees hidden, for move picker) */
    function buildTree(container, opts) {
        var nodes = {};
        var selectedPath = null;

        function isExcluded(path) {
            var excluded = opts.excludePaths || [];
            return excluded.some(function (prefix) {
                return path === prefix || path.indexOf(prefix + "/") === 0;
            });
        }

        function setSelected(path) {
            if (selectedPath !== null && nodes[selectedPath]) {
                nodes[selectedPath].rowEl.classList.remove("selected");
            }
            selectedPath = path;
            if (path !== null && nodes[path]) {
                nodes[path].rowEl.classList.add("selected");
            }
        }

        function renderChildren(node, folders) {
            node.childrenEl.innerHTML = "";
            var visible = folders.filter(function (folder) { return !isExcluded(folder.path); });
            if (!visible.length) {
                var empty = document.createElement("div");
                empty.className = "drive-tree-empty";
                empty.textContent = "（无子文件夹）";
                node.childrenEl.appendChild(empty);
                return;
            }
            visible.forEach(function (folder) {
                node.childrenEl.appendChild(createNode(folder.path, folder.name));
            });
        }

        function toggleNode(node) {
            if (node.expanded) {
                node.expanded = false;
                node.childrenEl.style.display = "none";
                node.toggleEl.textContent = "▸";
                return;
            }
            node.expanded = true;
            node.childrenEl.style.display = "";
            node.toggleEl.textContent = "▾";
            if (!node.loaded) reloadNode(node.path);
        }

        function createNode(path, name) {
            var wrapper = document.createElement("div");
            var row = document.createElement("div");
            row.className = "drive-tree-node";
            var toggle = document.createElement("span");
            toggle.className = "drive-tree-toggle";
            toggle.textContent = "▸";
            var label = document.createElement("span");
            label.className = "drive-tree-label";
            if (path === "") {
                label.textContent = name;
            } else {
                label.appendChild(createIcon("folder"));
                var labelText = document.createElement("span");
                labelText.textContent = name;
                label.appendChild(labelText);
            }
            row.appendChild(toggle);
            row.appendChild(label);
            var children = document.createElement("div");
            children.className = "drive-tree-children";
            children.style.display = "none";
            wrapper.appendChild(row);
            wrapper.appendChild(children);

            var node = {
                path: path,
                rowEl: row,
                toggleEl: toggle,
                childrenEl: children,
                loaded: false,
                expanded: false
            };
            nodes[path] = node;
            if (path === selectedPath) row.classList.add("selected");

            toggle.addEventListener("click", function (event) {
                event.stopPropagation();
                toggleNode(node);
            });
            row.addEventListener("click", function () {
                setSelected(path);
                opts.onSelect(path);
                if (!node.expanded) toggleNode(node);
            });
            return wrapper;
        }

        function reloadNode(path) {
            var node = nodes[path];
            if (!node) return Promise.resolve();
            return fetchFolders(path)
                .then(function (folders) {
                    node.loaded = true;
                    renderChildren(node, folders);
                })
                .catch(function (err) { showToast(err.message, "error"); });
        }

        function reset() {
            nodes = {};
            container.innerHTML = "";
            var rootLabel = typeof opts.rootLabel === "function" ? opts.rootLabel() : opts.rootLabel;
            container.appendChild(createNode("", rootLabel));
            var root = nodes[""];
            root.expanded = true;
            root.childrenEl.style.display = "";
            root.toggleEl.textContent = "▾";
            return reloadNode("");
        }

        return {
            reset: reset,
            reloadNode: reloadNode,
            setSelected: setSelected,
            getSelected: function () { return selectedPath; }
        };
    }

    var mainTree = buildTree(document.getElementById("drive-tree"), {
        rootLabel: function () { return state.scope === "public" ? "公共文件区" : "租户文件区"; },
        onSelect: function (path) {
            state.path = path;
            loadEntries();
        }
    });

    /* Refresh tree children for each affected directory after a write op. */
    function refreshTree(paths) {
        var seen = {};
        paths.forEach(function (path) {
            if (seen[path]) return;
            seen[path] = true;
            mainTree.reloadNode(path);
        });
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

    /* Row actions are trimmed down: bulk move/delete live in the batch bar. */
    function entryActions(entry) {
        var cell = document.createElement("td");
        cell.className = "drive-actions";
        if (entry.type === "file") {
            var downloadBtn = document.createElement("button");
            downloadBtn.className = "btn-secondary";
            downloadBtn.textContent = "下载";
            downloadBtn.addEventListener("click", function () { downloadEntry(entry); });
            cell.appendChild(downloadBtn);
        }
        if (hasPermission("drive.manage")) {
            if (entry.type === "file" && isTextFile(entry.name)) {
                var editBtn = document.createElement("button");
                editBtn.className = "btn-secondary";
                editBtn.textContent = "编辑";
                editBtn.addEventListener("click", function () { openEditor(entry); });
                cell.appendChild(editBtn);
            }
            var renameBtn = document.createElement("button");
            renameBtn.className = "btn-secondary";
            renameBtn.textContent = "重命名";
            renameBtn.addEventListener("click", function () { renameEntry(entry); });
            cell.appendChild(renameBtn);
        }
        return cell;
    }

    function renderEntries(listing) {
        renderBreadcrumbs(listing.breadcrumbs);
        currentEntries = listing.entries;
        clearSelection();
        tableBody.innerHTML = "";
        if (!listing.entries.length) {
            var emptyRow = document.createElement("tr");
            var emptyCell = document.createElement("td");
            emptyCell.colSpan = 5;
            emptyCell.textContent = "该目录为空";
            emptyRow.appendChild(emptyCell);
            tableBody.appendChild(emptyRow);
            return;
        }
        listing.entries.forEach(function (entry) {
            var row = document.createElement("tr");

            var checkCell = document.createElement("td");
            var checkbox = document.createElement("input");
            checkbox.type = "checkbox";
            checkbox.className = "drive-row-check";
            checkbox.addEventListener("change", function () {
                if (checkbox.checked) selection[entry.path] = true;
                else delete selection[entry.path];
                updateBatchBar();
            });
            checkCell.appendChild(checkbox);
            row.appendChild(checkCell);

            var nameCell = document.createElement("td");
            var nameSpan = document.createElement("span");
            nameSpan.className = "drive-entry-name " + entry.type;
            nameSpan.appendChild(createIcon(entry.type === "folder" ? "folder" : "file"));
            var nameText = document.createElement("span");
            nameText.textContent = entry.name;
            nameSpan.appendChild(nameText);
            if (entry.type === "folder") {
                nameSpan.addEventListener("click", function () { navigate(entry.path); });
            } else {
                nameSpan.addEventListener("click", function () { openFile(entry); });
            }
            nameCell.appendChild(nameSpan);
            if (entry.knowledge_links && entry.knowledge_links.length) {
                var linked = document.createElement("span");
                var hasIssue = entry.knowledge_links.some(function (item) {
                    return item.status === "stale_modified" ||
                        item.status === "source_missing" || item.status === "failed";
                });
                linked.className = "drive-knowledge-badge" + (hasIssue ? " issue" : "");
                linked.textContent = hasIssue
                    ? "知识索引需更新"
                    : "已入库 " + entry.knowledge_links.length;
                linked.title = entry.knowledge_links.map(function (item) {
                    return item.category_name;
                }).join("、");
                nameCell.appendChild(linked);
            }
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
            .then(function (listing) {
                if (!hasPermission("knowledge.read")) return listing;
                return fetch("/api/knowledge/drive-links" + query(params))
                    .then(function (r) { return r.ok ? r.json() : { links: [] }; })
                    .then(function (data) {
                        var links = {};
                        (data.links || []).forEach(function (item) {
                            if (!links[item.path]) links[item.path] = [];
                            links[item.path].push(item);
                        });
                        (listing.entries || []).forEach(function (entry) {
                            entry.knowledge_links = links[entry.path] || [];
                        });
                        return listing;
                    });
            })
            .then(renderEntries)
            .catch(function (err) { showToast(err.message, "error"); });
    }

    /* Navigate from breadcrumbs/table; sync tree highlight for loaded nodes. */
    function navigate(path) {
        state.path = path;
        mainTree.setSelected(path);
        loadEntries();
    }

    /* ---- selection & batch bar ---- */

    function selectedEntries() {
        return currentEntries.filter(function (entry) { return selection[entry.path]; });
    }

    function updateBatchBar() {
        var picked = selectedEntries();
        var files = picked.filter(function (entry) { return entry.type === "file"; });
        var countEl = document.getElementById("drive-selection-count");
        countEl.textContent = picked.length ? "已选 " + picked.length + " 项" : "";
        document.getElementById("drive-batch-download").disabled = !files.length;
        document.getElementById("drive-batch-knowledge").disabled =
            !files.some(function (entry) {
                return /\.(txt|md|markdown|pdf|docx|xlsx|pptx)$/i.test(entry.name);
            }) || !hasPermission("knowledge.manage");
        document.getElementById("drive-batch-move").disabled = !picked.length;
        document.getElementById("drive-batch-delete").disabled = !picked.length;
        selectAllBox.checked = currentEntries.length > 0 && picked.length === currentEntries.length;
    }

    function clearSelection() {
        selection = {};
        updateBatchBar();
    }

    selectAllBox.addEventListener("change", function () {
        selection = {};
        if (selectAllBox.checked) {
            currentEntries.forEach(function (entry) { selection[entry.path] = true; });
        }
        document.querySelectorAll(".drive-row-check").forEach(function (box) {
            box.checked = selectAllBox.checked;
        });
        updateBatchBar();
    });

    /* Run an async op per item sequentially, collecting failures. */
    function runSequential(items, fn) {
        var failures = [];
        return items.reduce(function (chain, item) {
            return chain.then(function () {
                return fn(item).catch(function (err) {
                    failures.push(item.name + "：" + err.message);
                });
            });
        }, Promise.resolve()).then(function () { return failures; });
    }

    /* ---- shared single-input modal ---- */

    var inputModal = document.getElementById("drive-input-modal");
    var inputForm = document.getElementById("drive-input-form");
    var inputField = document.getElementById("drive-input-value");
    var inputResolve = null;

    function closeInputModal(value) {
        inputModal.style.display = "none";
        document.removeEventListener("keydown", inputEscHandler);
        if (inputResolve) {
            var resolve = inputResolve;
            inputResolve = null;
            resolve(value);
        }
    }

    function inputEscHandler(event) {
        if (event.key === "Escape") closeInputModal(null);
    }

    function openInputModal(title, label, initial) {
        document.getElementById("drive-input-title").textContent = title;
        document.getElementById("drive-input-label").textContent = label;
        inputField.value = initial || "";
        inputModal.style.display = "";
        document.addEventListener("keydown", inputEscHandler);
        inputField.focus();
        inputField.select();
        return new Promise(function (resolve) { inputResolve = resolve; });
    }

    inputForm.addEventListener("submit", function (event) {
        event.preventDefault();
        var value = inputField.value.trim();
        if (!value) return;
        closeInputModal(value);
    });
    document.getElementById("drive-input-cancel").addEventListener("click", function () {
        closeInputModal(null);
    });
    document.getElementById("drive-input-close").addEventListener("click", function () {
        closeInputModal(null);
    });

    /* ---- move target picker modal ---- */

    var moveModal = document.getElementById("drive-move-modal");
    var moveHint = document.getElementById("drive-move-hint");
    var moveResolve = null;
    var moveTree = null;
    var moveTarget = null;

    function closeMoveModal(value) {
        moveModal.style.display = "none";
        document.removeEventListener("keydown", moveEscHandler);
        if (moveResolve) {
            var resolve = moveResolve;
            moveResolve = null;
            resolve(value);
        }
    }

    function moveEscHandler(event) {
        if (event.key === "Escape") closeMoveModal(null);
    }

    function openMoveModal(entries) {
        var title = entries.length === 1
            ? "移动「" + entries[0].name + "」到"
            : "移动 " + entries.length + " 项到";
        document.getElementById("drive-move-title").textContent = title;
        moveTarget = null;
        moveHint.textContent = "请选择目标目录";
        moveTree = buildTree(document.getElementById("drive-move-tree"), {
            rootLabel: state.scope === "public" ? "公共文件区" : "租户文件区",
            excludePaths: entries
                .filter(function (entry) { return entry.type === "folder"; })
                .map(function (entry) { return entry.path; }),
            onSelect: function (path) {
                moveTarget = path;
                moveHint.textContent = "目标目录：" + (path === "" ? "（根目录）" : path);
            }
        });
        moveTree.reset();
        moveModal.style.display = "";
        document.addEventListener("keydown", moveEscHandler);
        return new Promise(function (resolve) { moveResolve = resolve; });
    }

    document.getElementById("drive-move-confirm").addEventListener("click", function () {
        if (moveTarget === null) {
            showToast("请先选择目标目录", "error");
            return;
        }
        closeMoveModal(moveTarget);
    });
    document.getElementById("drive-move-cancel").addEventListener("click", function () {
        closeMoveModal(null);
    });
    document.getElementById("drive-move-close").addEventListener("click", function () {
        closeMoveModal(null);
    });

    /* ---- write operations ---- */

    function createFolder() {
        openInputModal("新建文件夹", "文件夹名称", "").then(function (name) {
            if (!name) return;
            fetch("/api/drive/folders", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    scope: state.scope,
                    tenant_id: state.scope === "tenant" ? state.tenantId : null,
                    path: state.path,
                    name: name
                })
            })
                .then(function (r) { return r.ok ? r.json() : handleError(r); })
                .then(function () {
                    showToast("文件夹创建成功", "success");
                    loadEntries();
                    refreshTree([state.path]);
                })
                .catch(function (err) { showToast(err.message, "error"); });
        });
    }

    function uploadFile(file, overwrite) {
        var form = new FormData();
        form.append("scope", state.scope);
        if (state.scope === "tenant") form.append("tenant_id", state.tenantId);
        form.append("path", state.path);
        form.append("overwrite", overwrite ? "true" : "false");
        form.append("file", file);
        return fetch("/api/drive/upload", { method: "POST", body: form })
            .then(function (r) {
                if (r.ok) return r.json();
                return r.json().catch(function () { return {}; }).then(function (body) {
                    var detail = body.detail || "上传失败（" + r.status + "）";
                    if (!overwrite && detail.indexOf("同名文件已存在") !== -1) {
                        return showConfirm("同名文件已存在，是否覆盖 " + file.name + "？").then(function (yes) {
                            if (yes) return uploadFile(file, true);
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

    /* Upload a batch sequentially; per-file errors are toasted inside uploadFile. */
    function uploadFiles(files) {
        Array.prototype.slice.call(files).reduce(function (chain, file) {
            return chain.then(function () { return uploadFile(file, false); });
        }, Promise.resolve());
    }

    /* ---- folder upload confirmation, directory creation and progress ---- */

    var folderUploadModal = document.getElementById("drive-folder-upload-modal");
    var folderUploadTitle = document.getElementById("drive-folder-upload-title");
    var folderUploadTarget = document.getElementById("drive-folder-upload-target");
    var folderUploadSelection = document.getElementById("drive-folder-upload-selection");
    var folderUploadItems = document.getElementById("drive-folder-upload-items");
    var folderUploadSelectAll = document.getElementById("drive-folder-upload-select-all");
    var folderUploadProgress = document.getElementById("drive-folder-upload-progress");
    var folderUploadProgressBar = document.getElementById("drive-folder-upload-progress-bar");
    var folderUploadProgressText = document.getElementById("drive-folder-upload-progress-text");
    var folderUploadResult = document.getElementById("drive-folder-upload-result");
    var folderUploadClose = document.getElementById("drive-folder-upload-close");
    var folderUploadCancel = document.getElementById("drive-folder-upload-cancel");
    var folderUploadConfirm = document.getElementById("drive-folder-upload-confirm");
    var folderUploadTask = null;
    var folderUploadRunning = false;
    var folderUploadCompleted = false;

    function joinDrivePath(left, right) {
        return [left, right].filter(function (part) { return Boolean(part); }).join("/");
    }

    function normalizeRelativePath(path, fallback) {
        var normalized = String(path || fallback || "").replace(/\\/g, "/");
        return normalized.replace(/^\/+|\/+$/g, "").replace(/\/{2,}/g, "/");
    }

    function uploadDirectoryOf(relativePath) {
        var index = relativePath.lastIndexOf("/");
        return index === -1 ? "" : relativePath.slice(0, index);
    }

    function uploadFilenameOf(relativePath) {
        var index = relativePath.lastIndexOf("/");
        return index === -1 ? relativePath : relativePath.slice(index + 1);
    }

    function selectedFolderUploadItems() {
        if (!folderUploadTask) return [];
        return folderUploadTask.items.filter(function (item) { return item.selected; });
    }

    function updateFolderUploadSelection() {
        if (!folderUploadTask) return;
        var selected = selectedFolderUploadItems();
        var totalBytes = selected.reduce(function (total, item) {
            return total + item.file.size;
        }, 0);
        folderUploadSelection.textContent = "已选 " + selected.length + " / " +
            folderUploadTask.items.length + " 个文件，共 " + formatBytes(totalBytes);
        folderUploadSelectAll.checked = Boolean(folderUploadTask.items.length) &&
            selected.length === folderUploadTask.items.length;
        folderUploadSelectAll.indeterminate = selected.length > 0 &&
            selected.length < folderUploadTask.items.length;
        folderUploadConfirm.disabled = folderUploadRunning || !selected.length;
    }

    function setFolderUploadItemStatus(item, text, kind) {
        item.status = text;
        item.statusKind = kind || "";
        if (!item.statusEl) return;
        item.statusEl.textContent = text;
        item.statusEl.className = "drive-folder-upload-status" +
            (item.statusKind ? " " + item.statusKind : "");
        item.statusEl.title = text;
    }

    function renderFolderUploadItems() {
        folderUploadItems.innerHTML = "";
        if (!folderUploadTask) return;
        folderUploadTask.items.forEach(function (item) {
            var row = document.createElement("div");
            row.className = "drive-folder-upload-row";
            row.setAttribute("role", "row");

            var checkLabel = document.createElement("label");
            checkLabel.className = "drive-folder-upload-check";
            var checkbox = document.createElement("input");
            checkbox.type = "checkbox";
            checkbox.checked = item.selected;
            checkbox.disabled = folderUploadRunning || folderUploadCompleted;
            checkbox.setAttribute("aria-label", "选择 " + item.relativePath);
            checkbox.addEventListener("change", function () {
                item.selected = checkbox.checked;
                setFolderUploadItemStatus(item, item.selected ? "等待" : "未选择", "");
                updateFolderUploadSelection();
            });
            checkLabel.appendChild(checkbox);

            var pathEl = document.createElement("span");
            pathEl.className = "drive-folder-upload-path";
            pathEl.textContent = item.relativePath;
            pathEl.title = item.relativePath;

            var sizeEl = document.createElement("span");
            sizeEl.textContent = formatBytes(item.file.size);

            var statusEl = document.createElement("span");
            statusEl.className = "drive-folder-upload-status" +
                (item.statusKind ? " " + item.statusKind : "");
            statusEl.textContent = item.status;
            statusEl.title = item.status;

            var remove = document.createElement("button");
            remove.type = "button";
            remove.className = "drive-folder-upload-remove";
            remove.textContent = "移除";
            remove.disabled = folderUploadRunning || folderUploadCompleted;
            remove.addEventListener("click", function () {
                folderUploadTask.items = folderUploadTask.items.filter(function (candidate) {
                    return candidate !== item;
                });
                renderFolderUploadItems();
                updateFolderUploadSelection();
            });

            item.statusEl = statusEl;
            row.appendChild(checkLabel);
            row.appendChild(pathEl);
            row.appendChild(sizeEl);
            row.appendChild(statusEl);
            row.appendChild(remove);
            folderUploadItems.appendChild(row);
        });
    }

    function closeFolderUploadModal() {
        if (folderUploadRunning) return;
        folderUploadModal.style.display = "none";
        folderUploadTask = null;
        folderUploadCompleted = false;
    }

    function openFolderUploadModal(candidates) {
        var seen = {};
        var items = [];
        candidates.forEach(function (candidate) {
            var relativePath = normalizeRelativePath(candidate.relativePath, candidate.file.name);
            if (!relativePath || seen[relativePath]) return;
            seen[relativePath] = true;
            items.push({
                file: candidate.file,
                relativePath: relativePath,
                selected: true,
                status: "等待",
                statusKind: "",
                statusEl: null
            });
        });
        if (!items.length) {
            showToast("文件夹中没有可上传的文件", "error");
            return;
        }
        folderUploadTask = {
            scope: state.scope,
            tenantId: state.scope === "tenant" ? state.tenantId : "",
            basePath: state.path,
            items: items
        };
        folderUploadRunning = false;
        folderUploadCompleted = false;
        folderUploadTitle.textContent = "确认上传文件夹";
        folderUploadTarget.textContent = "上传到：" +
            (state.scope === "public" ? "公共文件区" : "租户文件区") +
            (state.path ? " / " + state.path : " / 根目录");
        folderUploadProgress.style.display = "none";
        folderUploadProgressBar.style.width = "0%";
        folderUploadProgressText.textContent = "";
        folderUploadResult.style.display = "none";
        folderUploadResult.textContent = "";
        folderUploadSelectAll.disabled = false;
        folderUploadClose.disabled = false;
        folderUploadCancel.disabled = false;
        folderUploadCancel.style.display = "";
        folderUploadConfirm.disabled = false;
        folderUploadConfirm.textContent = "开始上传";
        renderFolderUploadItems();
        updateFolderUploadSelection();
        folderUploadModal.style.display = "";
    }

    function folderUploadDirectories(items) {
        var directories = {};
        items.forEach(function (item) {
            var directory = uploadDirectoryOf(item.relativePath);
            if (!directory) return;
            var parts = directory.split("/");
            var current = [];
            parts.forEach(function (part) {
                current.push(part);
                directories[current.join("/")] = true;
            });
        });
        return Object.keys(directories).sort(function (left, right) {
            var depth = left.split("/").length - right.split("/").length;
            return depth || left.localeCompare(right, "zh-CN");
        });
    }

    function ensureFolderUploadDirectory(task, relativeDirectory) {
        var fullPath = joinDrivePath(task.basePath, relativeDirectory);
        var parent = parentPath(fullPath);
        var name = uploadFilenameOf(fullPath);
        return fetch("/api/drive/folders", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                scope: task.scope,
                tenant_id: task.scope === "tenant" ? task.tenantId : null,
                path: parent,
                name: name,
                exist_ok: true
            })
        }).then(function (response) {
            return response.ok ? response.json() : handleError(response);
        });
    }

    function folderUploadItemBlocked(item, failedDirectories) {
        var directory = uploadDirectoryOf(item.relativePath);
        var failed = Object.keys(failedDirectories).find(function (candidate) {
            return directory === candidate || directory.indexOf(candidate + "/") === 0;
        });
        return failed ? failedDirectories[failed] : "";
    }

    function uploadFolderItem(task, item, onProgress) {
        return new Promise(function (resolve, reject) {
            var form = new FormData();
            form.append("scope", task.scope);
            if (task.scope === "tenant") form.append("tenant_id", task.tenantId);
            form.append("path", joinDrivePath(task.basePath, uploadDirectoryOf(item.relativePath)));
            form.append("overwrite", "true");
            form.append("file", item.file, uploadFilenameOf(item.relativePath));

            var xhr = new XMLHttpRequest();
            xhr.open("POST", "/api/drive/upload");
            xhr.upload.addEventListener("progress", function (event) {
                if (event.lengthComputable) onProgress(Math.min(event.loaded, item.file.size));
            });
            xhr.addEventListener("load", function () {
                if (xhr.status >= 200 && xhr.status < 300) {
                    resolve();
                    return;
                }
                var detail = "上传失败（" + xhr.status + "）";
                try {
                    detail = JSON.parse(xhr.responseText).detail || detail;
                } catch (error) {
                    // Keep the status-based fallback for non-JSON responses.
                }
                reject(new Error(detail));
            });
            xhr.addEventListener("error", function () {
                reject(new Error("网络错误，上传失败"));
            });
            xhr.send(form);
        });
    }

    function runFolderUpload() {
        if (!folderUploadTask || folderUploadRunning || folderUploadCompleted) return;
        var task = folderUploadTask;
        var selected = selectedFolderUploadItems();
        if (!selected.length) return;
        var directories = folderUploadDirectories(selected);
        var failedDirectories = {};
        var totalBytes = selected.reduce(function (total, item) {
            return total + item.file.size;
        }, 0);
        var settledBytes = 0;
        var completedCount = 0;
        var successCount = 0;
        var failureCount = 0;

        folderUploadRunning = true;
        folderUploadTitle.textContent = "正在上传文件夹";
        folderUploadProgress.style.display = "";
        folderUploadResult.style.display = "none";
        folderUploadSelectAll.disabled = true;
        folderUploadClose.disabled = true;
        folderUploadCancel.disabled = true;
        folderUploadConfirm.disabled = true;
        folderUploadConfirm.textContent = "上传中";
        task.items.forEach(function (item) {
            if (item.selected) setFolderUploadItemStatus(item, "创建目录", "");
            else setFolderUploadItemStatus(item, "未选择", "");
        });
        renderFolderUploadItems();

        function updateProgress(currentLoaded) {
            var loaded = Math.min(totalBytes, settledBytes + (currentLoaded || 0));
            var percent = totalBytes ? Math.round(loaded / totalBytes * 100) : 100;
            folderUploadProgressBar.style.width = percent + "%";
            folderUploadProgressText.textContent = "已处理 " + completedCount + " / " +
                selected.length + " 个文件，" + percent + "%";
        }

        updateProgress(0);
        var directoryChain = directories.reduce(function (chain, directory) {
            return chain.then(function () {
                return ensureFolderUploadDirectory(task, directory).catch(function (error) {
                    failedDirectories[directory] = error.message;
                });
            });
        }, Promise.resolve());

        directoryChain.then(function () {
            selected.forEach(function (item) {
                if (!folderUploadItemBlocked(item, failedDirectories)) {
                    setFolderUploadItemStatus(item, "等待上传", "");
                }
            });
            return selected.reduce(function (chain, item) {
                return chain.then(function () {
                    var blocked = folderUploadItemBlocked(item, failedDirectories);
                    if (blocked) {
                        failureCount += 1;
                        completedCount += 1;
                        settledBytes += item.file.size;
                        setFolderUploadItemStatus(item, "失败：" + blocked, "error");
                        updateProgress(0);
                        return null;
                    }
                    setFolderUploadItemStatus(item, "上传中 0%", "");
                    return uploadFolderItem(task, item, function (loaded) {
                        var filePercent = item.file.size ?
                            Math.round(loaded / item.file.size * 100) : 0;
                        setFolderUploadItemStatus(item, "上传中 " + filePercent + "%", "");
                        updateProgress(loaded);
                    }).then(function () {
                        successCount += 1;
                        setFolderUploadItemStatus(item, "成功", "success");
                    }).catch(function (error) {
                        failureCount += 1;
                        setFolderUploadItemStatus(item, "失败：" + error.message, "error");
                    }).then(function () {
                        completedCount += 1;
                        settledBytes += item.file.size;
                        updateProgress(0);
                    });
                });
            }, Promise.resolve());
        }).then(function () {
            folderUploadRunning = false;
            folderUploadCompleted = true;
            folderUploadTitle.textContent = "上传完成";
            folderUploadProgressBar.style.width = "100%";
            folderUploadProgressText.textContent = "已处理 " + completedCount + " / " +
                selected.length + " 个文件，100%";
            folderUploadResult.style.display = "";
            folderUploadResult.textContent = "上传完成：成功 " + successCount + " 个，失败 " +
                failureCount + " 个，未选择 " + (task.items.length - selected.length) + " 个。";
            folderUploadClose.disabled = false;
            folderUploadCancel.style.display = "none";
            folderUploadConfirm.disabled = false;
            folderUploadConfirm.textContent = "完成";
            renderFolderUploadItems();
            loadEntries();
            refreshTree([task.basePath]);
        }).catch(function (error) {
            folderUploadRunning = false;
            folderUploadCompleted = true;
            folderUploadTitle.textContent = "上传失败";
            folderUploadResult.style.display = "";
            folderUploadResult.textContent = "上传任务异常：" + error.message;
            folderUploadClose.disabled = false;
            folderUploadCancel.style.display = "none";
            folderUploadConfirm.disabled = false;
            folderUploadConfirm.textContent = "完成";
            renderFolderUploadItems();
        });
    }

    function readAllDirectoryEntries(reader) {
        var entries = [];
        return new Promise(function (resolve, reject) {
            function readNext() {
                reader.readEntries(function (batch) {
                    if (!batch.length) {
                        resolve(entries);
                        return;
                    }
                    entries = entries.concat(Array.prototype.slice.call(batch));
                    readNext();
                }, reject);
            }
            readNext();
        });
    }

    function walkDroppedEntry(entry, parent) {
        var relativePath = joinDrivePath(parent, entry.name);
        if (entry.isFile) {
            return new Promise(function (resolve, reject) {
                entry.file(function (file) {
                    resolve([{ file: file, relativePath: relativePath }]);
                }, reject);
            });
        }
        if (!entry.isDirectory) return Promise.resolve([]);
        return readAllDirectoryEntries(entry.createReader()).then(function (children) {
            return Promise.all(children.map(function (child) {
                return walkDroppedEntry(child, relativePath);
            }));
        }).then(function (groups) {
            return groups.reduce(function (all, group) { return all.concat(group); }, []);
        });
    }

    function collectDroppedItems(dataTransfer) {
        var transferItems = dataTransfer.items ? Array.prototype.slice.call(dataTransfer.items) : [];
        var entryItems = transferItems.map(function (item) {
            return item.webkitGetAsEntry ? item.webkitGetAsEntry() : null;
        }).filter(function (entry) { return Boolean(entry); });
        var hasDirectory = entryItems.some(function (entry) { return entry.isDirectory; });
        if (entryItems.length) {
            return Promise.all(entryItems.map(function (entry) {
                return walkDroppedEntry(entry, "");
            })).then(function (groups) {
                return {
                    hasDirectory: hasDirectory,
                    items: groups.reduce(function (all, group) { return all.concat(group); }, [])
                };
            });
        }
        return Promise.resolve({
            hasDirectory: false,
            items: Array.prototype.slice.call(dataTransfer.files || []).map(function (file) {
                return { file: file, relativePath: file.name };
            })
        });
    }

    /* ---- text editor (create / edit) ---- */

    var editorModal = document.getElementById("drive-editor-modal");
    var editorTitle = document.getElementById("drive-editor-title");
    var editorNameGroup = document.getElementById("drive-editor-name-group");
    var editorNameInput = document.getElementById("drive-editor-name");
    var editorContent = document.getElementById("drive-editor-content");
    var editorEntry = null; // null means create mode

    function saveTextFile(dir, filename, content, overwrite) {
        var form = new FormData();
        form.append("scope", state.scope);
        if (state.scope === "tenant") form.append("tenant_id", state.tenantId);
        form.append("path", dir);
        form.append("overwrite", overwrite ? "true" : "false");
        form.append("file", new File([content], filename, { type: "text/plain" }));
        return fetch("/api/drive/upload", { method: "POST", body: form })
            .then(function (r) { return r.ok ? r.json() : handleError(r); });
    }

    function openEditor(entry) {
        var params = scopeParams();
        params.path = entry.path;
        params.max_bytes = EDIT_MAX_BYTES;
        fetch("/api/drive/preview" + query(params))
            .then(function (r) { return r.ok ? r.json() : handleError(r); })
            .then(function (result) {
                if (result.truncated) {
                    showToast("文件超过 256KB，不支持在线编辑", "error");
                    return;
                }
                editorEntry = entry;
                editorTitle.textContent = "编辑：" + entry.name;
                editorNameGroup.style.display = "none";
                editorContent.value = result.content;
                editorModal.style.display = "";
                editorContent.focus();
            })
            .catch(function (err) { showToast(err.message, "error"); });
    }

    function openNewFile() {
        editorEntry = null;
        editorTitle.textContent = "新建文件";
        editorNameGroup.style.display = "";
        editorNameInput.value = "";
        editorContent.value = "";
        editorModal.style.display = "";
        editorNameInput.focus();
    }

    function closeEditor() {
        editorModal.style.display = "none";
    }

    function saveEditor(overwrite) {
        var dir;
        var filename;
        if (editorEntry) {
            dir = parentPath(editorEntry.path);
            filename = editorEntry.name;
            overwrite = true;
        } else {
            dir = state.path;
            filename = editorNameInput.value.trim();
            if (!filename) {
                showToast("请填写文件名", "error");
                return;
            }
        }
        saveTextFile(dir, filename, editorContent.value, Boolean(overwrite))
            .then(function (result) {
                showToast("保存成功：" + result.path, "success");
                closeEditor();
                loadEntries();
            })
            .catch(function (err) {
                if (!editorEntry && err.message.indexOf("同名文件已存在") !== -1) {
                    showConfirm("同名文件已存在，是否覆盖 " + filename + "？").then(function (yes) {
                        if (yes) saveEditor(true);
                    });
                    return;
                }
                showToast(err.message, "error");
            });
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
        openInputModal("重命名", "新名称", entry.name).then(function (name) {
            if (!name || name === entry.name) return;
            entryAction("rename", entry, name)
                .then(function () {
                    showToast("重命名成功", "success");
                    loadEntries();
                    refreshTree([parentPath(entry.path)]);
                })
                .catch(function (err) { showToast(err.message, "error"); });
        });
    }

    function moveSelected() {
        var entries = selectedEntries();
        if (!entries.length) return;
        openMoveModal(entries).then(function (target) {
            if (target === null) return;
            var pending = entries.filter(function (entry) {
                return target !== parentPath(entry.path);
            });
            if (!pending.length) {
                showToast("目标目录与当前位置相同", "error");
                return;
            }
            runSequential(pending, function (entry) {
                return entryAction("move", entry, target);
            }).then(function (failures) {
                if (failures.length) {
                    showToast("部分移动失败：" + failures.join("；"), "error");
                } else {
                    showToast("已移动 " + pending.length + " 项", "success");
                }
                loadEntries();
                refreshTree([state.path, target]);
            });
        });
    }

    function deleteSelected() {
        var entries = selectedEntries();
        if (!entries.length) return;
        var hasFolder = entries.some(function (entry) { return entry.type === "folder"; });
        var message = "确定删除选中的 " + entries.length + " 项吗？" +
            (hasFolder ? "文件夹将递归删除其全部内容。" : "");
        showConfirm(message).then(function (yes) {
            if (!yes) return;
            runSequential(entries, function (entry) {
                var params = scopeParams();
                params.path = entry.path;
                if (entry.type === "folder") params.recursive = "true";
                return fetch("/api/drive/entries" + query(params), { method: "DELETE" })
                    .then(function (r) { return r.ok ? r.json() : handleError(r); });
            }).then(function (failures) {
                if (failures.length) {
                    showToast("部分删除失败：" + failures.join("；"), "error");
                } else {
                    showToast("已删除 " + entries.length + " 项", "success");
                }
                loadEntries();
                refreshTree([state.path]);
            });
        });
    }

    function downloadEntry(entry) {
        var params = scopeParams();
        params.path = entry.path;
        var anchor = document.createElement("a");
        anchor.href = "/api/drive/download" + query(params);
        anchor.download = entry.name;
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
    }

    function downloadSelected() {
        var files = selectedEntries().filter(function (entry) { return entry.type === "file"; });
        if (!files.length) {
            showToast("请先勾选要下载的文件（文件夹不支持打包下载）", "error");
            return;
        }
        // Stagger downloads slightly so the browser accepts every one.
        files.forEach(function (entry, index) {
            setTimeout(function () { downloadEntry(entry); }, index * 400);
        });
        showToast("开始下载 " + files.length + " 个文件", "success");
    }

    /* ---- preview / open in place ---- */

    var previewModal = document.getElementById("drive-preview-modal");
    var previewContent = document.getElementById("drive-preview-content");
    var previewMarkdown = document.getElementById("drive-preview-markdown");
    var previewImage = document.getElementById("drive-preview-image");

    function showPreviewModal(title) {
        document.getElementById("drive-preview-title").textContent = title;
        previewModal.style.display = "";
    }

    function closePreviewModal() {
        previewModal.style.display = "none";
        previewImage.removeAttribute("src");
        previewMarkdown.innerHTML = "";
    }

    function showPreviewPane(pane) {
        previewContent.style.display = pane === "text" ? "" : "none";
        previewMarkdown.style.display = pane === "markdown" ? "" : "none";
        previewImage.style.display = pane === "image" ? "" : "none";
    }

    /* Sanitized markdown rendering, same pattern as chat.js. */
    function renderMarkdown(target, source) {
        if (!window.marked || typeof window.marked.parse !== "function" ||
                !window.DOMPurify || typeof window.DOMPurify.sanitize !== "function") {
            throw new Error("Markdown 渲染依赖不可用");
        }
        var parsed = window.marked.parse(source, { async: false });
        target.innerHTML = window.DOMPurify.sanitize(parsed, {
            USE_PROFILES: { html: true }
        });
        target.querySelectorAll("a[href]").forEach(function (link) {
            var href = link.getAttribute("href");
            try {
                var url = new URL(href, window.location.href);
                if ((url.protocol === "http:" || url.protocol === "https:") &&
                        url.origin !== window.location.origin) {
                    link.target = "_blank";
                    link.rel = "noopener noreferrer";
                }
            } catch (e) {
                link.removeAttribute("href");
            }
        });
    }

    function previewFile(entry) {
        var params = scopeParams();
        params.path = entry.path;
        return fetch("/api/drive/preview" + query(params))
            .then(function (r) { return r.ok ? r.json() : handleError(r); })
            .then(function (result) {
                var suffix = result.truncated ? "（内容过长，仅显示开头部分）" : "";
                if (isMarkdownFile(entry.name)) {
                    try {
                        renderMarkdown(previewMarkdown, result.content);
                        showPreviewPane("markdown");
                        showPreviewModal(entry.name + suffix);
                        return;
                    } catch (e) {
                        // Fall back to plain text when the renderer is missing.
                    }
                }
                showPreviewPane("text");
                previewContent.textContent = result.content;
                showPreviewModal(entry.name + suffix);
            });
    }

    function previewImageFile(entry) {
        var params = scopeParams();
        params.path = entry.path;
        return fetch("/api/drive/download" + query(params))
            .then(function (r) { return r.ok ? r.blob() : handleError(r); })
            .then(function (blob) {
                // Render via data URL: page CSP allows img-src data: but not blob:.
                return new Promise(function (resolve, reject) {
                    var reader = new FileReader();
                    reader.onload = function () { resolve(reader.result); };
                    reader.onerror = function () { reject(new Error("图片读取失败")); };
                    reader.readAsDataURL(blob);
                });
            })
            .then(function (dataUrl) {
                showPreviewPane("image");
                previewImage.src = dataUrl;
                showPreviewModal(entry.name);
            });
    }

    /* Open a file in place: images inline, pdf in a new tab, text preview,
       anything unreadable falls back to download. */
    function openFile(entry) {
        var ext = extOf(entry.name);
        if (IMAGE_EXTS.indexOf(ext) !== -1) {
            previewImageFile(entry).catch(function (err) { showToast(err.message, "error"); });
            return;
        }
        if (ext === "pdf") {
            // Open the tab synchronously so popup blockers allow it.
            var win = window.open("", "_blank");
            var params = scopeParams();
            params.path = entry.path;
            fetch("/api/drive/download" + query(params))
                .then(function (r) { return r.ok ? r.blob() : handleError(r); })
                .then(function (blob) {
                    win.location = URL.createObjectURL(
                        new Blob([blob], { type: "application/pdf" }));
                })
                .catch(function (err) {
                    if (win) win.close();
                    showToast(err.message, "error");
                });
            return;
        }
        previewFile(entry).catch(function () {
            showConfirm("该文件无法在线预览，是否下载 " + entry.name + "？").then(function (yes) {
                if (yes) downloadEntry(entry);
            });
        });
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

    /* ---- tabs ---- */

    function switchScope(scope) {
        state.scope = scope;
        state.path = "";
        tenantSelect.style.display = scope === "tenant" ? "" : "none";
        mainTree.reset().then(function () { mainTree.setSelected(""); });
        loadEntries();
    }

    function activateTab(tab) {
        document.querySelectorAll(".drive-tabs .tab-btn").forEach(function (item) {
            item.classList.toggle("active", item === tab);
        });
        var target = tab.getAttribute("data-drive-tab");
        if (target === "audit") {
            filesPanel.style.display = "none";
            auditPanel.style.display = "";
            state.auditOffset = 0;
            loadAudit();
            return;
        }
        filesPanel.style.display = "";
        auditPanel.style.display = "none";
        if (target === "tenant" && !state.tenantId) {
            showToast("暂无可用租户", "error");
        }
        switchScope(target);
    }

    document.querySelectorAll(".drive-tabs .tab-btn").forEach(function (tab) {
        tab.addEventListener("click", function () { activateTab(tab); });
    });

    /* ---- wiring ---- */

    tenantSelect.addEventListener("change", function () {
        state.tenantId = tenantSelect.value;
        state.path = "";
        mainTree.reset().then(function () { mainTree.setSelected(""); });
        loadEntries();
    });
    document.getElementById("drive-mkdir-btn").addEventListener("click", createFolder);
    document.getElementById("drive-newfile-btn").addEventListener("click", openNewFile);
    document.getElementById("drive-upload-btn").addEventListener("click", function () {
        fileInput.click();
    });
    document.getElementById("drive-upload-folder-btn").addEventListener("click", function () {
        folderInput.click();
    });
    fileInput.addEventListener("change", function () {
        if (fileInput.files.length) uploadFiles(fileInput.files);
        fileInput.value = "";
    });
    folderInput.addEventListener("change", function () {
        if (folderInput.files.length) {
            openFolderUploadModal(Array.prototype.slice.call(folderInput.files).map(function (file) {
                return { file: file, relativePath: file.webkitRelativePath || file.name };
            }));
        }
        folderInput.value = "";
    });
    folderUploadSelectAll.addEventListener("change", function () {
        if (!folderUploadTask || folderUploadRunning || folderUploadCompleted) return;
        folderUploadTask.items.forEach(function (item) {
            item.selected = folderUploadSelectAll.checked;
            setFolderUploadItemStatus(item, item.selected ? "等待" : "未选择", "");
        });
        renderFolderUploadItems();
        updateFolderUploadSelection();
    });
    folderUploadClose.addEventListener("click", closeFolderUploadModal);
    folderUploadCancel.addEventListener("click", closeFolderUploadModal);
    folderUploadConfirm.addEventListener("click", function () {
        if (folderUploadCompleted) closeFolderUploadModal();
        else runFolderUpload();
    });
    folderUploadModal.addEventListener("keydown", function (event) {
        if (event.key === "Escape") closeFolderUploadModal();
    });
    document.getElementById("drive-editor-close").addEventListener("click", closeEditor);
    document.getElementById("drive-editor-cancel").addEventListener("click", closeEditor);
    document.getElementById("drive-editor-save").addEventListener("click", function () {
        saveEditor(false);
    });
    editorModal.addEventListener("keydown", function (event) {
        if (event.key === "Escape") closeEditor();
    });

    /* ---- global drag & drop upload ---- */

    var dropOverlay = document.getElementById("drive-drop-overlay");
    var dragDepth = 0;

    function dragHasFiles(event) {
        var types = event.dataTransfer && event.dataTransfer.types;
        return Boolean(types && Array.prototype.indexOf.call(types, "Files") !== -1);
    }

    function dropUploadEnabled() {
        return hasPermission("drive.manage") &&
            filesPanel.style.display !== "none" &&
            !(state.scope === "tenant" && !state.tenantId);
    }

    document.addEventListener("dragenter", function (event) {
        if (!dragHasFiles(event) || !dropUploadEnabled()) return;
        event.preventDefault();
        dragDepth += 1;
        dropOverlay.style.display = "";
    });
    document.addEventListener("dragover", function (event) {
        if (!dragHasFiles(event) || !dropUploadEnabled()) return;
        event.preventDefault();
    });
    document.addEventListener("dragleave", function (event) {
        if (!dragHasFiles(event)) return;
        dragDepth = Math.max(0, dragDepth - 1);
        if (!dragDepth) dropOverlay.style.display = "none";
    });
    document.addEventListener("drop", function (event) {
        dragDepth = 0;
        dropOverlay.style.display = "none";
        if (!dragHasFiles(event) || !dropUploadEnabled()) return;
        event.preventDefault();
        collectDroppedItems(event.dataTransfer).then(function (result) {
            if (!result.items.length) {
                showToast("未读取到文件，请使用“上传文件夹”按钮重试", "error");
                return;
            }
            if (result.hasDirectory) {
                openFolderUploadModal(result.items);
                return;
            }
            uploadFiles(result.items.map(function (item) { return item.file; }));
        }).catch(function () {
            showToast("无法读取拖拽的文件夹，请使用“上传文件夹”按钮重试", "error");
        });
    });
    document.getElementById("drive-preview-close").addEventListener("click", closePreviewModal);
    document.getElementById("drive-batch-download").addEventListener("click", downloadSelected);
    var knowledgeModal = document.getElementById("drive-knowledge-modal");
    function closeKnowledgeModal() { knowledgeModal.style.display = "none"; }
    document.getElementById("drive-batch-knowledge").addEventListener("click", function () {
        var files = selectedEntries().filter(function (entry) {
            return entry.type === "file" &&
                /\.(txt|md|markdown|pdf|docx|xlsx|pptx)$/i.test(entry.name);
        });
        if (!files.length) { showToast("请选择支持的文档文件", "error"); return; }
        var params = { scope: state.scope };
        if (state.scope === "tenant") params.tenant_id = state.tenantId;
        fetch("/api/knowledge/categories" + query(params))
            .then(function (r) { return r.ok ? r.json() : handleError(r); })
            .then(function (data) {
                var categories = (data.categories || []).filter(function (item) {
                    return item.scope === state.scope &&
                        (state.scope === "public" || item.tenant_id === state.tenantId);
                });
                if (!categories.length) {
                    showToast("当前范围尚未创建知识库", "error"); return;
                }
                document.getElementById("drive-knowledge-category").innerHTML =
                    categories.map(function (item) {
                        return '<option value="' + escapeHtml(item.category_id) + '">' +
                            escapeHtml(item.name) + "</option>";
                    }).join("");
                document.getElementById("drive-knowledge-summary").textContent =
                    "将处理 " + files.length + " 个文件";
                knowledgeModal.style.display = "";
            }).catch(function (err) { showToast(err.message, "error"); });
    });
    document.getElementById("drive-knowledge-confirm").addEventListener("click", function () {
        var files = selectedEntries().filter(function (entry) {
            return entry.type === "file" &&
                /\.(txt|md|markdown|pdf|docx|xlsx|pptx)$/i.test(entry.name);
        });
        fetch("/api/knowledge/from-drive", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                category_id: document.getElementById("drive-knowledge-category").value,
                scope: state.scope,
                tenant_id: state.scope === "tenant" ? state.tenantId : null,
                paths: files.map(function (entry) { return entry.path; })
            })
        }).then(function (r) { return r.ok ? r.json() : handleError(r); })
            .then(function (data) {
                var failed = (data.items || []).filter(function (item) { return !item.ok; }).length;
                closeKnowledgeModal();
                showToast(failed ? failed + " 个文件处理失败" : "文件已加入知识库",
                    failed ? "error" : "success");
                loadEntries();
            }).catch(function (err) { showToast(err.message, "error"); });
    });
    document.getElementById("drive-knowledge-close").addEventListener("click", closeKnowledgeModal);
    document.getElementById("drive-knowledge-cancel").addEventListener("click", closeKnowledgeModal);
    document.getElementById("drive-batch-move").addEventListener("click", moveSelected);
    document.getElementById("drive-batch-delete").addEventListener("click", deleteSelected);
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
        .then(function () {
            mainTree.reset().then(function () { mainTree.setSelected(""); });
            return loadEntries();
        })
        .catch(function (err) { showToast(err.message || "加载失败", "error"); });
}
