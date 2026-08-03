/* Platform-only compatibility bridge.
 *
 * The restored module pages keep their proven form UI, while this bridge moves
 * their CRUD traffic to the database-backed v2 catalog.  It deliberately
 * lives only on platform pages; organization pages always use their scoped
 * v2 endpoints directly.
 */
(function () {
    "use strict";
    var nativeFetch = window.fetch.bind(window);

    function jsonResponse(value, status) {
        return new Response(JSON.stringify(value), {
            status: status || 200,
            headers: { "Content-Type": "application/json" }
        });
    }

    function payload(options) {
        if (!options || !options.body) return Promise.resolve({});
        if (typeof options.body === "string") {
            try { return Promise.resolve(JSON.parse(options.body)); }
            catch (error) { return Promise.resolve({}); }
        }
        return Promise.resolve({});
    }

    function items(type) {
        return nativeFetch("/api/v2/platform/catalog/" + type).then(function (response) {
            if (!response.ok) return response;
            return response.json().then(function (data) {
                return jsonResponse((data.items || []).map(function (item) {
                    return Object.assign({ id: item.resource_id }, item.payload || {});
                }));
            });
        });
    }

    function item(type, id) {
        return nativeFetch("/api/v2/platform/catalog/" + type + "/" + encodeURIComponent(id))
            .then(function (response) {
                if (!response.ok) return response;
                return response.json().then(function (data) {
                    return jsonResponse(Object.assign({ id: data.resource_id }, data.payload || {}));
                });
            });
    }

    function save(type, id, options) {
        return payload(options).then(function (body) {
            body.id = body.id || id;
            return nativeFetch("/api/v2/platform/catalog/" + type + "/" + encodeURIComponent(body.id), {
                method: "PUT", headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body)
            }).then(function (response) {
                if (!response.ok) return response;
                return response.json().then(function (data) {
                    var result = Object.assign({ id: data.resource_id }, data.payload || {});
                    result.restart_required = data.activation_state === "restart_required";
                    return jsonResponse(result, 200);
                });
            });
        });
    }

    function remove(type, id) {
        return nativeFetch("/api/v2/platform/catalog/" + type + "/" + encodeURIComponent(id), {
            method: "DELETE"
        });
    }

    var resourceByPath = {
        "/api/models": "models", "/api/agents": "agents",
        "/api/skills": "skills", "/api/mcp": "mcp"
    };

    window.fetch = function (input, options) {
        var raw = typeof input === "string" ? input : input.url;
        var url = new URL(raw, window.location.origin);
        var method = String((options && options.method) || "GET").toUpperCase();
        var matched = Object.keys(resourceByPath).filter(function (prefix) {
            return url.pathname === prefix || url.pathname.indexOf(prefix + "/") === 0;
        })[0];
        if (!matched) return nativeFetch(input, options);
        var type = resourceByPath[matched];
        var tail = url.pathname.slice(matched.length).replace(/^\//, "");

        if (type === "models" && ["status", "roles", "switch"].indexOf(tail) >= 0) {
            return nativeFetch(input, options);
        }

        // Non-CRUD subresources (MCP debugging and plugin installation/setup)
        // stay on their specialised endpoints.
        if (tail && (tail.indexOf("tools/") >= 0 || tail.indexOf("setup") >= 0 ||
            tail.indexOf("package") >= 0 || tail.indexOf("data") >= 0)) {
            return nativeFetch(input, options);
        }
        if (type === "agents" && tail === "active" && method === "GET") {
            return items(type).then(function (response) {
                return response.json().then(function (values) {
                    return jsonResponse(values.filter(function (value) { return value.enabled !== false; })[0] || {});
                });
            });
        }
        if (type === "agents" && tail.indexOf("knowledge-categories") >= 0) {
            return Promise.resolve(jsonResponse(method === "GET" ? { category_ids: [] } : { category_ids: [] }));
        }
        if (!tail && method === "GET") return items(type);
        if (!tail && (method === "POST" || method === "PUT")) {
            return payload(options).then(function (body) { return save(type, body.id, options); });
        }
        if (tail && tail.indexOf("/") === -1) {
            if (method === "GET") return item(type, tail);
            if (method === "PUT" || method === "PATCH") return save(type, tail, options);
            if (method === "DELETE") return remove(type, tail);
        }
        return nativeFetch(input, options);
    };
}());
