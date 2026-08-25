/* Explicit catalog client for the platform module pages.
 *
 * Replaces the legacy fetch-hijacking bridge (platform-catalog-client.js) with
 * named functions that hit the database-backed v2 catalog directly.  Every
 * helper returns a promise that rejects with an Error whose message is the
 * server's `detail` (or a status hint), so callers can do
 * `.catch(err => showToast(msg + err.message, "error"))`.
 */
(function () {
    "use strict";

    // Catalog rows come back as {resource_id, payload, revision, status,
    // activation_state, ...}.  Flatten payload to the top level and re-expose
    // the id under `id` (payload may or may not repeat it).
    function flatten(item) {
        var payload = item.payload || {};
        var flat = Object.assign({}, payload);
        flat.id = item.resource_id;
        flat.revision = item.revision;
        flat.status = item.status;
        flat.restart_required = item.activation_state === "restart_required";
        if (item.activation_error) flat.activation_error = item.activation_error;
        return flat;
    }

    function readJson(response) {
        if (response.ok) return response.json();
        return response.json().then(
            function (data) { throw new Error((data && data.detail) || "请求失败"); },
            function () { throw new Error("请求失败(" + response.status + ")"); }
        );
    }

    function request(method, url, body) {
        var opts = { method: method, headers: {} };
        if (body !== undefined) {
            opts.headers["Content-Type"] = "application/json";
            opts.body = JSON.stringify(body);
        }
        return fetch(url, opts).then(readJson);
    }

    function list(type) {
        return request("GET", "/api/v2/platform/catalog/" + type).then(function (data) {
            return (data.items || []).map(flatten);
        });
    }

    function get(type, id) {
        return request("GET", "/api/v2/platform/catalog/" + type + "/" + encodeURIComponent(id))
            .then(flatten);
    }

    function save(type, id, payload) {
        return request(
            "PUT",
            "/api/v2/platform/catalog/" + type + "/" + encodeURIComponent(id),
            payload
        ).then(function (data) {
            return Object.assign(flatten(data), {
                restart_required: data.activation_state === "restart_required",
            });
        });
    }

    function remove(type, id) {
        return request("DELETE", "/api/v2/platform/catalog/" + type + "/" + encodeURIComponent(id))
            .then(function () { return { status: "ok" }; });
    }

    // Fetch the current payload, shallow-merge the changes, then PUT it back.
    // Use this instead of save() when only a few fields change, so a partial
    // update never wipes the rest of the payload.
    function patch(type, id, changes) {
        return get(type, id).then(function (current) {
            return save(type, id, Object.assign({}, current, changes));
        });
    }

    window.CatalogApi = {
        list: list,
        get: get,
        save: save,
        remove: remove,
        patch: patch,
        _flatten: flatten,
    };
})();
