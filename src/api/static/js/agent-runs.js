/* Organization-wide durable task management, including schedules and channels. */
(function () {
    var host = document.getElementById("agent-runs-list");
    var signature = "";
    var labels = { queued: "等待执行", running: "正在执行", stopping: "正在停止", waiting_approval: "等待确认", waiting_input: "需要补充信息", paused: "已暂停", succeeded: "已完成", failed: "执行失败", cancelled: "已停止" };
    var sources = { web: "网页对话", schedule: "定时任务", workflow: "工作流", wechat: "微信", feishu: "飞书", wecom: "企业微信", internal: "内部任务" };
    function request(url, data) {
        return fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data || {}) })
            .then(function (r) { return r.json().then(function (body) { if (!r.ok) throw new Error(body.detail || "操作失败"); return body; }); })
            .then(function () { signature = ""; refresh(); }).catch(function (error) { showToast(error.message, "error"); });
    }
    function refresh() {
        var organization = activeOrganizationId();
        if (!organization || document.hidden) return;
        fetch("/api/agent-runs?organization_id=" + encodeURIComponent(organization))
            .then(function (r) { if (!r.ok) throw new Error("读取任务失败"); return r.json(); })
            .then(function (data) {
                var next = JSON.stringify(data.items);
                if (next === signature || (host.contains(document.activeElement) && document.activeElement.tagName === "TEXTAREA")) return;
                signature = next; host.replaceChildren();
                if (!data.items.length) { host.textContent = "当前组织暂无执行任务。"; return; }
                data.items.forEach(function (run) {
                    var card = document.createElement("section"); card.className = "agent-run-panel";
                    var title = document.createElement("strong"); title.textContent = (labels[run.status] || run.status) + " · " + run.agent_id + " · " + (sources[run.source] || run.source);
                    card.appendChild(title);
                    var description = document.createElement("p"); description.textContent = ["succeeded", "failed", "cancelled"].indexOf(run.status) !== -1 ? (run.answer || run.reason || labels[run.status]) : (run.reason || run.input_question || "任务会在后台继续执行。"); card.appendChild(description);
                    var meta = document.createElement("small"); meta.textContent = "任务编号：" + run.run_id + " · 已执行 " + run.tool_calls + " 项操作"; card.appendChild(meta);
                    var base = "/api/agent-runs/" + encodeURIComponent(run.run_id);
                    var actions = document.createElement("div"); actions.className = "agent-run-actions";
                    function button(label, action) { var btn = document.createElement("button"); btn.type = "button"; btn.className = "btn-secondary"; btn.textContent = label; btn.onclick = action; actions.appendChild(btn); }
                    if (run.approval && run.status === "waiting_approval") {
                        var preview = document.createElement("pre"); preview.className = "agent-run-preview";
                        preview.textContent = run.approval.calls.map(function (call) { return call.preview; }).join("\n\n"); card.appendChild(preview);
                        button("同意执行", function () { return request(base + "/approvals", { approval_id: run.approval.approval_id, approved: true }); });
                        button("拒绝", function () { return request(base + "/approvals", { approval_id: run.approval.approval_id, approved: false }); });
                    }
                    if (["paused", "waiting_input"].indexOf(run.status) !== -1) {
                        if (run.reason_code === "execution_unknown") {
                            var note = document.createElement("p"); note.textContent = "请管理员核对下方操作的实际执行结果。"; card.appendChild(note);
                            var uncertain = document.createElement("pre"); uncertain.className = "agent-run-preview"; uncertain.textContent = JSON.stringify((run.uncertain_step || {}).call || {}, null, 2); card.appendChild(uncertain);
                            var actual = document.createElement("textarea"); actual.placeholder = "填写核对到的实际结果"; actions.appendChild(actual);
                            button("确认已完成", function () { if (!actual.value.trim()) { showToast("请填写实际结果", "error"); return; } return request(base + "/reconcile", { step_key: run.uncertain_step.step_key, ok: true, data: { confirmation: actual.value } }); });
                            button("确认未完成", function () { return request(base + "/reconcile", { step_key: run.uncertain_step.step_key, ok: false, error: actual.value || "管理员确认未完成" }); });
                        } else {
                            var input = document.createElement("textarea"); input.rows = 2; input.placeholder = "补充信息或调整要求"; actions.appendChild(input);
                            button("继续任务", function () { return request(base + "/resume", { user_input: input.value }); });
                        }
                    }
                    if (["succeeded", "failed", "cancelled"].indexOf(run.status) === -1) button("停止任务", function () { return request(base + "/cancel"); });
                    card.appendChild(actions); host.appendChild(card);
                });
            }).catch(function (error) { if (!host.children.length) host.textContent = error.message; });
    }
    document.getElementById("runs-refresh").onclick = refresh;
    (window.BP_CONTEXT_READY || Promise.resolve()).then(refresh);
    var timer = window.setInterval(refresh, 2000);
    window.addEventListener("pagehide", function () { window.clearInterval(timer); });
})();
