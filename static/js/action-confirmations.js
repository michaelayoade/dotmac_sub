(function () {
    'use strict';

    function inferVariant(element, message) {
        var explicit = element && element.getAttribute('data-confirm-variant');
        if (explicit) return explicit;
        var text = (message || '').toLowerCase();
        if (/delete|remove|factory reset|decommission|erase|permanent|revoke/.test(text)) {
            return 'danger';
        }
        if (/reboot|disable|disconnect|archive|stop|replace|apply|push|release/.test(text)) {
            return 'warning';
        }
        return 'info';
    }

    function inferTitle(element, variant) {
        var explicit = element && element.getAttribute('data-confirm-title');
        if (explicit) return explicit;
        if (variant === 'danger') return 'Confirm destructive action';
        if (variant === 'warning') return 'Confirm device action';
        return 'Confirm action';
    }

    function inferLabel(element, variant, message) {
        var explicit = element && element.getAttribute('data-confirm-label');
        if (explicit) return explicit;
        if (variant === 'danger' && /delete|remove/.test((message || '').toLowerCase())) {
            return 'Delete';
        }
        return 'Continue';
    }

    function openConfirmation(element, message, onConfirm) {
        var variant = inferVariant(element, message);
        window.dispatchEvent(new CustomEvent('confirm-action', {
            detail: {
                title: inferTitle(element, variant),
                message: message,
                variant: variant,
                confirmLabel: inferLabel(element, variant, message),
                onConfirm: onConfirm
            }
        }));
    }

    document.addEventListener('htmx:confirm', function (event) {
        var detail = event.detail || {};
        if (!detail.question || typeof detail.issueRequest !== 'function') return;
        event.preventDefault();
        openConfirmation(detail.elt, detail.question, function () {
            detail.issueRequest(true);
        });
    });

    document.addEventListener('submit', function (event) {
        var form = event.target;
        if (!(form instanceof HTMLFormElement)) return;
        var submitter = event.submitter;
        var confirmationSource = submitter && submitter.hasAttribute('data-confirm')
            ? submitter
            : form;
        var message = confirmationSource.getAttribute('data-confirm');
        if (!message || form.dataset.confirmBypass === 'true') return;

        event.preventDefault();
        event.stopImmediatePropagation();
        openConfirmation(confirmationSource, message, function () {
            form.dataset.confirmBypass = 'true';
            try {
                form.requestSubmit(submitter || undefined);
            } finally {
                window.setTimeout(function () {
                    delete form.dataset.confirmBypass;
                }, 0);
            }
        });
    }, true);
})();
