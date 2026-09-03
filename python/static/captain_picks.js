/* The captain-picks page: the [This week | History] pills and the history view.
 *
 * "This week" is server-rendered and always in the DOM; this file only toggles
 * it against the history view and, when history is first opened, fetches the
 * track record and steps through it a gameweek at a time.
 *
 * Loaded after util.js (for esc). No other dependency: the history cards are
 * plain text and a club name, so there is no shirt to draw and no need for the
 * pitch machinery in app.js.
 */

'use strict';

(function () {
    const tabs = document.getElementById('captainViewTabs');
    if (!tabs) return;
    const weekView = document.getElementById('capWeekView');
    const histView = document.getElementById('capHistoryView');

    tabs.querySelectorAll('.nav-link').forEach(btn => {
        btn.addEventListener('click', () => {
            tabs.querySelectorAll('.nav-link').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            const history = btn.dataset.view === 'history';
            weekView.classList.toggle('d-none', history);
            histView.classList.toggle('d-none', !history);
            if (history) loadHistory();
        });
    });

    let history = null;   // the fetched list, newest first
    let idx = 0;          // which gameweek is on screen
    let loading = false;

    const body = document.getElementById('capHistBody');
    const nav = document.getElementById('capHistNav');
    const title = document.getElementById('capHistTitle');
    const prev = document.getElementById('capHistPrev');
    const next = document.getElementById('capHistNext');

    // Newest first, so "previous" (older) steps forward through the array and
    // "next" (newer) steps back. Wired to read that way round so the arrows
    // point the way a reader expects: left is back in time.
    prev.addEventListener('click', () => step(1));
    next.addEventListener('click', () => step(-1));

    function step(delta) {
        if (!history) return;
        idx = Math.min(history.length - 1, Math.max(0, idx + delta));
        render();
    }

    function loadHistory() {
        if (history || loading) return;
        loading = true;
        fetch('/api/captain-history')
            .then(r => r.json())
            .then(d => {
                history = (d && d.history) || [];
                idx = 0;
                render();
            })
            .catch(() => {
                body.innerHTML = '<p class="text-muted small">Couldn’t load the '
                    + 'captain history just now.</p>';
            })
            .finally(() => { loading = false; });
    }

    function pts(v) { return (v === null || v === undefined) ? '–' : v; }

    // One player, as a card. `kind` styles the model side and the actual side
    // apart; `foot` is the small line under the figure (the projection, or the
    // real return).
    function card(p, kind, figure, foot) {
        const name = p.path
            ? '<a href="' + esc(p.path) + '">' + esc(p.name) + '</a>'
            : esc(p.name || '—');
        return '<article class="gw-card cap-hist-card cap-hist-' + kind + '">'
            + '<div class="cap-hist-role">' + (kind === 'model'
                ? 'Model’s captain' : 'Top scorer') + '</div>'
            + '<h3 class="gw-card-name">' + name + '</h3>'
            + '<p class="text-muted small mb-2">' + esc(p.team_name || '') + '</p>'
            + '<p class="gw-card-figure">' + figure + '</p>'
            + '<p class="text-muted small mb-0">' + foot + '</p>'
            + '</article>';
    }

    function render() {
        if (!history) return;
        if (!history.length) {
            nav.hidden = true;
            body.innerHTML = '<p class="text-muted small">No settled gameweeks yet. '
                + 'This fills in once a gameweek has been played and written up.</p>';
            return;
        }
        nav.hidden = false;
        const h = history[idx];
        title.textContent = 'Gameweek ' + h.gameweek;
        prev.disabled = idx >= history.length - 1;   // no older week
        next.disabled = idx <= 0;                    // no newer week

        const model = h.predicted, actual = h.actual;
        const modelFig = (model.projected != null
            ? Number(model.projected).toFixed(1) + ' projected' : 'projected');
        const modelFoot = model.actual_points != null
            ? 'Actually scored <strong>' + pts(model.actual_points) + '</strong>'
            : 'His real return isn’t in the week’s top scorers';
        const actualFoot = 'Scored <strong>' + pts(actual.points) + '</strong> — '
            + 'the week’s highest';

        const verdict = h.matched
            ? '<div class="cap-hist-verdict is-hit">✓ The model captained the '
              + 'gameweek’s top scorer.</div>'
            : '<div class="cap-hist-verdict is-miss">The top scorer was '
              + esc(actual.name || 'someone else')
              + (model.actual_points != null && actual.points != null
                  ? ' — ' + (actual.points - model.actual_points)
                    + ' points more than the pick returned.'
                  : '.')
              + '</div>';

        body.innerHTML = '<div class="gw-cards cap-hist-pair">'
            + card(model, 'model', modelFig, modelFoot)
            + card(actual, 'actual', pts(actual.points) + ' points', actualFoot)
            + '</div>' + verdict;
    }
})();
