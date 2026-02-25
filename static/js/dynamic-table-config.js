(function () {
  function qs(root, selector) {
    return root.querySelector(selector);
  }

  function qsa(root, selector) {
    return Array.from(root.querySelectorAll(selector));
  }

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/\"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  class DynamicTableConfig {
    constructor(root) {
      this.root = root;
      this.tableKey = root.dataset.tableKey;
      this.filterForm = document.querySelector(
        `[data-table-filters="${this.tableKey}"]`
      );
      this.apiBase = `/api/v1/tables/${this.tableKey}`;
      this.columns = [];
      this.availableColumns = [];
      this.rows = [];
      this.count = 0;
      this.limit = 25;
      this.offset = 0;
      this.sortBy = "created_at";
      this.sortDir = "desc";
      this.isLoading = false;
      this.dragIndex = null;
    }

    getDetailUrl(row) {
      const id = row.id;
      if (!id) return null;
      if (this.tableKey === "customers") {
        const type = row.customer_type === "organization" ? "organization" : "person";
        return `/admin/customers/${type}/${id}`;
      }
      if (this.tableKey === "subscribers") {
        return `/admin/subscribers/${id}`;
      }
      return null;
    }

    formatValue(value) {
      if (value == null) return "";
      if (typeof value === "boolean") return value ? "Yes" : "No";
      return String(value);
    }

    formatDate(value) {
      if (!value) return "N/A";
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return this.formatValue(value);
      return date.toLocaleDateString("en-US", {
        month: "short",
        day: "2-digit",
        year: "numeric",
      });
    }

    titleCase(value) {
      return String(value || "")
        .replace(/_/g, " ")
        .split(" ")
        .filter(Boolean)
        .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
        .join(" ");
    }

    renderStatusBadge(rawValue) {
      const value = String(rawValue || "").toLowerCase();
      if (value === "active") {
        return '<span class="inline-flex items-center rounded-full bg-emerald-100 px-2.5 py-1 text-xs font-semibold text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300">Active</span>';
      }
      if (value === "suspended") {
        return '<span class="inline-flex items-center rounded-full bg-amber-100 px-2.5 py-1 text-xs font-semibold text-amber-700 dark:bg-amber-900/30 dark:text-amber-300">Suspended</span>';
      }
      return `<span class="inline-flex items-center rounded-full bg-slate-100 px-2.5 py-1 text-xs font-semibold text-slate-700 dark:bg-slate-700 dark:text-slate-300">${escapeHtml(this.titleCase(value || "unknown"))}</span>`;
    }

    renderDefaultCell(value) {
      return `<td class="whitespace-nowrap px-6 py-4 text-sm text-slate-600 dark:text-slate-400">${escapeHtml(this.formatValue(value))}</td>`;
    }

    renderKeyedCell(columnKey, value) {
      const key = String(columnKey || "").toLowerCase();
      if (key === "status") {
        return `<td class="whitespace-nowrap px-6 py-4">${this.renderStatusBadge(value)}</td>`;
      }
      if (key === "created_at" || key === "updated_at" || key.endsWith("_at")) {
        return `<td class="whitespace-nowrap px-6 py-4"><span class="text-sm text-slate-500 dark:text-slate-400">${escapeHtml(this.formatDate(value))}</span></td>`;
      }
      if (key === "min_balance" || key === "balance") {
        const numeric = Number(value || 0);
        const css =
          numeric > 0
            ? "text-amber-600 dark:text-amber-400"
            : numeric < 0
              ? "text-emerald-600 dark:text-emerald-400"
              : "text-slate-600 dark:text-slate-400";
        return `<td class="whitespace-nowrap px-6 py-4"><span class="text-sm font-bold font-mono tabular-nums ${css}">${escapeHtml(this.formatValue(value))}</span></td>`;
      }
      if (key.endsWith("_id") || key === "id") {
        return `<td class="whitespace-nowrap px-6 py-4"><span class="text-xs font-mono text-slate-500 dark:text-slate-400">${escapeHtml(this.formatValue(value))}</span></td>`;
      }
      if (key === "email") {
        return `<td class="whitespace-nowrap px-6 py-4"><span class="text-sm text-slate-600 dark:text-slate-400">${escapeHtml(this.formatValue(value))}</span></td>`;
      }
      return this.renderDefaultCell(value);
    }

    async init() {
      this.renderShell();
      this.bindFilterForm();
      await this.loadColumns();
      await this.loadData();
    }

    renderShell() {
      this.root.innerHTML = `
        <div class="animate-fade-in-up overflow-hidden rounded-2xl border border-slate-200/60 bg-white/80 shadow-premium backdrop-blur-sm dark:border-slate-700/60 dark:bg-slate-800/80">
          <div class="flex items-center justify-between border-b border-slate-200/70 px-4 py-3 dark:border-slate-700/70">
            <p class="text-sm font-semibold text-slate-700 dark:text-slate-200">Table View</p>
            <div class="flex items-center gap-2">
              <button data-action="reset" class="rounded-lg border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-600 hover:bg-slate-100 dark:border-slate-600 dark:text-slate-300 dark:hover:bg-slate-700">Reset to default</button>
              <button data-action="configure" class="rounded-lg bg-amber-500 px-3 py-1.5 text-xs font-semibold text-white hover:bg-amber-600">Configure columns</button>
            </div>
          </div>
          <div data-role="table-wrap" class="overflow-x-auto"></div>
          <div data-role="pager" class="flex items-center justify-between border-t border-slate-200/70 px-4 py-3 text-xs text-slate-500 dark:border-slate-700/70 dark:text-slate-400"></div>
        </div>
        <div data-role="modal" class="hidden fixed inset-0 z-50 items-center justify-center bg-slate-900/50 p-4">
          <div class="w-full max-w-lg rounded-2xl border border-slate-200 bg-white p-5 shadow-xl dark:border-slate-700 dark:bg-slate-800">
            <div class="mb-3 flex items-center justify-between">
              <h3 class="text-base font-semibold text-slate-900 dark:text-white">Column configuration</h3>
              <button data-action="close-modal" class="text-slate-500 hover:text-slate-800 dark:hover:text-slate-200">Close</button>
            </div>
            <p class="mb-3 text-xs text-slate-500 dark:text-slate-400">Drag to reorder. Toggle to show/hide.</p>
            <div data-role="modal-list" class="max-h-80 overflow-y-auto rounded-xl border border-slate-200 p-2 dark:border-slate-700"></div>
            <div class="mt-4 flex justify-end gap-2">
              <button data-action="cancel" class="rounded-lg border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-600 hover:bg-slate-100 dark:border-slate-600 dark:text-slate-300 dark:hover:bg-slate-700">Cancel</button>
              <button data-action="save" class="rounded-lg bg-amber-500 px-3 py-1.5 text-xs font-semibold text-white hover:bg-amber-600">Save</button>
            </div>
          </div>
        </div>
      `;

      qs(this.root, '[data-action="configure"]').addEventListener("click", () => {
        this.renderModal();
        const modal = qs(this.root, '[data-role="modal"]');
        modal.classList.remove("hidden");
        modal.classList.add("flex");
      });

      qs(this.root, '[data-action="close-modal"]').addEventListener("click", () => this.closeModal());
      qs(this.root, '[data-action="cancel"]').addEventListener("click", () => this.closeModal());
      qs(this.root, '[data-action="save"]').addEventListener("click", () => this.saveColumns());
      qs(this.root, '[data-action="reset"]').addEventListener("click", () => this.resetColumns());
    }

    closeModal() {
      const modal = qs(this.root, '[data-role="modal"]');
      modal.classList.add("hidden");
      modal.classList.remove("flex");
    }

    bindFilterForm() {
      if (!this.filterForm) return;
      this.filterForm.addEventListener("submit", (event) => {
        event.preventDefault();
        this.offset = 0;
        this.loadData();
      });

      qsa(this.filterForm, "input,select").forEach((input) => {
        input.addEventListener("change", () => {
          this.offset = 0;
          this.loadData();
        });
      });
    }

    buildParams() {
      const params = new URLSearchParams();
      params.set("limit", String(this.limit));
      params.set("offset", String(this.offset));
      params.set("sort_by", this.sortBy);
      params.set("sort_dir", this.sortDir);

      if (this.filterForm) {
        const formData = new FormData(this.filterForm);
        formData.forEach((value, key) => {
          const textValue = String(value || "").trim();
          if (!textValue) return;
          if (key === "search") {
            params.set("q", textValue);
            return;
          }
          if (key === "status" && (textValue === "active" || textValue === "inactive")) {
            params.set("activation_state", textValue);
            return;
          }
          if (key === "per_page") {
            const parsed = Number(textValue);
            if (!Number.isNaN(parsed) && parsed > 0) {
              this.limit = parsed;
              params.set("limit", String(parsed));
            }
            return;
          }
          params.set(key, textValue);
        });
      }

      return params;
    }

    async loadColumns() {
      const response = await fetch(`${this.apiBase}/columns?_ts=${Date.now()}`, {
        credentials: "same-origin",
        cache: "no-store",
      });
      if (!response.ok) {
        let message = `Failed to load columns for ${this.tableKey}`;
        try {
          const errorPayload = await response.json();
          if (errorPayload && errorPayload.detail) {
            message = String(errorPayload.detail);
          }
        } catch (_) {
          // Keep generic fallback when non-JSON error response is returned.
        }
        throw new Error(message);
      }
      const payload = await response.json();
      this.columns = payload.columns || [];
      this.availableColumns = payload.available_columns || [];
    }

    async loadData() {
      this.setLoading(true);
      try {
        const params = this.buildParams();
        params.set("_ts", String(Date.now()));
        const response = await fetch(`${this.apiBase}/data?${params.toString()}`, {
          credentials: "same-origin",
          cache: "no-store",
        });
        if (!response.ok) {
          let message = `Failed to load data for ${this.tableKey}`;
          try {
            const errorPayload = await response.json();
            if (errorPayload && errorPayload.detail) {
              message = String(errorPayload.detail);
            }
          } catch (_) {
            // Keep generic fallback when non-JSON error response is returned.
          }
          throw new Error(message);
        }
        const payload = await response.json();
        this.rows = payload.items || [];
        this.columns = payload.columns || this.columns;
        this.count = payload.count || 0;
        this.limit = payload.limit || this.limit;
        this.offset = payload.offset || this.offset;
        this.renderTable();
      } catch (error) {
        const wrap = qs(this.root, '[data-role="table-wrap"]');
        wrap.innerHTML = `<div class="p-4 text-sm text-red-600">${escapeHtml(error.message)}</div>`;
      } finally {
        this.setLoading(false);
      }
    }

    setLoading(isLoading) {
      this.isLoading = isLoading;
      const wrap = qs(this.root, '[data-role="table-wrap"]');
      if (isLoading) {
        wrap.innerHTML = '<div class="p-4 text-sm text-slate-500">Loading table data...</div>';
      }
    }

    renderTable() {
      const visibleColumns = [...this.columns]
        .filter((column) => column.is_visible)
        .sort((a, b) => a.display_order - b.display_order);

      const head = visibleColumns
        .map((column) => {
          const sortIcon =
            this.sortBy === column.column_key
              ? this.sortDir === "asc"
                ? " ↑"
                : " ↓"
              : "";
          const interaction = column.sortable
            ? `cursor-pointer select-none hover:text-indigo-600 dark:hover:text-indigo-400`
            : "";
          const sortAttr = column.sortable ? `data-sort="${column.column_key}"` : "";
          return `<th ${sortAttr} class="px-6 py-4 text-left text-xs font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400 ${interaction}">${escapeHtml(column.label)}${sortIcon}</th>`;
        })
        .join("");
      const actionsHead =
        this.tableKey === "customers" || this.tableKey === "subscribers"
          ? '<th class="px-6 py-4 text-right text-xs font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400">Actions</th>'
          : "";

      const colspan =
        Math.max(1, visibleColumns.length) +
        (this.tableKey === "customers" || this.tableKey === "subscribers" ? 1 : 0);

      const body = this.rows.length
        ? this.rows
            .map((row) => {
              const detailUrl = this.getDetailUrl(row);
              const customerType = row.customer_type === "organization" ? "organization" : "person";
              const cells = visibleColumns
                .map((column, index) => {
                  const value = this.formatValue(row[column.column_key]);
                  if (index === 0 && detailUrl) {
                    const icon =
                      this.tableKey === "customers"
                        ? customerType === "organization"
                          ? '<svg class="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M19 21V5a2 2 0 00-2-2H7a2 2 0 00-2 2v16m14 0h2m-2 0h-5m-9 0H3m2 0h5"/></svg>'
                          : '<svg class="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z"/></svg>'
                        : '<svg class="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z"/></svg>';
                    const subtitle =
                      this.tableKey === "customers"
                        ? escapeHtml(row.email || row.customer_type || "")
                        : escapeHtml(row.email || row.subscriber_number || "");
                    return `<td class="whitespace-nowrap px-6 py-4">
                      <a href="${escapeHtml(detailUrl)}" class="flex items-center gap-3">
                        <span class="flex h-10 w-10 items-center justify-center rounded-full bg-gradient-to-br from-indigo-100 to-violet-100 text-indigo-700 dark:from-indigo-900/40 dark:to-violet-900/40 dark:text-indigo-300">${icon}</span>
                        <span class="min-w-0">
                          <span class="block truncate text-sm font-semibold text-slate-900 hover:text-indigo-600 dark:text-white dark:hover:text-indigo-400">${escapeHtml(value || "View details")}</span>
                          ${
                            subtitle
                              ? `<span class="block truncate text-xs text-slate-500 dark:text-slate-400">${subtitle}</span>`
                              : ""
                          }
                        </span>
                      </a>
                    </td>`;
                  }
                  return this.renderKeyedCell(column.column_key, value);
                })
                .join("");
              const actionsCell = detailUrl
                ? `<td class="whitespace-nowrap px-6 py-4 text-right">
                    <div class="flex items-center justify-end gap-2 opacity-0 transition-opacity group-hover:opacity-100">
                      <a href="${escapeHtml(detailUrl)}" class="group/btn inline-flex items-center gap-1.5 rounded-xl border border-indigo-200 bg-indigo-50 px-3 py-2 text-xs font-semibold text-indigo-700 transition-all duration-200 hover:border-indigo-300 hover:bg-indigo-100 hover:shadow-sm dark:border-indigo-800 dark:bg-indigo-900/30 dark:text-indigo-300 dark:hover:bg-indigo-900/50" title="View Details">
                        <svg class="h-4 w-4 transition-transform duration-200 group-hover/btn:scale-110" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/>
                          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"/>
                        </svg>
                        View
                      </a>
                    </div>
                  </td>`
                : "";
              const rowAttrs = detailUrl
                ? `data-row-url="${escapeHtml(detailUrl)}" class="group cursor-pointer transition-colors duration-200 hover:bg-indigo-50/50 dark:hover:bg-indigo-900/10"`
                : `class="group transition-colors duration-200 hover:bg-indigo-50/50 dark:hover:bg-indigo-900/10"`;
              return `<tr ${rowAttrs}>${cells}${actionsCell}</tr>`;
            })
            .join("")
        : `<tr><td class="px-5 py-12 text-center text-sm text-slate-500" colspan="${colspan}">No records found</td></tr>`;

      qs(this.root, '[data-role="table-wrap"]').innerHTML = `
        <table class="min-w-full divide-y divide-slate-200/60 dark:divide-slate-700/60">
          <thead><tr class="bg-slate-50/80 dark:bg-slate-800/50">${head}${actionsHead}</tr></thead>
          <tbody class="divide-y divide-slate-100 dark:divide-slate-700/50">${body}</tbody>
        </table>
      `;

      qsa(this.root, "[data-sort]").forEach((element) => {
        element.addEventListener("click", () => {
          const nextSortBy = element.dataset.sort;
          if (!nextSortBy) return;
          if (this.sortBy === nextSortBy) {
            this.sortDir = this.sortDir === "asc" ? "desc" : "asc";
          } else {
            this.sortBy = nextSortBy;
            this.sortDir = "asc";
          }
          this.offset = 0;
          this.loadData();
        });
      });

      qsa(this.root, "[data-row-url]").forEach((row) => {
        row.addEventListener("click", (event) => {
          const target = event.target;
          if (target instanceof HTMLElement && target.closest("a,button,input,select")) {
            return;
          }
          const url = row.getAttribute("data-row-url");
          if (url) {
            window.location.href = url;
          }
        });
      });

      this.renderPager();
    }

    renderPager() {
      const start = this.count === 0 ? 0 : this.offset + 1;
      const end = Math.min(this.offset + this.limit, this.count);
      const hasPrev = this.offset > 0;
      const hasNext = this.offset + this.limit < this.count;

      const pager = qs(this.root, '[data-role="pager"]');
      pager.innerHTML = `
        <span>Showing ${start}-${end} of ${this.count}</span>
        <div class="flex items-center gap-2">
          <button data-action="prev" ${hasPrev ? "" : "disabled"} class="rounded border border-slate-300 px-2 py-1 disabled:opacity-40 dark:border-slate-600">Prev</button>
          <button data-action="next" ${hasNext ? "" : "disabled"} class="rounded border border-slate-300 px-2 py-1 disabled:opacity-40 dark:border-slate-600">Next</button>
        </div>
      `;

      qs(pager, '[data-action="prev"]').addEventListener("click", () => {
        if (!hasPrev) return;
        this.offset = Math.max(0, this.offset - this.limit);
        this.loadData();
      });

      qs(pager, '[data-action="next"]').addEventListener("click", () => {
        if (!hasNext) return;
        this.offset += this.limit;
        this.loadData();
      });
    }

    renderModal() {
      const ordered = [...this.columns].sort((a, b) => a.display_order - b.display_order);
      const list = qs(this.root, '[data-role="modal-list"]');
      list.innerHTML = ordered
        .map(
          (column, index) => `
            <div data-index="${index}" draggable="true" class="mb-2 flex items-center justify-between rounded-lg border border-slate-200 px-3 py-2 text-sm dark:border-slate-700">
              <div class="flex items-center gap-2">
                <span class="cursor-grab text-slate-400">::</span>
                <label class="inline-flex items-center gap-2">
                  <input type="checkbox" data-toggle="${escapeHtml(column.column_key)}" ${column.is_visible ? "checked" : ""}>
                  <span class="text-slate-700 dark:text-slate-200">${escapeHtml(column.label)}</span>
                </label>
              </div>
              <span class="text-xs text-slate-400">${escapeHtml(column.column_key)}</span>
            </div>
          `
        )
        .join("");

      qsa(list, "[data-toggle]").forEach((checkbox) => {
        checkbox.addEventListener("change", (event) => {
          const key = event.target.dataset.toggle;
          this.columns = this.columns.map((column) =>
            column.column_key === key
              ? { ...column, is_visible: event.target.checked }
              : column
          );
        });
      });

      qsa(list, "[draggable='true']").forEach((item) => {
        item.addEventListener("dragstart", (event) => {
          this.dragIndex = Number(event.currentTarget.dataset.index);
        });

        item.addEventListener("dragover", (event) => {
          event.preventDefault();
        });

        item.addEventListener("drop", (event) => {
          event.preventDefault();
          const targetIndex = Number(event.currentTarget.dataset.index);
          this.reorderColumns(this.dragIndex, targetIndex);
          this.renderModal();
        });
      });
    }

    reorderColumns(fromIndex, toIndex) {
      if (fromIndex === toIndex || fromIndex == null || toIndex == null) return;
      const ordered = [...this.columns].sort((a, b) => a.display_order - b.display_order);
      const [moved] = ordered.splice(fromIndex, 1);
      ordered.splice(toIndex, 0, moved);
      this.columns = ordered.map((column, index) => ({
        ...column,
        display_order: index,
      }));
    }

    async saveColumns() {
      const snapshot = [...this.columns];
      const payload = [...this.columns]
        .sort((a, b) => a.display_order - b.display_order)
        .map((column, index) => ({
          column_key: column.column_key,
          display_order: index,
          is_visible: Boolean(column.is_visible),
        }));

      try {
        const response = await fetch(`${this.apiBase}/columns`, {
          method: "POST",
          credentials: "same-origin",
          cache: "no-store",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify(payload),
        });
        if (!response.ok) {
          let message = "Failed to save table configuration";
          try {
            const errorPayload = await response.json();
            if (errorPayload && errorPayload.detail) {
              message = String(errorPayload.detail);
            }
          } catch (_) {
            // Keep generic fallback when non-JSON error response is returned.
          }
          throw new Error(message);
        }
        const result = await response.json();
        this.columns = result.columns || this.columns;
        this.closeModal();
        await this.loadData();
      } catch (error) {
        this.columns = snapshot;
        alert(error.message || "Could not save configuration");
      }
    }

    async resetColumns() {
      try {
        const response = await fetch(`${this.apiBase}/columns`, {
          method: "POST",
          credentials: "same-origin",
          cache: "no-store",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify([]),
        });
        if (!response.ok) {
          let message = "Failed to reset table configuration";
          try {
            const errorPayload = await response.json();
            if (errorPayload && errorPayload.detail) {
              message = String(errorPayload.detail);
            }
          } catch (_) {
            // Keep generic fallback when non-JSON error response is returned.
          }
          throw new Error(message);
        }
        const result = await response.json();
        this.columns = result.columns || this.columns;
        await this.loadData();
      } catch (error) {
        alert(error.message || "Could not reset configuration");
      }
    }
  }

  function initDynamicTables() {
    document.querySelectorAll("[data-dynamic-table]").forEach((element) => {
      const instance = new DynamicTableConfig(element);
      instance.init();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initDynamicTables);
  } else {
    initDynamicTables();
  }
})();
