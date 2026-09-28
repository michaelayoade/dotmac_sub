/* Governed Automation Center client-script adapter.
 *
 * Published scripts receive only a frozen form snapshot and the explicit api
 * below. They never receive document/window/fetch or a database/write client.
 */
(() => {
    "use strict";

    const bundles = new Map();
    const forms = new WeakSet();

    const sha256 = async (source) => {
        const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(source));
        return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
    };

    const fieldsFor = (form) => Object.freeze(Object.fromEntries(
        [...form.elements]
            .filter((element) => element.name && !element.disabled)
            .map((element) => [element.name, element.value])
    ));

    const loadBundle = async (target, eventName) => {
        const key = `${target}:${eventName}`;
        if (!bundles.has(key)) {
            bundles.set(key, fetch(`/admin/automation/client-scripts?target_type=${encodeURIComponent(target)}&event_name=${encodeURIComponent(eventName)}`, {
                credentials: "same-origin",
                headers: {Accept: "application/json"},
            }).then((response) => {
                if (response.status === 404 || response.status === 403) return [];
                if (!response.ok) throw new Error("Client script bundle unavailable");
                return response.json().then((body) => body.scripts || []);
            }));
        }
        return bundles.get(key);
    };

    const run = async (form, eventName, domEvent, fieldName) => {
        const scripts = await loadBundle(form.dataset.automationTarget, eventName);
        if (!scripts.length) return true;
        const errors = [];
        form.setCustomValidity("");
        const values = fieldsFor(form);
        const currentValues = {...values};
        const context = Object.freeze({
            targetType: form.dataset.automationTarget,
            recordId: form.dataset.automationRecordId || null,
            eventName,
            fieldName: fieldName || null,
            fields: values,
        });
        const api = Object.freeze({
            get: (name) => currentValues[name],
            set: (name, value) => {
                const control = form.elements.namedItem(name);
                if (!control || !("value" in control)) throw new Error(`Unknown form field: ${name}`);
                control.value = value == null ? "" : String(value);
                currentValues[name] = control.value;
                control.dispatchEvent(new Event("input", {bubbles: true}));
            },
            error: (message) => {
                const text = String(message || "Client validation failed");
                errors.push(text);
                form.setCustomValidity(text);
            },
            clearError: () => form.setCustomValidity(""),
            preventDefault: () => domEvent?.preventDefault(),
        });
        for (const script of scripts) {
            try {
                if (await sha256(script.source_code) !== script.content_sha256) {
                    throw new Error("Client script integrity check failed");
                }
                const execute = new Function("context", "api", `"use strict";\n${script.source_code}\n`); // eslint-disable-line no-new-func
                await execute(context, api);
            } catch (error) {
                console.error("Automation client script failed", script.key, error);
                if (eventName === "form.validate") api.error("A client automation validation failed.");
            }
        }
        return errors.length === 0;
    };

    const attach = (form) => {
        if (forms.has(form) || !form.dataset.automationTarget) return;
        forms.add(form);
        void run(form, "form.load", null, null);
        form.addEventListener("change", (event) => {
            const target = event.target;
            if (target instanceof HTMLElement && target.name) void run(form, "field.change", event, target.name);
        });
        form.addEventListener("submit", (event) => {
            if (form.dataset.automationBypass === "1") {
                delete form.dataset.automationBypass;
                return;
            }
            event.preventDefault();
            event.stopImmediatePropagation();
            void run(form, "form.validate", event, null).then((valid) => {
                const nativeValid = form.noValidate || form.checkValidity();
                if (valid && nativeValid) {
                    form.dataset.automationBypass = "1";
                    form.requestSubmit();
                } else if (!form.noValidate) {
                    form.reportValidity();
                }
            });
        }, true);
    };

    const scan = () => document.querySelectorAll("form[data-automation-target]").forEach(attach);
    document.addEventListener("DOMContentLoaded", scan);
    new MutationObserver(scan).observe(document.documentElement, {childList: true, subtree: true});
})();
