function initWorkflowPage() {
    "use strict";
    var root = document.getElementById("workflow-app");
    if (!root) return;
    var mode = root.getAttribute("data-mode");
    var list = document.getElementById("workflow-list");
    var editor = document.getElementById("workflow-editor");
    var canvas = document.getElementById("workflow-canvas");
    var world = document.getElementById("workflow-world");
    var nodesHost = document.getElementById("workflow-nodes");
    var edgesSvg = document.getElementById("workflow-edges");
    var selectionBox = document.getElementById("workflow-selection-box");
    var state = {
        items: [], catalog: [], options: {}, current: null, definition: null,
        selectedNodes: [], selectedEdge: "", zoom: 1, panX: 0, panY: 0,
        undo: [], redo: [], copiedNodes: [], copiedEdges: [], saveTimer: null,
        saveChain: Promise.resolve(), saveInFlight: null, dirtyVersion: 0, savedVersion: 0,
        pointer: null, connecting: null, spaceDown: false, saveConflict: false
    };

    function clone(value) { return JSON.parse(JSON.stringify(value)); }
    function workflowId(item) { return item.workflow_id || item.resource_id; }
    function workflowDefinition(item) { return clone(item.definition || item.payload || {}); }
    function api(path) { return mode === "organization" ? organizationApi(path) : "/api/v2/platform/workflow-templates" + path; }
    function canEditWorkflow() { return mode === "organization" ? canWriteOrganization() : hasPermission("panel.write"); }
    function readonlyTitle() { return canEditWorkflow() ? "" : "当前账号只有查看权限"; }
    function errorMessage(body, fallback) {
        var detail = body && body.detail;
        if (typeof detail === "string") return detail;
        if (Array.isArray(detail)) return detail.map(function (item) { return item.msg || JSON.stringify(item); }).join("；");
        if (detail && typeof detail === "object") return detail.message || JSON.stringify(detail);
        return fallback || "请求失败";
    }
    function request(url, options) {
        return fetch(url, options).then(function (response) {
            return response.json().catch(function () { return {}; }).then(function (body) {
                if (!response.ok) { var error = new Error(errorMessage(body)); error.status = response.status; throw error; }
                return body;
            });
        });
    }
    function jsonOptions(method, body) { return { method: method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) }; }
    function statusText(value) { return { draft: "草稿", published: "已发布", disabled: "已停用", archived: "已归档", queued: "排队中", running: "运行中", waiting: "等待处理", succeeded: "成功", failed: "失败", timed_out: "已超时", canceled: "已取消", needs_attention: "需要处理" }[value] || value; }
    function busy(button, promise, label) {
        if (!button || button.disabled) return promise;
        var original = button.textContent;
        button.disabled = true; button.setAttribute("aria-busy", "true"); button.textContent = label || "处理中…";
        return Promise.resolve(promise).finally(function () { button.disabled = false; button.removeAttribute("aria-busy"); button.textContent = original; });
    }
    function report(action, promise) {
        return Promise.resolve(promise).catch(function (error) { showToast(action + "失败：" + (error.message || "未知错误"), "error"); throw error; });
    }
    function emptyDefinition(name) {
        return { schema_version: 1, name: name, description: "", inputs: [], outputs: [], triggers: [{ id: "manual", type: "manual", config: {} }], nodes: [
            { id: "start", type: "start", name: "开始", position: { x: 100, y: 220 }, config: {}, error_policy: { mode: "stop", max_retries: 0 } },
            { id: "end", type: "end", name: "结束", position: { x: 520, y: 220 }, config: {}, error_policy: { mode: "stop", max_retries: 0 } }
        ], edges: [{ id: "edge_start_end", source: "start", source_port: "default", target: "end", target_port: "default" }], settings: { timeout_seconds: 86400, max_steps: 500 } };
    }
    function catalogItem(type) { return state.catalog.filter(function (item) { return item.type === type; })[0] || { type: type, name: type, config_fields: [], output_ports: [{ key: "default", label: "继续" }] }; }
    function defaultConfig(type) {
        var result = {};
        (catalogItem(type).config_fields || []).forEach(function (field) { if (field.default !== undefined) result[field.key] = clone(field.default); });
        return result;
    }
    function nodeById(id) { return state.definition && state.definition.nodes.filter(function (node) { return node.id === id; })[0]; }
    function edgeById(id) { return state.definition && state.definition.edges.filter(function (edge) { return edge.id === id; })[0]; }
    function selectedNode() { return state.selectedNodes.length === 1 ? nodeById(state.selectedNodes[0]) : null; }
    function isEditableTarget(target) { return !!(target && target.closest && target.closest("input,textarea,select,[contenteditable=true],.modal-overlay,.workflow-panel-overlay")); }

    function load() {
        return request(mode === "organization" ? api("/workflows") : api("")).then(function (data) { state.items = data.items || []; renderList(); });
    }
    function renderList() {
        var createButton = document.getElementById("workflow-new"), editable = canEditWorkflow();
        createButton.disabled = !editable; createButton.title = editable ? "" : readonlyTitle();
        list.innerHTML = state.items.length ? state.items.map(function (item) {
            var id = workflowId(item), definition = workflowDefinition(item), status = item.status || "draft";
            var readonly = editable ? "" : ' disabled title="' + readonlyTitle() + '"';
            var actions = '<button data-action="edit" data-id="' + escapeHtml(id) + '">' + (editable ? "编辑" : "查看") + '</button>';
            if (mode === "organization") {
                actions += '<button data-action="run" data-id="' + escapeHtml(id) + '"' + (status !== "published" ? ' disabled title="请先发布工作流"' : "") + '>运行</button>';
                if (canManageOrganization()) actions += '<button data-action="manage" data-id="' + escapeHtml(id) + '">管理</button>';
                actions += '<button data-action="archive" data-id="' + escapeHtml(id) + '"' + readonly + '>归档</button>';
            } else actions += '<button data-action="delete" data-id="' + escapeHtml(id) + '"' + readonly + '>删除</button>';
            return '<article class="workflow-card"><h3>' + escapeHtml(item.name || definition.name || id) + '</h3><p>' + escapeHtml(item.description || definition.description || "暂无描述") + '</p><div class="workflow-card-meta"><span class="workflow-status">' + escapeHtml(statusText(status)) + '</span><span class="workflow-status">节点 ' + (definition.nodes || []).length + '</span>' + (item.published_version ? '<span class="workflow-status">v' + item.published_version + '</span>' : '') + '</div><div class="workflow-card-actions">' + actions + '</div></article>';
        }).join("") : '<div class="organization-empty">暂无工作流，点击“新建工作流”开始编排。</div>';
    }
    function findItem(id) { return state.items.filter(function (item) { return workflowId(item) === id; })[0]; }
    function openItem(id) {
        var item = findItem(id);
        var promise = mode === "organization" ? request(api("/workflows/" + encodeURIComponent(id))) : request(api("/" + encodeURIComponent(id)));
        return report("打开工作流", promise).then(function (value) {
            state.current = value || item; state.definition = workflowDefinition(value || item); state.selectedNodes = []; state.selectedEdge = ""; state.undo = []; state.redo = [];
            state.dirtyVersion = 0; state.savedVersion = 0; state.saveConflict = false;
            state.saveChain = Promise.resolve(); state.saveInFlight = null;
            document.getElementById("workflow-name").value = state.definition.name || value.name || "";
            var manage = document.getElementById("workflow-manage"); if (manage) manage.hidden = mode === "organization" && !canManageOrganization();
            list.hidden = true; var tabs = document.querySelector(".workflow-tabs"); if (tabs) tabs.hidden = true; editor.hidden = false;
            editor.classList.toggle("workflow-readonly", !canEditWorkflow()); renderCanvas(); applyEditorPermissions(); requestAnimationFrame(fitCanvas);
            return value;
        });
    }
    function syncHeader() { if (state.definition) state.definition.name = document.getElementById("workflow-name").value.trim() || "未命名工作流"; }
    function pushUndo() { if (!state.definition || !canEditWorkflow()) return; state.undo.push(clone(state.definition)); if (state.undo.length > 80) state.undo.shift(); state.redo = []; updateHistoryButtons(); }
    function updateHistoryButtons() { document.getElementById("workflow-undo").disabled = !canEditWorkflow() || !state.undo.length; document.getElementById("workflow-redo").disabled = !canEditWorkflow() || !state.redo.length; }
    function applyEditorPermissions() {
        var editable = canEditWorkflow(), title = readonlyTitle();
        ["workflow-name", "workflow-settings", "workflow-ai", "workflow-validate", "workflow-manage", "workflow-publish", "workflow-undo", "workflow-redo", "workflow-node-name", "workflow-node-config", "workflow-error-mode", "workflow-max-retries", "workflow-variable-insert", "workflow-connect", "workflow-delete-node", "workflow-delete-edge"].forEach(function (id) {
            var element = document.getElementById(id); if (!element) return;
            if (!editable) { element.disabled = true; element.title = title; }
        });
        document.querySelectorAll("#workflow-node-fields input,#workflow-node-fields textarea,#workflow-node-fields select").forEach(function (element) { element.disabled = !editable; if (!editable) element.title = title; });
        document.querySelectorAll(".workflow-palette-node").forEach(function (element) { element.disabled = !editable; element.draggable = editable; if (!editable) element.title = title; });
        updateHistoryButtons();
    }
    function changed() {
        if (!canEditWorkflow()) return;
        state.dirtyVersion += 1; document.getElementById("workflow-save-state").textContent = "未保存";
        clearTimeout(state.saveTimer); state.saveTimer = setTimeout(function () { queueSave().catch(function () {}); }, 700);
    }
    function queueSave() {
        clearTimeout(state.saveTimer); state.saveTimer = null; syncHeader();
        if (!canEditWorkflow()) return Promise.resolve(state.current);
        if (!state.current || state.dirtyVersion <= state.savedVersion) return state.saveChain;
        if (state.saveConflict) return Promise.reject(new Error("草稿存在保存冲突，请重新加载后继续编辑"));
        if (state.saveInFlight) {
            state.saveChain = state.saveInFlight.then(function () { return queueSave(); });
            return state.saveChain;
        }
        var requestedVersion = state.dirtyVersion, snapshot = clone(state.definition), id = workflowId(state.current), call;
        document.getElementById("workflow-save-state").textContent = "保存中…";
        if (mode === "organization") call = request(api("/workflows/" + encodeURIComponent(id) + "/draft"), jsonOptions("PUT", { definition: snapshot, base_revision: state.current.draft_revision }));
        else call = request(api("/" + encodeURIComponent(id) + "/draft"), jsonOptions("PUT", { definition: snapshot }));
        state.saveInFlight = call.then(function (item) {
            state.current = item; state.savedVersion = requestedVersion;
            state.saveConflict = false;
            if (mode === "platform") state.current.payload = snapshot;
            document.getElementById("workflow-save-state").textContent = state.dirtyVersion > state.savedVersion ? "未保存" : "已保存";
            return item;
        }).catch(function (error) {
            state.saveConflict = error.status === 409;
            document.getElementById("workflow-save-state").textContent = state.saveConflict ? "保存冲突" : "保存失败";
            showToast("保存工作流失败：" + error.message, "error"); throw error;
        }).finally(function () { state.saveInFlight = null; });
        state.saveChain = state.saveInFlight.then(function (item) { return state.dirtyVersion > state.savedVersion ? queueSave() : item; });
        return state.saveChain;
    }
    function closeEditor() {
        return queueSave().then(function () { editor.hidden = true; list.hidden = false; var tabs = document.querySelector(".workflow-tabs"); if (tabs) tabs.hidden = false; state.current = null; return load(); });
    }
    function reloadCurrent() {
        if (!state.current) return load();
        var id = workflowId(state.current), needsConfirm = state.saveConflict || state.dirtyVersion > state.savedVersion;
        var confirmation = needsConfirm ? showConfirm("重新加载会丢弃当前未保存内容，是否继续？") : Promise.resolve(true);
        return confirmation.then(function (yes) {
            if (!yes) return null;
            var url = mode === "organization" ? api("/workflows/" + encodeURIComponent(id)) : api("/" + encodeURIComponent(id));
            return request(url).then(function (item) {
                state.current = item; state.definition = workflowDefinition(item);
                state.dirtyVersion = 0; state.savedVersion = 0; state.saveConflict = false;
                state.saveChain = Promise.resolve(); state.saveInFlight = null;
                state.selectedNodes = []; state.selectedEdge = ""; state.undo = []; state.redo = [];
                document.getElementById("workflow-name").value = state.definition.name || item.name || "";
                document.getElementById("workflow-save-state").textContent = "已重新加载";
                renderCanvas(); showToast("工作流已重新加载", "success"); return item;
            });
        });
    }

    function worldTransform() { world.style.transform = "translate(" + state.panX + "px," + state.panY + "px) scale(" + state.zoom + ")"; document.getElementById("workflow-zoom-label").textContent = Math.round(state.zoom * 100) + "%"; }
    function canvasPoint(clientX, clientY) { var rect = canvas.getBoundingClientRect(); return { x: (clientX - rect.left - state.panX) / state.zoom, y: (clientY - rect.top - state.panY) / state.zoom }; }
    function outputPorts(node) {
        var ports = clone(catalogItem(node.type).output_ports || []);
        if (node.type === "switch") {
            ports = (node.config.cases || []).filter(function (item) { return item && typeof item === "object"; }).map(function (item) { var key = String(item.key !== undefined ? item.key : item.value); return { key: "case:" + key, label: key }; });
            ports.push({ key: "default", label: "默认" }, { key: "error", label: "错误" });
        }
        if ((node.error_policy || {}).mode === "error_branch" && !ports.some(function (port) { return port.key === "error"; })) ports.push({ key: "error", label: "错误" });
        return ports;
    }
    function portY(node, portKey) { var ports = outputPorts(node), index = Math.max(0, ports.map(function (port) { return port.key; }).indexOf(portKey)); return 27 + index * 20; }
    function nodeHeight(node) { return Math.max(76, 46 + outputPorts(node).length * 20); }
    function edgePath(source, target, sourcePort) {
        var x1 = Number(source.position.x) + 170, y1 = Number(source.position.y) + portY(source, sourcePort), x2 = Number(target.position.x), y2 = Number(target.position.y) + 35, bend = Math.max(60, Math.abs(x2 - x1) / 2);
        return "M " + x1 + " " + y1 + " C " + (x1 + bend) + " " + y1 + ", " + (x2 - bend) + " " + y2 + ", " + x2 + " " + y2;
    }
    function fitCanvas() {
        if (!state.definition || !state.definition.nodes.length) return;
        var rect = canvas.getBoundingClientRect(), xs = state.definition.nodes.map(function (node) { return Number(node.position.x || 0); }), ys = state.definition.nodes.map(function (node) { return Number(node.position.y || 0); });
        var minX = Math.min.apply(null, xs), minY = Math.min.apply(null, ys), maxX = Math.max.apply(null, state.definition.nodes.map(function (node) { return Number(node.position.x || 0) + 170; })), maxY = Math.max.apply(null, state.definition.nodes.map(function (node) { return Number(node.position.y || 0) + nodeHeight(node); }));
        var width = Math.max(170, maxX - minX), height = Math.max(80, maxY - minY), padding = 100;
        state.zoom = Math.max(.35, Math.min(1.3, (rect.width - padding) / width, (rect.height - padding) / height)); state.panX = (rect.width - width * state.zoom) / 2 - minX * state.zoom; state.panY = (rect.height - height * state.zoom) / 2 - minY * state.zoom; worldTransform();
    }
    function renderCanvas() {
        if (!state.definition) return;
        nodesHost.innerHTML = state.definition.nodes.map(function (node) {
            var selected = state.selectedNodes.indexOf(node.id) >= 0, ports = outputPorts(node);
            var portHtml = ports.map(function (port, index) { var top = 27 + index * 20; return '<span class="workflow-port out" data-port="' + escapeHtml(port.key) + '" title="拖动连接：' + escapeHtml(port.label || port.key) + '" style="top:' + top + 'px"></span><span class="workflow-port-label" style="top:' + top + 'px">' + escapeHtml(port.label || port.key) + '</span>'; }).join("");
            return '<div class="workflow-node' + (selected ? (state.selectedNodes.length > 1 ? ' multi-selected' : ' selected') : '') + '" data-node-id="' + escapeHtml(node.id) + '" style="left:' + Number(node.position.x || 0) + 'px;top:' + Number(node.position.y || 0) + 'px;min-height:' + nodeHeight(node) + 'px"><span class="workflow-port in" data-port="default" title="输入端口"></span><div class="workflow-node-title">' + escapeHtml(node.name || node.type) + '</div><div class="workflow-node-type">' + escapeHtml(node.type) + '</div>' + portHtml + '</div>';
        }).join("");
        drawEdges(); renderProperties(); worldTransform(); applyEditorPermissions();
    }
    function drawEdges(preview) {
        var html = state.definition.edges.map(function (edge) {
            var source = nodeById(edge.source), target = nodeById(edge.target); if (!source || !target) return "";
            var d = edgePath(source, target, edge.source_port || "default"), selected = edge.id === state.selectedEdge ? " selected" : "";
            return '<path class="workflow-edge' + selected + '" data-edge-id="' + escapeHtml(edge.id) + '" d="' + d + '"></path><path class="workflow-edge-hit" data-edge-id="' + escapeHtml(edge.id) + '" d="' + d + '"><title>' + escapeHtml(edge.source_port || "default") + '</title></path>';
        }).join("");
        if (preview) html += '<path class="workflow-edge-preview" d="' + preview + '"></path>';
        edgesSvg.innerHTML = html;
    }
    function renderProperties() {
        var node = selectedNode(), edge = edgeById(state.selectedEdge), empty = document.getElementById("workflow-property-empty"), form = document.getElementById("workflow-property-form"), edgeForm = document.getElementById("workflow-edge-form");
        empty.hidden = !!node || !!edge; form.hidden = !node; edgeForm.hidden = !edge;
        if (edge) { document.getElementById("workflow-edge-summary").textContent = edge.source + " · " + edge.source_port + " → " + edge.target; return; }
        if (!node) return;
        document.getElementById("workflow-node-name").value = node.name || "";
        document.getElementById("workflow-node-config").value = JSON.stringify(node.config || {}, null, 2);
        document.getElementById("workflow-json-error").hidden = true;
        document.getElementById("workflow-error-mode").value = (node.error_policy || {}).mode || "stop";
        document.getElementById("workflow-max-retries").value = Math.max(1, Number((node.error_policy || {}).max_retries || 3));
        document.getElementById("workflow-retry-wrap").hidden = (node.error_policy || {}).mode !== "retry";
        document.getElementById("workflow-delete-node").disabled = node.type === "start" || node.type === "end";
        document.getElementById("workflow-connect").disabled = node.type === "end";
        renderStructuredFields(node);
    }
    function renderStructuredFields(node) {
        var spec = catalogItem(node.type), fields = spec.config_fields || [];
        document.getElementById("workflow-node-fields").innerHTML = fields.map(function (field) {
            var value = node.config[field.key]; if (value === undefined && field.default !== undefined) value = field.default;
            var serialized = typeof value === "object" ? JSON.stringify(value, null, 2) : (value === undefined || value === null ? "" : String(value));
            var control;
            if (field.type === "resource" && (state.options[field.resource] || []).length) control = '<select data-config-key="' + escapeHtml(field.key) + '" data-config-type="resource"><option value="">请选择</option>' + state.options[field.resource].map(function (option) { return '<option value="' + escapeHtml(option.value) + '"' + (String(option.value) === serialized ? " selected" : "") + '>' + escapeHtml(option.label) + '</option>'; }).join("") + '</select>';
            else if (field.type === "textarea" || field.type === "json") control = '<textarea data-config-key="' + escapeHtml(field.key) + '" data-config-type="' + field.type + '" rows="' + (field.type === "json" ? 5 : 4) + '">' + escapeHtml(serialized) + '</textarea>';
            else if (field.type === "select") control = '<select data-config-key="' + escapeHtml(field.key) + '" data-config-type="select">' + (field.options || []).map(function (option) { return '<option value="' + escapeHtml(option) + '"' + (String(option) === serialized ? " selected" : "") + '>' + escapeHtml(option) + '</option>'; }).join("") + '</select>';
            else { var constraints = field.constraints || {}; control = '<input data-config-key="' + escapeHtml(field.key) + '" data-config-type="' + escapeHtml(field.type) + '" type="' + (field.type === "number" ? "number" : "text") + '" value="' + escapeHtml(serialized) + '"' + (constraints.min !== undefined ? ' min="' + escapeHtml(constraints.min) + '"' : "") + (constraints.max !== undefined ? ' max="' + escapeHtml(constraints.max) + '"' : "") + (field.required ? " required" : "") + '>'; }
            return '<label class="workflow-field">' + escapeHtml(field.label) + control + (field.help ? '<span class="workflow-field-help">' + escapeHtml(field.help) + '</span>' : '') + '</label>';
        }).join("") || '<p class="workflow-field-help">此节点没有可配置字段。</p>';
        var ins = (spec.inputs || []).map(function (item) { return item.key + ":" + item.type; }).join("、") || "无";
        var outs = (spec.outputs || []).map(function (item) { return item.key + ":" + item.type; }).join("、") || "无";
        document.getElementById("workflow-node-contract").textContent = "输入：" + ins + "；输出：" + outs;
        renderVariablePicker(node);
    }
    function renderVariablePicker(node) {
        var upstream = {}, changedUpstream = true;
        while (changedUpstream) { changedUpstream = false; state.definition.edges.forEach(function (edge) { if (edge.target === node.id || upstream[edge.target]) { if (!upstream[edge.source]) { upstream[edge.source] = true; changedUpstream = true; } } }); }
        var values = (state.definition.inputs || []).map(function (field) { return "input." + field.key; });
        values.push("trigger");
        state.definition.nodes.forEach(function (item) { if (upstream[item.id] && item.type !== "start") { var outputs = catalogItem(item.type).outputs || []; if (outputs.length === 1 && outputs[0].key === "output") values.push("nodes." + item.id + ".output"); else if (outputs.length) outputs.forEach(function (output) { values.push("nodes." + item.id + ".output." + output.key); }); else values.push("nodes." + item.id + ".output"); } });
        document.getElementById("workflow-variable-picker").innerHTML = values.map(function (value) { return '<option value="' + escapeHtml(value) + '">{{' + escapeHtml(value) + '}}</option>'; }).join("");
    }
    function readStructuredFields() {
        var node = selectedNode(); if (!node) return false; var next = clone(node.config || {});
        var failed = "";
        document.querySelectorAll("#workflow-node-fields [data-config-key]").forEach(function (element) {
            var key = element.getAttribute("data-config-key"), type = element.getAttribute("data-config-type"), value = element.value;
            if (type === "json") { try { value = value.trim() ? JSON.parse(value) : null; } catch (error) { failed = key + " 必须是有效 JSON"; } }
            else if (type === "number") value = value === "" ? null : Number(value);
            next[key] = value;
        });
        if (failed) { showToast(failed, "error"); return false; }
        pushUndo(); node.config = next; changed(); renderCanvas(); return true;
    }
    function addNode(type, x, y) {
        if (!canEditWorkflow()) return;
        pushUndo(); var base = type.replace(/[^a-z0-9]/g, "_") || "node", id = base, index = 2; while (nodeById(id)) id = base + "_" + index++;
        var meta = catalogItem(type); state.definition.nodes.push({ id: id, type: type, name: meta.name, position: { x: Math.round(x / 20) * 20, y: Math.round(y / 20) * 20 }, config: defaultConfig(type), error_policy: { mode: "stop", max_retries: 0 } });
        state.selectedNodes = [id]; state.selectedEdge = ""; renderCanvas(); changed();
    }
    function wouldCycle(source, target) {
        var outgoing = {}; state.definition.edges.forEach(function (edge) { (outgoing[edge.source] = outgoing[edge.source] || []).push(edge.target); });
        var stack = [target], seen = {}; while (stack.length) { var current = stack.pop(); if (current === source) return true; if (seen[current]) continue; seen[current] = true; (outgoing[current] || []).forEach(function (id) { stack.push(id); }); } return false;
    }
    function connect(source, sourcePort, target) {
        if (!canEditWorkflow()) return;
        var sourceNode = nodeById(source), targetNode = nodeById(target);
        if (!sourceNode || !targetNode || source === target) return showToast("节点不能连接到自身", "error");
        if (sourceNode.type === "end" || targetNode.type === "start") return showToast("开始节点不能有入线，结束节点不能有出线", "error");
        if (state.definition.edges.some(function (edge) { return edge.source === source && edge.source_port === sourcePort; })) return showToast("该输出端口已经连接，请先删除原连线", "error");
        if (wouldCycle(source, target)) return showToast("工作流必须是无环图，不能连接回上游节点", "error");
        pushUndo(); state.definition.edges.push({ id: "edge_" + Date.now() + "_" + Math.floor(Math.random() * 1000), source: source, source_port: sourcePort, target: target, target_port: "default" });
        state.selectedEdge = state.definition.edges[state.definition.edges.length - 1].id; state.selectedNodes = []; state.connecting = null; document.getElementById("workflow-connect-hint").textContent = ""; renderCanvas(); changed();
    }
    function deleteSelection() {
        if (!canEditWorkflow()) return;
        if (state.selectedEdge) { pushUndo(); state.definition.edges = state.definition.edges.filter(function (edge) { return edge.id !== state.selectedEdge; }); state.selectedEdge = ""; renderCanvas(); changed(); return; }
        var deletable = state.selectedNodes.filter(function (id) { var node = nodeById(id); return node && node.type !== "start" && node.type !== "end"; });
        if (!deletable.length) return; pushUndo(); var ids = {}; deletable.forEach(function (id) { ids[id] = true; });
        state.definition.nodes = state.definition.nodes.filter(function (node) { return !ids[node.id]; }); state.definition.edges = state.definition.edges.filter(function (edge) { return !ids[edge.source] && !ids[edge.target]; }); state.selectedNodes = []; renderCanvas(); changed();
    }

    function renderCatalog(query) {
        var groups = {}; state.catalog.forEach(function (item) { if (query && (item.name + item.type).toLowerCase().indexOf(query.toLowerCase()) < 0) return; if (["start", "end"].indexOf(item.type) >= 0) return; (groups[item.category] = groups[item.category] || []).push(item); });
        document.getElementById("workflow-node-catalog").innerHTML = Object.keys(groups).map(function (group) { return '<div class="workflow-node-group"><h4>' + escapeHtml(group) + '</h4>' + groups[group].map(function (item) { return '<button class="workflow-palette-node" draggable="true" data-node-type="' + escapeHtml(item.type) + '" title="' + escapeHtml((item.outputs || []).map(function (out) { return out.key; }).join("、")) + '">' + escapeHtml(item.name) + '</button>'; }).join("") + '</div>'; }).join(""); applyEditorPermissions();
    }
    function loadCatalog() { return request("/api/v2/workflow-node-catalog").then(function (data) { state.catalog = data.items || []; renderCatalog(""); }); }
    function loadEditorOptions() { if (mode !== "organization") return Promise.resolve(); return request(api("/workflow-editor-options")).then(function (data) { state.options = data || {}; }); }

    function validateCurrent(button) {
        var id = workflowId(state.current), url = mode === "organization" ? api("/workflows/" + encodeURIComponent(id) + "/validate") : api("/" + encodeURIComponent(id) + "/validate");
        return busy(button, report("校验工作流", queueSave().then(function () { return request(url, jsonOptions("POST", { definition: state.definition })); })).then(function () { showToast("工作流校验通过", "success"); }), "校验中…");
    }
    function publish(button) {
        var id = encodeURIComponent(workflowId(state.current)), url = mode === "platform" ? api("/" + id + "/publish") : api("/workflows/" + id + "/publish");
        return busy(button, report("发布工作流", queueSave().then(function () { return request(url, { method: "POST" }); })).then(function (item) { state.current = item; showToast(mode === "platform" ? "平台模板已发布" : "工作流版本已发布", "success"); }), "发布中…");
    }
    function testRun(button) {
        return showFormDialog({ title: "试运行输入", fields: [{ name: "inputs", label: "输入 JSON", type: "textarea", value: "{}", required: true }, { name: "allow_side_effects", label: "允许真实外部动作（输入 true）" }] }).then(function (value) {
            if (!value) return; var inputs; try { inputs = JSON.parse(value.inputs || "{}"); if (!inputs || Array.isArray(inputs) || typeof inputs !== "object") throw new Error(); } catch (error) { showToast("输入必须是 JSON 对象", "error"); return; }
            var promise = report("试运行", queueSave().then(function () { return request(api("/workflows/" + encodeURIComponent(workflowId(state.current)) + "/test"), jsonOptions("POST", { inputs: inputs, allow_side_effects: String(value.allow_side_effects).toLowerCase() === "true", wait: true, timeout: 30 })); })).then(showRunDebug);
            return busy(button, promise, "运行中…");
        });
    }
    function runPublished(button) {
        return showFormDialog({ title: "运行已发布工作流", fields: [{ name: "inputs", label: "输入 JSON", type: "textarea", value: "{}", required: true }] }).then(function (value) {
            if (!value) return; var inputs; try { inputs = JSON.parse(value.inputs || "{}"); if (!inputs || Array.isArray(inputs) || typeof inputs !== "object") throw new Error(); } catch (error) { showToast("输入必须是 JSON 对象", "error"); return; }
            var promise = report("运行工作流", request(api("/workflows/" + encodeURIComponent(workflowId(state.current)) + "/runs"), jsonOptions("POST", { inputs: inputs, wait: true, timeout: 30 }))).then(showRunDebug);
            return busy(button, promise, "运行中…");
        });
    }
    function showRunDebug(run) { document.getElementById("workflow-debug-content").textContent = JSON.stringify(run, null, 2); document.getElementById("workflow-debug").hidden = false; return run; }
    function aiDesign(button) {
        return showFormDialog({ title: "AI 搭建工作流", fields: [{ name: "instruction", label: "描述希望生成或修改的流程", type: "textarea", required: true }] }).then(function (value) {
            if (!value) return; if (!value.instruction.trim()) return showToast("AI 搭建要求不能为空", "error");
            var id = encodeURIComponent(workflowId(state.current)), url = mode === "platform" ? api("/" + id + "/design-suggestions") : api("/workflows/" + id + "/design-suggestions");
            var promise = report("AI 搭建", request(url, jsonOptions("POST", { instruction: value.instruction, definition: state.definition }))).then(function (result) {
                return showConfirm("AI 已生成候选草稿，应用后仍需人工检查与发布。是否应用？").then(function (yes) { if (!yes) return; pushUndo(); state.definition = result.proposal; document.getElementById("workflow-name").value = state.definition.name; state.selectedNodes = []; state.selectedEdge = ""; renderCanvas(); changed(); showToast("AI 候选草稿已应用", "success"); });
            });
            return busy(button, promise, "AI 生成中…");
        });
    }

    function openPanel(title, html, saveLabel) {
        var overlay = document.getElementById("workflow-panel-overlay"); document.getElementById("workflow-panel-title").textContent = title; document.getElementById("workflow-panel-body").innerHTML = html; document.getElementById("workflow-panel-save").textContent = saveLabel || "保存"; overlay.hidden = false;
        return new Promise(function (resolve) {
            function finish(value) { overlay.hidden = true; document.getElementById("workflow-panel-save").onclick = null; document.getElementById("workflow-panel-cancel").onclick = null; document.getElementById("workflow-panel-close").onclick = null; resolve(value); }
            document.getElementById("workflow-panel-save").onclick = function () { finish(true); }; document.getElementById("workflow-panel-cancel").onclick = function () { finish(false); }; document.getElementById("workflow-panel-close").onclick = function () { finish(false); };
        });
    }
    function fieldRows(items) { return (items || []).map(function (field) { return '<tr><td><input data-key value="' + escapeHtml(field.key || "") + '"></td><td><input data-label value="' + escapeHtml(field.label || "") + '"></td><td><select data-type>' + ["string", "number", "integer", "boolean", "object", "array", "file_ref"].map(function (type) { return '<option' + (field.type === type ? " selected" : "") + '>' + type + '</option>'; }).join("") + '</select></td><td><input data-default placeholder="可选 JSON/文本" value="' + escapeHtml(field.default === undefined ? "" : (typeof field.default === "string" ? field.default : JSON.stringify(field.default))) + '"></td><td><input data-required type="checkbox"' + (field.required ? " checked" : "") + '></td><td><button type="button" data-remove-row>×</button></td></tr>'; }).join(""); }
    function triggerRows(items) { return (items || []).map(function (trigger) { return '<tr><td><input data-id value="' + escapeHtml(trigger.id || "") + '"></td><td><select data-trigger-type>' + ["manual", "api", "webhook", "schedule"].map(function (type) { return '<option' + (trigger.type === type ? " selected" : "") + '>' + type + '</option>'; }).join("") + '</select></td><td><input data-cron placeholder="五段 cron" value="' + escapeHtml((trigger.config || {}).cron || "") + '"></td><td><button type="button" data-remove-row>×</button></td></tr>'; }).join(""); }
    function workflowSettings() {
        var html = '<div class="workflow-panel-grid"><label class="wide">描述<textarea id="wf-setting-description" rows="3">' + escapeHtml(state.definition.description || "") + '</textarea></label><label>超时秒数<input id="wf-setting-timeout" type="number" min="1" max="2592000" value="' + Number(state.definition.settings.timeout_seconds || 86400) + '"></label><label>最大步骤<input id="wf-setting-steps" type="number" min="1" max="500" value="' + Number(state.definition.settings.max_steps || 500) + '"></label></div>';
        html += '<h4 class="workflow-section-title">输入字段 <button type="button" data-add-field="inputs">新增</button></h4><table class="workflow-panel-table"><thead><tr><th>键</th><th>标签</th><th>类型</th><th>默认值</th><th>必填</th><th></th></tr></thead><tbody id="wf-inputs">' + fieldRows(state.definition.inputs) + '</tbody></table>';
        html += '<h4 class="workflow-section-title">输出字段 <button type="button" data-add-field="outputs">新增</button></h4><table class="workflow-panel-table"><thead><tr><th>键</th><th>标签</th><th>类型</th><th>默认值</th><th>必填</th><th></th></tr></thead><tbody id="wf-outputs">' + fieldRows(state.definition.outputs) + '</tbody></table>';
        html += '<h4 class="workflow-section-title">触发器 <button type="button" data-add-trigger>新增</button></h4><table class="workflow-panel-table"><thead><tr><th>ID</th><th>类型</th><th>定时 cron</th><th></th></tr></thead><tbody id="wf-triggers">' + triggerRows(state.definition.triggers) + '</tbody></table>';
        var promise = openPanel("流程设置", html, "应用");
        var body = document.getElementById("workflow-panel-body"); body.onclick = function (event) {
            if (event.target.matches("[data-remove-row]")) event.target.closest("tr").remove();
            if (event.target.matches("[data-add-field]")) { var target = document.getElementById("wf-" + event.target.getAttribute("data-add-field")); target.insertAdjacentHTML("beforeend", fieldRows([{ key: "field", label: "字段", type: "string", required: false }])); }
            if (event.target.matches("[data-add-trigger]")) document.getElementById("wf-triggers").insertAdjacentHTML("beforeend", triggerRows([{ id: "trigger", type: "manual", config: {} }]));
        };
        return promise.then(function (yes) { body.onclick = null; if (!yes) return; function readFields(id) { return Array.prototype.map.call(document.querySelectorAll("#" + id + " tr"), function (row) { var field = { key: row.querySelector("[data-key]").value.trim(), label: row.querySelector("[data-label]").value.trim(), type: row.querySelector("[data-type]").value, required: row.querySelector("[data-required]").checked }, raw = row.querySelector("[data-default]").value; if (raw !== "") { try { field.default = JSON.parse(raw); } catch (error) { field.default = raw; } } return field; }); } function readTriggers() { return Array.prototype.map.call(document.querySelectorAll("#wf-triggers tr"), function (row) { var type = row.querySelector("[data-trigger-type]").value, config = {}; if (type === "schedule") config.cron = row.querySelector("[data-cron]").value.trim(); return { id: row.querySelector("[data-id]").value.trim(), type: type, config: config }; }); }
            pushUndo(); state.definition.description = document.getElementById("wf-setting-description").value.trim(); state.definition.settings = { timeout_seconds: Number(document.getElementById("wf-setting-timeout").value), max_steps: Number(document.getElementById("wf-setting-steps").value) }; state.definition.inputs = readFields("wf-inputs"); state.definition.outputs = readFields("wf-outputs"); state.definition.triggers = readTriggers(); changed();
        });
    }

    function managePlatformTemplate(item) {
        var targetId = workflowId(item || state.current), encodedId = encodeURIComponent(targetId), body = document.getElementById("workflow-panel-body");
        function loadManagement() {
            return request(api("/" + encodedId)).then(function (current) {
                if (state.current && workflowId(state.current) === targetId) {
                    state.current = current; state.definition = workflowDefinition(current);
                    state.savedVersion = state.dirtyVersion; document.getElementById("workflow-name").value = state.definition.name || current.name || ""; renderCanvas();
                }
                var versions = current.versions || [];
                body.innerHTML = '<h4 class="workflow-section-title">平台模板历史版本</h4><table class="workflow-panel-table"><tbody>' + versions.map(function (version) { return '<tr><td>v' + version.revision + '</td><td>' + escapeHtml(version.lifecycle || "") + '</td><td>' + escapeHtml(version.published_at || version.created_at || "") + '</td><td><button data-platform-manage-action="rollback" data-version="' + version.revision + '">回滚</button></td></tr>'; }).join("") + '</tbody></table>';
                return current;
            });
        }
        return report("加载平台模板版本", queueSave().then(loadManagement)).then(function () {
            var panelPromise = openPanel("平台模板管理", body.innerHTML, "关闭");
            body.onclick = function (event) { var button = event.target.closest("[data-platform-manage-action]"); if (!button || button.disabled) return; var call = showConfirm("回滚会将指定历史版本重新发布，是否继续？").then(function (yes) { return yes ? request(api("/" + encodedId + "/rollback"), jsonOptions("POST", { version: Number(button.getAttribute("data-version")) })) : null; }); busy(button, report("平台模板回滚", call).then(function (result) { if (!result) return; showToast("平台模板已回滚", "success"); return loadManagement(); }), "处理中…").catch(function () {}); };
            return panelPromise.finally(function () { body.onclick = null; return load(); });
        });
    }

    function manageWorkflow(item) {
        if (mode === "platform") return managePlatformTemplate(item);
        var targetId = workflowId(item || state.current), encodedId = encodeURIComponent(targetId), body = document.getElementById("workflow-panel-body");
        function loadManagement() {
            return Promise.all([
                request(api("/workflows/" + encodedId)),
                request(api("/workflows/" + encodedId + "/access-tokens")),
                request(api("/credentials"))
            ]).then(function (values) {
                var current = values[0], versions = current.versions || [], bindings = current.trigger_bindings || [], tokens = values[1].items || [];
                var credentials = (values[2].items || []).filter(function (credential) { return credential.resource_type === "workflow_http"; });
                if (state.current && workflowId(state.current) === targetId) {
                    state.current = current;
                    state.definition = workflowDefinition(current);
                    state.savedVersion = state.dirtyVersion;
                    document.getElementById("workflow-name").value = state.definition.name || current.name || "";
                    renderCanvas();
                }
                var html = '<div class="workflow-card-actions"><button data-manage-action="' + (current.status === "published" ? "unpublish" : "publish") + '">' + (current.status === "published" ? "停用" : "发布") + '</button></div><h4 class="workflow-section-title">版本</h4><table class="workflow-panel-table"><tbody>' + versions.map(function (version) { return '<tr><td>v' + version.version + '</td><td>' + escapeHtml(version.published_at || "") + '</td><td><button data-manage-action="rollback" data-version="' + version.version + '">回滚</button></td></tr>'; }).join("") + '</tbody></table>';
                html += '<h4 class="workflow-section-title">访问令牌 <button data-manage-action="new-token">签发</button></h4><table class="workflow-panel-table"><tbody>' + tokens.map(function (token) { return '<tr><td>' + escapeHtml(token.label || token.token_id) + '</td><td>' + escapeHtml(token.revoked_at ? "已撤销" : "有效") + '</td><td><button data-manage-action="revoke-token" data-token="' + escapeHtml(token.token_id) + '"' + (token.revoked_at ? " disabled" : "") + '>撤销</button></td></tr>'; }).join("") + '</tbody></table>';
                html += '<h4 class="workflow-section-title">Webhook</h4><table class="workflow-panel-table"><tbody>' + bindings.filter(function (binding) { return binding.trigger_type === "webhook"; }).map(function (binding) { return '<tr><td>' + escapeHtml(binding.trigger_key) + '</td><td>' + (binding.enabled ? "已启用" : "未签发") + '</td><td><button data-manage-action="webhook-secret" data-trigger="' + escapeHtml(binding.trigger_id) + '">签发/轮换</button> <button data-manage-action="webhook-revoke" data-trigger="' + escapeHtml(binding.trigger_id) + '"' + (binding.enabled ? "" : " disabled") + '>撤销</button></td></tr>'; }).join("") + '</tbody></table>';
                html += '<h4 class="workflow-section-title">HTTP 凭据 <button data-manage-action="new-credential">新增/更新</button></h4><table class="workflow-panel-table"><tbody>' + credentials.map(function (credential) { return '<tr><td>' + escapeHtml(credential.credential_id) + '</td><td>' + escapeHtml(credential.label || "") + '</td><td><button data-manage-action="delete-credential" data-credential="' + escapeHtml(credential.credential_id) + '">删除</button></td></tr>'; }).join("") + '</tbody></table>';
                body.innerHTML = html;
                return current;
            });
        }
        var before = state.current && workflowId(state.current) === targetId ? queueSave() : Promise.resolve();
        return report("加载工作流管理信息", before.then(loadManagement)).then(function () {
            var panelPromise = openPanel("工作流管理", body.innerHTML, "关闭");
            body.onclick = function (event) { var button = event.target.closest("[data-manage-action]"); if (!button || button.disabled) return; var action = button.getAttribute("data-manage-action"), call;
                if (action === "publish") call = request(api("/workflows/" + encodedId + "/publish"), { method: "POST" });
                else if (action === "unpublish") call = showConfirm("停用后新的运行将被拒绝，是否继续？").then(function (yes) { return yes ? request(api("/workflows/" + encodedId + "/unpublish"), { method: "POST" }) : null; });
                else if (action === "rollback") call = showConfirm("回滚会创建并发布一个新版本，是否继续？").then(function (yes) { return yes ? request(api("/workflows/" + encodedId + "/rollback"), jsonOptions("POST", { version: Number(button.getAttribute("data-version")) })) : null; });
                else if (action === "new-token") call = showFormDialog({ title: "签发访问令牌", fields: [{ name: "label", label: "标签", required: true }] }).then(function (value) { return value && request(api("/workflows/" + encodedId + "/access-tokens"), jsonOptions("POST", { label: value.label })); }).then(function (token) { if (token) { showNoticeDialog("访问令牌仅展示一次", token.token); return token; } });
                else if (action === "revoke-token") call = showConfirm("撤销后使用该令牌的调用会立即失败，是否继续？").then(function (yes) { return yes ? request(api("/workflows/" + encodedId + "/access-tokens/" + encodeURIComponent(button.getAttribute("data-token"))), { method: "DELETE" }) : null; });
                else if (action === "webhook-secret") call = request(api("/workflows/" + encodedId + "/webhook-triggers/" + encodeURIComponent(button.getAttribute("data-trigger")) + "/secret"), { method: "POST" }).then(function (secret) { showNoticeDialog("Webhook 密钥仅展示一次", secret.token); return secret; });
                else if (action === "webhook-revoke") call = showConfirm("撤销后该 Webhook 地址会立即失效，是否继续？").then(function (yes) { return yes ? request(api("/workflows/" + encodedId + "/webhook-triggers/" + encodeURIComponent(button.getAttribute("data-trigger")) + "/secret"), { method: "DELETE" }) : null; });
                else if (action === "new-credential") call = showFormDialog({ title: "HTTP 凭据", fields: [{ name: "id", label: "凭据编号", required: true }, { name: "label", label: "标签" }, { name: "secret", label: "密钥或 JSON 请求头", type: "textarea", required: true }] }).then(function (value) { return value && request(api("/workflow-http-credentials/" + encodeURIComponent(value.id)), jsonOptions("PUT", { label: value.label, secret: value.secret })); });
                else if (action === "delete-credential") call = showConfirm("删除后引用该凭据的 HTTP 节点将无法运行，是否继续？").then(function (yes) { return yes ? request(api("/credentials/" + encodeURIComponent(button.getAttribute("data-credential"))), { method: "DELETE" }) : null; });
                if (call) busy(button, report("管理操作", call).then(function (result) { if (!result) return; showToast("操作成功", "success"); return loadManagement(); }), "处理中…").catch(function () {});
            };
            return panelPromise.finally(function () { body.onclick = null; return load(); });
        });
    }

    function resolveWaitWithInput(wait) {
        var fields = ((wait.payload || {}).fields || []).map(function (field) {
            var workflowType = field.type || "string", type = "text", options;
            if (workflowType === "number" || workflowType === "integer") type = "number";
            else if (workflowType === "boolean") { type = "select"; options = [{ value: "true", label: "是" }, { value: "false", label: "否" }]; }
            else if (workflowType === "object" || workflowType === "array") type = "textarea";
            return { name: field.key, label: field.label || field.key, type: type, options: options, workflowType: workflowType, required: !!field.required };
        });
        if (!fields.length) fields = [{ name: "response", label: "输入 JSON", type: "textarea", value: "{}", workflowType: "object", required: true }];
        return showFormDialog({ title: (wait.payload || {}).title || "补充输入", fields: fields }).then(function (value) {
            if (!value) return; var response = {};
            if (fields.length === 1 && fields[0].name === "response") { try { response = JSON.parse(value.response); } catch (error) { showToast("补充输入必须是 JSON 对象", "error"); return; } }
            else {
                for (var index = 0; index < fields.length; index += 1) {
                    var field = fields[index], raw = value[field.name];
                    if (raw === "" && !field.required) continue;
                    if (field.workflowType === "number" || field.workflowType === "integer") {
                        raw = Number(raw);
                        if (!Number.isFinite(raw) || (field.workflowType === "integer" && !Number.isInteger(raw))) { showToast(field.label + "必须是" + (field.workflowType === "integer" ? "整数" : "数字"), "error"); return; }
                    } else if (field.workflowType === "boolean") raw = raw === "true";
                    else if (field.workflowType === "object" || field.workflowType === "array") {
                        try { raw = JSON.parse(raw); } catch (error) { showToast(field.label + "必须是有效 JSON", "error"); return; }
                        if ((field.workflowType === "object" && (!raw || Array.isArray(raw) || typeof raw !== "object")) || (field.workflowType === "array" && !Array.isArray(raw))) { showToast(field.label + "的 JSON 类型不正确", "error"); return; }
                    }
                    response[field.name] = raw;
                }
            }
            return request(api("/workflow-waits/" + encodeURIComponent(wait.wait_id) + "/resolve"), jsonOptions("POST", { status: "resolved", response: response })).then(loadWaits);
        });
    }

    function loadRuns() { return report("加载运行记录", request(api("/workflow-runs"))).then(function (data) { document.getElementById("workflow-runs-body").innerHTML = (data.items || []).map(function (run) { var active = ["queued", "running", "waiting", "needs_attention"].indexOf(run.status) >= 0; return '<tr><td>' + escapeHtml(run.created_at || "") + '</td><td>' + escapeHtml(run.workflow_id) + '</td><td>' + escapeHtml(run.trigger_type) + '</td><td>' + escapeHtml(statusText(run.status)) + '</td><td><button data-action="run-detail" data-id="' + escapeHtml(run.run_id) + '">详情</button>' + (active ? ' <button data-action="run-cancel" data-id="' + escapeHtml(run.run_id) + '">取消</button>' : "") + (run.status === "needs_attention" && canManageOrganization() ? ' <button data-action="run-attention" data-id="' + escapeHtml(run.run_id) + '">处置</button>' : "") + '</td></tr>'; }).join("") || '<tr><td colspan="5">暂无运行记录</td></tr>'; }); }
    function loadWaits() { return report("加载工作流待办", request(api("/workflow-waits"))).then(function (data) { state.waits = data.items || []; document.getElementById("workflow-waits-list").innerHTML = state.waits.map(function (wait) { var type = wait.wait_type, actions = "", countdown = type === "delay" ? ' · 剩余 <span data-wait-countdown="' + escapeHtml(wait.expires_at || "") + '"></span>' : ""; if (type === "approval") actions = '<button data-action="wait-approve" data-id="' + escapeHtml(wait.wait_id) + '">通过</button><button data-action="wait-reject" data-id="' + escapeHtml(wait.wait_id) + '">拒绝</button>'; else if (type === "input") actions = '<button data-action="wait-input" data-id="' + escapeHtml(wait.wait_id) + '">填写</button>'; return '<article class="workflow-card workflow-wait-card"><h3>' + escapeHtml((wait.payload || {}).title || (type === "delay" ? "延迟等待" : "工作流待办")) + '</h3><p>类型：' + escapeHtml(type) + ' · 截止：' + escapeHtml(wait.expires_at || "无") + countdown + '</p><div class="workflow-card-actions">' + actions + '</div></article>'; }).join("") || '<div class="organization-empty">暂无待办</div>'; updateWaitCountdowns(); }); }
    function updateWaitCountdowns() { document.querySelectorAll("[data-wait-countdown]").forEach(function (element) { var seconds = Math.max(0, Math.ceil((Date.parse(element.getAttribute("data-wait-countdown")) - Date.now()) / 1000)); element.textContent = seconds ? seconds + " 秒" : "即将恢复"; }); }

    function startNodePointer(event, nodeEl) {
        if (event.button !== 0 || event.target.closest(".workflow-port")) return;
        if (state.connecting && !state.pointer) { event.preventDefault(); connect(state.connecting.source, state.connecting.port, nodeEl.getAttribute("data-node-id")); return; }
        var id = nodeEl.getAttribute("data-node-id"), multi = event.shiftKey || event.metaKey || event.ctrlKey;
        if (multi) { if (state.selectedNodes.indexOf(id) >= 0) state.selectedNodes = state.selectedNodes.filter(function (value) { return value !== id; }); else state.selectedNodes.push(id); }
        else if (state.selectedNodes.indexOf(id) < 0) state.selectedNodes = [id];
        state.selectedEdge = "";
        nodesHost.querySelectorAll(".workflow-node").forEach(function (item) {
            var selected = state.selectedNodes.indexOf(item.getAttribute("data-node-id")) >= 0;
            item.classList.toggle("selected", selected && state.selectedNodes.length === 1);
            item.classList.toggle("multi-selected", selected && state.selectedNodes.length > 1);
        });
        renderProperties(); if (!canEditWorkflow()) return; nodeEl.setPointerCapture(event.pointerId);
        var originals = {}; state.selectedNodes.forEach(function (nodeId) { originals[nodeId] = clone(nodeById(nodeId).position); });
        state.pointer = { kind: "nodes", pointerId: event.pointerId, startX: event.clientX, startY: event.clientY, originals: originals, moved: false, collapseTo: !multi && state.selectedNodes.length > 1 ? id : "", capture: nodeEl };
    }
    function startCanvasPointer(event) {
        if (event.target.closest(".workflow-node,.workflow-edge-hit,.workflow-canvas-tools") || (event.button !== 0 && event.button !== 1)) return;
        canvas.focus(); var pan = event.button === 1 || state.spaceDown;
        canvas.setPointerCapture(event.pointerId);
        if (pan) state.pointer = { kind: "pan", pointerId: event.pointerId, startX: event.clientX, startY: event.clientY, panX: state.panX, panY: state.panY, capture: canvas };
        else { var point = canvasPoint(event.clientX, event.clientY); state.selectedNodes = []; state.selectedEdge = ""; state.pointer = { kind: "select", pointerId: event.pointerId, start: point, current: point, capture: canvas }; selectionBox.hidden = false; updateSelectionBox(point, point); renderProperties(); }
    }
    function updateSelectionBox(a, b) { selectionBox.style.left = Math.min(a.x, b.x) + "px"; selectionBox.style.top = Math.min(a.y, b.y) + "px"; selectionBox.style.width = Math.abs(a.x - b.x) + "px"; selectionBox.style.height = Math.abs(a.y - b.y) + "px"; }
    function onPointerMove(event) {
        var pointer = state.pointer; if (!pointer || pointer.pointerId !== event.pointerId) return;
        if (pointer.kind === "nodes") { var dx = (event.clientX - pointer.startX) / state.zoom, dy = (event.clientY - pointer.startY) / state.zoom; if (Math.abs(dx) + Math.abs(dy) > 2) pointer.moved = true; state.selectedNodes.forEach(function (id) { var node = nodeById(id), original = pointer.originals[id]; node.position.x = Math.round((Number(original.x) + dx) / 20) * 20; node.position.y = Math.round((Number(original.y) + dy) / 20) * 20; var el = nodesHost.querySelector('[data-node-id="' + CSS.escape(id) + '"]'); if (el) { el.style.left = node.position.x + "px"; el.style.top = node.position.y + "px"; } }); drawEdges(); }
        else if (pointer.kind === "pan") { state.panX = pointer.panX + event.clientX - pointer.startX; state.panY = pointer.panY + event.clientY - pointer.startY; worldTransform(); }
        else if (pointer.kind === "select") { pointer.current = canvasPoint(event.clientX, event.clientY); updateSelectionBox(pointer.start, pointer.current); }
        else if (pointer.kind === "connect") { var point = canvasPoint(event.clientX, event.clientY), source = nodeById(pointer.source), x1 = Number(source.position.x) + 170, y1 = Number(source.position.y) + portY(source, pointer.port), bend = Math.max(60, Math.abs(point.x - x1) / 2); drawEdges("M " + x1 + " " + y1 + " C " + (x1 + bend) + " " + y1 + ", " + (point.x - bend) + " " + point.y + ", " + point.x + " " + point.y); }
    }
    function onPointerUp(event) {
        var pointer = state.pointer; if (!pointer || pointer.pointerId !== event.pointerId) return; state.pointer = null;
        try { pointer.capture.releasePointerCapture(event.pointerId); } catch (error) {}
        if (pointer.kind === "nodes" && pointer.moved) { state.undo.push({ __positions: pointer.originals }); if (state.undo.length > 80) state.undo.shift(); state.redo = []; changed(); updateHistoryButtons(); }
        else if (pointer.kind === "nodes" && pointer.collapseTo) { state.selectedNodes = [pointer.collapseTo]; renderCanvas(); }
        else if (pointer.kind === "select") { selectionBox.hidden = true; var a = pointer.start, b = pointer.current, left = Math.min(a.x, b.x), right = Math.max(a.x, b.x), top = Math.min(a.y, b.y), bottom = Math.max(a.y, b.y); state.selectedNodes = state.definition.nodes.filter(function (node) { return Number(node.position.x) < right && Number(node.position.x) + 170 > left && Number(node.position.y) < bottom && Number(node.position.y) + nodeHeight(node) > top; }).map(function (node) { return node.id; }); renderCanvas(); }
        else if (pointer.kind === "connect") { var targetEl = document.elementFromPoint(event.clientX, event.clientY); var port = targetEl && targetEl.closest(".workflow-port.in"); var targetNode = port && port.closest(".workflow-node"); drawEdges(); if (targetNode) connect(pointer.source, pointer.port, targetNode.getAttribute("data-node-id")); else { state.connecting = null; document.getElementById("workflow-connect-hint").textContent = ""; } }
    }

    document.getElementById("workflow-new").onclick = function () { var button = this, templates = mode === "organization" ? request("/api/v2/catalog/workflows").catch(function () { return { items: [] }; }) : Promise.resolve({ items: [] }); busy(button, templates.then(function (catalog) { var fields = []; if (mode === "organization") fields.push({ name: "template", label: "创建来源", type: "select", options: [{ value: "", label: "空白工作流" }].concat((catalog.items || []).map(function (item) { return { value: item.resource_id, label: (item.payload || {}).name || item.resource_id }; })) }); fields.push({ name: "id", label: "工作流 ID", required: true }, { name: "name", label: "名称", required: true }); return showFormDialog({ title: mode === "platform" ? "新建工作流模板" : "新建组织工作流", fields: fields }); }).then(function (value) { if (!value) return; var definition = emptyDefinition(value.name), call; if (mode === "organization" && value.template) call = request(api("/workflow-templates/" + encodeURIComponent(value.template) + "/copy"), jsonOptions("POST", { id: value.id, name: value.name })); else if (mode === "organization") call = request(api("/workflows"), jsonOptions("POST", { id: value.id, name: value.name, definition: definition })); else call = request(api("/" + encodeURIComponent(value.id) + "/draft"), jsonOptions("PUT", { definition: definition })); return report("新建工作流", call).then(function (item) { state.items.unshift(item); renderList(); return openItem(workflowId(item)); }); }), "创建中…").catch(function () {}); };
    document.getElementById("workflow-refresh").onclick = function () { busy(this, report("刷新", editor.hidden ? load() : reloadCurrent()), "刷新中…").catch(function () {}); };
    document.getElementById("workflow-reload").onclick = function () { busy(this, report("重新加载", reloadCurrent()), "加载中…").catch(function () {}); };
    document.getElementById("workflow-back").onclick = function () { busy(this, report("返回", closeEditor()), "保存中…").catch(function () {}); };
    document.getElementById("workflow-publish").onclick = function () { publish(this).catch(function () {}); };
    document.getElementById("workflow-validate").onclick = function () { validateCurrent(this).catch(function () {}); };
    document.getElementById("workflow-ai").onclick = function () { aiDesign(this).catch(function () {}); };
    document.getElementById("workflow-settings").onclick = function () { workflowSettings().catch(function (error) { showToast("打开流程设置失败：" + (error.message || "未知错误"), "error"); }); };
    var testButton = document.getElementById("workflow-test"); if (testButton) testButton.onclick = function () { testRun(this).catch(function () {}); };
    var manageButton = document.getElementById("workflow-manage"); if (manageButton) manageButton.onclick = function () { manageWorkflow(state.current).catch(function () {}); };
    document.getElementById("workflow-name").oninput = changed;
    document.getElementById("workflow-node-search").oninput = function () { renderCatalog(this.value); };
    document.getElementById("workflow-node-name").onchange = function () { var node = selectedNode(); if (!node) return; pushUndo(); node.name = this.value; renderCanvas(); changed(); };
    document.getElementById("workflow-node-fields").addEventListener("change", readStructuredFields);
    document.getElementById("workflow-variable-insert").onclick = function () { var picker = document.getElementById("workflow-variable-picker"), target = document.activeElement; if (!target || !target.matches("#workflow-node-fields input,#workflow-node-fields textarea")) target = document.querySelector("#workflow-node-fields textarea,#workflow-node-fields input"); if (!target || !picker.value) return; var token = "{{" + picker.value + "}}", start = target.selectionStart === null ? target.value.length : target.selectionStart, end = target.selectionEnd === null ? start : target.selectionEnd; target.value = target.value.slice(0, start) + token + target.value.slice(end); target.dispatchEvent(new Event("change", { bubbles: true })); };
    document.getElementById("workflow-node-config").onchange = function () { var node = selectedNode(); if (!node) return; try { var value = JSON.parse(this.value || "{}"); if (!value || Array.isArray(value) || typeof value !== "object") throw new Error("必须是对象"); pushUndo(); node.config = value; document.getElementById("workflow-json-error").hidden = true; renderCanvas(); changed(); } catch (error) { var element = document.getElementById("workflow-json-error"); element.textContent = "配置必须是有效 JSON 对象：" + error.message; element.hidden = false; } };
    document.querySelectorAll("[data-config-tab]").forEach(function (button) { button.onclick = function () { document.querySelectorAll("[data-config-tab]").forEach(function (item) { item.classList.toggle("active", item === button); }); var json = button.getAttribute("data-config-tab") === "json"; document.getElementById("workflow-node-fields").hidden = json; document.getElementById("workflow-node-json").hidden = !json; }; });
    document.getElementById("workflow-error-mode").onchange = function () { var node = selectedNode(); if (!node) return; pushUndo(); node.error_policy = { mode: this.value, max_retries: this.value === "retry" ? Math.max(1, Math.min(3, Number(document.getElementById("workflow-max-retries").value || 3))) : 0 }; renderCanvas(); changed(); };
    document.getElementById("workflow-max-retries").onchange = function () { var node = selectedNode(); if (!node || (node.error_policy || {}).mode !== "retry") return; pushUndo(); node.error_policy.max_retries = Math.max(1, Math.min(3, Number(this.value || 1))); this.value = node.error_policy.max_retries; changed(); };
    document.getElementById("workflow-delete-node").onclick = deleteSelection; document.getElementById("workflow-delete-edge").onclick = deleteSelection;
    document.getElementById("workflow-connect").onclick = function () { var node = selectedNode(); if (!node) return; var ports = outputPorts(node); if (!ports.length) return; state.connecting = { source: node.id, port: ports[0].key }; document.getElementById("workflow-connect-hint").textContent = "请从节点输出端口拖到目标输入端口"; };
    document.getElementById("workflow-property-close").onclick = function () { state.selectedNodes = []; state.selectedEdge = ""; renderCanvas(); };
    document.getElementById("workflow-undo").onclick = function () { if (!state.undo.length) return; var item = state.undo.pop(); state.redo.push(clone(state.definition)); if (item.__positions) Object.keys(item.__positions).forEach(function (id) { if (nodeById(id)) nodeById(id).position = item.__positions[id]; }); else state.definition = item; state.selectedNodes = []; state.selectedEdge = ""; renderCanvas(); changed(); };
    document.getElementById("workflow-redo").onclick = function () { if (!state.redo.length) return; state.undo.push(clone(state.definition)); state.definition = state.redo.pop(); state.selectedNodes = []; state.selectedEdge = ""; renderCanvas(); changed(); };
    document.getElementById("workflow-zoom-in").onclick = function () { state.zoom = Math.min(1.8, state.zoom + .1); worldTransform(); }; document.getElementById("workflow-zoom-out").onclick = function () { state.zoom = Math.max(.35, state.zoom - .1); worldTransform(); }; document.getElementById("workflow-fit").onclick = fitCanvas; document.getElementById("workflow-debug-close").onclick = function () { document.getElementById("workflow-debug").hidden = true; };

    document.getElementById("workflow-node-catalog").addEventListener("dragstart", function (event) { var button = event.target.closest("[data-node-type]"); if (button) event.dataTransfer.setData("text/workflow-node", button.getAttribute("data-node-type")); });
    document.getElementById("workflow-node-catalog").addEventListener("click", function (event) { var button = event.target.closest("[data-node-type]"); if (!button) return; var rect = canvas.getBoundingClientRect(); addNode(button.getAttribute("data-node-type"), (rect.width / 2 - state.panX) / state.zoom - 85, (rect.height / 2 - state.panY) / state.zoom - 40); });
    canvas.addEventListener("dragover", function (event) { event.preventDefault(); }); canvas.addEventListener("drop", function (event) { event.preventDefault(); var type = event.dataTransfer.getData("text/workflow-node"); if (!type) return; var point = canvasPoint(event.clientX, event.clientY); addNode(type, point.x, point.y); });
    nodesHost.addEventListener("pointerdown", function (event) { var port = event.target.closest(".workflow-port.out"); if (port) { event.stopPropagation(); if (!canEditWorkflow()) return; var nodeEl = port.closest(".workflow-node"); port.setPointerCapture(event.pointerId); state.pointer = { kind: "connect", pointerId: event.pointerId, source: nodeEl.getAttribute("data-node-id"), port: port.getAttribute("data-port"), capture: port }; state.connecting = { source: state.pointer.source, port: state.pointer.port }; document.getElementById("workflow-connect-hint").textContent = "拖到目标节点的输入端口"; return; } var nodeEl = event.target.closest(".workflow-node"); if (nodeEl) startNodePointer(event, nodeEl); });
    canvas.addEventListener("pointerdown", startCanvasPointer); window.addEventListener("pointermove", onPointerMove); window.addEventListener("pointerup", onPointerUp); window.addEventListener("pointercancel", onPointerUp);
    edgesSvg.addEventListener("pointerdown", function (event) { var hit = event.target.closest(".workflow-edge-hit"); if (!hit) return; event.stopPropagation(); state.selectedEdge = hit.getAttribute("data-edge-id"); state.selectedNodes = []; renderCanvas(); canvas.focus(); });
    canvas.addEventListener("wheel", function (event) { if (!state.definition) return; event.preventDefault(); var rect = canvas.getBoundingClientRect(), before = canvasPoint(event.clientX, event.clientY), next = Math.max(.35, Math.min(1.8, state.zoom * (event.deltaY < 0 ? 1.1 : .9))); state.zoom = next; state.panX = event.clientX - rect.left - before.x * next; state.panY = event.clientY - rect.top - before.y * next; worldTransform(); }, { passive: false });

    root.addEventListener("click", function (event) { var button = event.target.closest("[data-action]"); if (!button || button.disabled) return; var action = button.getAttribute("data-action"), id = button.getAttribute("data-id"), item = findItem(id), call;
        if (action === "edit") call = openItem(id);
        else if (action === "delete") call = showConfirm("确认删除平台工作流模板？").then(function (yes) { return yes && request("/api/v2/platform/catalog/workflows/" + encodeURIComponent(id), { method: "DELETE" }).then(load); });
        else if (action === "archive") call = showConfirm("归档后不会删除历史运行，是否继续？").then(function (yes) { return yes && request(api("/workflows/" + encodeURIComponent(id) + "/archive"), { method: "POST" }).then(load); });
        else if (action === "run") call = openItem(id).then(function () { return runPublished(document.getElementById("workflow-test")); });
        else if (action === "manage") call = manageWorkflow(item);
        else if (action === "run-detail") call = request(api("/workflow-runs/" + encodeURIComponent(id))).then(function (run) { return request(api("/workflow-runs/" + encodeURIComponent(id) + "/events")).catch(function () { return { items: [] }; }).then(function (events) { return openPanel("运行详情", '<pre class="workflow-json-block">' + escapeHtml(JSON.stringify({ run: run, events: events.items }, null, 2)) + '</pre>', "关闭"); }); });
        else if (action === "run-cancel") call = showConfirm("确认取消该工作流运行？").then(function (yes) { return yes && request(api("/workflow-runs/" + encodeURIComponent(id) + "/cancel"), { method: "POST" }).then(loadRuns); });
        else if (action === "run-attention") call = showFormDialog({ title: "异常运行处置", fields: [{ name: "action", label: "操作", type: "select", options: [{ value: "retry", label: "重试" }, { value: "skip", label: "跳过" }, { value: "terminate", label: "终止" }] }, { name: "comment", label: "备注" }] }).then(function (value) { return value && request(api("/workflow-runs/" + encodeURIComponent(id) + "/attention"), jsonOptions("POST", value)).then(loadRuns); });
        else if (action === "wait-approve" || action === "wait-reject") call = showFormDialog({ title: action === "wait-approve" ? "通过审批" : "拒绝审批", fields: [{ name: "comment", label: "备注" }] }).then(function (value) { return value && request(api("/workflow-waits/" + encodeURIComponent(id) + "/resolve"), jsonOptions("POST", { status: action === "wait-approve" ? "approved" : "rejected", comment: value.comment })).then(loadWaits); });
        else if (action === "wait-input") call = resolveWaitWithInput((state.waits || []).filter(function (wait) { return wait.wait_id === id; })[0]);
        if (call) busy(button, report("操作", call), "处理中…").catch(function () {});
    });
    document.querySelectorAll("[data-workflow-tab]").forEach(function (button) { button.onclick = function () { document.querySelectorAll("[data-workflow-tab]").forEach(function (item) { item.classList.toggle("active", item === button); }); var tab = button.getAttribute("data-workflow-tab"); list.hidden = tab !== "flows"; document.getElementById("workflow-runs").hidden = tab !== "runs"; document.getElementById("workflow-waits").hidden = tab !== "waits"; if (tab === "runs") loadRuns().catch(function () {}); if (tab === "waits") loadWaits().catch(function () {}); }; });

    window.addEventListener("keydown", function (event) {
        if (event.key === " ") state.spaceDown = true;
        if (editor.hidden || isEditableTarget(event.target)) return;
        var shortcut = event.metaKey || event.ctrlKey, key = event.key.toLowerCase();
        var editable = canEditWorkflow();
        if (shortcut && key === "z") { event.preventDefault(); document.getElementById(event.shiftKey ? "workflow-redo" : "workflow-undo").click(); }
        else if (shortcut && key === "y") { event.preventDefault(); document.getElementById("workflow-redo").click(); }
        else if (shortcut && key === "a") { event.preventDefault(); state.selectedNodes = state.definition.nodes.map(function (node) { return node.id; }); state.selectedEdge = ""; renderCanvas(); }
        else if (shortcut && key === "c" && state.selectedNodes.length) { event.preventDefault(); var selected = {}; state.copiedNodes = state.selectedNodes.map(nodeById).filter(function (node) { return node && node.type !== "start" && node.type !== "end"; }).map(function (node) { selected[node.id] = true; return clone(node); }); state.copiedEdges = state.definition.edges.filter(function (edge) { return selected[edge.source] && selected[edge.target]; }).map(clone); }
        else if (shortcut && key === "v" && editable && state.copiedNodes.length) { event.preventDefault(); pushUndo(); var idMap = {}; state.selectedNodes = []; state.copiedNodes.forEach(function (original) { var copied = clone(original), base = copied.id + "_copy", id = base, index = 2; while (nodeById(id) || Object.values(idMap).indexOf(id) >= 0) id = base + index++; idMap[original.id] = id; copied.id = id; copied.name += " 副本"; copied.position.x += 40; copied.position.y += 40; state.definition.nodes.push(copied); state.selectedNodes.push(id); }); state.copiedEdges.forEach(function (edge, index) { if (!idMap[edge.source] || !idMap[edge.target]) return; var copied = clone(edge); copied.id = "edge_copy_" + Date.now() + "_" + index; copied.source = idMap[edge.source]; copied.target = idMap[edge.target]; state.definition.edges.push(copied); }); renderCanvas(); changed(); }
        else if (event.key === "Delete" || event.key === "Backspace") { event.preventDefault(); deleteSelection(); }
        else if (event.key === "Escape") { state.connecting = null; state.selectedNodes = []; state.selectedEdge = ""; document.getElementById("workflow-connect-hint").textContent = ""; renderCanvas(); }
        else if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].indexOf(event.key) >= 0 && editable && state.selectedNodes.length) { event.preventDefault(); pushUndo(); var amount = event.shiftKey ? 20 : 1; state.selectedNodes.forEach(function (id) { var node = nodeById(id); if (event.key === "ArrowLeft") node.position.x -= amount; if (event.key === "ArrowRight") node.position.x += amount; if (event.key === "ArrowUp") node.position.y -= amount; if (event.key === "ArrowDown") node.position.y += amount; }); renderCanvas(); changed(); }
    });
    window.addEventListener("keyup", function (event) { if (event.key === " ") state.spaceDown = false; });
    window.setInterval(updateWaitCountdowns, 1000);

    runScopedModule("workflows", function () { return Promise.all([loadCatalog(), loadEditorOptions(), load()]); });
}
