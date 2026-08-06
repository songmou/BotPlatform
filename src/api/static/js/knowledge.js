/* Scoped knowledge library management page. */

function initKnowledge() {
    var page = document.getElementById("knowledge-page");
    var resourceMode = page ? page.getAttribute("data-resource-mode") : "platform-public";
    var organizationMode = resourceMode === "organization";
    var state = {
        tenants: [], categories: [], sources: [], selected: {}, drivePath: "",
        driveSelected: {}, editingCategory: null, embeddingEnabled: true
    };
    var scopeEl = document.getElementById("knowledge-scope");
    var tenantEl = document.getElementById("knowledge-tenant");
    var categoryEl = document.getElementById("knowledge-category");
    var tableBody = document.getElementById("knowledge-table-body");
    var statusEl = document.getElementById("knowledge-status");
    var searchResults = document.getElementById("knowledge-search-results");
    var libraryPanel = document.getElementById("knowledge-library-panel");
    var embeddingPanel = document.getElementById("knowledge-embedding-panel");

    function knowledgeUrl(path) {
        return organizationMode
            ? organizationApi("/knowledge" + path)
            : "/api/v2/platform/knowledge" + path;
    }
    function driveUrl(path, scope) {
        if (!organizationMode) return "/api/v2/platform/drive" + path;
        var separator = path.indexOf("?") === -1 ? "?" : "&";
        return organizationApi("/drive" + path) + separator +
            "scope=" + encodeURIComponent(scope === "public" ? "public" : "organization");
    }
    function readOnlyScope() {
        return organizationMode && scopeEl.value === "public";
    }
    function updateWritableState() {
        var disabled = readOnlyScope();
        [
            "knowledge-category-add", "knowledge-category-edit", "knowledge-category-delete",
            "knowledge-add-text-btn", "knowledge-from-drive-btn", "knowledge-upload-btn",
            "knowledge-refresh-selected", "knowledge-move-selected", "knowledge-move-target",
            "knowledge-delete-selected", "knowledge-reindex-btn", "knowledge-rebuild-btn",
            "knowledge-select-all"
        ].forEach(function (id) {
            var element = document.getElementById(id);
            if (element) element.disabled = disabled;
        });
    }

    function apiError(response) {
        return response.json().catch(function () { return {}; }).then(function (data) {
            throw new Error(data.detail || "请求失败（" + response.status + "）");
        });
    }
    function jsonFetch(url, options) {
        return fetch(url, options).then(function (r) { return r.ok ? r.json() : apiError(r); });
    }
    function query(params) {
        return "?" + Object.keys(params).filter(function (key) {
            return params[key] !== null && params[key] !== undefined && params[key] !== "";
        }).map(function (key) {
            return encodeURIComponent(key) + "=" + encodeURIComponent(params[key]);
        }).join("&");
    }
    function setStatus(message) {
        statusEl.style.display = message ? "" : "none";
        statusEl.textContent = message || "";
    }
    function currentTenant() {
        return organizationMode ? selectedOrganizationId() : (tenantEl.value || "");
    }
    function currentCategoryId() { return categoryEl.value || ""; }
    function currentCategory() {
        return state.categories.find(function (item) {
            return item.category_id === currentCategoryId();
        }) || null;
    }
    function currentOwner() {
        var category = currentCategory();
        return {
            scope: category ? category.scope : scopeEl.value,
            tenant_id: category && category.scope === "tenant" ? category.tenant_id : null
        };
    }
    function statusLabel(status) {
        return {
            ready: "已就绪", pending_embedding: "等待向量化",
            stale_modified: "源文件已修改", source_missing: "源文件已删除",
            failed: "处理失败"
        }[status] || status;
    }
    function supportedFile(name) {
        return /\.(txt|md|markdown|pdf|docx|xlsx|pptx)$/i.test(name || "");
    }

    function activateTab(target) {
        document.querySelectorAll(".knowledge-tabs .tab-btn").forEach(function (tab) {
            var active = tab.getAttribute("data-knowledge-tab") === target;
            tab.classList.toggle("active", active);
            tab.setAttribute("aria-selected", active ? "true" : "false");
        });
        if (target === "embedding") {
            libraryPanel.style.display = "none";
            embeddingPanel.style.display = "";
            loadEmbeddingConfig();
            return;
        }
        libraryPanel.style.display = "";
        embeddingPanel.style.display = "none";
        scopeEl.value = target;
        updateWritableState();
        loadCategories();
    }

    document.querySelectorAll(".knowledge-tabs .tab-btn").forEach(function (tab) {
        tab.addEventListener("click", function () {
            activateTab(tab.getAttribute("data-knowledge-tab"));
        });
    });

    function loadTenants() {
        state.tenants = organizationMode
            ? [{ tenant_id: selectedOrganizationId() }]
            : [];
        tenantEl.innerHTML = organizationMode
            ? '<option value="' + escapeHtml(selectedOrganizationId()) + '">当前组织</option>'
            : '<option value="">公共范围</option>';
        tenantEl.style.display = "none";
        updateWritableState();
        return loadCategories();
    }

    function updateEmbeddingNotice() {
        var notice = document.getElementById("knowledge-embedding-notice");
        var reindexBtn = document.getElementById("knowledge-reindex-btn");
        var rebuildBtn = document.getElementById("knowledge-rebuild-btn");
        notice.style.display = state.embeddingEnabled ? "none" : "";
        reindexBtn.disabled = !state.embeddingEnabled || readOnlyScope();
        reindexBtn.title = state.embeddingEnabled
            ? "" : "向量化服务未启用，无法补齐向量";
        rebuildBtn.disabled = !state.embeddingEnabled || readOnlyScope();
        rebuildBtn.title = state.embeddingEnabled
            ? "用当前向量模型覆盖该库全部向量" : "向量化服务未启用，无法重建向量";
    }

    function loadEmbeddingHealth() {
        var banner = document.getElementById("knowledge-health-banner");
        if (!currentCategoryId() || readOnlyScope()) {
            banner.style.display = "none";
            return Promise.resolve();
        }
        return jsonFetch(knowledgeUrl("/embedding-health") +
            "?category_id=" + encodeURIComponent(currentCategoryId())).then(function (data) {
            if (data.stale && data.stale > 0) {
                banner.style.display = "";
                banner.innerHTML = "检测到向量模型已变更，当前知识库有 <strong>" + data.stale +
                    "</strong> 个分块需重新向量化。" +
                    '<button id="knowledge-health-rebuild" class="btn-secondary">一键强制重建</button>';
                var btn = document.getElementById("knowledge-health-rebuild");
                if (btn) btn.addEventListener("click", rebuildCurrentLibrary);
            } else {
                banner.style.display = "none";
            }
        }).catch(function () { banner.style.display = "none"; });
    }

    function rebuildCurrentLibrary() {
        if (!state.embeddingEnabled) {
            showToast("向量模型未配置，请先在“模型管理 → 角色绑定”启用向量模型并完整重启服务", "error");
            return;
        }
        jsonFetch(knowledgeUrl("/reindex"), {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ category_ids: [currentCategoryId()], force: true })
        }).then(function (result) {
            showToast("已强制重建 " + result.completed + " 个向量" +
                (result.failed ? "，" + result.failed + " 个失败" : ""), result.failed ? "error" : "success");
            loadSources();
        }).catch(function (err) { showToast(err.message, "error"); });
    }

    function loadCategories(preferred) {
        var scope = scopeEl.value;
        tenantEl.style.display = "none";
        if (scope === "tenant" && !state.tenants.length) {
            state.categories = [];
            categoryEl.innerHTML = '<option value="">暂无租户</option>';
            rebuildMoveTargets();
            setStatus("暂无租户，请先接入机器人用户后再管理私有知识库");
            return loadSources();
        }
        var url = knowledgeUrl("/categories");
        return jsonFetch(url).then(function (data) {
            state.embeddingEnabled = data.embedding_enabled !== false;
            updateEmbeddingNotice();
            state.categories = (data.categories || data.items || []).filter(function (item) {
                return item.scope === scope &&
                    (scope === "public" || item.tenant_id === currentTenant());
            });
            categoryEl.innerHTML = state.categories.length
                ? state.categories.map(function (item) {
                    return '<option value="' + escapeHtml(item.category_id) + '">' +
                        escapeHtml(item.name) + "（" + Number(item.source_count || 0) + "）</option>";
                }).join("")
                : '<option value="">暂无知识库</option>';
            if (preferred && state.categories.some(function (item) {
                return item.category_id === preferred;
            })) categoryEl.value = preferred;
            rebuildMoveTargets();
            updateWritableState();
            return loadSources();
        }).catch(function (err) { setStatus(err.message); });
    }

    function rebuildMoveTargets() {
        var current = currentCategoryId();
        document.getElementById("knowledge-move-target").innerHTML =
            '<option value="">移动到…</option>' + state.categories.filter(function (item) {
                return item.category_id !== current;
            }).map(function (item) {
                return '<option value="' + escapeHtml(item.category_id) + '">' +
                    escapeHtml(item.name) + "</option>";
            }).join("");
    }

    function loadSources() {
        state.selected = {};
        updateBatchButtons();
        searchResults.innerHTML = "";
        if (!currentCategoryId()) {
            state.sources = [];
            renderSources();
            return Promise.resolve();
        }
        return jsonFetch(knowledgeUrl("/sources?category_id=") +
            encodeURIComponent(currentCategoryId())).then(function (data) {
            state.sources = data.sources || data.items || [];
            setStatus("");
            renderSources();
            loadEmbeddingHealth();
        }).catch(function (err) { setStatus(err.message); });
    }

    function renderSources() {
        var filter = document.getElementById("knowledge-status-filter").value;
        var sources = state.sources.filter(function (item) {
            return !filter || item.status === filter;
        });
        if (!sources.length) {
            tableBody.innerHTML = '<tr><td colspan="7" class="empty-cell">当前知识库暂无符合条件的来源</td></tr>';
            return;
        }
        tableBody.innerHTML = sources.map(function (source) {
            var sourceLabel = source.source_type === "file" ? "网盘文件" : "录入文本";
            var sourceCell = '<div class="knowledge-source-cell"><span class="knowledge-source-badge">' +
                sourceLabel + "</span>";
            if (source.drive_path) {
                var href = driveUrl(
                    "/download" + query({ path: source.drive_path }),
                    source.drive_scope
                );
                sourceCell += '<a class="knowledge-source-link" href="' + escapeHtml(href) +
                    '" data-preview-source="' + escapeHtml(source.source_id) +
                    '" title="预览网盘原文件：' + escapeHtml(source.drive_path) + '">' +
                    escapeHtml(source.drive_path) + "</a>";
            }
            sourceCell += "</div>";
            var error = source.last_error ? ' title="' + escapeHtml(source.last_error) + '"' : "";
            return '<tr>' +
                '<td><input type="checkbox" class="knowledge-row-check" data-id="' +
                escapeHtml(source.source_id) + '"' +
                (state.selected[source.source_id] ? " checked" : "") +
                (readOnlyScope() ? " disabled" : "") + '></td>' +
                "<td>" + escapeHtml(source.name) + "</td>" +
                "<td>" + sourceCell + "</td>" +
                '<td><span class="knowledge-status-badge ' + escapeHtml(source.status) + '"' + error + ">" +
                escapeHtml(statusLabel(source.status)) + "</span></td>" +
                "<td>" + Number(source.chunks || 0) + "</td>" +
                "<td>" + escapeHtml((source.updated_at || "").slice(0, 19).replace("T", " ")) + "</td>" +
                '<td>' + (readOnlyScope() ? '<span class="text-muted">只读</span>' :
                '<div class="knowledge-row-actions">' +
                '<button class="btn-secondary btn-small" data-reembed-source="' +
                escapeHtml(source.source_id) + '"' +
                (state.embeddingEnabled ? '' : ' disabled title="向量模型未启用"') +
                '>重新向量化</button>' +
                '<button class="btn-danger btn-small" data-delete-source="' +
                escapeHtml(source.source_id) + '">删除</button>' +
                '</div>') + '</td></tr>';
        }).join("");
    }

    function selectedIds() { return Object.keys(state.selected).filter(function (id) { return state.selected[id]; }); }
    function updateBatchButtons() {
        var count = selectedIds().length;
        var any = count > 0;
        document.getElementById("knowledge-refresh-selected").disabled = !any || readOnlyScope();
        document.getElementById("knowledge-move-selected").disabled = !any || readOnlyScope();
        document.getElementById("knowledge-delete-selected").disabled = !any || readOnlyScope();
        document.getElementById("knowledge-selection-count").textContent =
            count ? "已选 " + count + " 项" : "";
    }

    scopeEl.addEventListener("change", function () { loadCategories(); });
    tenantEl.addEventListener("change", function () { loadCategories(); });
    categoryEl.addEventListener("change", function () {
        rebuildMoveTargets();
        loadSources();
    });
    document.getElementById("knowledge-status-filter").addEventListener("change", renderSources);
    document.getElementById("knowledge-select-all").addEventListener("change", function (evt) {
        state.sources.forEach(function (item) { state.selected[item.source_id] = evt.target.checked; });
        renderSources();
        updateBatchButtons();
    });
    tableBody.addEventListener("change", function (evt) {
        if (!evt.target.classList.contains("knowledge-row-check")) return;
        state.selected[evt.target.getAttribute("data-id")] = evt.target.checked;
        updateBatchButtons();
    });
    tableBody.addEventListener("click", function (evt) {
        var previewLink = evt.target.closest("[data-preview-source]");
        if (previewLink) {
            evt.preventDefault();
            previewSource(previewLink.getAttribute("data-preview-source"));
            return;
        }
        var reembedButton = evt.target.closest("[data-reembed-source]");
        if (reembedButton) {
            if (!state.embeddingEnabled) {
                showToast("向量模型未配置，请先在“模型管理 → 角色绑定”启用向量模型并完整重启服务", "error");
                return;
            }
            var reembedId = reembedButton.getAttribute("data-reembed-source");
            jsonFetch(knowledgeUrl("/reembed"), {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ source_ids: [reembedId] })
            }).then(function (result) {
                if (result.failed) {
                    showToast("重新向量化失败：" + (result.errors[0] || {}).error, "error");
                } else {
                    showToast("已重新向量化 " + result.chunks + " 个分块", "success");
                }
                loadSources();
            }).catch(function (err) { showToast(err.message, "error"); });
            return;
        }
        var button = evt.target.closest("[data-delete-source]");
        if (!button) return;
        var sourceId = button.getAttribute("data-delete-source");
        showConfirm("只删除知识索引和关联，不会删除网盘原文件。确定继续吗？").then(function (ok) {
            if (!ok) return;
            return jsonFetch(knowledgeUrl("/sources/" + encodeURIComponent(sourceId)), { method: "DELETE" })
                .then(function () { showToast("知识来源已删除", "success"); return loadCategories(currentCategoryId()); })
                .catch(function (err) { showToast(err.message, "error"); });
        });
    });

    var categoryModal = document.getElementById("knowledge-category-modal");
    function closeCategoryModal() { categoryModal.style.display = "none"; }
    function openCategoryModal(category) {
        state.editingCategory = category || null;
        document.getElementById("knowledge-category-modal-title").textContent =
            category ? "编辑知识库" : "新建知识库";
        document.getElementById("knowledge-category-name").value = category ? category.name : "";
        document.getElementById("knowledge-category-description").value = category ? category.description : "";
        categoryModal.style.display = "";
    }
    document.getElementById("knowledge-category-add").addEventListener("click", function () { openCategoryModal(null); });
    document.getElementById("knowledge-category-edit").addEventListener("click", function () {
        var category = currentCategory();
        if (!category) { showToast("请先选择知识库", "error"); return; }
        openCategoryModal(category);
    });
    document.getElementById("knowledge-category-delete").addEventListener("click", function () {
        var category = currentCategory();
        if (!category) { showToast("请先选择知识库", "error"); return; }
        showConfirm("确定删除知识库“" + category.name + "”吗？非空知识库不能删除。").then(function (ok) {
            if (!ok) return;
            jsonFetch(knowledgeUrl("/categories/" + encodeURIComponent(category.category_id)), { method: "DELETE" })
                .then(function () { showToast("知识库已删除", "success"); loadCategories(); })
                .catch(function (err) { showToast(err.message, "error"); });
        });
    });
    document.getElementById("knowledge-category-modal-close").addEventListener("click", closeCategoryModal);
    document.getElementById("knowledge-category-cancel").addEventListener("click", closeCategoryModal);
    document.getElementById("knowledge-category-form").addEventListener("submit", function (evt) {
        evt.preventDefault();
        var body = {
            name: document.getElementById("knowledge-category-name").value.trim(),
            description: document.getElementById("knowledge-category-description").value.trim()
        };
        var editing = state.editingCategory;
        var url = knowledgeUrl("/categories");
        var method = "POST";
        if (editing) {
            url += "/" + encodeURIComponent(editing.category_id);
            method = "PUT";
        } else {
            body.scope = scopeEl.value;
            body.tenant_id = scopeEl.value === "tenant" ? currentTenant() : null;
        }
        jsonFetch(url, { method: method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
            .then(function (saved) {
                closeCategoryModal();
                showToast("知识库已保存", "success");
                loadCategories(saved.category_id);
            }).catch(function (err) { showToast(err.message, "error"); });
    });

    var fileInput = document.getElementById("knowledge-file-input");
    document.getElementById("knowledge-upload-btn").addEventListener("click", function () {
        if (!currentCategoryId()) { showToast("请先选择知识库", "error"); return; }
        fileInput.click();
    });
    fileInput.addEventListener("change", function () {
        var file = fileInput.files[0];
        fileInput.value = "";
        if (!file) return;
        var owner = currentOwner();
        var form = new FormData();
        form.append("category_id", currentCategoryId());
        if (owner.tenant_id) form.append("tenant_id", owner.tenant_id);
        form.append("file", file);
        showToast("正在上传、解析并建立索引…", "info");
        fetch(knowledgeUrl("/upload"), { method: "POST", body: form })
            .then(function (r) { return r.ok ? r.json() : apiError(r); })
            .then(function (result) {
                showToast("已处理 " + result.chunks + " 个分块", "success");
                loadCategories(currentCategoryId());
            }).catch(function (err) { showToast(err.message, "error"); });
    });

    var textModal = document.getElementById("knowledge-text-modal");
    function closeTextModal() { textModal.style.display = "none"; }
    document.getElementById("knowledge-add-text-btn").addEventListener("click", function () {
        if (!currentCategoryId()) { showToast("请先选择知识库", "error"); return; }
        document.getElementById("knowledge-text-form").reset();
        textModal.style.display = "";
    });
    document.getElementById("knowledge-text-modal-close").addEventListener("click", closeTextModal);
    document.getElementById("knowledge-text-modal-cancel").addEventListener("click", closeTextModal);
    document.getElementById("knowledge-text-form").addEventListener("submit", function (evt) {
        evt.preventDefault();
        jsonFetch(knowledgeUrl("/text"), {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                category_id: currentCategoryId(),
                tenant_id: currentOwner().tenant_id,
                name: document.getElementById("knowledge-text-name").value.trim(),
                content: document.getElementById("knowledge-text-content").value
            })
        }).then(function () {
            closeTextModal(); showToast("文本知识已保存", "success");
            loadCategories(currentCategoryId());
        }).catch(function (err) { showToast(err.message, "error"); });
    });

    document.getElementById("knowledge-refresh-selected").addEventListener("click", function () {
        jsonFetch(knowledgeUrl("/refresh"), {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ source_ids: selectedIds() })
        }).then(function (data) {
            var failed = (data.items || []).filter(function (item) { return !item.ok; }).length;
            showToast(failed ? failed + " 项刷新失败" : "所选来源已刷新", failed ? "error" : "success");
            loadCategories(currentCategoryId());
        }).catch(function (err) { showToast(err.message, "error"); });
    });
    document.getElementById("knowledge-move-selected").addEventListener("click", function () {
        var target = document.getElementById("knowledge-move-target").value;
        if (!target) { showToast("请选择目标知识库", "error"); return; }
        jsonFetch(knowledgeUrl("/sources/move"), {
            method: "PATCH", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ source_ids: selectedIds(), target_category_id: target })
        }).then(function () {
            showToast("知识来源已移动", "success");
            loadCategories(currentCategoryId());
        }).catch(function (err) { showToast(err.message, "error"); });
    });
    document.getElementById("knowledge-delete-selected").addEventListener("click", function () {
        var sourceIds = selectedIds();
        if (!sourceIds.length) return;
        showConfirm("只删除所选知识索引和关联，不会删除网盘原文件。确定继续吗？")
            .then(function (ok) {
                if (!ok) return null;
                return Promise.all(sourceIds.map(function (sourceId) {
                    return jsonFetch(
                        knowledgeUrl("/sources/" + encodeURIComponent(sourceId)),
                        { method: "DELETE" }
                    );
                }));
            })
            .then(function (result) {
                if (!result) return;
                showToast("所选知识来源已删除", "success");
                return loadCategories(currentCategoryId());
            })
            .catch(function (err) { showToast(err.message, "error"); });
    });
    document.getElementById("knowledge-reindex-btn").addEventListener("click", function () {
        if (!state.embeddingEnabled) {
            showToast("向量模型未配置，请先在“模型管理 → 角色绑定”启用向量模型并完整重启服务", "error");
            return;
        }
        jsonFetch(knowledgeUrl("/reindex"), {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ category_ids: [currentCategoryId()] })
        }).then(function (result) {
            showToast("已补齐 " + result.completed + " 个向量" +
                (result.failed ? "，" + result.failed + " 个失败" : ""), result.failed ? "error" : "success");
            loadSources();
        }).catch(function (err) { showToast(err.message, "error"); });
    });
    document.getElementById("knowledge-rebuild-btn").addEventListener("click", rebuildCurrentLibrary);

    function runSearch() {
        var query = document.getElementById("knowledge-search-input").value.trim();
        if (!query || !currentCategoryId()) {
            showToast("检索测试需要知识库和关键词", "error"); return;
        }
        jsonFetch(knowledgeUrl("/search") + "?q=" + encodeURIComponent(query) + "&category_ids=" +
            encodeURIComponent(currentCategoryId())).then(function (data) {
            var results = data.results || [];
            searchResults.innerHTML = results.length ? results.map(function (item) {
                return '<div class="status-card"><strong>[' + Number(item.citation) + "] " +
                    escapeHtml(item.category_name + " / " + item.source_name) +
                    "</strong><br>" + escapeHtml(item.content.slice(0, 400)) + "</div>";
            }).join("") : '<div class="status-card">没有检索到相关内容</div>';
        }).catch(function (err) { showToast(err.message, "error"); });
    }
    document.getElementById("knowledge-search-btn").addEventListener("click", runSearch);
    document.getElementById("knowledge-search-input").addEventListener("keydown", function (evt) {
        if (evt.key === "Enter") runSearch();
    });

    var driveModal = document.getElementById("knowledge-drive-modal");
    function closeDriveModal() { driveModal.style.display = "none"; }
    function loadDrive(path) {
        var owner = currentOwner();
        state.drivePath = path || "";
        state.driveSelected = {};
        document.getElementById("knowledge-drive-path").textContent =
            state.drivePath || "根目录";
        var url = driveUrl(
            "/entries?path=" + encodeURIComponent(state.drivePath), owner.scope
        );
        jsonFetch(url).then(function (data) {
            var rows = [];
            if (state.drivePath) {
                var parent = state.drivePath.split("/").slice(0, -1).join("/");
                rows.push('<div class="knowledge-drive-row"><button data-drive-folder="' +
                    escapeHtml(parent) + '">← 返回上级</button></div>');
            }
            (data.entries || []).forEach(function (entry) {
                if (entry.type === "folder") {
                    rows.push('<div class="knowledge-drive-row">📁 <button data-drive-folder="' +
                        escapeHtml(entry.path) + '">' + escapeHtml(entry.name) + "</button></div>");
                } else {
                    rows.push('<label class="knowledge-drive-row"><input type="checkbox" data-drive-file="' +
                        escapeHtml(entry.path) + '"' + (supportedFile(entry.name) ? "" : " disabled") + "> " +
                        escapeHtml(entry.name) + (supportedFile(entry.name) ? "" : "（不支持）") + "</label>");
                }
            });
            document.getElementById("knowledge-drive-list").innerHTML =
                rows.join("") || '<div class="empty-cell">目录为空</div>';
        }).catch(function (err) { showToast(err.message, "error"); });
    }
    document.getElementById("knowledge-from-drive-btn").addEventListener("click", function () {
        if (!currentCategoryId()) { showToast("请先选择知识库", "error"); return; }
        driveModal.style.display = "";
        loadDrive("");
    });
    document.getElementById("knowledge-drive-list").addEventListener("click", function (evt) {
        var folder = evt.target.closest("[data-drive-folder]");
        if (folder) loadDrive(folder.getAttribute("data-drive-folder"));
    });
    document.getElementById("knowledge-drive-list").addEventListener("change", function (evt) {
        var path = evt.target.getAttribute("data-drive-file");
        if (path) state.driveSelected[path] = evt.target.checked;
    });
    document.getElementById("knowledge-drive-import").addEventListener("click", function () {
        var paths = Object.keys(state.driveSelected).filter(function (path) { return state.driveSelected[path]; });
        if (!paths.length) { showToast("请选择至少一个支持的文件", "error"); return; }
        var owner = currentOwner();
        jsonFetch(knowledgeUrl("/from-drive"), {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                category_id: currentCategoryId(), scope: owner.scope,
                tenant_id: owner.tenant_id, paths: paths
            })
        }).then(function (data) {
            var failed = (data.items || []).filter(function (item) { return !item.ok; }).length;
            closeDriveModal();
            showToast(failed ? failed + " 个文件处理失败" : "网盘文件已加入知识库", failed ? "error" : "success");
            loadCategories(currentCategoryId());
        }).catch(function (err) { showToast(err.message, "error"); });
    });
    document.getElementById("knowledge-drive-close").addEventListener("click", closeDriveModal);
    document.getElementById("knowledge-drive-cancel").addEventListener("click", closeDriveModal);

    var previewModal = document.getElementById("knowledge-preview-modal");
    function closePreviewModal() {
        previewModal.style.display = "none";
        document.getElementById("knowledge-preview-content").textContent = "";
    }
    function previewSource(sourceId) {
        var source = state.sources.find(function (item) { return item.source_id === sourceId; });
        if (!source) return;
        document.getElementById("knowledge-preview-title").textContent = source.name;
        document.getElementById("knowledge-preview-meta").textContent =
            (source.drive_scope === "public" ? "公共网盘 / " : "私有网盘 / ") +
            (source.drive_path || "");
        document.getElementById("knowledge-preview-content").textContent = "正在读取原文件…";
        previewModal.style.display = "";
        jsonFetch(knowledgeUrl("/sources/" + encodeURIComponent(sourceId)))
            .then(function (data) {
                document.getElementById("knowledge-preview-content").textContent =
                    data.content + (data.truncated ? "\n\n（内容过长，仅显示前 200000 个字符）" : "");
            }).catch(function (err) {
                closePreviewModal();
                showToast(err.message, "error");
            });
    }
    document.getElementById("knowledge-preview-close").addEventListener("click", closePreviewModal);
    previewModal.addEventListener("click", function (evt) {
        if (evt.target === previewModal) closePreviewModal();
    });

    function loadEmbeddingConfig() {
        return jsonFetch(knowledgeUrl("/embedding-config")).then(function (data) {
            var badge = document.getElementById("knowledge-embedding-badge");
            document.getElementById("knowledge-embedding-id").textContent = data.profile_id || "—";
            document.getElementById("knowledge-embedding-model").textContent = data.model || "—";
            document.getElementById("knowledge-embedding-dimensions").textContent =
                data.dimensions != null ? String(data.dimensions) : "—";
            document.getElementById("knowledge-embedding-runtime").textContent =
                data.runtime_enabled
                    ? "当前进程：向量服务已启用"
                    : "当前进程：向量服务未启用";
            if (!data.bound) {
                badge.textContent = "未绑定";
                badge.className = "badge badge-fallback";
            } else if (data.runtime_enabled) {
                badge.textContent = "已启用";
                badge.className = "badge badge-primary";
            } else {
                badge.textContent = "已绑定（未启用）";
                badge.className = "badge badge-fallback";
            }
        }).catch(function (err) { showToast(err.message, "error"); });
    }

    loadTenants();
}
