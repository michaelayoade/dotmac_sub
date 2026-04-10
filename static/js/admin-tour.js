(function() {
    const DEFAULT_STORAGE_KEY = 'dotmac_admin_tour_seen_v1';
    const ACTIVE_TARGET_CLASS = 'app-tour-target-active';

    function isVisible(element) {
        if (!element || !element.isConnected) {
            return false;
        }

        const style = window.getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        return style.display !== 'none' &&
            style.visibility !== 'hidden' &&
            rect.width > 0 &&
            rect.height > 0;
    }

    function clamp(value, min, max) {
        return Math.min(Math.max(value, min), max);
    }

    function resolveTarget(selector) {
        if (!selector) {
            return null;
        }
        return Array.from(document.querySelectorAll(selector)).find(isVisible) || null;
    }

    class GuidedTour {
        constructor(options) {
            const config = options || {};
            this.steps = Array.isArray(config.steps) ? config.steps : [];
            this.storageKey = config.storageKey || DEFAULT_STORAGE_KEY;
            this.autoStart = config.autoStart !== false;
            this.startDelay = Number(config.startDelay || 0);
            this.highlightPadding = Number(config.highlightPadding || 12);
            this.currentIndex = -1;
            this.active = false;
            this.target = null;
            this.root = null;
            this.overlay = null;
            this.spotlight = null;
            this.tooltip = null;
            this.titleEl = null;
            this.bodyEl = null;
            this.progressEl = null;
            this.backButton = null;
            this.nextButton = null;
            this.finishButton = null;
            this.handleKeydown = this.handleKeydown.bind(this);
            this.handleReposition = this.handleReposition.bind(this);
        }

        isCompleted() {
            try {
                return window.localStorage.getItem(this.storageKey) === '1';
            } catch (error) {
                return false;
            }
        }

        markCompleted() {
            try {
                window.localStorage.setItem(this.storageKey, '1');
            } catch (error) {}
        }

        shouldAutoStart() {
            return this.autoStart && !this.isCompleted();
        }

        start(options) {
            const config = options || {};
            if (!config.force && this.isCompleted()) {
                return false;
            }

            const firstIndex = this.findAvailableIndex(0, 1);
            if (firstIndex === -1) {
                return false;
            }

            if (!this.root) {
                this.build();
            }

            this.active = true;
            document.body.classList.add('app-tour-open');
            this.root.hidden = false;
            this.showStep(firstIndex);
            document.addEventListener('keydown', this.handleKeydown);
            window.addEventListener('resize', this.handleReposition);
            window.addEventListener('scroll', this.handleReposition, true);
            return true;
        }

        finish(markSeen) {
            if (markSeen !== false) {
                this.markCompleted();
            }
            this.teardown();
        }

        teardown() {
            this.clearTarget();
            this.active = false;
            this.currentIndex = -1;
            document.body.classList.remove('app-tour-open');
            if (this.root) {
                this.root.hidden = true;
            }
            document.removeEventListener('keydown', this.handleKeydown);
            window.removeEventListener('resize', this.handleReposition);
            window.removeEventListener('scroll', this.handleReposition, true);
        }

        next() {
            const nextIndex = this.findAvailableIndex(this.currentIndex + 1, 1);
            if (nextIndex === -1) {
                this.finish(true);
                return;
            }
            this.showStep(nextIndex);
        }

        back() {
            const previousIndex = this.findAvailableIndex(this.currentIndex - 1, -1);
            if (previousIndex === -1) {
                return;
            }
            this.showStep(previousIndex);
        }

        skip() {
            this.finish(true);
        }

        build() {
            this.root = document.createElement('div');
            this.root.className = 'app-tour-root';
            this.root.hidden = true;
            this.root.innerHTML = [
                '<div class="app-tour-overlay" data-tour-overlay></div>',
                '<div class="app-tour-spotlight" data-tour-spotlight></div>',
                '<section class="app-tour-tooltip" data-tour-tooltip role="dialog" aria-modal="true" aria-label="Quick tour">',
                '  <div class="app-tour-progress" data-tour-progress></div>',
                '  <h2 class="app-tour-title" data-tour-title></h2>',
                '  <p class="app-tour-body" data-tour-body></p>',
                '  <div class="app-tour-actions">',
                '    <button type="button" class="app-tour-button app-tour-button-secondary" data-tour-back>Back</button>',
                '    <button type="button" class="app-tour-button app-tour-button-ghost" data-tour-skip>Skip</button>',
                '    <button type="button" class="app-tour-button app-tour-button-primary" data-tour-next>Next</button>',
                '    <button type="button" class="app-tour-button app-tour-button-primary" data-tour-finish hidden>Finish</button>',
                '  </div>',
                '</section>'
            ].join('');

            document.body.appendChild(this.root);
            this.overlay = this.root.querySelector('[data-tour-overlay]');
            this.spotlight = this.root.querySelector('[data-tour-spotlight]');
            this.tooltip = this.root.querySelector('[data-tour-tooltip]');
            this.titleEl = this.root.querySelector('[data-tour-title]');
            this.bodyEl = this.root.querySelector('[data-tour-body]');
            this.progressEl = this.root.querySelector('[data-tour-progress]');
            this.backButton = this.root.querySelector('[data-tour-back]');
            this.nextButton = this.root.querySelector('[data-tour-next]');
            this.finishButton = this.root.querySelector('[data-tour-finish]');

            this.root.querySelector('[data-tour-skip]').addEventListener('click', () => this.skip());
            this.backButton.addEventListener('click', () => this.back());
            this.nextButton.addEventListener('click', () => this.next());
            this.finishButton.addEventListener('click', () => this.finish(true));
        }

        findAvailableIndex(startIndex, direction) {
            if (!this.steps.length) {
                return -1;
            }

            for (
                let index = startIndex;
                index >= 0 && index < this.steps.length;
                index += direction
            ) {
                if (resolveTarget(this.steps[index].selector)) {
                    return index;
                }
            }

            return -1;
        }

        showStep(index) {
            const step = this.steps[index];
            const target = resolveTarget(step.selector);
            if (!step || !target) {
                const fallbackIndex = this.findAvailableIndex(index + 1, 1);
                if (fallbackIndex !== -1) {
                    this.showStep(fallbackIndex);
                } else {
                    this.finish(false);
                }
                return;
            }

            this.currentIndex = index;
            this.clearTarget();
            this.target = target;
            this.target.classList.add(ACTIVE_TARGET_CLASS);

            this.titleEl.textContent = step.title || '';
            this.bodyEl.textContent = step.body || '';
            this.progressEl.textContent = `Step ${this.getDisplayIndex(index)} of ${this.getStepCount()}`;

            const previousIndex = this.findAvailableIndex(index - 1, -1);
            const nextIndex = this.findAvailableIndex(index + 1, 1);
            this.backButton.disabled = previousIndex === -1;
            this.nextButton.hidden = nextIndex === -1;
            this.finishButton.hidden = nextIndex !== -1;

            target.scrollIntoView({
                behavior: 'smooth',
                block: step.block || 'center',
                inline: 'nearest'
            });

            window.setTimeout(() => this.position(), 240);
            window.setTimeout(() => this.position(), 420);
        }

        getStepCount() {
            return this.steps.filter((step) => resolveTarget(step.selector)).length || 1;
        }

        getDisplayIndex(index) {
            let count = 0;
            for (let current = 0; current <= index; current += 1) {
                if (resolveTarget(this.steps[current].selector)) {
                    count += 1;
                }
            }
            return count || 1;
        }

        position() {
            if (!this.active || !this.target || !this.tooltip || !this.spotlight) {
                return;
            }

            const rect = this.target.getBoundingClientRect();
            if (rect.width === 0 && rect.height === 0) {
                return;
            }

            const padding = this.highlightPadding;
            const spotlightLeft = Math.max(8, rect.left - padding);
            const spotlightTop = Math.max(8, rect.top - padding);
            const spotlightWidth = Math.min(window.innerWidth - 16 - spotlightLeft, rect.width + (padding * 2));
            const spotlightHeight = Math.min(window.innerHeight - 16 - spotlightTop, rect.height + (padding * 2));

            this.spotlight.style.left = `${spotlightLeft}px`;
            this.spotlight.style.top = `${spotlightTop}px`;
            this.spotlight.style.width = `${Math.max(spotlightWidth, 24)}px`;
            this.spotlight.style.height = `${Math.max(spotlightHeight, 24)}px`;

            const step = this.steps[this.currentIndex] || {};
            const tooltipRect = this.tooltip.getBoundingClientRect();
            const gap = 18;
            const margin = 12;
            const viewportWidth = window.innerWidth;
            const viewportHeight = window.innerHeight;
            const placements = [
                step.placement || 'bottom',
                'bottom',
                'bottom-end',
                'bottom-start',
                'right',
                'left',
                'top'
            ];

            let resolvedTop = margin;
            let resolvedLeft = margin;
            let resolvedPlacement = placements[0];

            for (const placement of placements) {
                const candidate = this.getTooltipPosition(placement, rect, tooltipRect, gap);
                const fitsVertically = candidate.top >= margin &&
                    (candidate.top + tooltipRect.height) <= (viewportHeight - margin);
                const fitsHorizontally = candidate.left >= margin &&
                    (candidate.left + tooltipRect.width) <= (viewportWidth - margin);

                if (fitsVertically && fitsHorizontally) {
                    resolvedTop = candidate.top;
                    resolvedLeft = candidate.left;
                    resolvedPlacement = placement;
                    break;
                }

                resolvedTop = clamp(candidate.top, margin, Math.max(margin, viewportHeight - tooltipRect.height - margin));
                resolvedLeft = clamp(candidate.left, margin, Math.max(margin, viewportWidth - tooltipRect.width - margin));
            }

            this.tooltip.dataset.placement = resolvedPlacement;
            this.tooltip.style.top = `${resolvedTop}px`;
            this.tooltip.style.left = `${resolvedLeft}px`;
        }

        getTooltipPosition(placement, rect, tooltipRect, gap) {
            switch (placement) {
                case 'bottom-start':
                    return {
                        top: rect.bottom + gap,
                        left: rect.left
                    };
                case 'bottom-end':
                    return {
                        top: rect.bottom + gap,
                        left: rect.right - tooltipRect.width
                    };
                case 'top-start':
                    return {
                        top: rect.top - tooltipRect.height - gap,
                        left: rect.left
                    };
                case 'top-end':
                    return {
                        top: rect.top - tooltipRect.height - gap,
                        left: rect.right - tooltipRect.width
                    };
                case 'left':
                    return {
                        top: rect.top + (rect.height / 2) - (tooltipRect.height / 2),
                        left: rect.left - tooltipRect.width - gap
                    };
                case 'right':
                    return {
                        top: rect.top + (rect.height / 2) - (tooltipRect.height / 2),
                        left: rect.right + gap
                    };
                case 'top':
                    return {
                        top: rect.top - tooltipRect.height - gap,
                        left: rect.left + (rect.width / 2) - (tooltipRect.width / 2)
                    };
                default:
                    return {
                        top: rect.bottom + gap,
                        left: rect.left + (rect.width / 2) - (tooltipRect.width / 2)
                    };
            }
        }

        clearTarget() {
            if (this.target) {
                this.target.classList.remove(ACTIVE_TARGET_CLASS);
            }
            this.target = null;
        }

        handleKeydown(event) {
            if (!this.active) {
                return;
            }

            if (event.key === 'Escape') {
                event.preventDefault();
                this.skip();
            } else if (event.key === 'ArrowRight' || event.key === 'Enter') {
                event.preventDefault();
                if (this.finishButton.hidden) {
                    this.next();
                } else {
                    this.finish(true);
                }
            } else if (event.key === 'ArrowLeft') {
                event.preventDefault();
                this.back();
            }
        }

        handleReposition() {
            window.requestAnimationFrame(() => this.position());
        }
    }

    window.DotmacTour = {
        create(options) {
            const tour = new GuidedTour(options);
            if (tour.shouldAutoStart()) {
                window.setTimeout(() => {
                    tour.start();
                }, tour.startDelay);
            }
            return tour;
        }
    };
})();
