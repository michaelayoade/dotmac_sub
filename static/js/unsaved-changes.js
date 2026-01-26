/**
 * Unsaved Changes Warning Component
 * ==================================
 * Alpine.js component to track form changes and warn before navigation.
 *
 * Features:
 * - Tracks initial form state on page load
 * - Compares current state on input events
 * - beforeunload event handler for browser navigation
 * - Intercepts internal link clicks with confirmation
 * - Resets state on successful form submission
 * - HTMX integration for SPA-style navigation
 *
 * Usage:
 * <form x-data="unsavedChanges()" @submit="markAsSaved()">
 *     ...
 * </form>
 */

document.addEventListener('alpine:init', () => {
    Alpine.data('unsavedChanges', (config = {}) => ({
        isDirty: false,
        initialState: null,
        enabled: config.enabled !== false,
        warningMessage: config.message || 'You have unsaved changes. Are you sure you want to leave?',
        excludeFields: config.excludeFields || ['_csrf_token'],

        init() {
            if (!this.enabled) return;

            // Capture initial state after a short delay to ensure form is fully rendered
            this.$nextTick(() => {
                setTimeout(() => {
                    this.initialState = this.getFormState();
                }, 100);
            });

            // Add beforeunload handler
            window.addEventListener('beforeunload', this.handleBeforeUnload.bind(this));

            // Listen for successful form submission
            this.$el.addEventListener('submit', () => {
                this.markAsSaved();
            });

            // HTMX integration - mark as saved on successful swap
            this.$el.addEventListener('htmx:afterSwap', (event) => {
                if (event.detail.successful) {
                    this.markAsSaved();
                }
            });

            // Intercept link clicks within the form
            document.addEventListener('click', this.handleLinkClick.bind(this));
        },

        destroy() {
            window.removeEventListener('beforeunload', this.handleBeforeUnload.bind(this));
            document.removeEventListener('click', this.handleLinkClick.bind(this));
        },

        getFormState() {
            const form = this.$el;
            const state = {};

            // Get all form inputs
            const inputs = form.querySelectorAll('input, select, textarea');
            inputs.forEach(input => {
                const name = input.name;
                if (!name || this.excludeFields.includes(name)) return;

                if (input.type === 'checkbox' || input.type === 'radio') {
                    if (input.type === 'checkbox') {
                        state[name] = input.checked;
                    } else if (input.checked) {
                        state[name] = input.value;
                    }
                } else {
                    state[name] = input.value;
                }
            });

            return JSON.stringify(state);
        },

        checkForChanges() {
            if (!this.enabled || !this.initialState) return;

            const currentState = this.getFormState();
            this.isDirty = currentState !== this.initialState;
        },

        markAsSaved() {
            this.isDirty = false;
            this.initialState = this.getFormState();
        },

        markAsDirty() {
            this.isDirty = true;
        },

        reset() {
            this.isDirty = false;
            this.initialState = this.getFormState();
        },

        handleBeforeUnload(event) {
            if (this.isDirty) {
                event.preventDefault();
                event.returnValue = this.warningMessage;
                return this.warningMessage;
            }
        },

        handleLinkClick(event) {
            if (!this.isDirty) return;

            const link = event.target.closest('a');
            if (!link) return;

            // Skip if it's an external link, anchor, or has special attributes
            const href = link.getAttribute('href');
            if (!href ||
                href.startsWith('#') ||
                href.startsWith('javascript:') ||
                link.hasAttribute('download') ||
                link.getAttribute('target') === '_blank' ||
                link.hasAttribute('data-no-unsaved-check')) {
                return;
            }

            // Check if this is a navigation link (not HTMX)
            if (!link.hasAttribute('hx-get') && !link.hasAttribute('hx-post')) {
                if (!confirm(this.warningMessage)) {
                    event.preventDefault();
                    event.stopPropagation();
                }
            }
        },

        // Call this when user wants to discard changes
        discardChanges() {
            this.isDirty = false;
        },

        // Check if form can be safely navigated away from
        canNavigate() {
            return !this.isDirty || confirm(this.warningMessage);
        }
    }));

    // Global dirty form tracking
    Alpine.store('dirtyForms', {
        forms: new Set(),

        register(formId) {
            this.forms.add(formId);
        },

        unregister(formId) {
            this.forms.delete(formId);
        },

        hasDirtyForms() {
            return this.forms.size > 0;
        },

        getDirtyFormCount() {
            return this.forms.size;
        }
    });
});

/**
 * HTMX integration for unsaved changes
 * Intercepts HTMX navigation requests when forms are dirty
 */
document.addEventListener('htmx:beforeRequest', (event) => {
    // Check if this is a navigation request (not a form submission)
    const trigger = event.detail.elt;
    if (trigger.tagName === 'A' || trigger.hasAttribute('hx-push-url')) {
        // Check for dirty forms
        const dirtyForms = document.querySelectorAll('[x-data*="unsavedChanges"]');
        for (const form of dirtyForms) {
            const alpineData = Alpine.$data(form);
            if (alpineData && alpineData.isDirty) {
                const message = alpineData.warningMessage || 'You have unsaved changes. Are you sure you want to leave?';
                if (!confirm(message)) {
                    event.preventDefault();
                    return;
                }
                // User confirmed, mark as saved to prevent multiple prompts
                alpineData.discardChanges();
            }
        }
    }
});
