/* The price-changes page: the [Current | History] pills.
 *
 * Both views are server-rendered and in the DOM already - the history list is
 * plain HTML so a crawler and a no-JS reader see it too - so this only toggles
 * which one is shown. No fetch, no dependency.
 */

'use strict';

(function () {
    const tabs = document.getElementById('priceViewTabs');
    if (!tabs) return;
    const current = document.getElementById('pcCurrentView');
    const history = document.getElementById('pcHistoryView');
    tabs.querySelectorAll('.nav-link').forEach(btn => {
        btn.addEventListener('click', () => {
            tabs.querySelectorAll('.nav-link').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            const showHistory = btn.dataset.view === 'history';
            current.classList.toggle('d-none', showHistory);
            history.classList.toggle('d-none', !showHistory);
        });
    });
})();
