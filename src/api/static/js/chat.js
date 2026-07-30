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
                allAgents = data.agents.filter(function (a) { return a.enabled !== false; });
                selectedAgentIds = allAgents
                    .filter(function (a) { return a.id === data.activeId; })
                    .map(function (a) { return a.id; });
                if (selectedAgentIds.length === 0 && allAgents.length > 0) {
                    selectedAgentIds = [allAgents[0].id];
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

    function renderMarkdown(target, content) {
        var source = typeof content === "string" ? content : String(content || "");
        try {
            if (!window.marked || typeof window.marked.parse !== "function" ||
                    !window.DOMPurify || typeof window.DOMPurify.sanitize !== "function") {
                throw new Error("Markdown 渲染依赖不可用");
            }
            var parsed = window.marked.parse(source, { async: false });
            target.innerHTML = window.DOMPurify.sanitize(parsed, {
                USE_PROFILES: { html: true },
            });
            target.classList.remove("markdown-fallback");
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
        } catch (error) {
            target.textContent = source;
            target.classList.add("markdown-fallback");
        }
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
            renderMarkdown(contentDiv, content);
            bubble.appendChild(contentDiv);
            contentEl = contentDiv;
        } else {
            bubble.textContent = content;
        }
        msg.appendChild(avatar);
        msg.appendChild(bubble);
        row.appendChild(msg);
        messagesEl.appendChild(row);
        scrollToBottom();
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

    function addFeedbackActions(row, runId) {
        if (!runId) return;
        var actions = row.querySelector(".msg-actions");
        if (!actions || actions.querySelector("[data-feedback]")) return;
        ["good", "bad"].forEach(function (rating) {
            var btn = document.createElement("button");
            btn.className = "msg-action-btn";
            btn.setAttribute("data-feedback", rating);
            btn.innerHTML = rating === "good" ? "<span>👍 好评</span>" : "<span>👎 差评</span>";
            btn.addEventListener("click", function () {
                var reasons = [];
                var comment = "";
                if (rating === "bad") {
                    var reason = window.prompt(
                        "差评原因：答非所问、事实错误、格式表达、工具执行失败、响应过慢、其他",
                        "答非所问"
                    );
                    if (reason === null) return;
                    reasons = [reason.trim() || "其他"];
                    comment = window.prompt("补充说明（可选，最多 500 字）", "") || "";
                }
                fetch("/api/model-runs/" + encodeURIComponent(runId) + "/feedback", {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ rating: rating, reasons: reasons, comment: comment }),
                }).then(function (r) {
                    if (!r.ok) return r.json().then(function (d) { throw new Error(d.detail); });
                    row.querySelectorAll("[data-feedback]").forEach(function (item) {
                        item.classList.toggle("selected", item.getAttribute("data-feedback") === rating);
                    });
                    showToast("感谢反馈", "success");
                }).catch(function (err) {
                    showToast("提交反馈失败：" + err.message, "error");
                });
            });
            actions.appendChild(btn);
        });
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

    var IMAGE_URL_RE = /\.(png|jpe?g|gif|webp|bmp|svg)(\?[^\s)"']*)?$/i;

    function inlineImages(el) {
        el.querySelectorAll("a").forEach(function (a) {
            var href = a.getAttribute("href") || "";
            if (!IMAGE_URL_RE.test(href.split("#")[0])) return;
            var img = document.createElement("img");
            img.src = href;
            img.alt = a.textContent || "";
            a.replaceWith(img);
        });
        el.querySelectorAll("img").forEach(function (img) {
            img.addEventListener("load", scrollToBottom);
        });
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
        var currentRunId = null;
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
                if (summaryRow && summaryRow.parentElement === messagesEl) {
                    messagesEl.insertBefore(traceContainer, summaryRow);
                } else {
                    messagesEl.appendChild(traceContainer);
                }
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
            } else if (ev.type === "tool_call" || ev.type === "tool_result") {
                // 工具调用过程不在页面展示
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
                    renderMarkdown(streamContentEl, fullText);
                }
                scrollToBottom();
            } else if (ev.type === "error") {
                fullText += "\n\n⚠️ " + ev.message;
                if (streamContentEl) {
                    renderMarkdown(streamContentEl, fullText);
                }
                showToast(ev.message, "error");
            } else if (ev.type === "done") {
                fullText = ev.full_text || fullText;
                currentRunId = ev.run_id || currentRunId;
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
                inlineImages(streamContentEl || summaryBubble);
            }
            if (summaryRow) {
                var getText = function () { return fullText; };
                addAssistantActions(summaryRow, getText);
                addFeedbackActions(summaryRow, currentRunId);
                setRegenerate(summaryRow, getText, userText);
            }
            if (sourcesData.length > 0 && summaryRow) {
                var sourcesEl = document.createElement("div");
                sourcesEl.className = "msg-sources";
                sourcesEl.innerHTML = '<span class="msg-sources-label">参考来源：</span>' +
                    sourcesData.map(function (s) {
                        var label = "[" + Number(s.citation || 0) + "] " +
                            (s.category_name ? s.category_name + " / " : "") +
                            (s.source_name || s.name || "") +
                            (s.heading ? " · " + s.heading : "");
                        if (s.download_url) {
                            return '<a class="msg-source-item" href="' +
                                escapeHtml(s.download_url) + '">' +
                                escapeHtml(label) + "</a>";
                        }
                        return '<span class="msg-source-item">' +
                            escapeHtml(label) + "</span>";
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
            .then(function (r) {
                if (!r.ok) throw new Error("HTTP " + r.status);
                return r.json();
            })
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
                        inlineImages(refs.bubble);
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
            })
            .catch(function (err) {
                showToast("加载会话历史失败：" + err.message, "error");
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
