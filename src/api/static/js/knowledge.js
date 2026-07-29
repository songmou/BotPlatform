/* Knowledge base management page. */

function initKnowledge() {
    var tenantSelect = document.getElementById("knowledge-tenant");
    var statusCard = document.getElementById("knowledge-status");
    var tableBody = document.getElementById("knowledge-table-body");
    var searchResults = document.getElementById("knowledge-search-results");
    var fileInput = document.getElementById("knowledge-file-input");

    function currentTenant() {
        return tenantSelect.value || "";
    }

    function setStatus(message) {
        if (!message) {
            statusCard.style.display = "none";
            statusCard.textContent = "";
            return;
        }
        statusCard.style.display = "";
        statusCard.textContent = message;
    }

    function apiError(response) {
        return response.json()
            .catch(function () { return {}; })
            .then(function (data) {
                throw new Error(data.detail || "请求失败（" + response.status + "）");
            });
    }

    function loadTenants() {
        return fetch("/api/knowledge/tenants")
            .then(function (r) { return r.ok ? r.json() : apiError(r); })
            .then(function (tenants) {
                tenantSelect.innerHTML = "";
                if (!tenants.length) {
                    setStatus("暂无租户。租户在用户首次与机器人对话或创建 Web 会话后自动生成。");
                    return;
                }
                var saved = "";
                try { saved = localStorage.getItem("bp-knowledge-tenant") || ""; } catch (e) {}
                tenants.forEach(function (tenant) {
                    var option = document.createElement("option");
                    option.value = tenant.tenant_id;
                    option.textContent = tenant.bot_id + " / " + tenant.user_id +
                        "（" + tenant.tenant_id.slice(0, 8) + "）";
                    tenantSelect.appendChild(option);
                });
                if (saved && tenants.some(function (t) { return t.tenant_id === saved; })) {
                    tenantSelect.value = saved;
                }
                loadSources();
            })
            .catch(function (err) { setStatus(err.message); });
    }

    function statusLabel(status) {
        var map = {
            ready: "已就绪",
            pending_embedding: "等待向量化",
            failed: "失败"
        };
        return map[status] || status;
    }

    function loadSources() {
        var tenant = currentTenant();
        tableBody.innerHTML = "";
        searchResults.innerHTML = "";
        if (!tenant) return;
        fetch("/api/knowledge?tenant_id=" + encodeURIComponent(tenant))
            .then(function (r) { return r.ok ? r.json() : apiError(r); })
            .then(function (data) {
                setStatus("");
                var sources = data.sources || [];
                if (!sources.length) {
                    tableBody.innerHTML =
                        '<tr><td colspan="6" class="empty-cell">该租户还没有知识来源</td></tr>';
                    return;
                }
                sources.forEach(function (source) {
                    var row = document.createElement("tr");
                    var name = source.name +
                        (source.relative_path && source.relative_path !== source.name
                            ? "（" + source.relative_path + "）" : "");
                    row.innerHTML =
                        "<td>" + escapeHtml(name) + "</td>" +
                        "<td>" + (source.source_type === "file" ? "文件" : "文本") + "</td>" +
                        "<td>" + escapeHtml(statusLabel(source.status)) + "</td>" +
                        "<td>" + Number(source.chunks || 0) + "</td>" +
                        "<td>" + escapeHtml((source.updated_at || "").slice(0, 19).replace("T", " ")) + "</td>";
                    var actionCell = document.createElement("td");
                    var deleteBtn = document.createElement("button");
                    deleteBtn.className = "btn-danger btn-small";
                    deleteBtn.textContent = "删除";
                    deleteBtn.setAttribute("data-source-id", source.source_id);
                    actionCell.appendChild(deleteBtn);
                    row.appendChild(actionCell);
                    tableBody.appendChild(row);
                });
            })
            .catch(function (err) { setStatus(err.message); });
    }

    tenantSelect.addEventListener("change", function () {
        try { localStorage.setItem("bp-knowledge-tenant", currentTenant()); } catch (e) {}
        loadSources();
    });

    /* Delete a source */
    tableBody.addEventListener("click", function (evt) {
        var button = evt.target.closest("[data-source-id]");
        if (!button) return;
        var sourceId = button.getAttribute("data-source-id");
        showConfirm("确定删除该知识来源及其全部索引吗？").then(function (ok) {
            if (!ok) return;
            fetch("/api/knowledge/" + encodeURIComponent(sourceId) +
                  "?tenant_id=" + encodeURIComponent(currentTenant()), { method: "DELETE" })
                .then(function (r) { return r.ok ? r.json() : apiError(r); })
                .then(function () {
                    showToast("知识来源已删除", "success");
                    loadSources();
                })
                .catch(function (err) { showToast(err.message, "error"); });
        });
    });

    /* Upload a document */
    document.getElementById("knowledge-upload-btn").addEventListener("click", function () {
        if (!currentTenant()) { showToast("请先选择租户", "error"); return; }
        fileInput.click();
    });
    fileInput.addEventListener("change", function () {
        var file = fileInput.files[0];
        fileInput.value = "";
        if (!file) return;
        var form = new FormData();
        form.append("tenant_id", currentTenant());
        form.append("file", file);
        showToast("正在上传并解析文档…", "info");
        fetch("/api/knowledge/upload", { method: "POST", body: form })
            .then(function (r) { return r.ok ? r.json() : apiError(r); })
            .then(function (result) {
                if (result.unchanged) {
                    showToast("文档内容未变化，索引保持不变", "info");
                } else {
                    showToast("已索引：" + result.name + "（" + result.chunks + " 个分块）", "success");
                }
                loadSources();
            })
            .catch(function (err) { showToast(err.message, "error"); });
    });

    /* Manual text modal */
    var textModal = document.getElementById("knowledge-text-modal");
    function closeTextModal() { textModal.style.display = "none"; }
    document.getElementById("knowledge-add-text-btn").addEventListener("click", function () {
        if (!currentTenant()) { showToast("请先选择租户", "error"); return; }
        document.getElementById("knowledge-text-form").reset();
        textModal.style.display = "";
    });
    document.getElementById("knowledge-text-modal-close").addEventListener("click", closeTextModal);
    document.getElementById("knowledge-text-modal-cancel").addEventListener("click", closeTextModal);
    document.getElementById("knowledge-text-form").addEventListener("submit", function (evt) {
        evt.preventDefault();
        fetch("/api/knowledge/text", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                tenant_id: currentTenant(),
                name: document.getElementById("knowledge-text-name").value.trim(),
                content: document.getElementById("knowledge-text-content").value
            })
        })
            .then(function (r) { return r.ok ? r.json() : apiError(r); })
            .then(function (result) {
                closeTextModal();
                showToast("已保存：" + result.name + "（" + result.chunks + " 个分块）", "success");
                loadSources();
            })
            .catch(function (err) { showToast(err.message, "error"); });
    });

    /* Rebuild missing embeddings */
    document.getElementById("knowledge-reindex-btn").addEventListener("click", function () {
        if (!currentTenant()) { showToast("请先选择租户", "error"); return; }
        fetch("/api/knowledge/reindex", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ tenant_id: currentTenant() })
        })
            .then(function (r) { return r.ok ? r.json() : apiError(r); })
            .then(function (result) {
                showToast("向量补齐完成：" + result.completed + " 个，剩余 " + result.remaining + " 个", "success");
                loadSources();
            })
            .catch(function (err) { showToast(err.message, "error"); });
    });

    /* Retrieval test */
    function runSearch() {
        var query = document.getElementById("knowledge-search-input").value.trim();
        if (!query || !currentTenant()) return;
        fetch("/api/knowledge/search?tenant_id=" + encodeURIComponent(currentTenant()) +
              "&q=" + encodeURIComponent(query))
            .then(function (r) { return r.ok ? r.json() : apiError(r); })
            .then(function (data) {
                var results = data.results || [];
                if (!results.length) {
                    searchResults.innerHTML = '<div class="status-card">没有检索到相关内容</div>';
                    return;
                }
                searchResults.innerHTML = results.map(function (item) {
                    var label = item.source_name + (item.locator ? " / " + item.locator : "");
                    return '<div class="status-card"><strong>' + escapeHtml(label) +
                        "</strong>（得分 " + Number(item.score || 0) + "）<br>" +
                        escapeHtml(item.content.slice(0, 400)) + "</div>";
                }).join("");
            })
            .catch(function (err) { showToast(err.message, "error"); });
    }
    document.getElementById("knowledge-search-btn").addEventListener("click", runSearch);
    document.getElementById("knowledge-search-input").addEventListener("keydown", function (evt) {
        if (evt.key === "Enter") runSearch();
    });

    loadTenants();
}
