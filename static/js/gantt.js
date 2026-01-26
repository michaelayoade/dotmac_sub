/* Minimal Gantt renderer with due-date drag updates. */
(() => {
    const DAY_MS = 24 * 60 * 60 * 1000;
    const DAY_WIDTH = 24;

    function getCsrfToken() {
        const meta = document.querySelector('meta[name="csrf-token"]');
        if (meta && meta.content) return meta.content;
        const match = document.cookie.match(/csrf_token=([^;]+)/);
        return match ? match[1] : "";
    }

    function parseDate(value) {
        if (!value) return null;
        const parsed = new Date(value);
        if (!Number.isNaN(parsed.getTime())) return parsed;
        const fallback = new Date(`${value}T00:00:00Z`);
        return Number.isNaN(fallback.getTime()) ? null : fallback;
    }

    function formatDate(date) {
        return date.toISOString().slice(0, 10);
    }

    function daysBetween(start, end) {
        return Math.round((end - start) / DAY_MS);
    }

    function updateBar(bar, startDate, dueDate, minDate) {
        const offsetDays = daysBetween(minDate, startDate);
        const widthDays = Math.max(1, daysBetween(startDate, dueDate) + 1);
        bar.style.left = `${offsetDays * DAY_WIDTH}px`;
        bar.style.width = `${widthDays * DAY_WIDTH}px`;
        bar.dataset.due = formatDate(dueDate);
        const label = bar.querySelector("[data-gantt-due]");
        if (label) label.textContent = formatDate(dueDate);
    }

    async function persistDragUpdate(endpoint, id, field, value) {
        if (!endpoint) return true;
        const response = await fetch(endpoint, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-CSRF-Token": getCsrfToken(),
            },
            credentials: "same-origin",
            body: JSON.stringify({
                id,
                field,
                value,
            }),
        });
        return response.ok;
    }

    function attachResize(handle, bar, minDate, updateEndpoint, dragField) {
        handle.addEventListener("pointerdown", (event) => {
            event.preventDefault();
            handle.setPointerCapture(event.pointerId);

            const startX = event.clientX;
            const startDate = parseDate(bar.dataset.start);
            const originalDue = parseDate(bar.dataset.due);
            if (!startDate || !originalDue) return;

            const onMove = (moveEvent) => {
                const deltaPx = moveEvent.clientX - startX;
                const deltaDays = Math.round(deltaPx / DAY_WIDTH);
                const nextDue = new Date(originalDue.getTime() + deltaDays * DAY_MS);
                if (nextDue < startDate) {
                    updateBar(bar, startDate, startDate, minDate);
                } else {
                    updateBar(bar, startDate, nextDue, minDate);
                }
            };

            const onUp = async () => {
                window.removeEventListener("pointermove", onMove);
                window.removeEventListener("pointerup", onUp);

                const nextDue = parseDate(bar.dataset.due);
                if (!nextDue) return;

                const originalDueIso = formatDate(originalDue);
                const nextDueIso = formatDate(nextDue);
                if (originalDueIso === nextDueIso) return;

                const ok = await persistDragUpdate(
                    updateEndpoint,
                    bar.dataset.id,
                    dragField,
                    nextDueIso
                );
                if (!ok) {
                    updateBar(bar, startDate, originalDue, minDate);
                    bar.classList.add("border", "border-rose-400");
                }
            };

            window.addEventListener("pointermove", onMove);
            window.addEventListener("pointerup", onUp);
        });
    }

    function renderGantt(container, items, updateEndpoint, config) {
        const idField = config.idField || "id";
        const titleField = config.titleField || "name";
        const startField = config.startField || "start_date";
        const dragField = config.dragField || "due_date";
        const parsedItems = items
            .map((item) => {
                const start = parseDate(item[startField]) || parseDate(item.created_at);
                const due = parseDate(item[dragField]) || start;
                return {
                    id: item[idField],
                    name: item[titleField] || "Untitled",
                    start: start || due,
                    due: due || start,
                };
            })
            .filter((item) => item.start && item.due);

        if (!parsedItems.length) {
            container.innerHTML =
                '<p class="text-sm text-slate-500 dark:text-slate-400">No projects to display.</p>';
            return;
        }

        let minDate = parsedItems[0].start;
        let maxDate = parsedItems[0].due;
        parsedItems.forEach((item) => {
            if (item.start < minDate) minDate = item.start;
            if (item.due < item.start) item.due = item.start;
            if (item.due > maxDate) maxDate = item.due;
        });

        const totalDays = daysBetween(minDate, maxDate) + 1;
        const trackWidth = Math.max(1, totalDays) * DAY_WIDTH;

        container.innerHTML = "";
        const list = document.createElement("div");
        list.className = "space-y-3";

        parsedItems.forEach((item) => {
            const row = document.createElement("div");
            row.className = "flex flex-col gap-2 sm:flex-row sm:items-center";

            const label = document.createElement("div");
            label.className = "text-sm font-medium text-slate-700 dark:text-slate-200 sm:w-56";
            label.textContent = item.name;

            const scroll = document.createElement("div");
            scroll.className = "flex-1 overflow-x-auto";

            const track = document.createElement("div");
            track.className =
                "relative h-10 min-w-full rounded-lg border border-slate-200 bg-slate-50 dark:border-slate-700 dark:bg-slate-900/40";
            track.style.width = `${trackWidth}px`;
            track.style.backgroundImage =
                "linear-gradient(to right, rgba(148,163,184,0.3) 1px, transparent 1px)";
            track.style.backgroundSize = `${DAY_WIDTH}px 100%`;

            const bar = document.createElement("div");
            bar.className =
                "absolute top-1/2 flex h-6 -translate-y-1/2 items-center rounded-md bg-primary-500 px-2 text-xs font-semibold text-white shadow-sm";
            bar.dataset.id = item.id;
            bar.dataset.start = formatDate(item.start);
            bar.dataset.due = formatDate(item.due);

            updateBar(bar, item.start, item.due, minDate);

            const dueLabel = document.createElement("span");
            dueLabel.dataset.ganttDue = "";
            dueLabel.textContent = formatDate(item.due);
            bar.appendChild(dueLabel);

            const handle = document.createElement("div");
            handle.className =
                "absolute right-0 top-0 h-full w-2 cursor-ew-resize rounded-r-md bg-primary-700/80";
            bar.appendChild(handle);

            attachResize(handle, bar, minDate, updateEndpoint, dragField);

            track.appendChild(bar);
            scroll.appendChild(track);
            row.appendChild(label);
            row.appendChild(scroll);
            list.appendChild(row);
        });

        container.appendChild(list);
    }

    async function initGantt(container) {
        const endpoint = container.dataset.ganttEndpoint;
        const updateEndpoint = container.dataset.updateEndpoint;
        let config = {};
        if (container.dataset.config) {
            try {
                config = JSON.parse(container.dataset.config);
            } catch (error) {
                console.warn("gantt: invalid config", error);
            }
        }
        if (!endpoint) {
            container.innerHTML =
                '<p class="text-sm text-slate-500 dark:text-slate-400">Missing Gantt endpoint.</p>';
            return;
        }

        try {
            const response = await fetch(endpoint, { credentials: "same-origin" });
            if (!response.ok) throw new Error("Gantt fetch failed");
            const payload = await response.json();
            renderGantt(container, payload.items || [], updateEndpoint, config);
        } catch (error) {
            console.error("gantt: fetch error", error);
            container.innerHTML =
                '<p class="text-sm text-slate-500 dark:text-slate-400">Unable to load Gantt.</p>';
        }
    }

    function initAll() {
        document.querySelectorAll("[data-gantt]").forEach((container) => {
            initGantt(container);
        });
    }

    document.addEventListener("DOMContentLoaded", initAll);
    window.DotmacGantt = { initAll };
})();
