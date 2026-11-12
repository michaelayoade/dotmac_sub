(function () {
    let loading = false;

    async function loadPage(page, destination, updateHistory) {
        const current = document.getElementById('outage-reachability-page');
        if (!current || loading || String(page) === current.dataset.page) return;

        const status = current.querySelector('[data-reachability-status]');
        const spinner = current.querySelector('[data-reachability-spinner]');
        const links = current.querySelectorAll('[data-reachability-page]');
        loading = true;
        status.textContent = 'Loading devices…';
        spinner.classList.remove('hidden');
        links.forEach((link) => {
            link.setAttribute('aria-disabled', 'true');
            link.style.pointerEvents = 'none';
        });

        try {
            const response = await fetch(`${current.dataset.url}?reachability_page=${page}`, {
                credentials: 'same-origin',
                headers: { 'Accept': 'text/html' },
            });
            if (!response.ok) throw new Error('Reachability request failed');
            const fragment = new DOMParser().parseFromString(await response.text(), 'text/html');
            const replacement = fragment.getElementById('outage-reachability-page');
            if (!replacement?.dataset.page) throw new Error('Invalid reachability response');
            current.replaceWith(replacement);
            const resolvedUrl = new URL(destination);
            resolvedUrl.searchParams.set('reachability_page', replacement.dataset.page);
            if (updateHistory) window.history.pushState(null, '', resolvedUrl);
            else if (replacement.dataset.page !== String(page)) {
                window.history.replaceState(null, '', resolvedUrl);
            }
        } catch (error) {
            status.textContent = 'Couldn’t load devices. Try again.';
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
        const link = event.target.closest?.('[data-reachability-page]');
        if (!link) return;
        event.preventDefault();
        if (loading) return;
        const page = new URL(link.href).searchParams.get('reachability_page');
        if (page) void loadPage(page, link.href, true);
    });

    window.addEventListener('popstate', () => {
        const page = new URL(window.location.href).searchParams.get('reachability_page') || '1';
        void loadPage(page, window.location.href, false);
    });
})();
