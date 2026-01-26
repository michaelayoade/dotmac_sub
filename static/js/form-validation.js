/**
 * Form Validation Component
 * =========================
 * Alpine.js component for real-time field validation with debouncing.
 *
 * Features:
 * - Debounced validation (400ms delay)
 * - Client-side validation rules
 * - Server-side async validation for uniqueness checks
 * - Visual feedback (error/success states)
 * - ARIA live regions for accessibility
 */

document.addEventListener('alpine:init', () => {
    // Validated Input Component
    Alpine.data('validatedInput', (config = {}) => ({
        name: config.name || '',
        value: config.initialValue || '',
        validationUrl: config.validationUrl || '',
        rules: config.rules || '',
        touched: false,
        validating: false,
        hasError: false,
        isValid: false,
        errorMessage: '',
        abortController: null,

        init() {
            // Parse rules string into array
            this.parsedRules = this.rules.split('|').filter(r => r);
        },

        async validate() {
            this.touched = true;

            // Client-side validation first
            const clientError = this.validateClient();
            if (clientError) {
                this.setError(clientError);
                return;
            }

            // Server-side validation if URL provided
            if (this.validationUrl && this.value) {
                await this.validateServer();
            } else {
                this.setValid();
            }
        },

        validateClient() {
            const value = this.value?.trim() || '';

            for (const rule of this.parsedRules) {
                const [ruleName, ruleParam] = rule.split(':');

                switch (ruleName) {
                    case 'required':
                        if (!value) return 'This field is required';
                        break;

                    case 'email':
                        if (value && !this.isValidEmail(value)) {
                            return 'Please enter a valid email address';
                        }
                        break;

                    case 'phone':
                        if (value && !this.isValidPhone(value)) {
                            return 'Please enter a valid phone number';
                        }
                        break;

                    case 'url':
                        if (value && !this.isValidUrl(value)) {
                            return 'Please enter a valid URL';
                        }
                        break;

                    case 'min':
                        if (value.length < parseInt(ruleParam)) {
                            return `Must be at least ${ruleParam} characters`;
                        }
                        break;

                    case 'max':
                        if (value.length > parseInt(ruleParam)) {
                            return `Must be no more than ${ruleParam} characters`;
                        }
                        break;

                    case 'numeric':
                        if (value && isNaN(parseFloat(value))) {
                            return 'Must be a number';
                        }
                        break;

                    case 'currency':
                        if (value && !this.isValidCurrency(value)) {
                            return 'Please enter a valid currency amount';
                        }
                        break;

                    case 'date':
                        if (value && !this.isValidDate(value)) {
                            return 'Please enter a valid date';
                        }
                        break;

                    case 'regex':
                        try {
                            const regex = new RegExp(ruleParam);
                            if (value && !regex.test(value)) {
                                return 'Invalid format';
                            }
                        } catch (e) {
                            console.error('Invalid regex pattern:', ruleParam);
                        }
                        break;
                }
            }

            return null;
        },

        async validateServer() {
            // Cancel previous request
            if (this.abortController) {
                this.abortController.abort();
            }
            this.abortController = new AbortController();

            this.validating = true;

            try {
                const response = await fetch(this.validationUrl, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]')?.content || ''
                    },
                    body: JSON.stringify({
                        field: this.name,
                        value: this.value
                    }),
                    signal: this.abortController.signal
                });

                const data = await response.json();

                if (data.valid) {
                    this.setValid();
                } else {
                    this.setError(data.message || 'Invalid value');
                }
            } catch (error) {
                if (error.name !== 'AbortError') {
                    console.error('Validation request failed:', error);
                    // Don't show error for network issues, just clear state
                    this.hasError = false;
                    this.isValid = false;
                }
            } finally {
                this.validating = false;
            }
        },

        setError(message) {
            this.hasError = true;
            this.isValid = false;
            this.errorMessage = message;
            this.validating = false;

            // Announce error to screen readers
            if (window.announceToScreenReader) {
                window.announceToScreenReader(`${this.name}: ${message}`, 'assertive');
            }
        },

        setValid() {
            this.hasError = false;
            this.isValid = true;
            this.errorMessage = '';
            this.validating = false;
        },

        // Validation helpers
        isValidEmail(email) {
            return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);
        },

        isValidPhone(phone) {
            // Allow various phone formats
            const cleaned = phone.replace(/[\s\-\(\)\.]/g, '');
            return /^\+?[0-9]{7,15}$/.test(cleaned);
        },

        isValidUrl(url) {
            try {
                new URL(url);
                return true;
            } catch {
                return false;
            }
        },

        isValidCurrency(value) {
            // Allow numbers with optional decimal places
            return /^-?\d+(\.\d{1,2})?$/.test(value.replace(/[,\s]/g, ''));
        },

        isValidDate(value) {
            const date = new Date(value);
            return date instanceof Date && !isNaN(date);
        }
    }));

    // Form Submission Handler Component
    Alpine.data('formSubmit', (config = {}) => ({
        submitting: false,
        errors: {},

        init() {
            // Listen for HTMX validation errors
            this.$el.addEventListener('htmx:responseError', (event) => {
                this.submitting = false;
            });

            this.$el.addEventListener('htmx:afterRequest', (event) => {
                if (event.detail.successful) {
                    this.submitting = false;
                    this.errors = {};
                } else {
                    this.submitting = false;
                    // Try to parse validation errors from response
                    try {
                        const data = JSON.parse(event.detail.xhr.responseText);
                        if (data.errors) {
                            this.errors = data.errors;
                        }
                    } catch (e) {
                        // Ignore parse errors
                    }
                }
            });
        },

        handleSubmit(event) {
            // Prevent double submission
            if (this.submitting) {
                event.preventDefault();
                return false;
            }

            // Check for any invalid fields
            const invalidFields = this.$el.querySelectorAll('[aria-invalid="true"]');
            if (invalidFields.length > 0) {
                event.preventDefault();
                invalidFields[0].focus();
                window.dispatchEvent(new CustomEvent('show-toast', {
                    detail: { message: 'Please fix the errors before submitting', type: 'error' }
                }));
                return false;
            }

            this.submitting = true;
            return true;
        },

        getFieldError(fieldName) {
            return this.errors[fieldName] || null;
        },

        hasFieldError(fieldName) {
            return !!this.errors[fieldName];
        },

        clearFieldError(fieldName) {
            delete this.errors[fieldName];
        }
    }));
});

/**
 * Global validation utility functions
 */
window.FormValidation = {
    // Validate email format
    isValidEmail(email) {
        return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);
    },

    // Validate phone number
    isValidPhone(phone) {
        const cleaned = phone.replace(/[\s\-\(\)\.]/g, '');
        return /^\+?[0-9]{7,15}$/.test(cleaned);
    },

    // Format phone number
    formatPhone(phone, countryCode = 'NG') {
        const cleaned = phone.replace(/\D/g, '');
        if (countryCode === 'NG' && cleaned.length === 11) {
            return `+234 ${cleaned.slice(1, 4)} ${cleaned.slice(4, 7)} ${cleaned.slice(7)}`;
        }
        return phone;
    },

    // Validate currency amount
    isValidCurrency(value) {
        return /^-?\d+(\.\d{1,2})?$/.test(value.toString().replace(/[,\s]/g, ''));
    },

    // Format currency
    formatCurrency(amount, currency = 'NGN') {
        return new Intl.NumberFormat('en-NG', {
            style: 'currency',
            currency: currency,
            minimumFractionDigits: 2
        }).format(amount);
    },

    // Parse currency string to number
    parseCurrency(value) {
        if (typeof value === 'number') return value;
        return parseFloat(value.toString().replace(/[^0-9.-]/g, '')) || 0;
    }
};
