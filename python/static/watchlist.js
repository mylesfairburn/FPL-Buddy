/* The watchlist: a shortlist that survives the page.
 *
 * Its own file, like kits.js and compare.js - app.js is three and a half
 * thousand lines that cannot be exercised without a whole document around it.
 * See tests/js/test_watchlist.js.
 *
 * Loaded AFTER app.js. Both directions resolve at call time through the global
 * scope classic scripts share:
 *
 *   here needs    getSavedId, fixtureTile, shirtImg   (app.js)
 *                 esc                                 (util.js)
 *   app.js needs  loadWatchlist, toggleWatch, updateWatchButton
 *
 * Stored server-side against the FPL ID, unauthenticated like the drafts - see
 * drafts.py and watchlist.py.
 */

// A Set of codes, so the pop-up's Watch button can answer "is this one on it"
// without a request per player. Refreshed on load and on change, never guessed.
let watchedCodes = new Set();

function loadWatchlist() {
    const id = getSavedId();
    const body = document.getElementById('watchlistBody');
    if (!body) return Promise.resolve();
    if (!id) {
        body.innerHTML = '<p class="text-muted small mb-0">Enter your FPL ID on the '
            + 'My Team tab first — the watchlist is stored against it.</p>';
        return Promise.resolve();
    }
    body.innerHTML = '<p class="text-muted small mb-0">Loading…</p>';
    return fetch('/api/watchlist/' + id)
        .then(r => r.json())
        .then(d => {
            const players = d.players || [];
            watchedCodes = new Set(players.map(p => p.code));
            renderWatchlist(players);
        })
        .catch(() => {
            body.innerHTML = '<p class="text-muted small mb-0">Couldn’t load the '
                + 'watchlist just now.</p>';
        });
}

function watchlistRow(p) {
    // A departed player keeps his row - dropping it would look like the site
    // had lost the entry. `available` is decided in watchlist.get().
    if (!p.available) {
        return '<tr><td colspan="8" class="text-muted small">'
            + 'Player ' + esc(p.code) + ' is no longer in the game'
            + (p.note ? ' — ' + esc(p.note) : '')
            + ' <button type="button" class="btn btn-link btn-sm p-0 align-baseline wl-remove"'
            + ' data-code="' + esc(p.code) + '">remove</button></td></tr>';
    }
    const gws = (p.next_gameweeks || []).slice(0, 3).map(g => fixtureTile(g)).join('');
    const name = p.path
        ? '<a href="' + esc(p.path) + '">' + esc(p.web_name) + '</a>'
        : esc(p.web_name);
    return '<tr>'
        + '<td class="ps-name">' + shirtImg(p.team_code, p.pos, 'shirt-sm')
        + '<span>' + name + '</span></td>'
        + '<td>' + esc(p.pos) + '</td>'
        + '<td class="text-muted">' + esc(p.team_name || '') + '</td>'
        + '<td class="text-end">' + (p.cost != null ? '£' + p.cost.toFixed(1) + 'm' : '') + '</td>'
        + '<td class="text-end">' + (p.predicted != null ? p.predicted.toFixed(1) : '–') + '</td>'
        + '<td class="wl-fixtures">' + gws + '</td>'
        + '<td class="wl-note">' + esc(p.note || '') + '</td>'
        + '<td class="text-end"><button type="button" class="btn btn-link btn-sm p-0 wl-remove"'
        + ' data-code="' + esc(p.code) + '" aria-label="Remove ' + esc(p.web_name) + '">&times;</button></td>'
        + '</tr>';
}

function renderWatchlist(players) {
    const body = document.getElementById('watchlistBody');
    if (!players.length) {
        body.innerHTML = '<p class="text-muted small mb-0">Nothing on your watchlist '
            + 'yet. Open a player from any table and choose <strong>Watch</strong>.</p>';
        return;
    }
    body.innerHTML = '<div class="ps-list"><table class="table table-sm wl-table mb-0">'
        + '<thead><tr><th>Player</th><th>Pos</th><th>Club</th>'
        + '<th class="text-end">Price</th><th class="text-end">Next GW</th>'
        + '<th>Next 3</th><th>Note</th><th></th></tr></thead>'
        + '<tbody>' + players.map(watchlistRow).join('') + '</tbody></table></div>';
    body.querySelectorAll('.wl-remove').forEach(btn =>
        btn.addEventListener('click', () => removeFromWatchlist(+btn.dataset.code)));
}

function removeFromWatchlist(code) {
    const id = getSavedId();
    if (!id) return;
    fetch('/api/watchlist/' + id + '/' + code, { method: 'DELETE' })
        .then(() => { watchedCodes.delete(code); loadWatchlist(); })
        .catch(() => {});
}

function toggleWatch(p) {
    const id = getSavedId();
    if (!id) {
        alert('Enter your FPL ID on the My Team tab first — the watchlist is stored against it.');
        return;
    }
    const on = watchedCodes.has(p.code);
    const req = on
        ? fetch('/api/watchlist/' + id + '/' + p.code, { method: 'DELETE' })
        : fetch('/api/watchlist/' + id, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ code: p.code }),
        });
    req.then(r => r.json().catch(() => ({})))
        .then(d => {
            // The cap is enforced server-side; a refusal arrives as `detail`.
            if (d && d.detail) { alert(d.detail); return; }
            if (on) watchedCodes.delete(p.code); else watchedCodes.add(p.code);
            updateWatchButton(p);
            const view = document.getElementById('watchlistView');
            if (view && !view.classList.contains('d-none')) loadWatchlist();
        })
        .catch(() => {});
}

function updateWatchButton(p) {
    const btn = document.getElementById('pmWatch');
    if (!btn) return;
    const on = watchedCodes.has(p.code);
    btn.textContent = on ? '★ Watching' : '☆ Watch';
    btn.classList.toggle('btn-primary', on);
    btn.classList.toggle('btn-outline-primary', !on);
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
}

// The Watch button must be right the first time a pop-up opens, which is
// usually before the watchlist tab has ever been visited.
let watchedPrimed = false;
function primeWatchedCodes() {
    if (watchedPrimed) return Promise.resolve();
    const id = getSavedId();
    if (!id) return Promise.resolve();
    watchedPrimed = true;
    return fetch('/api/watchlist/' + id)
        .then(r => r.json())
        .then(d => { watchedCodes = new Set((d.players || []).map(p => p.code)); })
        .catch(() => {});
}
