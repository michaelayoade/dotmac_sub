/**
 * Live Search — companion JS for the live_search Jinja2 macro.
 *
 * Responsibilities:
 * 1. Show/hide loading spinner on HTMX requests within [data-live-search].
 * 2. Auto-enhance caller-block elements (selects, date inputs) inside the
 *    live_search form that lack hx-get — attaches HTMX attributes so they
 *    trigger live filtering on change.
 * 3. Provide Alpine.js `liveSearch(entityType)` component for autosuggest
 *    dropdown (entity types with search API support).
 */
(function () {
    "use strict";

    /* ─── 1. Loading spinner ─────────────────────────────────────────── */

    document.body.addEventListener("htmx:beforeRequest", function (evt) {
        var container = evt.target.closest("[data-live-search]");
        if (!container) return;
        var spinner = container.querySelector("[data-live-search-spinner]");
        if (spinner) spinner.classList.remove("hidden");
    });

    document.body.addEventListener("htmx:afterRequest", function (evt) {
        var container = evt.target.closest("[data-live-search]");
        if (!container) return;
        var spinner = container.querySelector("[data-live-search-spinner]");
        if (spinner) spinner.classList.add("hidden");
    });

    /* ─── 2. Auto-enhance caller-block elements ──────────────────────── */

    function enhanceCallerElements() {
        document.querySelectorAll("[data-live-search] form").forEach(function (form) {
            // Find the search input to extract HTMX config
            var searchInput = form.querySelector("input[name='search'][hx-get]");
            if (!searchInput) return;

            var hxGet = searchInput.getAttribute("hx-get");
            var hxTarget = searchInput.getAttribute("hx-target");
            var hxSelect = searchInput.getAttribute("hx-select");

            // Enhance selects and date/number inputs that don't already have hx-get
            form.querySelectorAll("select:not([hx-get]), input[type='date']:not([hx-get]), input[type='number']:not([hx-get])").forEach(function (el) {
                // Skip the search input itself
                if (el.name === "search") return;

                el.setAttribute("hx-get", hxGet);
                el.setAttribute("hx-trigger", "change");
                el.setAttribute("hx-target", hxTarget);
                if (hxSelect) el.setAttribute("hx-select", hxSelect);
                el.setAttribute("hx-push-url", "true");
                el.setAttribute("hx-include", "closest form");

                // Tell HTMX to process the new attributes
                if (window.htmx) {
                    htmx.process(el);
                }
            });
        });
    }

    // Run on DOM ready and after HTMX swaps (in case new forms are loaded)
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", enhanceCallerElements);
    } else {
        enhanceCallerElements();
    }
    document.body.addEventListener("htmx:afterSettle", enhanceCallerElements);

    /* ─── 3. Alpine.js liveSearch component ──────────────────────────── */

    // Entity type → detail URL mapping
    var ENTITY_URLS = {
        subscribers: "/admin/subscribers/",
        subscriptions: "/admin/subscriptions/",
        invoices: "/admin/billing/invoices/",
        payments: "/admin/billing/payments/",
        service_orders: "/admin/service-orders/",
        catalog_offers: "/admin/catalog/offers/",
        organizations: "/admin/subscribers/organizations/",
        resellers: "/admin/resellers/"
    };

    window.liveSearch = function (entityType) {
        return {
            query: "",
            entityType: entityType,
            suggestions: [],
            selectedIndex: -1,
            showSuggestions: false,
            suggestLoading: false,
            hasMore: false,

            init: function () {
                // Sync initial value from the input (set by Jinja2)
                var input = this.$refs.searchInput;
                if (input && input.value) {
                    this.query = input.value;
                }
            },

            fetchSuggestions: async function () {
                if (this.query.length < 2) {
                    this.suggestions = [];
                    this.showSuggestions = false;
                    return;
                }

                this.suggestLoading = true;
                try {
                    var params = new URLSearchParams({
                        entity_type: this.entityType,
                        q: this.query,
                        limit: "10"
                    });
                    var response = await fetch("/api/v1/search/suggestions?" + params);
                    if (response.ok) {
                        var data = await response.json();
                        this.suggestions = data.suggestions || data.items || [];
                        this.hasMore = data.has_more || false;
                        this.selectedIndex = -1;
                        this.showSuggestions = true;
                    }
                } catch (err) {
                    console.error("Live search suggestions error:", err);
                    this.suggestions = [];
                } finally {
                    this.suggestLoading = false;
                }
            },

            navigateDown: function () {
                if (!this.showSuggestions) {
                    this.showSuggestions = true;
                    return;
                }
                this.selectedIndex = Math.min(this.selectedIndex + 1, this.suggestions.length - 1);
            },

            navigateUp: function () {
                this.selectedIndex = Math.max(this.selectedIndex - 1, -1);
            },

            selectCurrent: function (event) {
                if (this.selectedIndex >= 0 && this.suggestions[this.selectedIndex]) {
                    event.preventDefault();
                    this.selectSuggestion(this.suggestions[this.selectedIndex]);
                } else {
                    // Let HTMX handle the search via input event
                    this.showSuggestions = false;
                }
            },

            selectSuggestion: function (suggestion) {
                this.query = suggestion.label || suggestion.name || "";
                this.showSuggestions = false;
                this.selectedIndex = -1;

                // Navigate to entity detail page if URL mapping exists
                var baseUrl = ENTITY_URLS[this.entityType];
                var entityId = suggestion.id || suggestion.ref;
                if (baseUrl && entityId) {
                    window.location.href = baseUrl + entityId;
                } else {
                    // Fall back to setting the search value and triggering HTMX
                    var input = this.$refs.searchInput;
                    if (input) {
                        input.value = this.query;
                        htmx.trigger(input, "search");
                    }
                }
            }
        };
    };
})();
