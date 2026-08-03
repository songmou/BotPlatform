(function () {
    "use strict";
    function setLabel(name, value) {
        var target = document.querySelector('[data-label="' + name + '"]');
        if (target) target.textContent = value;
    }
    function count(name, url) {
        return fetch(url).then(function (response) {
            if (!response.ok) throw new Error("加载失败");
            return response.json();
        }).then(function (data) {
            var target = document.querySelector('[data-stat="' + name + '"]');
            if (target) target.textContent = String((data.items || []).length);
        }).catch(function () {
            var target = document.querySelector('[data-stat="' + name + '"]');
            if (target) target.textContent = "—";
        });
    }
    var ready = window.BP_CONTEXT_READY || Promise.resolve();
    ready.then(function () {
        var title = document.getElementById("overview-title");
        var description = document.getElementById("overview-description");
        title.textContent = "平台概览";
        description.textContent = "统一管理组织、智能体能力与运行治理；配置保存后按资源类型即时应用或提示重启。";
        count("first", "/api/v2/platform/organizations");
        count("second", "/api/v2/platform/catalog/agents");
        count("third", "/api/v2/platform/catalog/models");
        count("fourth", "/api/v2/platform/catalog/plugins");
    });
})();
