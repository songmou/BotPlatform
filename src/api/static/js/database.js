/* global showToast, showConfirm, escapeHtml */
function initDatabase() {
    'use strict';

    var API = '/api/datasources';
    var editingId = null;
    var currentDs = null;

    var listPane = document.getElementById('ds-pane-list');
    var detailPane = document.getElementById('ds-pane-detail');
    var modal = document.getElementById('ds-modal');

    /* ===== Init ===== */
    document.getElementById('ds-create-btn').addEventListener('click', openCreate);
    document.getElementById('ds-modal-close').addEventListener('click', closeModal);
    document.getElementById('ds-modal-cancel').addEventListener('click', closeModal);
    modal.addEventListener('click', function (e) { if (e.target === modal) closeModal(); });
    document.getElementById('ds-form').addEventListener('submit', submitForm);
    document.getElementById('ds-test-btn').addEventListener('click', testFromForm);

    document.getElementById('ds-detail-back').addEventListener('click', showList);
    document.getElementById('ds-detail-edit').addEventListener('click', function () { if (currentDs) openEdit(currentDs); });
    document.getElementById('ds-detail-toggle').addEventListener('click', toggleStatus);
    document.getElementById('ds-detail-delete').addEventListener('click', deleteCurrent);
    document.getElementById('ds-detail-test').addEventListener('click', testFromDetail);
    document.getElementById('ds-detail-copy').addEventListener('click', function () {
        var text = document.getElementById('ds-detail-config').textContent;
        navigator.clipboard.writeText(text).then(function () { showToast('已复制配置', 'success'); });
    });
    document.getElementById('ds-schema-refresh').addEventListener('click', loadSchema);
    document.getElementById('ds-schema-fetch').addEventListener('click', fetchRemoteTables);
    document.getElementById('ds-query-run').addEventListener('click', runQuery);

    /* Engine selector (like MCP transport selector) */
    document.querySelectorAll('#ds-engine-selector .transport-option').forEach(function (btn) {
        btn.addEventListener('click', function () {
            var engine = this.getAttribute('data-engine');
            document.getElementById('ds-engine').value = engine;
            var port = document.getElementById('ds-port');
            if (engine === 'mysql' && (!port.value || port.value === '5432')) port.value = '3306';
            if (engine === 'postgresql' && (!port.value || port.value === '3306')) port.value = '5432';
            document.querySelectorAll('#ds-engine-selector .transport-option').forEach(function (b) {
                b.classList.toggle('active', b === btn);
            });
        });
    });

    /* Detail subtabs */
    document.querySelectorAll('.mcp-detail-subtab').forEach(function (btn) {
        btn.addEventListener('click', function () {
            switchDetailTab(this.getAttribute('data-detail-tab'));
        });
    });

    /* Card click via event delegation */
    document.addEventListener('click', function (e) {
        var action = e.target.closest('[data-ds-action]');
        if (action) {
            /* Inline button takes precedence over opening detail */
            e.stopPropagation();
            e.preventDefault();
            var id = action.getAttribute('data-ds-id');
            var type = action.getAttribute('data-ds-action');
            if (type === 'toggle') {
                toggleStatus(id, true);
            } else if (type === 'delete') {
                deleteFromCard(id);
            }
            return;
        }
        var tile = e.target.closest('[data-ds-id]');
        if (!tile) return;
        openDetail(tile.getAttribute('data-ds-id'));
    });

    loadList();

    /* ===== List ===== */
    function loadList() {
        var container = document.getElementById('ds-list');
        container.innerHTML = '<div class="empty-state">加载中…</div>';
        fetch(API, { cache: 'no-store' })
            .then(function (r) { if (!r.ok) throw new Error('请求失败'); return r.json(); })
            .then(function (items) {
                if (!items || !items.length) {
                    container.innerHTML = '<div class="empty-state">暂无数据源，点击"添加数据源"创建</div>';
                    return;
                }
                container.innerHTML = items.map(renderCard).join('');
            })
            .catch(function (err) {
                container.innerHTML = '<div class="empty-state">加载失败：' + escapeHtml(err.message) + '</div>';
            });
    }

    function renderCard(item) {
        var badge = item.enabled
            ? '<span class="badge badge-success">已启用</span>'
            : '<span class="badge badge-muted">已禁用</span>';
        if (!item.driver_ready) badge += ' <span class="badge badge-warning">驱动未安装</span>';
        if (item.read_only) badge += ' <span class="badge badge-primary">只读</span>';
        var tableCount = (item.tables && item.tables.length) || 0;
        var toggleLabel = item.enabled ? '禁用' : '启用';
        return '<div class="plugin-tile" data-ds-id="' + escapeHtml(item.id) + '"' +
            ' style="--plugin-color:#0ea5e9" tabindex="0">' +
            '<div class="plugin-tile-header">' +
                '<div class="plugin-avatar" style="background:#0ea5e9">D</div>' +
                '<div class="plugin-tile-info">' +
                    '<div class="plugin-tile-name">' + escapeHtml(item.name) + '</div>' +
                    '<div class="plugin-tile-meta">' + badge +
                        '<span class="text-muted">' + escapeHtml(item.engine.toUpperCase()) + ' · ' +
                        escapeHtml(item.host) + ':' + item.port + '/' + escapeHtml(item.database) + '</span>' +
                    '</div>' +
                '</div>' +
            '</div>' +
            '<p class="plugin-tile-desc">已授权表 ' + tableCount + ' 张' +
            (item.username ? ' · 用户 ' + escapeHtml(item.username) : '') + '</p>' +
            '<div class="plugin-tile-tags"><span class="tag">' + escapeHtml(item.id) + '</span></div>' +
            '<div class="plugin-tile-actions">' +
                '<button type="button" class="btn-secondary btn-sm" data-ds-action="toggle" data-ds-id="' + escapeHtml(item.id) + '">' + toggleLabel + '</button>' +
                '<button type="button" class="btn-danger btn-sm" data-ds-action="delete" data-ds-id="' + escapeHtml(item.id) + '">删除</button>' +
            '</div>' +
        '</div>';
    }

    /* ===== Detail ===== */
    function showList() {
        detailPane.style.display = 'none';
        listPane.style.display = '';
        currentDs = null;
        loadList();
    }

    function openDetail(id) {
        fetch(API, { cache: 'no-store' })
            .then(function (r) { return r.json(); })
            .then(function (items) {
                var item = items.find(function (i) { return i.id === id; });
                if (!item) { showToast('数据源不存在', 'error'); return; }
                currentDs = item;
                renderDetail(item);
            })
            .catch(function () { showToast('加载失败', 'error'); });
    }

    function renderDetail(item) {
        document.getElementById('ds-detail-title').textContent = item.name;
        document.getElementById('ds-detail-engine-tag').textContent = item.engine.toUpperCase();

        var status = item.enabled
            ? '<span class="badge badge-success">已启用</span>'
            : '<span class="badge badge-muted">已禁用</span>';
        var driverStatus = item.driver_ready
            ? '<span class="badge badge-success">已就绪</span>'
            : '<span class="badge badge-warning">驱动未安装</span>';
        var readonlyBadge = item.read_only
            ? '<span class="badge badge-primary">只读</span>'
            : '<span class="badge badge-warning">可写</span>';

        /* Toggle button reflects current state */
        var toggleBtn = document.getElementById('ds-detail-toggle');
        toggleBtn.textContent = item.enabled ? '禁用' : '启用';
        toggleBtn.className = item.enabled ? 'btn-secondary' : 'btn-primary';

        var rows = [
            ['名称', escapeHtml(item.name)],
            ['ID', escapeHtml(item.id)],
            ['数据库类型', escapeHtml(item.engine.toUpperCase())],
            ['主机', escapeHtml(item.host) + ':' + item.port],
            ['数据库', escapeHtml(item.database)],
            ['用户名', escapeHtml(item.username || '-')],
            ['密码', item.password_set ? '已设置' : '未设置'],
            ['状态', status],
            ['驱动', driverStatus],
            ['读写模式', readonlyBadge],
            ['连接超时', (item.connect_timeout_seconds || 5) + ' 秒'],
            ['SQL 超时', (item.statement_timeout_seconds || 15) + ' 秒'],
            ['连接池', (item.pool_size || 3) + ' 个连接'],
            ['最大行数', (item.max_rows || 200) + ' 行'],
            ['最大字节', (item.max_result_bytes || 262144) + ' B'],
        ];
        document.getElementById('ds-detail-info').innerHTML = rows.map(function (r) {
            return '<div class="mcp-info-item"><span class="mcp-info-label">' + r[0] +
                '</span><span class="mcp-info-value">' + r[1] + '</span></div>';
        }).join('');

        /* Config JSON preview */
        var config = {
            id: item.id, name: item.name, engine: item.engine,
            host: item.host, port: item.port, database: item.database,
            username: item.username || '', enabled: item.enabled, read_only: item.read_only,
            connect_timeout_seconds: item.connect_timeout_seconds || 5,
            statement_timeout_seconds: item.statement_timeout_seconds || 15,
            pool_size: item.pool_size || 3,
            max_rows: item.max_rows || 200,
            max_result_bytes: item.max_result_bytes || 262144,
        };
        if (item.tables && item.tables.length) config.tables = item.tables;
        document.getElementById('ds-detail-config').textContent = JSON.stringify(config, null, 2);

        /* Reset test result */
        document.getElementById('ds-detail-test-result').style.display = 'none';

        listPane.style.display = 'none';
        detailPane.style.display = '';
        switchDetailTab('overview');
    }

    function switchDetailTab(tab) {
        document.getElementById('ds-detail-overview').style.display = tab === 'overview' ? '' : 'none';
        document.getElementById('ds-detail-schema').style.display = tab === 'schema' ? '' : 'none';
        document.getElementById('ds-detail-query').style.display = tab === 'query' ? '' : 'none';
        document.getElementById('ds-detail-audit').style.display = tab === 'audit' ? '' : 'none';
        document.querySelectorAll('.mcp-detail-subtab').forEach(function (btn) {
            btn.classList.toggle('active', btn.getAttribute('data-detail-tab') === tab);
        });
        if (tab === 'schema' && currentDs) loadSchema();
        if (tab === 'audit' && currentDs) loadAudit();
    }

    /* ===== Schema ===== */
    function loadSchema() {
        if (!currentDs) return;
        var el = document.getElementById('ds-schema-list');
        el.innerHTML = '<div class="empty-state">加载表结构…</div>';
        fetch(API + '/' + encodeURIComponent(currentDs.id) + '/schema', { cache: 'no-store' })
            .then(function (r) { if (!r.ok) throw new Error('请求失败'); return r.json(); })
            .then(function (data) {
                var tables = data.tables || [];
                if (!tables.length) {
                    el.innerHTML = '<div class="empty-state">暂无已授权表。点击上方「拉取远端表」从数据库中选择需要授权的表。</div>';
                    return;
                }
                el.innerHTML = tables.map(renderSchemaTable).join('');
            })
            .catch(function (err) {
                el.innerHTML = '<div class="empty-state">加载表结构失败：' + escapeHtml(err.message) + '</div>';
            });
    }

    /* ===== Fetch remote tables & authorize ===== */
    function fetchRemoteTables() {
        if (!currentDs) return;
        var el = document.getElementById('ds-schema-list');
        el.innerHTML = '<div class="empty-state">正在拉取远端表…</div>';
        fetch(API + '/' + encodeURIComponent(currentDs.id) + '/tables?refresh=true', { cache: 'no-store' })
            .then(function (r) { if (!r.ok) throw new Error('请求失败'); return r.json(); })
            .then(function (data) {
                var tables = data.tables || [];
                if (!tables.length) {
                    el.innerHTML = '<div class="empty-state">远端数据库没有表</div>';
                    return;
                }
                /* Build a set of currently authorised table keys for pre-checking */
                var authorised = {};
                (currentDs.tables || []).forEach(function (t) {
                    var key = (t.schema || currentDs.database) + '.' + t.name;
                    authorised[key.toLowerCase()] = true;
                });
                renderTableSelector(el, tables, authorised);
            })
            .catch(function (err) {
                el.innerHTML = '<div class="empty-state">拉取远端表失败：' + escapeHtml(err.message) + '</div>';
            });
    }

    function renderTableSelector(container, tables, authorised) {
        var rows = tables.map(function (t, idx) {
            var key = (t.schema || '') + '.' + t.name;
            var checked = authorised[key.toLowerCase()] ? ' checked' : '';
            var label = escapeHtml(t.name);
            if (t.schema && t.schema !== currentDs.database) label += ' (' + escapeHtml(t.schema) + ')';
            var desc = '';
            if (t.comment) desc += escapeHtml(t.comment);
            if (t.estimated_rows) desc += (desc ? ' · ' : '') + '~' + t.estimated_rows + ' 行';
            return '<label class="ds-table-pick">' +
                '<input type="checkbox" class="ds-table-check" data-idx="' + idx + '"' + checked + '>' +
                '<div class="ds-table-label">' +
                    '<span class="ds-table-name">' + label + '</span>' +
                    (desc ? '<span class="ds-table-desc">' + desc + '</span>' : '') +
                '</div>' +
                '<input type="text" class="ds-table-alias" placeholder="备注（可选）" data-idx="' + idx + '"' +
                ' value="' + (function () {
                    var a = (currentDs.tables || []).find(function (x) {
                        return (x.schema || '').toLowerCase() === (t.schema || '').toLowerCase() &&
                            x.name.toLowerCase() === t.name.toLowerCase();
                    });
                    return a && a.description ? escapeHtml(a.description) : '';
                })() + '">' +
            '</label>';
        }).join('');

        container.innerHTML =
            '<div class="ds-table-picker">' +
                '<div class="ds-table-picker-toolbar">' +
                    '<label class="checkbox-label"><input type="checkbox" id="ds-table-all"> 全选</label>' +
                    '<span class="text-muted ds-table-count">已选 0 张</span>' +
                    '<div class="ds-table-picker-btns">' +
                        '<button type="button" class="btn-secondary btn-sm" id="ds-table-cancel">取消</button>' +
                        '<button type="button" class="btn-primary btn-sm" id="ds-table-save">保存授权</button>' +
                    '</div>' +
                '</div>' +
                '<div class="ds-table-picker-list">' + rows + '</div>' +
            '</div>';

        /* Wire up events */
        var countEl = container.querySelector('.ds-table-count');
        var checkEls = container.querySelectorAll('.ds-table-check');
        var allCheck = document.getElementById('ds-table-all');

        function updateCount() {
            var n = Array.prototype.filter.call(checkEls, function (c) { return c.checked; }).length;
            countEl.textContent = '已选 ' + n + ' 张';
            allCheck.checked = n === checkEls.length;
        }

        checkEls.forEach(function (cb) { cb.addEventListener('change', updateCount); });
        allCheck.addEventListener('change', function () {
            checkEls.forEach(function (cb) { cb.checked = allCheck.checked; });
            updateCount();
        });
        updateCount();

        document.getElementById('ds-table-cancel').addEventListener('click', loadSchema);
        document.getElementById('ds-table-save').addEventListener('click', function () {
            saveAuthorizedTables(tables, checkEls, container);
        });
    }

    function saveAuthorizedTables(tables, checkEls, container) {
        var selected = [];
        checkEls.forEach(function (cb) {
            if (!cb.checked) return;
            var idx = parseInt(cb.getAttribute('data-idx'), 10);
            var tbl = tables[idx];
            var aliasEl = container.querySelector('.ds-table-alias[data-idx="' + idx + '"]');
            var entry = { schema: tbl.schema || '', name: tbl.name };
            if (aliasEl && aliasEl.value.trim()) entry.description = aliasEl.value.trim();
            selected.push(entry);
        });

        var saveBtn = document.getElementById('ds-table-save');
        saveBtn.disabled = true;
        saveBtn.textContent = '保存中…';
        fetch(API + '/' + encodeURIComponent(currentDs.id), {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ tables: selected }),
        })
            .then(function (r) { if (!r.ok) return r.json().then(function (d) { throw new Error(d.detail || '保存失败'); }); return r.json(); })
            .then(function () {
                showToast('已授权 ' + selected.length + ' 张表', 'success');
                /* Refresh currentDs to reflect the updated tables list */
                openDetail(currentDs.id);
            })
            .catch(function (err) {
                showToast(String(err.message || err), 'error');
                saveBtn.disabled = false;
                saveBtn.textContent = '保存授权';
            });
    }

    function renderSchemaTable(tbl) {
        var html = '<div class="ds-schema-table">' +
            '<div class="ds-schema-table-header">' + escapeHtml(tbl.name);
        if (tbl.description) html += ' <small>' + escapeHtml(tbl.description) + '</small>';
        html += '</div><table><thead><tr><th>字段</th><th>类型</th><th>主键</th><th>可空</th><th>注释</th><th>默认值</th></tr></thead><tbody>';
        (tbl.columns || []).forEach(function (col) {
            html += '<tr>' +
                '<td><code>' + escapeHtml(col.name) + '</code></td>' +
                '<td>' + escapeHtml(col.type) + '</td>' +
                '<td>' + (col.is_pk ? '&#10003;' : '') + '</td>' +
                '<td>' + (col.nullable ? '是' : '否') + '</td>' +
                '<td>' + escapeHtml(col.comment || '') + '</td>' +
                '<td>' + escapeHtml(col.default || '') + '</td>' +
            '</tr>';
        });
        html += '</tbody></table></div>';
        return html;
    }

    /* ===== Query ===== */
    function runQuery() {
        var sql = document.getElementById('ds-query-sql').value.trim();
        if (!sql) { showToast('请输入 SQL', 'error'); return; }
        if (!currentDs) return;
        var status = document.getElementById('ds-query-status');
        var result = document.getElementById('ds-query-result');
        status.textContent = '执行中…';
        result.innerHTML = '';
        fetch(API + '/' + encodeURIComponent(currentDs.id) + '/query', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ sql: sql, limit: 200 }),
        })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data.error) {
                    status.textContent = '';
                    result.innerHTML = '<div class="ds-error-msg">' + escapeHtml(data.error) + '</div>';
                    return;
                }
                status.textContent = data.row_count + ' 行 / ' + data.duration_ms + 'ms' +
                    (data.truncated ? '（已截断）' : '');
                if (!data.columns || !data.rows) {
                    result.innerHTML = '<div class="empty-state">无数据</div>';
                    return;
                }
                var html = '<table><thead><tr>';
                data.columns.forEach(function (c) { html += '<th>' + escapeHtml(c) + '</th>'; });
                html += '</tr></thead><tbody>';
                data.rows.forEach(function (row) {
                    html += '<tr>';
                    row.forEach(function (val) {
                        html += '<td>' + (val === null ? '<em>NULL</em>' : escapeHtml(String(val))) + '</td>';
                    });
                    html += '</tr>';
                });
                html += '</tbody></table>';
                result.innerHTML = html;
            })
            .catch(function (err) {
                status.textContent = '';
                result.innerHTML = '<div class="ds-error-msg">请求失败：' + escapeHtml(String(err)) + '</div>';
            });
    }

    /* ===== Audit ===== */
    function loadAudit() {
        if (!currentDs) return;
        var el = document.getElementById('ds-audit-list');
        el.innerHTML = '<div class="empty-state">加载审计记录…</div>';
        fetch(API + '/' + encodeURIComponent(currentDs.id) + '/audit?limit=30', { cache: 'no-store' })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                var rows = data.rows || [];
                if (!rows.length) { el.innerHTML = '<div class="empty-state">暂无审计记录</div>'; return; }
                el.innerHTML = rows.map(renderAuditEntry).join('');
            })
            .catch(function () { el.innerHTML = '<div class="empty-state">加载审计记录失败</div>'; });
    }

    function renderAuditEntry(r) {
        var statusBadge = r.status === 'ok'
            ? '<span class="badge badge-success">成功</span>'
            : '<span class="badge badge-danger">失败</span>';
        return '<details class="ds-audit-entry">' +
            '<summary>' +
                '<span class="ds-audit-ts">' + escapeHtml(r.created_at || '') + '</span>' +
                '<code class="ds-audit-sql">' + escapeHtml((r.sql_text || '').slice(0, 100)) + '</code>' +
                '<span class="ds-audit-meta">' +
                    '<span class="badge badge-muted">' + escapeHtml(r.statement_kind || '') + '</span>' +
                    (r.tables ? '<span class="text-muted">' + escapeHtml(r.tables) + '</span>' : '') +
                '</span>' +
                '<span class="text-muted">' + (r.row_count || 0) + '行</span>' +
                statusBadge +
            '</summary>' +
            '<div class="ds-audit-detail">' +
                '<div class="ds-audit-detail-row"><span>SQL 全文</span><code>' + escapeHtml(r.sql_text || '') + '</code></div>' +
                '<div class="ds-audit-detail-row"><span>涉及表</span><code>' + escapeHtml(r.tables || '-') + '</code></div>' +
                '<div class="ds-audit-detail-row"><span>耗时</span><code>' + (r.duration_ms || 0) + ' ms</code></div>' +
                '<div class="ds-audit-detail-row"><span>行数</span><code>' + (r.row_count || 0) + '</code></div>' +
                (r.error ? '<div class="ds-audit-detail-row"><span>错误</span><code>' + escapeHtml(r.error) + '</code></div>' : '') +
            '</div>' +
        '</details>';
    }

    /* ===== Modal: create / edit ===== */
    function openCreate() {
        editingId = null;
        document.getElementById('ds-modal-title').textContent = '添加数据源';
        document.getElementById('ds-id-group').style.display = '';
        document.getElementById('ds-form').reset();
        document.getElementById('ds-engine').value = 'mysql';
        document.getElementById('ds-port').value = '3306';
        document.getElementById('ds-enabled').checked = true;
        document.getElementById('ds-readonly').checked = true;
        document.getElementById('ds-submit-btn').textContent = '立即创建';
        document.getElementById('ds-test-result').style.display = 'none';
        toggleEngineSelector('mysql');
        modal.style.display = '';
    }

    function openEdit(item) {
        editingId = item.id;
        document.getElementById('ds-modal-title').textContent = '编辑数据源';
        document.getElementById('ds-id-group').style.display = 'none';
        document.getElementById('ds-form-id').value = item.id;
        document.getElementById('ds-id').value = item.id;
        document.getElementById('ds-name').value = item.name;
        document.getElementById('ds-engine').value = item.engine;
        document.getElementById('ds-host').value = item.host;
        document.getElementById('ds-port').value = item.port;
        document.getElementById('ds-database').value = item.database;
        document.getElementById('ds-username').value = item.username || '';
        document.getElementById('ds-password').value = '';
        document.getElementById('ds-password').placeholder = item.password_set ? '留空表示保持不变' : '';
        document.getElementById('ds-enabled').checked = item.enabled;
        document.getElementById('ds-readonly').checked = item.read_only;
        document.getElementById('ds-connect-timeout').value = item.connect_timeout_seconds || 5;
        document.getElementById('ds-statement-timeout').value = item.statement_timeout_seconds || 15;
        document.getElementById('ds-pool-size').value = item.pool_size || 3;
        document.getElementById('ds-max-rows').value = item.max_rows || 200;
        document.getElementById('ds-max-bytes').value = item.max_result_bytes || 262144;
        document.getElementById('ds-submit-btn').textContent = '保存';
        document.getElementById('ds-test-result').style.display = 'none';
        toggleEngineSelector(item.engine);
        modal.style.display = '';
    }

    function toggleEngineSelector(engine) {
        document.querySelectorAll('#ds-engine-selector .transport-option').forEach(function (btn) {
            btn.classList.toggle('active', btn.getAttribute('data-engine') === engine);
        });
    }

    function closeModal() {
        modal.style.display = 'none';
    }

    function submitForm(e) {
        e.preventDefault();
        var isNew = !editingId;
        var payload = {
            name: document.getElementById('ds-name').value.trim(),
            engine: document.getElementById('ds-engine').value,
            host: document.getElementById('ds-host').value.trim(),
            port: parseInt(document.getElementById('ds-port').value, 10) || 0,
            database: document.getElementById('ds-database').value.trim(),
            username: document.getElementById('ds-username').value.trim(),
            password: document.getElementById('ds-password').value,
            enabled: document.getElementById('ds-enabled').checked,
            read_only: document.getElementById('ds-readonly').checked,
            connect_timeout_seconds: parseInt(document.getElementById('ds-connect-timeout').value, 10) || 5,
            statement_timeout_seconds: parseInt(document.getElementById('ds-statement-timeout').value, 10) || 15,
            pool_size: parseInt(document.getElementById('ds-pool-size').value, 10) || 3,
            max_rows: parseInt(document.getElementById('ds-max-rows').value, 10) || 200,
            max_result_bytes: parseInt(document.getElementById('ds-max-bytes').value, 10) || 262144,
        };
        if (!payload.name || !payload.engine || !payload.host || !payload.database) {
            showToast('请填写必填字段', 'error');
            return;
        }

        /* Pre-check: detect missing driver before saving */
        preCheckDriver(isNew, payload, function () { doSubmit(payload, isNew); });
    }

    function preCheckDriver(isNew, payload, onProceed) {
        var testUrl, testBody, testHeaders;
        if (isNew || payload.password) {
            /* Create or edit with password entered: use generic /test */
            testUrl = API + '/test';
            testHeaders = { 'Content-Type': 'application/json' };
            testBody = JSON.stringify({
                engine: payload.engine,
                host: payload.host,
                port: payload.port,
                database: payload.database,
                username: payload.username,
                password: payload.password || '',
                connect_timeout_seconds: payload.connect_timeout_seconds,
                statement_timeout_seconds: payload.statement_timeout_seconds,
            });
        } else {
            /* Edit without new password: use saved-password test endpoint */
            testUrl = API + '/' + encodeURIComponent(editingId) + '/test';
            testHeaders = {};
            testBody = null;
        }
        fetch(testUrl, {
            method: 'POST',
            headers: testHeaders,
            body: testBody,
        })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (!data.ok && isDriverError(data.error)) {
                    var result = document.getElementById('ds-test-result');
                    result.style.display = '';
                    result.className = 'ds-test-result error';
                    result.textContent = '连接失败：' + data.error;
                    promptInstallDrivers(result, function () { onProceed(); });
                } else {
                    onProceed();
                }
            })
            .catch(function () { onProceed(); });
    }

    function doSubmit(payload, isNew) {
        var method, url;
        if (isNew) {
            method = 'POST'; url = API;
            payload.id = document.getElementById('ds-id').value.trim();
            if (!payload.id) { showToast('请填写数据源 ID', 'error'); return; }
        } else {
            method = 'PUT'; url = API + '/' + encodeURIComponent(editingId);
            if (!payload.password) payload.password = null;
        }
        fetch(url, {
            method: method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        })
            .then(function (r) {
                if (!r.ok) return r.json().then(function (d) { throw new Error(d.detail || '请求失败'); });
                return r.json();
            })
            .then(function () {
                modal.style.display = 'none';
                showToast(isNew ? '数据源已添加' : '数据源已更新', 'success');
                if (currentDs && currentDs.id === editingId) {
                    openDetail(editingId);
                } else {
                    loadList();
                }
            })
            .catch(function (err) { showToast(String(err.message || err), 'error'); });
    }

    function testFromForm() {
        var result = document.getElementById('ds-test-result');
        result.style.display = '';
        result.className = 'ds-test-result';
        result.textContent = '测试中…';
        var payload = {
            engine: document.getElementById('ds-engine').value,
            host: document.getElementById('ds-host').value.trim(),
            port: parseInt(document.getElementById('ds-port').value, 10) || 0,
            database: document.getElementById('ds-database').value.trim(),
            username: document.getElementById('ds-username').value.trim(),
            password: document.getElementById('ds-password').value,
            connect_timeout_seconds: parseInt(document.getElementById('ds-connect-timeout').value, 10) || 5,
            statement_timeout_seconds: parseInt(document.getElementById('ds-statement-timeout').value, 10) || 15,
        };
        fetch(API + '/test', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data.ok) {
                    result.className = 'ds-test-result success';
                    result.textContent = '连接成功！延迟 ' + data.latency_ms + 'ms，版本 ' + data.version;
                } else {
                    result.className = 'ds-test-result error';
                    result.textContent = '连接失败：' + data.error;
                    if (isDriverError(data.error)) {
                        promptInstallDrivers(result, function () { testFromForm(); });
                    }
                }
            })
            .catch(function (err) {
                result.className = 'ds-test-result error';
                result.textContent = '测试失败：' + String(err);
            });
    }

    function testFromDetail() {
        if (!currentDs) return;
        var el = document.getElementById('ds-detail-test-result');
        el.style.display = '';
        el.className = 'ds-test-result';
        el.textContent = '测试中…';
        fetch(API + '/' + encodeURIComponent(currentDs.id) + '/test', {
            method: 'POST',
        })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data.ok) {
                    el.className = 'ds-test-result success';
                    el.textContent = '连接成功！延迟 ' + data.latency_ms + 'ms，版本 ' + data.version;
                } else {
                    el.className = 'ds-test-result error';
                    el.textContent = '连接失败：' + data.error;
                    if (isDriverError(data.error)) {
                        promptInstallDrivers(el, function () { testFromDetail(); });
                    }
                }
            })
            .catch(function (err) {
                el.className = 'ds-test-result error';
                el.textContent = '测试失败：' + String(err);
            });
    }

    function isDriverError(msg) {
        if (!msg) return false;
        return msg.indexOf('未安装') >= 0 && msg.indexOf('驱动') >= 0;
    }

    function promptInstallDrivers(resultEl, onDone) {
        /* Temporarily swap confirm button labels to "安装" / "取消" */
        var okBtn = document.getElementById('confirm-ok');
        var cancelBtn = document.getElementById('confirm-cancel');
        var origOk = okBtn.textContent;
        var origCancel = cancelBtn.textContent;
        okBtn.textContent = '安装';
        cancelBtn.textContent = '取消';
        showConfirm('检测到数据库驱动未安装，是否自动安装？\n安装完成后将自动重新测试连接。').then(function (ok) {
            okBtn.textContent = origOk;
            cancelBtn.textContent = origCancel;
            if (!ok) return;
            resultEl.className = 'ds-test-result';
            resultEl.textContent = '正在安装数据库驱动…';
            fetch(API + '/install-drivers', { method: 'POST' })
                .then(function (r) { return r.json(); })
                .then(function (data) {
                    if (data.ok) {
                        resultEl.className = 'ds-test-result success';
                        resultEl.textContent = '驱动安装成功，正在重新测试连接…';
                        showToast('数据库驱动安装成功', 'success');
                        if (onDone) setTimeout(onDone, 500);
                    } else {
                        resultEl.className = 'ds-test-result error';
                        var detail = data.stderr || data.stdout || '';
                        resultEl.textContent = '驱动安装失败' + (detail ? '：' + detail.slice(0, 300) : '');
                    }
                })
                .catch(function (err) {
                    resultEl.className = 'ds-test-result error';
                    resultEl.textContent = '安装请求失败：' + String(err);
                });
        });
    }

    function deleteCurrent() {
        if (!currentDs) return;
        var id = currentDs.id;
        showConfirm('确定删除数据源"' + id + '"吗？此操作不可撤销。').then(function (ok) {
            if (!ok) return null;
            return fetch(API + '/' + encodeURIComponent(id), { method: 'DELETE' });
        }).then(function (response) {
            if (!response) return;
            if (!response.ok) return response.json().then(function (d) { throw new Error(d.detail || '删除失败'); });
            showToast('数据源已删除', 'success');
            showList();
        }).catch(function (err) { showToast(String(err.message || err), 'error'); });
    }

    function deleteFromCard(id) {
        showConfirm('确定删除数据源"' + id + '"吗？此操作不可撤销。').then(function (ok) {
            if (!ok) return null;
            return fetch(API + '/' + encodeURIComponent(id), { method: 'DELETE' });
        }).then(function (response) {
            if (!response) return;
            if (!response.ok) return response.json().then(function (d) { throw new Error(d.detail || '删除失败'); });
            showToast('数据源已删除', 'success');
            loadList();
        }).catch(function (err) { showToast(String(err.message || err), 'error'); });
    }

    function toggleStatus(dsId, fromCard) {
        var id = dsId || (currentDs && currentDs.id);
        if (!id) return;
        /* Look up current state to determine the target state */
        fetch(API, { cache: 'no-store' })
            .then(function (r) { return r.json(); })
            .then(function (items) {
                var item = items.find(function (i) { return i.id === id; });
                if (!item) { showToast('数据源不存在', 'error'); return; }
                var next = !item.enabled;
                var verb = next ? '启用' : '禁用';
                return fetch(API + '/' + encodeURIComponent(id) + '/status', {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ enabled: next }),
                }).then(function (r) {
                    if (!r.ok) return r.json().then(function (d) { throw new Error(d.detail || '操作失败'); });
                    return r.json();
                }).then(function () {
                    showToast('数据源已' + verb, 'success');
                    if (fromCard) {
                        loadList();
                    } else if (currentDs && currentDs.id === id) {
                        openDetail(id);
                    } else {
                        loadList();
                    }
                });
            })
            .catch(function (err) { showToast(String(err.message || err), 'error'); });
    }
}
