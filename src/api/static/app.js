/* ===== Global utilities ===== */

var ICON_COPY = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
var ICON_REGEN = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>';

/* Theme toggle */
document.getElementById("theme-toggle").addEventListener("click", function () {
    var html = document.documentElement;
    var next = html.getAttribute("data-theme") === "dark" ? "light" : "dark";
    html.setAttribute("data-theme", next);
    try { localStorage.setItem("bp-theme", next); } catch (e) {}
});

/* Work-in-progress menu placeholder */
document.addEventListener("click", function (evt) {
    var el = evt.target.closest && evt.target.closest("[data-wip='1']");
    if (!el) return;
    evt.preventDefault();
    var name = el.getAttribute("data-wip-name") || "该功能";
    showToast(name + " 正在开发中，敬请期待", "info");
});

function showToast(message, type) {
    type = type || "info";
    var container = document.getElementById("toast-container");
    if (!container) return;
    var toast = document.createElement("div");
    toast.className = "toast toast-" + type;
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(function () { toast.classList.add("show"); }, 10);
    setTimeout(function () {
        toast.classList.remove("show");
        setTimeout(function () { toast.remove(); }, 300);
    }, 2600);
}

function showConfirm(message) {
    return new Promise(function (resolve) {
        var overlay = document.getElementById("confirm-overlay");
        var okBtn = document.getElementById("confirm-ok");
        var cancelBtn = document.getElementById("confirm-cancel");
        document.getElementById("confirm-message").textContent = message;
        overlay.style.display = "";
        function cleanup(result) {
            overlay.style.display = "none";
            okBtn.removeEventListener("click", onOk);
            cancelBtn.removeEventListener("click", onCancel);
            resolve(result);
        }
        function onOk() { cleanup(true); }
        function onCancel() { cleanup(false); }
        okBtn.addEventListener("click", onOk);
        cancelBtn.addEventListener("click", onCancel);
    });
}

function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
        return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve) {
        var ta = document.createElement("textarea");
        ta.value = text;
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        ta.remove();
        resolve();
    });
}

function escapeHtml(text) {
    var div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
}

/* ===== Chat page ===== */
function initChat() {
    var messagesEl = document.getElementById("chat-messages");
    var form = document.getElementById("chat-form");
    var input = document.getElementById("chat-input");
    var sendBtn = document.getElementById("send-btn");
    var welcome = document.getElementById("welcome-screen");
    var agentSelectBtn = document.getElementById("agent-select-btn");
    var agentSelectLabel = document.getElementById("agent-select-label");
    var allAgents = [];
    var selectedAgentIds = [];
    var convListEl = document.getElementById("conv-list");
    var newConvBtn = document.getElementById("new-conv-btn");
    var activeRegenBtn = null;
    var conversations = [];
    var currentConvId = null;

    loadAgentSelector();
    initConversations();

    function loadAgentSelector() {
        fetch("/api/agents")
            .then(function (r) { return r.json(); })
            .then(function (agents) {
                return fetch("/api/agents/active").then(function (r) {
                    return r.json().then(function (active) {
                        return { agents: agents, activeId: active.id };
                    });
                });
            })
            .then(function (data) {
                allAgents = data.agents;
                selectedAgentIds = data.agents
                    .filter(function (a) { return a.id === data.activeId; })
                    .map(function (a) { return a.id; });
                if (selectedAgentIds.length === 0 && data.agents.length > 0) {
                    selectedAgentIds = [data.agents[0].id];
                }
                renderAgentDropdown();
                updateAgentLabel();
                updateWelcomeScreen();
            });
    }

    function isConversationLocked() {
        return welcome && welcome.style.display === "none";
    }

    function renderAgentDropdown() {
        var listEl = document.getElementById("agent-modal-list");
        var locked = isConversationLocked();
        var displayAgents = locked
            ? allAgents.filter(function (a) { return selectedAgentIds.indexOf(a.id) !== -1; })
            : allAgents;
        listEl.innerHTML = displayAgents.map(function (a) {
            var selected = selectedAgentIds.indexOf(a.id) !== -1 ? " selected" : "";
            var capsHtml = (a.capabilities || []).slice(0, 5).map(function (c) {
                return '<span class="agent-cap-tag">' + escapeHtml(c.name) + '</span>';
            }).join("");
            var checkSvg = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><polyline points="20 6 9 17 4 12"/></svg>';
            var lockedClass = locked ? " locked" : "";
            return '<div class="agent-modal-card' + selected + lockedClass + '" data-agent-id="' + a.id + '">' +
                '<div class="agent-modal-card-header">' +
                    '<div class="agent-modal-card-icon"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2a4 4 0 0 1 4 4v2a4 4 0 0 1-8 0V6a4 4 0 0 1 4-4z"/><path d="M16 14H8a4 4 0 0 0-4 4v2h16v-2a4 4 0 0 0-4-4z"/></svg></div>' +
                    '<div class="agent-modal-card-info">' +
                        '<div class="agent-modal-card-name">' + escapeHtml(a.name) + '</div>' +
                        '<div class="agent-modal-card-role">' + escapeHtml(a.role || "") + '</div>' +
                    '</div>' +
                    (locked ? '' : '<div class="agent-modal-card-check">' + checkSvg + '</div>') +
                '</div>' +
                (a.description ? '<div class="agent-modal-card-desc">' + escapeHtml(a.description) + '</div>' : '') +
                (capsHtml ? '<div class="agent-modal-card-caps">' + capsHtml + '</div>' : '') +
            '</div>';
        }).join("");
    }

    function updateAgentLabel() {
        if (selectedAgentIds.length === 0) {
            agentSelectLabel.textContent = "选择智能体";
        } else if (selectedAgentIds.length === 1) {
            var a = allAgents.filter(function (x) { return x.id === selectedAgentIds[0]; })[0];
            agentSelectLabel.textContent = a ? a.name : "1 个智能体";
        } else {
            agentSelectLabel.textContent = selectedAgentIds.length + " 个智能体";
        }
    }

    var agentModalOverlay = document.getElementById("agent-modal-overlay");
    var agentModalClose = document.getElementById("agent-modal-close");
    var agentModalList = document.getElementById("agent-modal-list");

    agentSelectBtn.addEventListener("click", function (e) {
        e.stopPropagation();
        if (agentSelectBtn.disabled) return;
        renderAgentDropdown();
        var titleEl = agentModalOverlay.querySelector(".modal-header h3");
        titleEl.textContent = isConversationLocked() ? "当前智能体" : "选择智能体";
        agentModalOverlay.style.display = "";
    });

    agentModalClose.addEventListener("click", function () {
        agentModalOverlay.style.display = "none";
    });

    agentModalOverlay.addEventListener("click", function (e) {
        if (e.target === agentModalOverlay) {
            agentModalOverlay.style.display = "none";
        }
    });

    agentModalList.addEventListener("click", function (e) {
        var card = e.target.closest(".agent-modal-card");
        if (!card) return;
        if (isConversationLocked()) return;
        var id = card.getAttribute("data-agent-id");
        if (card.classList.contains("selected")) {
            selectedAgentIds = selectedAgentIds.filter(function (x) { return x !== id; });
            card.classList.remove("selected");
        } else {
            selectedAgentIds.push(id);
            card.classList.add("selected");
        }
        updateAgentLabel();
        updateWelcomeScreen();
    });

    function getSelectedAgentIds() {
        return selectedAgentIds.slice();
    }

    function currentAgentId() {
        if (selectedAgentIds.length === 1) return selectedAgentIds[0];
        return null;
    }

    /* ----- Conversation management ----- */
    function savedConvId() {
        try { return localStorage.getItem("bp-current-conv"); } catch (e) { return null; }
    }

    function saveConvId(id) {
        try { localStorage.setItem("bp-current-conv", id || ""); } catch (e) {}
    }

    function initConversations() {
        fetch("/api/chat/conversations")
            .then(function (r) { return r.json(); })
            .then(function (convs) {
                conversations = convs;
                if (conversations.length === 0) {
                    currentConvId = null;
                    saveConvId("");
                    renderConvList();
                    return;
                }
                var saved = savedConvId();
                var exists = conversations.some(function (c) { return c.id === saved; });
                currentConvId = exists ? saved : conversations[0].id;
                saveConvId(currentConvId);
                renderConvList();
                loadCurrentHistory();
            });
    }

    function createConversation(select, skipClear) {
        return fetch("/api/chat/conversations", { method: "POST" })
            .then(function (r) { return r.json(); })
            .then(function (conv) {
                conversations.unshift(conv);
                if (select !== false) {
                    currentConvId = conv.id;
                    saveConvId(currentConvId);
                    if (!skipClear) clearMessages();
                    renderConvList();
                }
                return conv;
            });
    }

    function renderConvList() {
        convListEl.innerHTML = "";
        if (conversations.length === 0) {
            convListEl.innerHTML = '<div class="conv-empty">暂无对话</div>';
            return;
        }
        conversations.forEach(function (c) {
            var item = document.createElement("div");
            item.className = "conv-item" + (c.id === currentConvId ? " active" : "");
            var title = document.createElement("span");
            title.className = "conv-title";
            title.textContent = c.title || "新对话";
            var del = document.createElement("button");
            del.className = "conv-delete";
            del.title = "删除对话";
            del.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>';
            del.addEventListener("click", function (e) {
                e.stopPropagation();
                deleteConversation(c.id);
            });
            item.appendChild(title);
            item.appendChild(del);
            item.addEventListener("click", function () { selectConversation(c.id); });
            convListEl.appendChild(item);
        });
    }

    function selectConversation(id) {
        if (id === currentConvId) return;
        currentConvId = id;
        saveConvId(id);
        clearMessages();
        renderConvList();
        loadCurrentHistory();
    }

    function deleteConversation(id) {
        showConfirm("确定要删除这条对话吗？删除后不可恢复。").then(function (ok) {
            if (!ok) return;
            fetch("/api/chat/conversations/" + id, { method: "DELETE" })
                .then(function (r) {
                    if (!r.ok) return r.json().then(function (d) { throw new Error(d.detail); });
                    conversations = conversations.filter(function (c) { return c.id !== id; });
                    showToast("已删除对话", "success");
                    if (currentConvId === id) {
                        if (conversations.length > 0) {
                            currentConvId = conversations[0].id;
                            saveConvId(currentConvId);
                            clearMessages();
                            loadCurrentHistory();
                        } else {
                            currentConvId = null;
                            saveConvId("");
                            clearMessages();
                        }
                    }
                    renderConvList();
                })
                .catch(function (err) { showToast("删除失败：" + err.message, "error"); });
        });
    }

    function clearMessages() {
        messagesEl.innerHTML = "";
        activeRegenBtn = null;
        if (welcome) {
            welcome.style.display = "";
            messagesEl.appendChild(welcome);
        }
    }

    newConvBtn.addEventListener("click", function () {
        if (!currentConvId || (welcome && welcome.style.display !== "none")) {
            return;
        }
        currentConvId = null;
        saveConvId("");
        clearMessages();
        renderConvList();
        updateWelcomeScreen();
    });

    function refreshConvList() {
        fetch("/api/chat/conversations")
            .then(function (r) { return r.json(); })
            .then(function (convs) {
                conversations = convs;
                renderConvList();
            });
    }

    var welcomeTitle = document.getElementById("welcome-title");
    var welcomeHints = document.getElementById("welcome-hints");
    var defaultGreeting = "你好，有什么可以帮你的？";
    var defaultHints = ["帮我写一段 Python 快速排序", "解释一下什么是大语言模型", "用简单的语言解释量子计算"];

    function updateWelcomeScreen() {
        var greeting = defaultGreeting;
        var hints = defaultHints;
        if (selectedAgentIds.length >= 1) {
            var agent = allAgents.filter(function (a) { return a.id === selectedAgentIds[0]; })[0];
            if (agent) {
                if (agent.greeting) greeting = agent.greeting;
                if (agent.greeting_hints && agent.greeting_hints.length > 0) hints = agent.greeting_hints;
            }
        }
        if (welcomeTitle) welcomeTitle.textContent = greeting;
        if (welcomeHints) {
            welcomeHints.innerHTML = "";
            hints.forEach(function (msg) {
                var btn = document.createElement("button");
                btn.className = "hint-chip";
                btn.setAttribute("data-msg", msg);
                btn.textContent = msg;
                btn.addEventListener("click", function () {
                    input.value = msg;
                    form.dispatchEvent(new Event("submit"));
                });
                welcomeHints.appendChild(btn);
            });
        }
    }

    updateWelcomeScreen();

    function hideWelcome() {
        if (welcome) welcome.style.display = "none";
    }

    function appendMessage(role, content, animate, agentName) {
        hideWelcome();
        var row = document.createElement("div");
        row.className = "message-row";
        var msg = document.createElement("div");
        msg.className = "message " + role + (animate ? " streaming" : "");
        var avatar = document.createElement("div");
        avatar.className = "avatar";
        avatar.textContent = role === "user" ? "我" : "AI";
        var bubble = document.createElement("div");
        bubble.className = "bubble";
        var contentEl = bubble;
        if (role === "assistant" && agentName) {
            var tag = document.createElement("div");
            tag.className = "msg-agent-tag";
            tag.textContent = agentName;
            bubble.appendChild(tag);
        }
        if (role === "assistant") {
            var contentDiv = document.createElement("div");
            contentDiv.className = "bubble-content";
            contentDiv.innerHTML = marked.parse(content);
            bubble.appendChild(contentDiv);
            contentEl = contentDiv;
        } else {
            bubble.textContent = content;
        }
        msg.appendChild(avatar);
        msg.appendChild(bubble);
        row.appendChild(msg);
        messagesEl.appendChild(row);
        return { bubble: bubble, contentEl: contentEl, row: row, msg: msg };
    }

    function addCodeCopyButtons(container) {
        container.querySelectorAll("pre").forEach(function (pre) {
            if (pre.querySelector(".code-copy-btn")) return;
            var btn = document.createElement("button");
            btn.className = "code-copy-btn";
            btn.textContent = "复制";
            btn.addEventListener("click", function () {
                var code = pre.querySelector("code");
                var text = (code || pre).innerText;
                copyText(text).then(function () {
                    btn.textContent = "已复制";
                    setTimeout(function () { btn.textContent = "复制"; }, 1500);
                });
            });
            pre.appendChild(btn);
        });
    }

    function stripMarkdown(md) {
        return md
            .replace(/```[\s\S]*?```/g, function (m) { return m.replace(/```\w*\n?/g, "").replace(/```$/g, ""); })
            .replace(/`([^`]+)`/g, "$1")
            .replace(/!\[([^\]]*)\]\([^)]+\)/g, "$1")
            .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
            .replace(/^#{1,6}\s+/gm, "")
            .replace(/(\*\*|__)(.*?)\1/g, "$2")
            .replace(/(\*|_)(.*?)\1/g, "$2")
            .replace(/~~(.*?)~~/g, "$1")
            .replace(/^[>\s]*>/gm, "")
            .replace(/^[-*+]\s+/gm, "")
            .replace(/^\d+\.\s+/gm, "")
            .replace(/\n{3,}/g, "\n\n")
            .trim();
    }

    function makeCopyActionBtn(getText) {
        var wrapper = document.createElement("div");
        wrapper.className = "copy-menu-wrapper";
        var btn = document.createElement("button");
        btn.className = "msg-action-btn";
        btn.innerHTML = ICON_COPY + "<span>复制</span>";
        var menu = document.createElement("div");
        menu.className = "copy-menu";
        menu.style.display = "none";
        menu.innerHTML = '<button class="copy-menu-item" data-type="markdown">复制 Markdown</button>' +
            '<button class="copy-menu-item" data-type="plain">复制纯文本</button>';
        wrapper.appendChild(btn);
        wrapper.appendChild(menu);

        btn.addEventListener("click", function (e) {
            e.stopPropagation();
            var showing = menu.style.display !== "none";
            document.querySelectorAll(".copy-menu").forEach(function (m) { m.style.display = "none"; });
            if (!showing) menu.style.display = "";
        });
        menu.addEventListener("click", function (e) {
            var item = e.target.closest("[data-type]");
            if (!item) return;
            e.stopPropagation();
            var raw = getText();
            var text = item.getAttribute("data-type") === "plain" ? stripMarkdown(raw) : raw;
            copyText(text).then(function () {
                showToast("已复制到剪贴板", "success");
            });
            menu.style.display = "none";
        });
        document.addEventListener("click", function () { menu.style.display = "none"; });
        return wrapper;
    }

    function makeRegenBtn(userText, row) {
        var btn = document.createElement("button");
        btn.className = "msg-action-btn";
        btn.innerHTML = ICON_REGEN + "<span>重新生成</span>";
        btn.addEventListener("click", function () {
            row.remove();
            streamAssistant(userText, true);
        });
        return btn;
    }

    function addAssistantActions(row, getText) {
        var actions = document.createElement("div");
        actions.className = "msg-actions";
        actions.appendChild(makeCopyActionBtn(getText));
        row.appendChild(actions);
        return actions;
    }

    function setRegenerate(row, getText, userText) {
        if (activeRegenBtn && activeRegenBtn.parentElement) activeRegenBtn.remove();
        var actions = row.querySelector(".msg-actions");
        if (!actions) actions = addAssistantActions(row, getText);
        var btn = makeRegenBtn(userText, row);
        actions.appendChild(btn);
        activeRegenBtn = btn;
    }

    function scrollToBottom() {
        messagesEl.scrollTop = messagesEl.scrollHeight;
    }

    function streamAssistant(userText, regenerate) {
        sendBtn.disabled = true;
        agentSelectBtn.disabled = true;
        var ids = getSelectedAgentIds();
        var isMultiAgent = ids.length > 1 && !regenerate;

        var requestBody = {
            message: userText,
            regenerate: regenerate,
            conversation_id: currentConvId,
        };
        if (isMultiAgent) {
            requestBody.agent_ids = ids;
        } else {
            requestBody.agent_id = ids[0] || null;
        }

        var orchCard = null;
        var orchItems = {};
        var summaryBubble = null;
        var summaryRow = null;
        var streamContentEl = null;
        var fullText = "";
        var inSummary = false;
        var activeAgentName = null;
        if (ids.length >= 1) {
            var matched = allAgents.filter(function (a) { return a.id === ids[0]; })[0];
            activeAgentName = matched ? matched.name : null;
        }

        if (!isMultiAgent) {
            var refs = appendMessage("assistant", "", true, activeAgentName);
            summaryBubble = refs.bubble;
            streamContentEl = refs.contentEl;
            summaryRow = refs.row;
        }

        fetch("/api/chat", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(requestBody),
        }).then(function (response) {
            var reader = response.body.getReader();
            var decoder = new TextDecoder();
            var buffer = "";
            function read() {
                reader.read().then(function (result) {
                    if (result.done) { finishStream(); return; }
                    buffer += decoder.decode(result.value, { stream: true });
                    var lines = buffer.split("\n");
                    buffer = lines.pop();
                    lines.forEach(function (line) {
                        if (!line.startsWith("data: ")) return;
                        try {
                            var ev = JSON.parse(line.slice(6));
                            handleSSEEvent(ev);
                        } catch (e) {}
                    });
                    read();
                });
            }
            read();
        }).catch(function (err) {
            if (streamContentEl) {
                streamContentEl.innerHTML = "<p>网络错误：" + escapeHtml(err.message) + "</p>";
            }
            showToast("网络错误：" + err.message, "error");
            finishStream();
        });

        var traceContainer = null;
        var sourcesData = [];

        function getTraceContainer() {
            if (!traceContainer) {
                hideWelcome();
                traceContainer = document.createElement("div");
                traceContainer.className = "trace-container";
                messagesEl.appendChild(traceContainer);
            }
            return traceContainer;
        }

        function handleSSEEvent(ev) {
            if (ev.type === "thinking") {
                var container = getTraceContainer();
                var card = document.createElement("div");
                card.className = "trace-card thinking-card";
                card.innerHTML = '<div class="trace-card-header" onclick="this.parentElement.classList.toggle(\'expanded\')">' +
                    '<span class="trace-card-icon">💭</span><span class="trace-card-title">思考过程</span>' +
                    '<span class="trace-card-toggle">▶</span></div>' +
                    '<div class="trace-card-body"><pre>' + escapeHtml(ev.content) + '</pre></div>';
                container.appendChild(card);
                scrollToBottom();
            } else if (ev.type === "tool_call") {
                var container = getTraceContainer();
                var card = document.createElement("div");
                card.className = "trace-card tool-card";
                card.setAttribute("data-tool", ev.name);
                var argsPreview = "";
                try { argsPreview = JSON.stringify(ev.arguments, null, 2); } catch (e) { argsPreview = "{}"; }
                card.innerHTML = '<div class="trace-card-header" onclick="this.parentElement.classList.toggle(\'expanded\')">' +
                    '<span class="trace-card-icon">🔧</span><span class="trace-card-title">调用工具: ' + escapeHtml(ev.name) + '</span>' +
                    '<span class="trace-card-status working">执行中...</span>' +
                    '<span class="trace-card-toggle">▶</span></div>' +
                    '<div class="trace-card-body"><div class="trace-section"><strong>参数</strong><pre>' + escapeHtml(argsPreview) + '</pre></div>' +
                    '<div class="trace-section trace-result"></div></div>';
                container.appendChild(card);
                scrollToBottom();
            } else if (ev.type === "tool_result") {
                var container = getTraceContainer();
                var cards = container.querySelectorAll('.tool-card[data-tool="' + ev.name + '"]');
                var card = cards[cards.length - 1];
                if (card) {
                    var status = card.querySelector(".trace-card-status");
                    var resultEl = card.querySelector(".trace-result");
                    var isOk = ev.result && ev.result.ok !== false;
                    if (status) {
                        status.className = "trace-card-status " + (isOk ? "done" : "error");
                        status.textContent = isOk ? "完成" : "失败";
                    }
                    if (resultEl) {
                        var resultText = "";
                        try { resultText = JSON.stringify(ev.result, null, 2); } catch (e) { resultText = "{}"; }
                        resultEl.innerHTML = '<strong>结果</strong><pre>' + escapeHtml(resultText.substring(0, 1000)) + '</pre>';
                    }
                }
                scrollToBottom();
            } else if (ev.type === "sources") {
                sourcesData = ev.sources || [];
            } else if (ev.type === "plan") {
                renderOrchCard(ev.plan);
                scrollToBottom();
            } else if (ev.type === "agent_start") {
                updateOrchItem(ev.agent_id, "working", ev.subtask);
                scrollToBottom();
            } else if (ev.type === "agent_done") {
                updateOrchItem(ev.agent_id, ev.status || "done", null, ev.full_text);
                scrollToBottom();
            } else if (ev.type === "summary_start") {
                inSummary = true;
                var refs = appendMessage("assistant", "", true, "多智能体协作");
                summaryBubble = refs.bubble;
                streamContentEl = refs.contentEl;
                summaryRow = refs.row;
                scrollToBottom();
            } else if (ev.type === "token") {
                fullText += ev.content;
                if (streamContentEl) {
                    streamContentEl.innerHTML = marked.parse(fullText);
                }
                scrollToBottom();
            } else if (ev.type === "error") {
                fullText += "\n\n⚠️ " + ev.message;
                if (streamContentEl) {
                    streamContentEl.innerHTML = marked.parse(fullText);
                }
                showToast(ev.message, "error");
            } else if (ev.type === "done") {
                fullText = ev.full_text || fullText;
            }
        }

        function renderOrchCard(plan) {
            hideWelcome();
            var row = document.createElement("div");
            row.className = "message-row";
            var card = document.createElement("div");
            card.className = "orch-card";
            var header = document.createElement("div");
            header.className = "orch-card-header";
            header.textContent = "多智能体协作 · " + plan.length + " 个子任务";
            card.appendChild(header);
            plan.forEach(function (item) {
                var el = document.createElement("div");
                el.className = "orch-item";
                el.setAttribute("data-agent-id", item.agent_id);
                el.innerHTML =
                    '<div class="orch-status">⏳</div>' +
                    '<div class="orch-body">' +
                    '<div class="orch-agent-name">' + escapeHtml(item.agent_name || item.agent_id) + '</div>' +
                    '<div class="orch-subtask">' + escapeHtml(item.subtask) + '</div>' +
                    '<div class="orch-output"></div>' +
                    '</div>';
                el.addEventListener("click", function () {
                    el.classList.toggle("expanded");
                });
                card.appendChild(el);
                orchItems[item.agent_id] = el;
            });
            row.appendChild(card);
            messagesEl.appendChild(row);
            orchCard = card;
        }

        function updateOrchItem(agentId, status, subtask, output) {
            var el = orchItems[agentId];
            if (!el) return;
            var statusEl = el.querySelector(".orch-status");
            if (status === "working") {
                statusEl.className = "orch-status working";
                statusEl.textContent = "⟳";
            } else if (status === "done" || status === "ok") {
                statusEl.className = "orch-status done";
                statusEl.textContent = "✓";
                el.classList.add("expanded");
            } else if (status === "error") {
                statusEl.className = "orch-status";
                statusEl.style.color = "#e53e3e";
                statusEl.textContent = "✗";
                el.classList.add("expanded");
            }
            if (output) {
                var outputEl = el.querySelector(".orch-output");
                outputEl.textContent = output;
            }
        }

        function finishStream() {
            if (summaryBubble && summaryBubble.parentElement) {
                summaryBubble.parentElement.classList.remove("streaming");
                addCodeCopyButtons(streamContentEl || summaryBubble);
            }
            if (summaryRow) {
                var getText = function () { return fullText; };
                addAssistantActions(summaryRow, getText);
                setRegenerate(summaryRow, getText, userText);
            }
            if (sourcesData.length > 0 && summaryRow) {
                var sourcesEl = document.createElement("div");
                sourcesEl.className = "msg-sources";
                sourcesEl.innerHTML = '<span class="msg-sources-label">参考来源：</span>' +
                    sourcesData.map(function (s) {
                        var label = s.name + (s.heading ? " · " + s.heading : "");
                        return '<span class="msg-source-item">' + escapeHtml(label) + '</span>';
                    }).join("");
                summaryRow.appendChild(sourcesEl);
            }
            sendBtn.disabled = false;
            agentSelectBtn.disabled = false;
            scrollToBottom();
            refreshConvList();
        }
    }

    function loadCurrentHistory() {
        if (!currentConvId) return;
        fetch("/api/chat/history?conversation_id=" + encodeURIComponent(currentConvId))
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data.messages.length === 0) return;
                hideWelcome();
                var lastUserText = "";
                var lastAssistant = null;
                data.messages.forEach(function (m) {
                    var refs = appendMessage(m.role, m.content, false);
                    if (m.role === "user") {
                        lastUserText = m.content;
                    } else {
                        addCodeCopyButtons(refs.bubble);
                        var content = m.content;
                        addAssistantActions(refs.row, function () { return content; });
                        lastAssistant = { row: refs.row, text: m.content, userText: lastUserText };
                    }
                });
                if (lastAssistant) {
                    var t = lastAssistant.text;
                    setRegenerate(lastAssistant.row, function () { return t; }, lastAssistant.userText);
                }
                scrollToBottom();
            });
    }

    input.addEventListener("keydown", function (e) {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            form.dispatchEvent(new Event("submit"));
        }
    });

    input.addEventListener("input", function () {
        this.style.height = "auto";
        this.style.height = Math.min(this.scrollHeight, 150) + "px";
    });

    form.addEventListener("submit", function (e) {
        e.preventDefault();
        var text = input.value.trim();
        if (!text) return;
        if (selectedAgentIds.length === 0) {
            showToast("请先关联智能体：前往「智能体」页面创建一个智能体后再开始对话", "error");
            return;
        }
        appendMessage("user", text, false);
        input.value = "";
        input.style.height = "auto";
        scrollToBottom();
        if (!currentConvId) {
            createConversation(true, true).then(function () {
                streamAssistant(text, false);
            });
        } else {
            streamAssistant(text, false);
        }
    });
}

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

/* ===== Agents page ===== */
function initAgents() {
    var listEl = document.getElementById("agent-list");
    var modal = document.getElementById("agent-modal");
    var modalTitle = document.getElementById("modal-title");
    var form = document.getElementById("agent-form");
    var idGroup = document.getElementById("form-id-group");
    var editingId = null;

    loadAgents();
    loadModelOptions();

    function loadModelOptions() {
        return fetch("/api/models")
            .then(function (r) { return r.json(); })
            .then(function (models) {
                var select = document.getElementById("agent-model");
                var options = '<option value="">跟随默认模型</option>';
                models.forEach(function (m) {
                    if (!m.enabled) return;
                    options += '<option value="' + m.id + '">' + m.id + "（" + m.model + "）</option>";
                });
                select.innerHTML = options;
            });
    }

    var tempSlider = document.getElementById("agent-temperature");
    var tempLabel = document.getElementById("temp-value");
    tempSlider.addEventListener("input", function () {
        tempLabel.textContent = tempSlider.value === "" ? "默认" : tempSlider.value;
    });

    function resetTempSlider() {
        tempSlider.value = "";
        tempLabel.textContent = "默认";
    }

    document.getElementById("create-agent-btn").addEventListener("click", function () {
        editingId = null;
        modalTitle.textContent = "新建智能体";
        idGroup.style.display = "";
        form.reset();
        document.getElementById("agent-id").disabled = false;
        document.getElementById("agent-model").value = "";
        document.getElementById("agent-greeting").value = "";
        document.getElementById("agent-hints").value = "";
        document.getElementById("agent-max-tokens").value = "";
        resetTempSlider();
        loadModelOptions();
        openModal();
    });

    document.getElementById("modal-close").addEventListener("click", closeModal);
    document.getElementById("modal-cancel").addEventListener("click", closeModal);
    modal.addEventListener("click", function (e) {
        if (e.target === modal) closeModal();
    });

    function openModal() { modal.style.display = ""; }
    function closeModal() { modal.style.display = "none"; }

    function loadAgents() {
        fetch("/api/agents")
            .then(function (r) { return r.json(); })
            .then(function (agents) {
                listEl.innerHTML = agents.map(function (a) {
                    var caps = a.capabilities.map(function (c) {
                        return "<li><strong>" + c.name + "</strong>：" + c.description + "</li>";
                    }).join("");
                    var tools = a.tools.length ? a.tools.join("、") : "无";
                    var modelInfo = a.model ? a.model : "跟随默认模型";
                    return '<details class="agent-card" data-id="' + a.id + '">' +
                        "<summary>" + a.name + " <small>" + a.role + "</small>" +
                        '<span class="agent-actions">' +
                        '<button class="btn-edit" data-action="edit" data-id="' + a.id + '">编辑</button>' +
                        '<button class="btn-danger" data-action="delete" data-id="' + a.id + '">删除</button>' +
                        "</span></summary>" +
                        '<div class="agent-detail">' +
                        "<p>" + escapeHtml(a.description) + "</p>" +
                        "<p><strong>模型：</strong>" + escapeHtml(modelInfo) + "</p>" +
                        (caps ? "<ul>" + caps + "</ul>" : "") +
                        "<p><strong>工具：</strong>" + escapeHtml(tools) + "</p>" +
                        "<p><strong>系统提示词：</strong></p>" +
                        "<pre>" + escapeHtml(a.system_prompt) + "</pre>" +
                        "</div></details>";
                }).join("");
            });
    }

    listEl.addEventListener("click", function (e) {
        var btn = e.target.closest("[data-action]");
        if (!btn) return;
        e.preventDefault();
        e.stopPropagation();
        var action = btn.getAttribute("data-action");
        var id = btn.getAttribute("data-id");

        if (action === "delete") {
            showConfirm("确定要删除智能体「" + id + "」吗？").then(function (ok) {
                if (!ok) return;
                fetch("/api/agents/" + id, { method: "DELETE" })
                    .then(function (r) {
                        if (!r.ok) return r.json().then(function (d) { throw new Error(d.detail); });
                        showToast("已删除智能体 " + id, "success");
                        loadAgents();
                    })
                    .catch(function (err) { showToast("删除失败：" + err.message, "error"); });
            });
        }

        if (action === "edit") {
            fetch("/api/agents/" + id)
                .then(function (r) { return r.json(); })
                .then(function (a) {
                    editingId = id;
                    modalTitle.textContent = "编辑智能体";
                    idGroup.style.display = "none";
                    document.getElementById("agent-id").value = a.id;
                    document.getElementById("agent-name").value = a.name;
                    document.getElementById("agent-role").value = a.role;
                    document.getElementById("agent-desc").value = a.description;
                    document.getElementById("agent-prompt").value = a.system_prompt;
                    document.getElementById("agent-greeting").value = a.greeting || "";
                    document.getElementById("agent-hints").value = (a.greeting_hints || []).join("；");
                    if (a.temperature != null) {
                        tempSlider.value = a.temperature;
                        tempLabel.textContent = String(a.temperature);
                    } else {
                        resetTempSlider();
                    }
                    document.getElementById("agent-max-tokens").value = a.max_tokens || "";
                    loadModelOptions().then(function () {
                        document.getElementById("agent-model").value = a.model || "";
                    });
                    openModal();
                });
        }
    });

    form.addEventListener("submit", function (e) {
        e.preventDefault();

        var hintsRaw = document.getElementById("agent-hints").value.trim();
        var hints = hintsRaw ? hintsRaw.split(/[;；]/).map(function (s) { return s.trim(); }).filter(Boolean) : [];
        var tempVal = document.getElementById("agent-temperature").value;
        var maxTokVal = document.getElementById("agent-max-tokens").value;

        var payload = {
            name: document.getElementById("agent-name").value,
            role: document.getElementById("agent-role").value,
            description: document.getElementById("agent-desc").value,
            system_prompt: document.getElementById("agent-prompt").value,
            model: document.getElementById("agent-model").value || null,
            greeting: document.getElementById("agent-greeting").value.trim() || null,
            greeting_hints: hints,
            temperature: tempVal !== "" ? parseFloat(tempVal) : null,
            max_tokens: maxTokVal ? parseInt(maxTokVal, 10) : null,
            tools: [],
            capabilities: []
        };

        var url, method;
        if (editingId) {
            url = "/api/agents/" + editingId;
            method = "PUT";
        } else {
            payload.id = document.getElementById("agent-id").value;
            url = "/api/agents";
            method = "POST";
        }

        fetch(url, {
            method: method,
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        })
            .then(function (r) {
                if (!r.ok) return r.json().then(function (d) { throw new Error(d.detail); });
                showToast(editingId ? "已保存修改" : "已创建智能体", "success");
                closeModal();
                loadAgents();
            })
            .catch(function (err) { showToast("保存失败：" + err.message, "error"); });
    });
}
