/* ===== Models page ===== */
function initModels() {
    var statusEl = document.getElementById("model-status");
    var listEl = document.getElementById("model-list");
    var modal = document.getElementById("model-modal");
    var modalTitle = document.getElementById("model-modal-title");
    var form = document.getElementById("model-form");
    var idGroup = document.getElementById("model-id-group");
    var editingId = null;

    loadModels();

    document.getElementById("create-model-btn").addEventListener("click", function () {
        editingId = null;
        modalTitle.textContent = "添加模型";
        idGroup.style.display = "";
        form.reset();
        document.getElementById("model-enabled").checked = true;
        openModal();
    });

    document.getElementById("model-modal-close").addEventListener("click", closeModal);
    document.getElementById("model-modal-cancel").addEventListener("click", closeModal);
    modal.addEventListener("click", function (e) { if (e.target === modal) closeModal(); });

    var providerInput = document.getElementById("model-provider");
    var providerDropdown = document.getElementById("provider-dropdown");
    var providerItems = providerDropdown.querySelectorAll(".dropdown-item");

    providerInput.addEventListener("focus", function () {
        filterProviders(this.value);
        providerDropdown.style.display = "";
    });
    providerInput.addEventListener("input", function () {
        filterProviders(this.value);
        providerDropdown.style.display = "";
    });
    providerDropdown.addEventListener("mousedown", function (e) {
        var item = e.target.closest(".dropdown-item");
        if (!item) return;
        e.preventDefault();
        providerInput.value = item.getAttribute("data-value");
        providerDropdown.style.display = "none";
    });
    document.addEventListener("mousedown", function (e) {
        if (!e.target.closest(".dropdown-wrap")) {
            providerDropdown.style.display = "none";
        }
    });

    function filterProviders(query) {
        var q = query.trim().toLowerCase();
        providerItems.forEach(function (item) {
            var val = item.getAttribute("data-value");
            item.style.display = (!q || val.indexOf(q) !== -1) ? "" : "none";
        });
    }

    function openModal() { modal.style.display = ""; }
    function closeModal() { modal.style.display = "none"; }

    function loadModels() {
        fetch("/api/models/status")
            .then(function (r) { return r.json(); })
            .then(function (s) {
                statusEl.innerHTML =
                    "<strong>路由状态</strong><br>" +
                    "主模型：" + s.primary_profile_id +
                    (s.cooling_down ? " <mark>冷却中</mark>" : " ✓") + "<br>" +
                    "兜底模型：" + s.fallback_profile_id + "<br>" +
                    (s.local_profile_id ? "本地模型：" + s.local_profile_id + "<br>" : "") +
                    (s.last_primary_error ? "<br><small>最近错误：" + s.last_primary_error + "</small>" : "");
            });

        fetch("/api/models")
            .then(function (r) { return r.json(); })
            .then(function (models) {
                listEl.innerHTML = models.map(function (m) {
                    var badges = "";
                    if (m.is_primary) badges += '<span class="badge badge-primary">主模型</span>';
                    if (m.is_fallback) badges += '<span class="badge badge-fallback">兜底</span>';
                    if (!m.enabled) badges += '<span class="badge badge-fallback">已禁用</span>';
                    var actions = '<div class="model-card-footer">';
                    if (m.enabled && !m.is_primary) {
                        actions += '<button class="btn-primary btn-switch" data-id="' + m.id + '">设为主模型</button> ';
                    }
                    actions += '<button class="btn-edit" data-action="edit-model" data-id="' + m.id + '">编辑</button> ';
                    if (!m.is_primary) {
                        actions += '<button class="btn-danger" data-action="delete-model" data-id="' + m.id + '">删除</button>';
                    }
                    actions += "</div>";
                    return '<div class="model-card">' +
                        "<h5>" + m.id + " " + badges + "</h5>" +
                        "<p>" + m.provider + " / " + m.model + "</p>" +
                        "<p>" + m.type + " · " + m.temperature + " · " + m.max_tokens + " tokens · " + m.timeout_seconds + "s</p>" +
                        actions +
                        "</div>";
                }).join("");
            });
    }

    listEl.addEventListener("click", function (e) {
        var switchBtn = e.target.closest(".btn-switch");
        if (switchBtn) {
            var id = switchBtn.getAttribute("data-id");
            switchBtn.disabled = true;
            switchBtn.textContent = "切换中...";
            fetch("/api/models/switch", {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ profile_id: id }),
            }).then(function (r) {
                if (!r.ok) return r.json().then(function (d) { throw new Error(d.detail); });
                showToast("已切换主模型为 " + id, "success");
                loadModels();
            }).catch(function (err) {
                showToast("切换失败：" + err.message, "error");
                switchBtn.disabled = false;
                switchBtn.textContent = "设为主模型";
            });
            return;
        }

        var btn = e.target.closest("[data-action]");
        if (!btn) return;
        var action = btn.getAttribute("data-action");
        var mid = btn.getAttribute("data-id");

        if (action === "delete-model") {
            showConfirm("确定要删除模型「" + mid + "」吗？").then(function (ok) {
                if (!ok) return;
                fetch("/api/models/" + mid, { method: "DELETE" })
                    .then(function (r) {
                        if (!r.ok) return r.json().then(function (d) { throw new Error(d.detail); });
                        showToast("已删除模型 " + mid, "success");
                        loadModels();
                    })
                    .catch(function (err) { showToast("删除失败：" + err.message, "error"); });
            });
        }

        if (action === "edit-model") {
            fetch("/api/models/" + mid)
                .then(function (r) { return r.json(); })
                .then(function (m) {
                    editingId = mid;
                    modalTitle.textContent = "编辑模型";
                    idGroup.style.display = "none";
                    document.getElementById("model-id").value = m.id;
                    document.getElementById("model-provider").value = m.provider;
                    document.getElementById("model-type").value = m.type;
                    document.getElementById("model-base-url").value = "";
                    document.getElementById("model-name").value = m.model;
                    document.getElementById("model-api-key-env").value = "";
                    document.getElementById("model-temperature").value = m.temperature;
                    document.getElementById("model-max-tokens").value = m.max_tokens;
                    document.getElementById("model-timeout").value = m.timeout_seconds;
                    document.getElementById("model-enabled").checked = m.enabled;
                    openModal();
                });
        }
    });

    form.addEventListener("submit", function (e) {
        e.preventDefault();
        var payload = {
            type: document.getElementById("model-type").value,
            provider: document.getElementById("model-provider").value,
            base_url: document.getElementById("model-base-url").value,
            model: document.getElementById("model-name").value,
            api_key_env: document.getElementById("model-api-key-env").value || null,
            temperature: parseFloat(document.getElementById("model-temperature").value),
            max_tokens: parseInt(document.getElementById("model-max-tokens").value),
            timeout_seconds: parseFloat(document.getElementById("model-timeout").value),
            enabled: document.getElementById("model-enabled").checked,
        };

        var url, method;
        if (editingId) {
            url = "/api/models/" + editingId;
            method = "PUT";
        } else {
            payload.id = document.getElementById("model-id").value;
            url = "/api/models";
            method = "POST";
        }

        fetch(url, {
            method: method,
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        })
            .then(function (r) {
                if (!r.ok) return r.json().then(function (d) { throw new Error(d.detail); });
                showToast(editingId ? "已保存修改" : "已添加模型", "success");
                closeModal();
                loadModels();
            })
            .catch(function (err) { showToast("保存失败：" + err.message, "error"); });
    });
}

