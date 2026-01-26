/**
 * Repeatable Fields Component
 * ===========================
 * Alpine.js component for managing dynamic field groups (add/remove/reorder).
 *
 * Usage:
 * <div x-data="repeatableFields({ min: 1, max: 10, itemTemplate: 'line-item-template' })">
 *     <template x-for="(item, index) in items" :key="item.id">
 *         ...
 *     </template>
 *     <button @click="addItem()">Add</button>
 * </div>
 */

// Register Alpine component when Alpine is ready
document.addEventListener('alpine:init', () => {
    Alpine.data('repeatableFields', (config = {}) => ({
        items: [],
        min: config.min || 0,
        max: config.max || 100,
        itemTemplate: config.itemTemplate || null,
        nextId: 1,
        fieldPrefix: config.fieldPrefix || 'item',

        init() {
            // Initialize with minimum items if needed
            const initialCount = config.initialItems || this.min || 1;
            for (let i = 0; i < initialCount; i++) {
                this.items.push(this.createItem(config.initialData?.[i] || {}));
            }

            // Watch for external data updates
            if (config.initialData && Array.isArray(config.initialData)) {
                this.items = config.initialData.map(data => this.createItem(data));
            }
        },

        createItem(data = {}) {
            return {
                id: this.nextId++,
                ...this.getDefaultValues(),
                ...data
            };
        },

        getDefaultValues() {
            // Override this in specific implementations
            return {};
        },

        addItem(data = {}) {
            if (this.items.length >= this.max) {
                window.dispatchEvent(new CustomEvent('show-toast', {
                    detail: { message: `Maximum of ${this.max} items allowed`, type: 'warning' }
                }));
                return;
            }
            this.items.push(this.createItem(data));
        },

        removeItem(index) {
            if (this.items.length <= this.min) {
                window.dispatchEvent(new CustomEvent('show-toast', {
                    detail: { message: `Minimum of ${this.min} item(s) required`, type: 'warning' }
                }));
                return;
            }
            this.items.splice(index, 1);
        },

        moveItem(fromIndex, toIndex) {
            if (toIndex < 0 || toIndex >= this.items.length) return;
            const item = this.items.splice(fromIndex, 1)[0];
            this.items.splice(toIndex, 0, item);
        },

        moveUp(index) {
            this.moveItem(index, index - 1);
        },

        moveDown(index) {
            this.moveItem(index, index + 1);
        },

        canAdd() {
            return this.items.length < this.max;
        },

        canRemove() {
            return this.items.length > this.min;
        },

        // Serialize items for form submission as JSON
        toJSON() {
            return JSON.stringify(this.items.map(item => {
                const { id, ...rest } = item;
                return rest;
            }));
        },

        // Get items as form-compatible array
        getFormData() {
            return this.items.map((item, index) => {
                const formItem = {};
                Object.keys(item).forEach(key => {
                    if (key !== 'id') {
                        formItem[`${this.fieldPrefix}[${index}][${key}]`] = item[key];
                    }
                });
                return formItem;
            });
        }
    }));

    // Invoice Line Items specific component
    Alpine.data('invoiceLineItems', (config = {}) => ({
        items: [],
        taxRates: config.taxRates || [],
        currency: config.currency || 'NGN',
        min: 1,
        max: 50,
        nextId: 1,

        init() {
            // Initialize with existing items or one empty item
            if (config.initialItems && config.initialItems.length > 0) {
                this.items = config.initialItems.map(item => ({
                    id: this.nextId++,
                    description: item.description || '',
                    quantity: parseFloat(item.quantity) || 1,
                    unitPrice: parseFloat(item.unit_price) || 0,
                    taxRateId: item.tax_rate_id || '',
                    taxRate: parseFloat(item.tax_rate) || 0
                }));
            } else {
                this.addItem();
            }
        },

        addItem() {
            if (this.items.length >= this.max) return;
            this.items.push({
                id: this.nextId++,
                description: '',
                quantity: 1,
                unitPrice: 0,
                taxRateId: '',
                taxRate: 0
            });
        },

        removeItem(index) {
            if (this.items.length <= this.min) return;
            this.items.splice(index, 1);
        },

        updateTaxRate(index, taxRateId) {
            const rate = this.taxRates.find(r => r.id === taxRateId);
            this.items[index].taxRateId = taxRateId;
            this.items[index].taxRate = rate ? parseFloat(rate.rate) : 0;
        },

        getLineTotal(item) {
            return item.quantity * item.unitPrice;
        },

        getLineTax(item) {
            return this.getLineTotal(item) * item.taxRate;
        },

        getSubtotal() {
            return this.items.reduce((sum, item) => sum + this.getLineTotal(item), 0);
        },

        getTaxTotal() {
            return this.items.reduce((sum, item) => sum + this.getLineTax(item), 0);
        },

        getTotal() {
            return this.getSubtotal() + this.getTaxTotal();
        },

        formatCurrency(amount) {
            return new Intl.NumberFormat('en-NG', {
                style: 'currency',
                currency: this.currency,
                minimumFractionDigits: 2
            }).format(amount);
        },

        toJSON() {
            return JSON.stringify(this.items.map(item => ({
                description: item.description,
                quantity: item.quantity,
                unit_price: item.unitPrice,
                tax_rate_id: item.taxRateId || null
            })));
        }
    }));

    // Contact Rows specific component for organization forms
    Alpine.data('contactRows', (config = {}) => ({
        contacts: [],
        min: 0,
        max: 10,
        nextId: 1,
        roles: ['primary', 'billing', 'technical', 'support'],

        init() {
            if (config.initialContacts && config.initialContacts.length > 0) {
                this.contacts = config.initialContacts.map(contact => ({
                    id: this.nextId++,
                    firstName: contact.first_name || '',
                    lastName: contact.last_name || '',
                    title: contact.title || '',
                    role: contact.role || 'primary',
                    email: contact.email || '',
                    phone: contact.phone || '',
                    isPrimary: contact.is_primary || false
                }));
            } else if (config.requireOne !== false) {
                this.addContact();
            }
        },

        addContact() {
            if (this.contacts.length >= this.max) return;
            this.contacts.push({
                id: this.nextId++,
                firstName: '',
                lastName: '',
                title: '',
                role: 'primary',
                email: '',
                phone: '',
                isPrimary: this.contacts.length === 0
            });
        },

        removeContact(index) {
            if (this.contacts.length <= this.min) return;
            const wasPrimary = this.contacts[index].isPrimary;
            this.contacts.splice(index, 1);
            // If we removed the primary, make the first one primary
            if (wasPrimary && this.contacts.length > 0) {
                this.contacts[0].isPrimary = true;
            }
        },

        setPrimary(index) {
            this.contacts.forEach((contact, i) => {
                contact.isPrimary = (i === index);
            });
        },

        toJSON() {
            return JSON.stringify(this.contacts.map(contact => ({
                first_name: contact.firstName,
                last_name: contact.lastName,
                title: contact.title,
                role: contact.role,
                email: contact.email,
                phone: contact.phone,
                is_primary: contact.isPrimary
            })));
        }
    }));
});

/**
 * Utility function to announce messages to screen readers
 */
function announceToScreenReader(message, priority = 'polite') {
    const region = document.getElementById('aria-live-region');
    if (region) {
        region.setAttribute('aria-live', priority);
        region.textContent = message;
        // Clear after a short delay to allow re-announcement of same message
        setTimeout(() => {
            region.textContent = '';
        }, 1000);
    }
}

// Export for global use
window.announceToScreenReader = announceToScreenReader;
