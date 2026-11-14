(function () {
    let loading = false;

    async function loadPage(page, destination, updateHistory) {
        const current = document.getElementById('customer-timeline-page');
        if (!current || loading || String(page) === current.dataset.page) return;

        const status = current.querySelector('[data-timeline-status]');
        const spinner = current.querySelector('[data-timeline-spinner]');
        const links = current.querySelectorAll('[data-timeline-page]');
        loading = true;
        status.textContent = 'Loading activity…';
        spinner.classList.remove('hidden');
        links.forEach((link) => {
            link.setAttribute('aria-disabled', 'true');
            link.style.pointerEvents = 'none';
        });

        try {
            const response = await fetch(`${current.dataset.url}?timeline_page=${page}`, {
                credentials: 'same-origin',
                headers: { 'Accept': 'text/html' },
            });
            if (!response.ok) throw new Error('Timeline request failed');
            const documentFragment = new DOMParser().parseFromString(await response.text(), 'text/html');
            const replacement = documentFragment.getElementById('customer-timeline-page');
            if (!replacement || replacement.dataset.page !== String(page)) {
                throw new Error('Invalid timeline response');
            }
            current.replaceWith(replacement);
            if (updateHistory) window.history.pushState(null, '', destination);
        } catch (error) {
            status.textContent = 'Couldn’t load activity. Try again.';
            links.forEach((link) => {
                link.removeAttribute('aria-disabled');
                link.style.pointerEvents = '';
            });
        } finally {
            spinner.classList.add('hidden');
            loading = false;
        }
    }

    document.addEventListener('click', (event) => {
        const link = event.target.closest?.('[data-timeline-page]');
        if (!link) return;
        event.preventDefault();
        if (loading) return;
        const page = new URL(link.href).searchParams.get('timeline_page');
        if (page) void loadPage(page, link.href, true);
    });

    window.addEventListener('popstate', () => {
        const page = new URL(window.location.href).searchParams.get('timeline_page') || '1';
        void loadPage(page, window.location.href, false);
    });
})();
