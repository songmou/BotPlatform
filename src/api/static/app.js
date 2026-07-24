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

/* Sidebar nav group expand/collapse */
document.addEventListener("click", function (evt) {
    var toggle = evt.target.closest && evt.target.closest(".nav-group-toggle");
    if (!toggle) return;
    var group = toggle.closest(".nav-group");
    if (group) group.classList.toggle("open");
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

    var toolContainers = {
        builtin: document.getElementById("tools-builtin"),
        plugin: document.getElementById("tools-plugin"),
        skill: document.getElementById("tools-skill"),
        mcp: document.getElementById("tools-mcp")
    };
    var toolKinds = ["builtin", "plugin", "skill", "mcp"];

    function toolCardHtml(value, label, description, kind) {
        var desc = description
            ? '<span class="tool-desc">' + escapeHtml(description) + "</span>"
            : "";
        return '<label class="tool-check">' +
            '<input type="checkbox" data-kind="' + kind + '" value="' + escapeHtml(value) + '">' +
            '<span class="tool-info">' +
            '<span class="tool-name">' + escapeHtml(label) + "</span>" +
            desc +
            "</span></label>";
    }

    function renderCheckboxes(container, items, kind) {
        if (!items.length) {
            container.innerHTML = '<div class="tool-empty">暂无可选项</div>';
            return;
        }
        container.innerHTML = items.map(function (it) {
            return toolCardHtml(it.value, it.label, it.description, kind);
        }).join("");
    }

    function renderPluginGroups(container, plugins) {
        var enabled = plugins.filter(function (p) {
            return p.enabled && (p.tools || []).length;
        });
        if (!enabled.length) {
            container.innerHTML = '<div class="tool-empty">暂无可选项</div>';
            return;
        }
        container.innerHTML = enabled.map(function (p) {
            var cards = p.tools.map(function (t) {
                return toolCardHtml(t.name, t.name, t.description, "plugin");
            }).join("");
            return '<div class="tool-plugin-group">' +
                '<div class="tool-plugin-name">' + escapeHtml(p.id) + "</div>" +
                '<div class="tool-checkboxes tool-checkboxes-nested">' + cards + "</div>" +
                "</div>";
        }).join("");
    }

    function updateCount(kind) {
        var checked = toolContainers[kind].querySelectorAll("input:checked").length;
        document.getElementById("tools-" + kind + "-count").textContent =
            checked ? "（已选 " + checked + "）" : "";
    }

    function loadToolOptions() {
        return Promise.all([
            fetch("/api/tools").then(function (r) { return r.json(); }),
            fetch("/api/plugins").then(function (r) { return r.json(); }),
            fetch("/api/skills").then(function (r) { return r.json(); }),
            fetch("/api/mcp").then(function (r) { return r.json(); })
        ]).then(function (results) {
            var builtinTools = results[0] || [];
            var plugins = results[1] || [];
            var skills = results[2] || [];
            var servers = results[3] || [];

            renderCheckboxes(toolContainers.builtin, builtinTools.map(function (t) {
                return { value: t.name, label: t.name, description: t.description };
            }), "builtin");

            renderPluginGroups(toolContainers.plugin, plugins);

            renderCheckboxes(toolContainers.skill, skills.map(function (s) {
                return { value: s.id, label: s.name + (s.enabled ? "" : "（已禁用）"), description: s.description };
            }), "skill");

            renderCheckboxes(toolContainers.mcp, servers.map(function (m) {
                return { value: m.id, label: m.name + (m.enabled ? "" : "（已禁用）"), description: mcpTransportLabel(m.transport) };
            }), "mcp");

            toolKinds.forEach(updateCount);
        });
    }

    function setToolSelection(agent) {
        var toolSet = {}, skillSet = {}, mcpSet = {};
        (agent.tools || []).forEach(function (n) { toolSet[n] = true; });
        (agent.skills || []).forEach(function (n) { skillSet[n] = true; });
        (agent.mcp_servers || []).forEach(function (n) { mcpSet[n] = true; });
        toolKinds.forEach(function (kind) {
            toolContainers[kind].querySelectorAll("input").forEach(function (box) {
                if (kind === "skill") box.checked = !!skillSet[box.value];
                else if (kind === "mcp") box.checked = !!mcpSet[box.value];
                else box.checked = !!toolSet[box.value];
            });
            updateCount(kind);
        });
    }

    function collectSelection() {
        var tools = [];
        ["builtin", "plugin"].forEach(function (kind) {
            toolContainers[kind].querySelectorAll("input:checked").forEach(function (b) {
                tools.push(b.value);
            });
        });
        var skills = [];
        toolContainers.skill.querySelectorAll("input:checked").forEach(function (b) { skills.push(b.value); });
        var mcpServers = [];
        toolContainers.mcp.querySelectorAll("input:checked").forEach(function (b) { mcpServers.push(b.value); });
        return { tools: tools, skills: skills, mcp_servers: mcpServers };
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
        loadToolOptions();
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
                    var skills = (a.skills && a.skills.length) ? a.skills.join("、") : "无";
                    var mcpServers = (a.mcp_servers && a.mcp_servers.length) ? a.mcp_servers.join("、") : "无";
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
                        "<p><strong>技能：</strong>" + escapeHtml(skills) + "</p>" +
                        "<p><strong>MCP 服务：</strong>" + escapeHtml(mcpServers) + "</p>" +
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
                    loadToolOptions().then(function () {
                        setToolSelection(a);
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
        var selection = collectSelection();

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
            tools: selection.tools,
            skills: selection.skills,
            mcp_servers: selection.mcp_servers,
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

/* ===== Schedules page ===== */
function initSchedules() {
    var listEl = document.getElementById("schedule-list");
    var statusEl = document.getElementById("schedule-status");
    var modal = document.getElementById("schedule-modal");
    var modalTitle = document.getElementById("schedule-modal-title");
    var form = document.getElementById("schedule-form");
    var idGroup = document.getElementById("schedule-id-group");
    var editingId = null;

    loadSchedules();

    document.getElementById("create-schedule-btn").addEventListener("click", function () {
        editingId = null;
        modalTitle.textContent = "新建任务";
        idGroup.style.display = "";
        form.reset();
        document.getElementById("schedule-enabled").checked = true;
        document.getElementById("schedule-action-type").value = "text";
        document.getElementById("schedule-condition-enabled").checked = false;
        document.getElementById("condition-fields").style.display = "none";
        updateActionFields();
        openModal();
    });

    document.getElementById("schedule-modal-close").addEventListener("click", closeModal);
    document.getElementById("schedule-modal-cancel").addEventListener("click", closeModal);
    modal.addEventListener("click", function (e) { if (e.target === modal) closeModal(); });

    function openModal() { modal.style.display = ""; }
    function closeModal() { modal.style.display = "none"; }

    document.getElementById("schedule-action-type").addEventListener("change", updateActionFields);

    function updateActionFields() {
        var type = document.getElementById("schedule-action-type").value;
        document.getElementById("action-text-group").style.display = type === "text" ? "" : "none";
        document.getElementById("action-agent-group").style.display = type === "agent_prompt" ? "" : "none";
        document.getElementById("action-script-group").style.display = type === "script" ? "" : "none";
        document.getElementById("action-plugin-group").style.display = type === "plugin" ? "" : "none";
    }

    document.getElementById("schedule-condition-enabled").addEventListener("change", function () {
        document.getElementById("condition-fields").style.display = this.checked ? "" : "none";
    });

    var actionLabels = {
        text: "文本消息",
        agent_prompt: "智能体生成",
        script: "脚本执行",
        plugin: "插件调用",
        image: "图片推送"
    };

    function loadSchedules() {
        fetch("/api/schedules")
            .then(function (r) { return r.json(); })
            .then(function (tasks) {
                var enabled = tasks.filter(function (t) { return t.enabled; }).length;
                statusEl.innerHTML = "<span>已启用 " + enabled + " / 共 " + tasks.length + " 项</span>";
                if (!tasks.length) {
                    listEl.innerHTML = '<div class="empty-state">暂无定时任务，点击「新建任务」创建</div>';
                    return;
                }
                listEl.innerHTML = tasks.map(function (t) {
                    var cronDisplay = (t.crons && t.crons.length) ? t.crons.join("<br>") : (t.cron || "—");
                    var condDisplay = t.condition
                        ? t.condition.type + " (" + t.condition.after_hours + "h~" + t.condition.before_hours + "h)"
                        : "无";
                    return '<div class="card schedule-card" data-id="' + t.id + '">' +
                        '<div class="card-header">' +
                        '<div><strong>' + escapeHtml(t.id) + '</strong>' +
                        (t.enabled ? '<span class="badge badge-success">启用</span>' : '<span class="badge badge-muted">禁用</span>') +
                        '</div>' +
                        '<div class="card-actions">' +
                        '<button class="btn-edit" data-action="edit" data-id="' + t.id + '">编辑</button>' +
                        '<button class="btn-danger" data-action="delete" data-id="' + t.id + '">删除</button>' +
                        '</div></div>' +
                        '<div class="card-body">' +
                        '<p><strong>Cron：</strong><code>' + cronDisplay + '</code></p>' +
                        '<p><strong>动作：</strong>' + (actionLabels[t.action.type] || t.action.type) + '</p>' +
                        '<p><strong>目标：</strong>' + escapeHtml(t.target) + '</p>' +
                        '<p><strong>条件：</strong>' + escapeHtml(condDisplay) + '</p>' +
                        '</div></div>';
                }).join("");
            });
    }

    listEl.addEventListener("click", function (e) {
        var btn = e.target.closest("[data-action]");
        if (!btn) return;
        e.preventDefault();
        var action = btn.getAttribute("data-action");
        var id = btn.getAttribute("data-id");

        if (action === "delete") {
            showConfirm("确定要删除定时任务「" + id + "」吗？").then(function (ok) {
                if (!ok) return;
                fetch("/api/schedules/" + id, { method: "DELETE" })
                    .then(function (r) {
                        if (!r.ok) return r.json().then(function (d) { throw new Error(d.detail); });
                        showToast("已删除任务 " + id, "success");
                        loadSchedules();
                    })
                    .catch(function (err) { showToast("删除失败：" + err.message, "error"); });
            });
        }

        if (action === "edit") {
            fetch("/api/schedules/" + id)
                .then(function (r) { return r.json(); })
                .then(function (t) {
                    editingId = id;
                    modalTitle.textContent = "编辑任务";
                    idGroup.style.display = "none";
                    document.getElementById("schedule-id").value = t.id;
                    document.getElementById("schedule-enabled").checked = t.enabled;
                    var crons = t.crons && t.crons.length ? t.crons : (t.cron ? [t.cron] : []);
                    document.getElementById("schedule-crons").value = crons.join("\n");
                    document.getElementById("schedule-target").value = t.target;
                    document.getElementById("schedule-action-type").value = t.action.type;
                    document.getElementById("action-content").value = t.action.content || "";
                    document.getElementById("action-agent-id").value = t.action.agent_id || "";
                    document.getElementById("action-prompt").value = t.action.prompt || "";
                    document.getElementById("action-script-id").value = t.action.script_id || "";
                    document.getElementById("action-plugin-id").value = t.action.plugin_id || "";
                    document.getElementById("action-tool-name").value = t.action.tool_name || "";
                    if (t.condition) {
                        document.getElementById("schedule-condition-enabled").checked = true;
                        document.getElementById("condition-fields").style.display = "";
                        document.getElementById("condition-after").value = t.condition.after_hours;
                        document.getElementById("condition-before").value = t.condition.before_hours;
                    } else {
                        document.getElementById("schedule-condition-enabled").checked = false;
                        document.getElementById("condition-fields").style.display = "none";
                    }
                    updateActionFields();
                    openModal();
                });
        }
    });

    form.addEventListener("submit", function (e) {
        e.preventDefault();

        var cronsRaw = document.getElementById("schedule-crons").value.trim();
        var crons = cronsRaw ? cronsRaw.split("\n").map(function (s) { return s.trim(); }).filter(Boolean) : [];
        var actionType = document.getElementById("schedule-action-type").value;
        var action = { type: actionType };

        if (actionType === "text") {
            action.content = document.getElementById("action-content").value.trim();
        } else if (actionType === "agent_prompt") {
            action.agent_id = document.getElementById("action-agent-id").value.trim();
            action.prompt = document.getElementById("action-prompt").value.trim();
        } else if (actionType === "script") {
            action.script_id = document.getElementById("action-script-id").value.trim();
        } else if (actionType === "plugin") {
            action.plugin_id = document.getElementById("action-plugin-id").value.trim();
            action.tool_name = document.getElementById("action-tool-name").value.trim();
        }

        var condition = null;
        if (document.getElementById("schedule-condition-enabled").checked) {
            condition = {
                type: "inactivity_once",
                after_hours: parseFloat(document.getElementById("condition-after").value) || 0,
                before_hours: parseFloat(document.getElementById("condition-before").value) || 24
            };
        }

        var payload = {
            enabled: document.getElementById("schedule-enabled").checked,
            crons: crons,
            target: document.getElementById("schedule-target").value,
            action: action,
            condition: condition
        };

        var url, method;
        if (editingId) {
            url = "/api/schedules/" + editingId;
            method = "PUT";
        } else {
            payload.id = document.getElementById("schedule-id").value;
            url = "/api/schedules";
            method = "POST";
        }

        fetch(url, {
            method: method,
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        })
            .then(function (r) {
                if (!r.ok) return r.json().then(function (d) { throw new Error(d.detail); });
                showToast(editingId ? "已保存修改" : "已创建任务", "success");
                closeModal();
                loadSchedules();
            })
            .catch(function (err) { showToast("保存失败：" + err.message, "error"); });
    });
}

/* ===== Plugins page ===== */
var PLUGIN_META = {
    browser_automation: { icon: "B", color: "#4285f4", desc: "Playwright 驱动的浏览器自动化，支持网页快照与交互" },
    codex_tasks: { icon: "C", color: "#10a37f", desc: "Codex 编码任务管理，支持创建、继续和审批" },
    todo: { icon: "T", color: "#f59e0b", desc: "私人待办事项管理，支持增删改查与提醒" }
};

function initTools() {
    var listEl = document.getElementById("plugin-list");
    var modal = document.getElementById("plugin-modal");
    var searchInput = document.getElementById("plugin-search");
    var filterSelect = document.getElementById("plugin-filter-status");
    var allPlugins = [];

    var validTabs = ["skills", "mcp", "plugins", "builtin", "audit"];
    function switchTab() {
        var hash = location.hash.replace("#", "");
        if (validTabs.indexOf(hash) === -1) hash = "builtin";
        var detailPane = document.getElementById("tools-pane-mcp-detail");
        if (detailPane) detailPane.style.display = "none";
        validTabs.forEach(function (t) {
            var pane = document.getElementById("tools-pane-" + t);
            if (pane) pane.style.display = t === hash ? "" : "none";
        });
        document.querySelectorAll(".nav-sub-item").forEach(function (el) {
            el.classList.toggle("active", el.getAttribute("data-tab") === hash);
        });
    }
    switchTab();
    window.addEventListener("hashchange", switchTab);

    loadPlugins();
    loadBuiltinTools();
    loadAuditLogs();
    loadSkills();
    loadMcpServers();

    searchInput.addEventListener("input", renderPlugins);
    filterSelect.addEventListener("change", renderPlugins);

    document.getElementById("plugin-modal-close").addEventListener("click", closeModal);
    document.getElementById("plugin-modal-cancel").addEventListener("click", closeModal);
    modal.addEventListener("click", function (e) { if (e.target === modal) closeModal(); });

    document.getElementById("plugin-edit-settings-btn").addEventListener("click", function () {
        var wrap = document.getElementById("plugin-settings-edit-wrap");
        var view = document.getElementById("plugin-settings-view");
        if (wrap.style.display === "none") {
            wrap.style.display = "";
            view.style.display = "none";
            this.textContent = "取消编辑";
        } else {
            wrap.style.display = "none";
            view.style.display = "";
            this.textContent = "编辑";
        }
    });

    document.getElementById("plugin-save-btn").addEventListener("click", function () {
        var editingId = modal.getAttribute("data-plugin-id");
        var settingsWrap = document.getElementById("plugin-settings-edit-wrap");
        var settings;

        if (settingsWrap.style.display !== "none") {
            var settingsText = document.getElementById("plugin-settings").value.trim();
            settings = {};
            if (settingsText) {
                try {
                    settings = JSON.parse(settingsText);
                } catch (err) {
                    showToast("设置 JSON 格式错误：" + err.message, "error");
                    return;
                }
            }
        } else {
            settings = JSON.parse(document.getElementById("plugin-settings-view").textContent || "{}");
        }

        var payload = {
            enabled: document.getElementById("plugin-enabled").checked,
            settings: settings
        };

        fetch("/api/plugins/" + editingId, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        })
            .then(function (r) {
                if (!r.ok) return r.json().then(function (d) { throw new Error(d.detail); });
                showToast("已保存修改", "success");
                closeModal();
                loadPlugins();
            })
            .catch(function (err) { showToast("保存失败：" + err.message, "error"); });
    });

    function openModal() { modal.style.display = ""; }
    function closeModal() { modal.style.display = "none"; }

    function getMeta(id) {
        return PLUGIN_META[id] || { icon: id.charAt(0).toUpperCase(), color: "#6b7280", desc: "" };
    }

    function loadPlugins() {
        fetch("/api/plugins")
            .then(function (r) { return r.json(); })
            .then(function (plugins) {
                allPlugins = plugins;
                renderPlugins();
            });
    }

    function renderPlugins() {
        var query = searchInput.value.trim().toLowerCase();
        var statusFilter = filterSelect.value;

        var filtered = allPlugins.filter(function (p) {
            if (query && p.id.toLowerCase().indexOf(query) === -1) return false;
            if (statusFilter === "enabled" && !p.enabled) return false;
            if (statusFilter === "disabled" && p.enabled) return false;
            return true;
        });

        if (!filtered.length) {
            listEl.innerHTML = '<div class="empty-state">' +
                (allPlugins.length ? "未找到匹配的插件" : "暂无已注册插件") + "</div>";
            return;
        }

        listEl.innerHTML = filtered.map(function (p) {
            var meta = getMeta(p.id);
            var statusBadge = p.enabled
                ? '<span class="badge badge-success">已启用</span>'
                : '<span class="badge badge-muted">已禁用</span>';

            return '<div class="plugin-tile" data-id="' + p.id + '">' +
                '<div class="plugin-tile-header">' +
                    '<div class="plugin-avatar" style="background:' + meta.color + '">' + meta.icon + "</div>" +
                    '<div class="plugin-tile-info">' +
                        '<div class="plugin-tile-name">' + escapeHtml(p.id) + "</div>" +
                        '<div class="plugin-tile-meta">' + statusBadge +
                        '<span class="text-muted">' + p.tool_count + " 个工具</span></div>" +
                    "</div>" +
                "</div>" +
                '<p class="plugin-tile-desc">' + escapeHtml(meta.desc) + "</p>" +
                '<div class="plugin-tile-tags">' +
                    p.tools.map(function (t) {
                        return '<span class="tag' + (t.requires_approval ? " tag-warning" : "") + '">' +
                            escapeHtml(t.name) + "</span>";
                    }).join("") +
                "</div>" +
            "</div>";
        }).join("");
    }

    listEl.addEventListener("click", function (e) {
        var tile = e.target.closest(".plugin-tile");
        if (!tile) return;
        var id = tile.getAttribute("data-id");
        openPluginDetail(id);
    });

    function openPluginDetail(id) {
        var p = allPlugins.find(function (x) { return x.id === id; });
        if (!p) return;
        var meta = getMeta(p.id);

        modal.setAttribute("data-plugin-id", id);
        document.getElementById("plugin-modal-icon").textContent = meta.icon;
        document.getElementById("plugin-modal-icon").style.background = meta.color;
        document.getElementById("plugin-modal-title").textContent = p.id;
        document.getElementById("plugin-modal-subtitle").textContent = meta.desc;
        document.getElementById("plugin-enabled").checked = p.enabled;
        document.getElementById("plugin-status-text").textContent = p.enabled ? "启用" : "禁用";
        document.getElementById("plugin-tool-count").textContent = p.tool_count;

        var toolsHtml = p.tools.map(function (t) {
            var approvalBadge = t.requires_approval
                ? '<span class="badge badge-warning">需审批</span>'
                : '<span class="badge badge-muted">自动</span>';
            return '<div class="tool-def-item">' +
                '<div class="tool-def-header">' +
                    '<code class="tool-def-name">' + escapeHtml(t.name) + "</code>" +
                    approvalBadge +
                "</div>" +
                '<p class="tool-def-desc">' + escapeHtml(t.description) + "</p>" +
                '<details class="tool-def-params"><summary>参数定义</summary>' +
                "<pre>" + escapeHtml(JSON.stringify(t.parameters, null, 2)) + "</pre></details>" +
            "</div>";
        }).join("");

        document.getElementById("plugin-tools-table").innerHTML = toolsHtml || '<p class="text-muted">无工具定义</p>';

        var settingsJson = JSON.stringify(p.settings, null, 2);
        document.getElementById("plugin-settings-view").textContent = settingsJson;
        document.getElementById("plugin-settings-view").style.display = "";
        document.getElementById("plugin-settings-edit-wrap").style.display = "none";
        document.getElementById("plugin-settings").value = settingsJson;
        document.getElementById("plugin-edit-settings-btn").textContent = "编辑";

        var enabledCheckbox = document.getElementById("plugin-enabled");
        enabledCheckbox.onchange = function () {
            document.getElementById("plugin-status-text").textContent = this.checked ? "启用" : "禁用";
        };

        openModal();
    }

    function loadBuiltinTools() {
        fetch("/api/tools")
            .then(function (r) { return r.json(); })
            .then(function (tools) {
                var container = document.getElementById("builtin-tools-list");
                var countEl = document.getElementById("builtin-tools-count");
                if (!tools.length) {
                    container.innerHTML = '<p class="text-muted">暂无内置工具</p>';
                    return;
                }
                countEl.textContent = "（" + tools.length + "）";

                var categories = {};
                tools.forEach(function (t) {
                    if (!categories[t.category]) categories[t.category] = [];
                    categories[t.category].push(t);
                });

                container.innerHTML = Object.keys(categories).map(function (cat) {
                    var items = categories[cat].map(function (t) {
                        var badges = t.available
                            ? '<span class="badge badge-success">可用</span>'
                            : '<span class="badge badge-muted">不可用</span>';
                        if (t.requires_approval) {
                            badges += ' <span class="badge badge-warning">需审批</span>';
                        }
                        var toggle = '<label class="switch-label">' +
                            '<input type="checkbox" class="tool-toggle" data-tool="' + escapeHtml(t.name) + '"' +
                            (t.enabled ? " checked" : "") + ">" +
                            '<span class="switch switch-sm"></span>' +
                            "</label>";
                        return '<div class="builtin-tool-item">' +
                            '<div class="builtin-tool-header">' +
                                '<code class="builtin-tool-name">' + escapeHtml(t.name) + "</code>" +
                                '<div class="builtin-tool-badges">' + toggle + badges + "</div>" +
                            "</div>" +
                            '<p class="builtin-tool-desc">' + escapeHtml(t.description) + "</p>" +
                        "</div>";
                    }).join("");
                    return '<div class="builtin-tool-category">' +
                        '<div class="builtin-tool-category-title">' + escapeHtml(cat) + "</div>" +
                        '<div class="builtin-tool-items">' + items + "</div>" +
                    "</div>";
                }).join("");

                container.querySelectorAll(".tool-toggle").forEach(function (cb) {
                    cb.addEventListener("change", function () {
                        var toolName = this.getAttribute("data-tool");
                        var enabled = this.checked;
                        fetch("/api/tools/" + encodeURIComponent(toolName), {
                            method: "PATCH",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ enabled: enabled }),
                        })
                            .then(function (r) {
                                if (!r.ok) return r.json().then(function (d) { throw new Error(d.detail); });
                                showToast(enabled ? "已启用 " + toolName : "已禁用 " + toolName, "success");
                                loadBuiltinTools();
                            })
                            .catch(function (err) { showToast("操作失败：" + err.message, "error"); });
                    });
                });
            });
    }

    var auditOffset = 0;
    var auditLimit = 20;

    function loadAuditLogs(append) {
        fetch("/api/tools/audit?limit=" + auditLimit + "&offset=" + auditOffset)
            .then(function (r) { return r.json(); })
            .then(function (data) {
                var container = document.getElementById("tool-audit-list");
                var loadMoreWrap = document.getElementById("audit-load-more-wrap");
                var items = data.items || [];
                if (!items.length && !append) {
                    container.innerHTML = '<p class="text-muted">暂无审计记录</p>';
                    loadMoreWrap.style.display = "none";
                    return;
                }
                var html = items.map(function (item) {
                    var statusBadge = item.status === "成功"
                        ? '<span class="badge badge-success">成功</span>'
                        : '<span class="badge badge-warning">失败</span>';
                    var ts = item.ts ? item.ts.replace("T", " ").substring(0, 19) : "";
                    return '<div class="tool-audit-row' + (item.status !== "成功" ? " audit-row-fail" : "") + '">' +
                        '<span class="audit-ts">' + escapeHtml(ts) + "</span>" +
                        '<code class="audit-tool">' + escapeHtml(item.tool_name) + "</code>" +
                        statusBadge +
                        '<span class="audit-duration">' + (item.duration_ms || 0) + "ms</span>" +
                        (item.error ? '<span class="audit-error">' + escapeHtml(item.error) + "</span>" : "") +
                    "</div>";
                }).join("");
                if (append) {
                    container.innerHTML += html;
                } else {
                    container.innerHTML = html;
                }
                loadMoreWrap.style.display = (auditOffset + items.length < data.total) ? "" : "none";
            });
    }

    var auditLoadMoreBtn = document.getElementById("audit-load-more");
    if (auditLoadMoreBtn) {
        auditLoadMoreBtn.addEventListener("click", function () {
            auditOffset += auditLimit;
            loadAuditLogs(true);
        });
    }

    /* ---- Skill 技能 ---- */
    var skillModal = document.getElementById("skill-modal");
    var skillEditingId = null;

    document.getElementById("create-skill-btn").addEventListener("click", function () {
        skillEditingId = null;
        document.getElementById("skill-modal-title").textContent = "新建技能";
        document.getElementById("skill-id-group").style.display = "";
        document.getElementById("skill-form").reset();
        document.getElementById("skill-enabled").checked = true;
        document.getElementById("skill-desc-count").textContent = "0";
        document.getElementById("skill-submit-btn").textContent = "立即创建";
        skillModal.style.display = "";
    });

    document.getElementById("skill-modal-close").addEventListener("click", function () { skillModal.style.display = "none"; });
    document.getElementById("skill-modal-cancel").addEventListener("click", function () { skillModal.style.display = "none"; });
    skillModal.addEventListener("click", function (e) { if (e.target === skillModal) skillModal.style.display = "none"; });

    document.getElementById("skill-description").addEventListener("input", function () {
        document.getElementById("skill-desc-count").textContent = this.value.length;
    });

    document.getElementById("skill-fill-example").addEventListener("click", function () {
        document.getElementById("skill-prompt").value =
            "你是一个多语言翻译专家。\n\n" +
            "## 任务\n当用户给出文本时，将其翻译为目标语言。\n\n" +
            "## 规则\n- 保持原文的语气和格式\n- 专有名词保留原文\n- 若未指定目标语言，默认翻译为英文\n\n" +
            "## 输出\n仅输出翻译结果，不要附加解释。";
    });

    document.getElementById("skill-form").addEventListener("submit", function (e) {
        e.preventDefault();
        var id = document.getElementById("skill-id").value.trim();
        var name = document.getElementById("skill-name").value.trim();
        var description = document.getElementById("skill-description").value.trim();
        var prompt = document.getElementById("skill-prompt").value.trim();
        var enabled = document.getElementById("skill-enabled").checked;
        if (!name || !prompt) { showToast("名称和指令不能为空", "error"); return; }

        var payload = { name: name, description: description, prompt: prompt, enabled: enabled };
        var method, url;
        if (skillEditingId) {
            method = "PUT"; url = "/api/skills/" + encodeURIComponent(skillEditingId);
        } else {
            method = "POST"; url = "/api/skills"; payload.id = id;
        }
        fetch(url, { method: method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) })
            .then(function (r) { if (!r.ok) return r.json().then(function (d) { throw new Error(d.detail); }); return r.json(); })
            .then(function () { showToast(skillEditingId ? "已更新技能" : "已创建技能", "success"); skillModal.style.display = "none"; loadSkills(); })
            .catch(function (err) { showToast("操作失败：" + err.message, "error"); });
    });

    function loadSkills() {
        fetch("/api/skills").then(function (r) { return r.json(); }).then(function (skills) {
            var container = document.getElementById("skill-list");
            if (!skills.length) { container.innerHTML = '<div class="empty-state">暂无技能，点击"新建技能"创建</div>'; return; }
            container.innerHTML = skills.map(function (s) {
                var badge = s.enabled ? '<span class="badge badge-success">已启用</span>' : '<span class="badge badge-muted">已禁用</span>';
                return '<div class="plugin-tile" data-skill-id="' + escapeHtml(s.id) + '">' +
                    '<div class="plugin-tile-header">' +
                        '<div class="plugin-avatar" style="background:#6366f1">S</div>' +
                        '<div class="plugin-tile-info">' +
                            '<div class="plugin-tile-name">' + escapeHtml(s.name) + "</div>" +
                            '<div class="plugin-tile-meta">' + badge +
                            '<span class="text-muted">' + escapeHtml(s.id) + "</span></div>" +
                        "</div>" +
                    "</div>" +
                    '<p class="plugin-tile-desc">' + escapeHtml(s.description || "") + "</p>" +
                    '<div class="plugin-tile-tags"><span class="tag">prompt</span></div>' +
                "</div>";
            }).join("");
        });
    }

    document.addEventListener("click", function (e) {
        var tile = e.target.closest("[data-skill-id]");
        if (!tile) return;
        var id = tile.getAttribute("data-skill-id");
        fetch("/api/skills").then(function (r) { return r.json(); }).then(function (skills) {
            var s = skills.find(function (x) { return x.id === id; });
            if (!s) return;
            skillEditingId = id;
            document.getElementById("skill-modal-title").textContent = "编辑技能";
            document.getElementById("skill-id-group").style.display = "none";
            document.getElementById("skill-name").value = s.name;
            document.getElementById("skill-description").value = s.description;
            document.getElementById("skill-prompt").value = s.prompt;
            document.getElementById("skill-enabled").checked = s.enabled;
            document.getElementById("skill-desc-count").textContent = (s.description || "").length;
            document.getElementById("skill-submit-btn").textContent = "保存";
            skillModal.style.display = "";
        });
    });

    /* ---- MCP 服务 ---- */
    var mcpModal = document.getElementById("mcp-modal");
    var mcpEditingId = null;

    document.getElementById("create-mcp-btn").addEventListener("click", function () {
        mcpEditingId = null;
        document.getElementById("mcp-modal-title").textContent = "添加 MCP 服务";
        document.getElementById("mcp-id-group").style.display = "";
        document.getElementById("mcp-form").reset();
        document.getElementById("mcp-enabled").checked = true;
        document.getElementById("mcp-transport").value = "stdio";
        document.getElementById("mcp-submit-btn").textContent = "立即创建";
        toggleMcpTransport();
        mcpModal.style.display = "";
    });

    function toggleMcpTransport() {
        var transport = document.getElementById("mcp-transport").value;
        var isStdio = transport === "stdio";
        document.getElementById("mcp-command-group").style.display = isStdio ? "" : "none";
        document.getElementById("mcp-args-group").style.display = isStdio ? "" : "none";
        document.getElementById("mcp-url-group").style.display = isStdio ? "none" : "";
        document.getElementById("mcp-headers-group").style.display = isStdio ? "none" : "";
        document.querySelectorAll("#mcp-transport-selector .transport-option").forEach(function (btn) {
            btn.classList.toggle("active", btn.getAttribute("data-transport") === transport);
        });
    }
    document.querySelectorAll("#mcp-transport-selector .transport-option").forEach(function (btn) {
        btn.addEventListener("click", function () {
            document.getElementById("mcp-transport").value = this.getAttribute("data-transport");
            toggleMcpTransport();
        });
    });

    document.getElementById("mcp-modal-close").addEventListener("click", function () { mcpModal.style.display = "none"; });
    document.getElementById("mcp-modal-cancel").addEventListener("click", function () { mcpModal.style.display = "none"; });
    mcpModal.addEventListener("click", function (e) { if (e.target === mcpModal) mcpModal.style.display = "none"; });

    document.getElementById("mcp-form").addEventListener("submit", function (e) {
        e.preventDefault();
        var id = document.getElementById("mcp-id").value.trim();
        var name = document.getElementById("mcp-name").value.trim();
        var transport = document.getElementById("mcp-transport").value;
        var command = document.getElementById("mcp-command").value.trim();
        var argsText = document.getElementById("mcp-args").value.trim();
        var url = document.getElementById("mcp-url").value.trim();
        var headersText = document.getElementById("mcp-headers").value.trim();
        var enabled = document.getElementById("mcp-enabled").checked;
        if (!name) { showToast("名称不能为空", "error"); return; }
        var headers = {};
        if (headersText) {
            try { headers = JSON.parse(headersText); }
            catch (err) { showToast("请求头必须是合法的 JSON 键值对", "error"); return; }
        }
        var args = argsText ? argsText.split(/\s+/) : [];
        var payload = { name: name, transport: transport, enabled: enabled };
        if (transport === "stdio") { payload.command = command; payload.args = args; }
        else { payload.url = url; payload.headers = headers; }
        var method, apiUrl;
        if (mcpEditingId) { method = "PUT"; apiUrl = "/api/mcp/" + encodeURIComponent(mcpEditingId); }
        else { method = "POST"; apiUrl = "/api/mcp"; payload.id = id; }
        fetch(apiUrl, { method: method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) })
            .then(function (r) { if (!r.ok) return r.json().then(function (d) { throw new Error(d.detail); }); return r.json(); })
            .then(function () { showToast(mcpEditingId ? "已更新 MCP 服务" : "已添加 MCP 服务", "success"); mcpModal.style.display = "none"; loadMcpServers(); })
            .catch(function (err) { showToast("操作失败：" + err.message, "error"); });
    });

    function loadMcpServers() {
        fetch("/api/mcp").then(function (r) { return r.json(); }).then(function (servers) {
            var container = document.getElementById("mcp-list");
            if (!servers.length) { container.innerHTML = '<div class="empty-state">暂无 MCP 服务，点击"添加服务"创建</div>'; return; }
            container.innerHTML = servers.map(function (s) {
                var badge = s.enabled ? '<span class="badge badge-success">已启用</span>' : '<span class="badge badge-muted">已禁用</span>';
                var transportTag = s.transport === "stdio"
                    ? '<span class="tag">' + escapeHtml(s.command || "") + "</span>"
                    : '<span class="tag">' + escapeHtml(s.url || "") + "</span>";
                return '<div class="plugin-tile" data-mcp-id="' + escapeHtml(s.id) + '">' +
                    '<div class="plugin-tile-header">' +
                        '<div class="plugin-avatar" style="background:#0ea5e9">M</div>' +
                        '<div class="plugin-tile-info">' +
                            '<div class="plugin-tile-name">' + escapeHtml(s.name) + "</div>" +
                            '<div class="plugin-tile-meta">' + badge +
                            '<span class="text-muted">' + escapeHtml(mcpTransportLabel(s.transport)) + "</span></div>" +
                        "</div>" +
                    "</div>" +
                    '<div class="plugin-tile-tags">' + transportTag + "</div>" +
                "</div>";
            }).join("");
        });
    }

    function openMcpEdit(s) {
        mcpEditingId = s.id;
        document.getElementById("mcp-modal-title").textContent = "编辑 MCP 服务";
        document.getElementById("mcp-id-group").style.display = "none";
        document.getElementById("mcp-name").value = s.name;
        document.getElementById("mcp-transport").value = s.transport;
        document.getElementById("mcp-command").value = s.command || "";
        document.getElementById("mcp-args").value = (s.args || []).join(" ");
        document.getElementById("mcp-url").value = s.url || "";
        var hdrs = s.headers || {};
        document.getElementById("mcp-headers").value = Object.keys(hdrs).length ? JSON.stringify(hdrs) : "";
        document.getElementById("mcp-enabled").checked = s.enabled;
        document.getElementById("mcp-submit-btn").textContent = "保存";
        toggleMcpTransport();
        mcpModal.style.display = "";
    }

    var currentMcpServer = null;
    var mcpDetailTools = {};
    var mcpListPane = document.getElementById("tools-pane-mcp");
    var mcpDetailPane = document.getElementById("tools-pane-mcp-detail");

    function showMcpList() {
        mcpDetailPane.style.display = "none";
        mcpListPane.style.display = "";
        loadMcpServers();
    }

    function switchMcpDetailTab(tab) {
        document.getElementById("mcp-detail-overview").style.display = tab === "overview" ? "" : "none";
        document.getElementById("mcp-detail-tools").style.display = tab === "tools" ? "" : "none";
        document.querySelectorAll(".mcp-detail-subtab").forEach(function (btn) {
            btn.classList.toggle("active", btn.getAttribute("data-detail-tab") === tab);
        });
    }

    function buildMcpConfigJson(s) {
        var entry = { transportType: s.transport };
        if (s.transport === "stdio") {
            entry.command = s.command || "";
            if (s.args && s.args.length) entry.args = s.args;
        } else {
            entry.url = s.url || "";
            if (s.headers && Object.keys(s.headers).length) entry.headers = s.headers;
        }
        var obj = { mcpServers: {} };
        obj.mcpServers[s.id] = entry;
        return JSON.stringify(obj, null, 2);
    }

    function openMcpDetail(id) {
        fetch("/api/mcp").then(function (r) { return r.json(); }).then(function (servers) {
            var s = servers.find(function (x) { return x.id === id; });
            if (!s) return;
            currentMcpServer = s;
            document.getElementById("mcp-detail-title").textContent = s.name;
            var status = s.enabled
                ? '<span class="badge badge-success">已启用</span>'
                : '<span class="badge badge-muted">已禁用</span>';
            var rows = [
                ["名称", escapeHtml(s.name)],
                ["ID", escapeHtml(s.id)],
                ["连接类型", escapeHtml(mcpTransportLabel(s.transport))],
                ["状态", status]
            ];
            if (s.transport === "stdio") rows.push(["命令", escapeHtml(s.command || "")]);
            else rows.push(["服务地址", escapeHtml(s.url || "")]);
            document.getElementById("mcp-detail-info").innerHTML = rows.map(function (r) {
                return '<div class="mcp-info-item"><span class="mcp-info-label">' + r[0] +
                    '</span><span class="mcp-info-value">' + r[1] + "</span></div>";
            }).join("");
            document.getElementById("mcp-detail-config").textContent = buildMcpConfigJson(s);
            mcpListPane.style.display = "none";
            mcpDetailPane.style.display = "";
            switchMcpDetailTab("overview");
            loadMcpDetailTools(id);
        });
    }

    function renderMcpToolFields(params) {
        params = params || {};
        var props = params.properties || {};
        var required = params.required || [];
        var keys = Object.keys(props);
        if (!keys.length) {
            return '<div class="mcp-field-empty">此工具无需参数</div>';
        }
        return keys.map(function (key) {
            var spec = props[key] || {};
            var type = spec.type || "string";
            var isRequired = required.indexOf(key) !== -1;
            var reqMark = isRequired ? '<span class="mcp-field-req">*</span>' : "";
            var desc = spec.description
                ? '<div class="mcp-field-desc">' + escapeHtml(spec.description) + "</div>" : "";
            var control;
            if (type === "boolean") {
                control = '<select class="mcp-field-input"><option value=""></option>' +
                    '<option value="true">true</option><option value="false">false</option></select>';
            } else if (type === "number" || type === "integer") {
                control = '<input type="number" class="mcp-field-input" placeholder="' + escapeHtml(type) + '">';
            } else if (type === "array" || type === "object") {
                control = '<textarea class="mcp-field-input" rows="2" placeholder="' +
                    (type === "array" ? "[...]（JSON）" : "{...}（JSON）") + '"></textarea>';
            } else {
                control = '<input type="text" class="mcp-field-input" placeholder="' + escapeHtml(type) + '">';
            }
            return '<div class="mcp-field" data-key="' + escapeHtml(key) + '" data-type="' + escapeHtml(type) + '">' +
                '<label class="mcp-field-label"><span class="mcp-field-name">' + escapeHtml(key) + "</span>" +
                reqMark + '<span class="mcp-field-type">' + escapeHtml(type) + "</span></label>" +
                desc + control +
            "</div>";
        }).join("");
    }

    function loadMcpDetailTools(id) {
        var listEl = document.getElementById("mcp-detail-tools-list");
        var countEl = document.getElementById("mcp-detail-tools-count");
        listEl.innerHTML = '<p class="text-muted">加载中…</p>';
        countEl.textContent = "";
        fetch("/api/mcp/" + encodeURIComponent(id) + "/tools")
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data.error && (!data.tools || !data.tools.length)) {
                    listEl.innerHTML = '<div class="empty-state">连接失败：' + escapeHtml(data.error) + "</div>";
                    return;
                }
                var tools = data.tools || [];
                countEl.textContent = "共 " + tools.length + " 个工具";
                mcpDetailTools = {};
                document.getElementById("mcp-tool-debug").innerHTML =
                    '<div class="mcp-debug-empty">选择左侧工具进行调试</div>';
                if (!tools.length) {
                    listEl.innerHTML = '<div class="empty-state">该服务未暴露工具</div>';
                    return;
                }
                listEl.innerHTML = tools.map(function (t) {
                    mcpDetailTools[t.name] = t;
                    return '<div class="mcp-tool-item" data-tool="' + escapeHtml(t.name) + '">' +
                        '<code class="mcp-tool-item-name">' + escapeHtml(t.name) + "</code>" +
                        '<span class="mcp-tool-desc">' + escapeHtml(t.description || "") + "</span>" +
                    "</div>";
                }).join("");
            })
            .catch(function () {
                listEl.innerHTML = '<div class="empty-state">加载工具失败</div>';
            });
    }

    function runMcpTool(toolName, fieldsEl, outputEl, btn) {
        var args = {};
        var fields = fieldsEl ? fieldsEl.querySelectorAll(".mcp-field") : [];
        for (var i = 0; i < fields.length; i++) {
            var field = fields[i];
            var key = field.getAttribute("data-key");
            var type = field.getAttribute("data-type");
            var input = field.querySelector(".mcp-field-input");
            var raw = (input.value || "").trim();
            if (raw === "") continue;
            if (type === "boolean") {
                args[key] = raw === "true";
            } else if (type === "number" || type === "integer") {
                var num = Number(raw);
                if (isNaN(num)) { outputEl.textContent = "参数 " + key + " 必须是数字"; return; }
                args[key] = num;
            } else if (type === "array" || type === "object") {
                try { args[key] = JSON.parse(raw); }
                catch (err) { outputEl.textContent = "参数 " + key + " 必须是合法 JSON"; return; }
            } else {
                args[key] = raw;
            }
        }
        btn.disabled = true;
        outputEl.textContent = "运行中…";
        fetch("/api/mcp/" + encodeURIComponent(currentMcpServer.id) + "/tools/" +
              encodeURIComponent(toolName) + "/invoke", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ arguments: args })
        })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data.ok) {
                    outputEl.textContent = typeof data.result === "string"
                        ? data.result : JSON.stringify(data.result, null, 2);
                } else {
                    outputEl.textContent = "错误：" + (data.error || "调用失败");
                }
            })
            .catch(function () { outputEl.textContent = "请求失败"; })
            .finally(function () { btn.disabled = false; });
    }

    function openMcpDebug(toolName) {
        var t = mcpDetailTools[toolName];
        if (!t) return;
        document.getElementById("mcp-tool-debug").innerHTML =
            '<div class="mcp-debug-title">调试工具 · <code>' + escapeHtml(t.name) + "</code></div>" +
            (t.description ? '<div class="mcp-debug-desc">' + escapeHtml(t.description) + "</div>" : "") +
            '<div class="mcp-tool-label">参数</div>' +
            '<div class="mcp-tool-fields">' + renderMcpToolFields(t.parameters) + "</div>" +
            '<div class="mcp-tool-run-row"><button class="btn-primary btn-sm mcp-tool-run">运行</button></div>' +
            '<div class="mcp-tool-label">输出</div>' +
            "<pre class=\"mcp-tool-output\">--</pre>";
    }

    document.getElementById("mcp-detail-back").addEventListener("click", showMcpList);
    document.querySelectorAll(".mcp-detail-subtab").forEach(function (btn) {
        btn.addEventListener("click", function () {
            switchMcpDetailTab(this.getAttribute("data-detail-tab"));
        });
    });
    document.getElementById("mcp-detail-edit").addEventListener("click", function () {
        if (currentMcpServer) openMcpEdit(currentMcpServer);
    });
    document.getElementById("mcp-detail-copy").addEventListener("click", function () {
        var text = document.getElementById("mcp-detail-config").textContent;
        navigator.clipboard.writeText(text).then(function () {
            showToast("已复制配置", "success");
        }, function () { showToast("复制失败", "error"); });
    });
    var mcpToolsListEl = document.getElementById("mcp-detail-tools-list");
    mcpToolsListEl.addEventListener("click", function (e) {
        var item = e.target.closest(".mcp-tool-item");
        if (!item) return;
        mcpToolsListEl.querySelectorAll(".mcp-tool-item").forEach(function (el) {
            el.classList.remove("active");
        });
        item.classList.add("active");
        openMcpDebug(item.getAttribute("data-tool"));
    });
    document.getElementById("mcp-tool-debug").addEventListener("click", function (e) {
        if (!e.target.classList.contains("mcp-tool-run")) return;
        var panel = document.getElementById("mcp-tool-debug");
        var active = mcpToolsListEl.querySelector(".mcp-tool-item.active");
        if (!active) return;
        runMcpTool(
            active.getAttribute("data-tool"),
            panel.querySelector(".mcp-tool-fields"),
            panel.querySelector(".mcp-tool-output"),
            e.target
        );
    });

    document.addEventListener("click", function (e) {
        var tile = e.target.closest("[data-mcp-id]");
        if (!tile) return;
        openMcpDetail(tile.getAttribute("data-mcp-id"));
    });
}

function mcpTransportLabel(transport) {
    var map = {
        stdio: "本地命令（stdio）",
        sse: "远程服务（SSE）",
        streamablehttp: "远程服务（Streamable HTTP）"
    };
    return map[transport] || transport;
}

var BOT_CHANNEL_META = {
    ilink: { icon: "iL", color: "#07c160", name: "iLink", desc: "微信 iLink 机器人" },
    wecom: { icon: "W", color: "#07c160", name: "企业微信", desc: "企业微信自建应用" },
    feishu: { icon: "F", color: "#3370ff", name: "飞书", desc: "飞书/Lark 机器人" },
};

function initBots() {
    var listEl = document.getElementById("bot-list");
    var emptyEl = document.getElementById("bot-empty");
    if (!listEl) return;

    fetch("/api/bots")
        .then(function (r) { return r.json(); })
        .then(function (bots) {
            if (!bots.length) {
                emptyEl.style.display = "";
                listEl.style.display = "none";
                return;
            }
            emptyEl.style.display = "none";
            listEl.style.display = "";
            listEl.innerHTML = bots.map(function (b) {
                var meta = BOT_CHANNEL_META[b.channel] || { icon: "?", color: "#6b7280", name: b.channel, desc: "" };
                var statusBadge = b.connected
                    ? '<span class="badge badge-success">已连接</span>'
                    : '<span class="badge badge-muted">未连接</span>';
                return '<div class="plugin-tile">' +
                    '<div class="plugin-tile-header">' +
                        '<div class="plugin-avatar" style="background:' + meta.color + '">' + meta.icon + "</div>" +
                        '<div class="plugin-tile-info">' +
                            '<div class="plugin-tile-name">' + escapeHtml(meta.name) + "</div>" +
                            '<div class="plugin-tile-meta">' + statusBadge +
                            '<span class="text-muted">' + escapeHtml(b.id) + "</span></div>" +
                        "</div>" +
                    "</div>" +
                    '<p class="plugin-tile-desc">' + escapeHtml(meta.desc) + "</p>" +
                    (b.bot_id ? '<div class="plugin-tile-tags"><span class="tag">bot_id: ' + escapeHtml(b.bot_id) + "</span></div>" : "") +
                "</div>";
            }).join("");

            var upcoming = [
                { channel: "wecom", name: "企业微信", desc: "企业微信自建应用机器人" },
                { channel: "feishu", name: "飞书", desc: "飞书/Lark 机器人" },
            ];
            var existing = bots.map(function (b) { return b.channel; });
            upcoming.forEach(function (u) {
                if (existing.indexOf(u.channel) === -1) {
                    listEl.innerHTML += '<div class="plugin-tile" style="opacity:0.5">' +
                        '<div class="plugin-tile-header">' +
                            '<div class="plugin-avatar" style="background:#d1d5db">' + u.name.charAt(0) + "</div>" +
                            '<div class="plugin-tile-info">' +
                                '<div class="plugin-tile-name">' + escapeHtml(u.name) + "</div>" +
                                '<div class="plugin-tile-meta"><span class="badge badge-muted">即将上线</span></div>' +
                            "</div>" +
                        "</div>" +
                        '<p class="plugin-tile-desc">' + escapeHtml(u.desc) + "</p>" +
                    "</div>";
                }
            });
        });
}
