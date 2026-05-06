(function () {
    function initTypeahead(container) {
        if (container.getAttribute("data-typeahead-ready") === "true") {
            return;
        }
        var input = container.querySelector("[data-typeahead-input]");
        var hidden = container.querySelector("[data-typeahead-hidden]");
        var results = container.querySelector("[data-typeahead-results]");
        var url = container.getAttribute("data-typeahead-url");
        var minChars = parseInt(container.getAttribute("data-typeahead-min") || "2", 10);
        var limit = parseInt(container.getAttribute("data-typeahead-limit") || "8", 10);
        if (!input || !hidden || !results || !url) {
            return;
        }
        container.setAttribute("data-typeahead-ready", "true");
        var timer = null;
        var lastQuery = "";
        var activeRequestId = 0;
        var activeController = null;

        function pluralLabel() {
            var explicitLabel = container.getAttribute("data-typeahead-label");
            if (explicitLabel) {
                return explicitLabel;
            }
            if (url.indexOf("subscribers") !== -1) {
                return "subscribers";
            }
            return "results";
        }

        function clearResults() {
            results.innerHTML = "";
        }

        function createMenu() {
            var menu = document.createElement("div");
            menu.className = "absolute z-50 mt-2 max-h-72 w-full overflow-y-auto rounded-lg border border-slate-200 bg-white shadow-lg dark:border-slate-700 dark:bg-slate-800";
            return menu;
        }

        function renderMessage(message, options) {
            var opts = options || {};
            var menu = createMenu();
            var row = document.createElement(opts.retry ? "button" : "div");
            if (opts.retry) {
                row.type = "button";
            }
            row.className = "w-full px-3 py-2 text-left text-sm text-slate-500 dark:text-slate-400";
            if (opts.retry) {
                row.className += " hover:bg-slate-50 dark:hover:bg-slate-700";
                row.addEventListener("click", function () {
                    var query = input.value.trim();
                    if (query.length >= minChars) {
                        fetchResults(query);
                    }
                });
            }
            row.textContent = message;
            menu.appendChild(row);
            results.innerHTML = "";
            results.appendChild(menu);
        }

        function abortActiveRequest() {
            if (activeController) {
                activeController.abort();
                activeController = null;
            }
        }

        function updateHiddenValue(value) {
            hidden.value = value || "";
            hidden.dispatchEvent(new Event("input", { bubbles: true }));
            hidden.dispatchEvent(new Event("change", { bubbles: true }));
        }

        function renderResults(items) {
            if (!items || !items.length) {
                renderMessage("No " + pluralLabel() + " found.");
                return;
            }
            var menu = createMenu();
            items.forEach(function (item) {
                var button = document.createElement("button");
                button.type = "button";
                button.className = "w-full px-3 py-2 text-left text-sm text-slate-700 hover:bg-slate-50 dark:text-slate-200 dark:hover:bg-slate-700";
                button.textContent = item.label || item.name || "";
                button.addEventListener("click", function () {
                    abortActiveRequest();
                    input.value = item.label || item.name || "";
                    updateHiddenValue(item.ref || item.id || "");
                    clearResults();
                });
                menu.appendChild(button);
            });
            results.innerHTML = "";
            results.appendChild(menu);
        }

        function fetchResults(query) {
            abortActiveRequest();
            activeController = typeof AbortController !== "undefined" ? new AbortController() : null;
            var controller = activeController;
            activeRequestId += 1;
            var requestId = activeRequestId;
            var requestUrl = url + "?q=" + encodeURIComponent(query) + "&limit=" + limit;
            var didTimeout = false;
            var timeout = controller
                ? window.setTimeout(function () {
                    didTimeout = true;
                    controller.abort();
                }, 10000)
                : null;
            renderMessage("Searching...");
            fetch(requestUrl, controller ? { signal: controller.signal } : {})
                .then(function (response) {
                    if (!response.ok) {
                        throw new Error("typeahead request failed");
                    }
                    return response.json();
                })
                .then(function (data) {
                    if (timeout) {
                        window.clearTimeout(timeout);
                    }
                    if (requestId !== activeRequestId) {
                        return;
                    }
                    if (activeController === controller) {
                        activeController = null;
                    }
                    renderResults((data && data.items) || []);
                })
                .catch(function (error) {
                    if (timeout) {
                        window.clearTimeout(timeout);
                    }
                    if (requestId !== activeRequestId) {
                        return;
                    }
                    if (error && error.name === "AbortError" && !didTimeout) {
                        return;
                    }
                    if (activeController === controller) {
                        activeController = null;
                    }
                    renderMessage("Could not load " + pluralLabel() + ". Try again.", { retry: true });
                });
        }

        input.addEventListener("input", function () {
            var query = input.value.trim();
            updateHiddenValue("");
            if (timer) {
                window.clearTimeout(timer);
                timer = null;
            }
            if (query.length < minChars) {
                abortActiveRequest();
                activeRequestId += 1;
                clearResults();
                lastQuery = query;
                return;
            }
            timer = window.setTimeout(function () {
                if (query !== lastQuery) {
                    fetchResults(query);
                    lastQuery = query;
                }
            }, 250);
        });

        document.addEventListener("click", function (event) {
            if (!container.contains(event.target)) {
                clearResults();
            }
        });
    }

    function initAll(root) {
        if (!root || !root.querySelectorAll) {
            root = document;
        }
        if (root.matches && root.matches("[data-typeahead-url]")) {
            initTypeahead(root);
        }
        var containers = root.querySelectorAll("[data-typeahead-url]");
        containers.forEach(function (container) {
            initTypeahead(container);
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", function () {
            initAll(document);
        });
    } else {
        initAll(document);
    }
    document.addEventListener("htmx:load", function (event) {
        initAll(event.detail && event.detail.elt ? event.detail.elt : document);
    });
    document.addEventListener("htmx:afterSettle", function (event) {
        initAll(event.detail && event.detail.elt ? event.detail.elt : document);
    });
    window.initTypeaheadFields = initAll;
})();
