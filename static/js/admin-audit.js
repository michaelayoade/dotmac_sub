(function () {
    "use strict";

    function normalize(value) {
        return String(value || "").trim().toLocaleLowerCase();
    }

    function initActionPicker() {
        const visibleInput = document.getElementById("audit-action");
        const storedInput = document.getElementById("audit-action-value");
        const datalist = document.getElementById("audit-action-options");
        const form = document.getElementById("audit-filters");
        if (!visibleInput || !storedInput || !datalist || !form) return;

        const options = Array.from(datalist.options).map((option) => ({
            display: option.value,
            label: option.dataset.actionLabel || "",
            stored: option.dataset.actionValue || "",
        }));

        function resolveAction() {
            const typed = normalize(visibleInput.value);
            if (!typed) {
                storedInput.value = "";
                visibleInput.setCustomValidity("");
                return true;
            }
            const matches = options.filter((option) =>
                [option.display, option.label, option.stored]
                    .map(normalize)
                    .includes(typed)
            );
            if (matches.length === 1) {
                storedInput.value = matches[0].stored;
                visibleInput.setCustomValidity("");
                return true;
            }
            storedInput.value = "";
            visibleInput.setCustomValidity("Choose a recorded action from the list.");
            return false;
        }

        visibleInput.addEventListener("input", resolveAction);
        visibleInput.addEventListener("change", resolveAction);
        form.addEventListener("submit", function (event) {
            if (!resolveAction()) {
                event.preventDefault();
                visibleInput.reportValidity();
            }
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initActionPicker);
    } else {
        initActionPicker();
    }
})();
