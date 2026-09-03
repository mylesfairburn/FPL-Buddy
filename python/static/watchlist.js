/* The watchlist: a shortlist that survives the page.
 *
 * Its own file, like kits.js and compare.js - app.js is three and a half
 * thousand lines that cannot be exercised without a whole document around it.
 * See tests/js/test_watchlist.js.
 *
 * Loaded AFTER app.js. Both directions resolve at call time through the global
 * scope classic scripts share:
 *
 *   here needs    currentFplId, esc                  (util.js)
 *                 fixtureTile, shirtImg               (app.js, tables only)
 *   app.js needs  loadWatchlist, toggleWatch, updateWatchButton
 *
 * Stored server-side against the FPL ID, unauthenticated like the drafts - see
 * drafts.py and watchlist.py.
 */

// A Set of codes, so the pop-up's Watch button can answer "is this one on it"
// without a request per player. Refreshed on load and on change, never guessed.
let watchedCodes = new Set();

// The star that appears beside a name in every table and on every profile.
//
// A <button>, not a glyph with a click handler, because it is a control: it
// reaches by keyboard, announces its pressed state, and speaks the same
// vocabulary as #pmWatch in the pop-up. Returned as a string rather than an
// element so it drops into the innerHTML the tables are assembled from -
// handing back a node would mean rewriting all three renderers that use it.
//
// `code` is FPL's season-stable player code. A row that hasn't got one yet -
// every server-rendered row, before /api/all_players lands - gets an empty
// string, so the cell is blank rather than the column going missing and the
// header sliding off the columns underneath it.
function watchStar(code, name) {
    if (code === null || code === undefined) return '';
    const on = watchedCodes.has(code);
    return '<button type="button" class="wl-star' + (on ? ' is-on' : '') + '"'
        + ' data-code="' + esc(code) + '"'
        + ' aria-pressed="' + (on ? 'true' : 'false') + '"'
        + ' title="' + (on ? 'On your watchlist' : 'Add to your watchlist') + '"'
        + ' aria-label="' + (on ? 'Remove ' : 'Add ') + esc(name || 'player')
        + (on ? ' from' : ' to') + ' your watchlist">'
        + (on ? '★' : '☆') + '</button>';
}

// Wire every star inside `root`. Called after each render rather than once at
// load, because these tables rebuild their own rows: a listener bound to a <tr>
// that has since been replaced is bound to nothing.
//
// The click is stopped from propagating deliberately. A star sits inside a row
// whose own click opens the player pop-up, and pressing "watch" should not also
// open a dialog on top of the thing you just pressed.
function bindWatchStars(root) {
    (root || document).querySelectorAll('.wl-star').forEach(btn => {
        if (btn.dataset.wired) return;
        btn.dataset.wired = '1';
        btn.addEventListener('click', ev => {
            ev.stopPropagation();
            ev.preventDefault();
            toggleWatch({ code: +btn.dataset.code }, btn);
        });
    });
}

// One star, repainted in place. The whole table is deliberately not re-rendered
// for a single press: the row the reader just touched would jump if the list
// re-sorted under it, and on the watchlist tab it would vanish mid-click.
function paintStar(btn, on) {
    btn.classList.toggle('is-on', on);
    btn.textContent = on ? '★' : '☆';
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    btn.title = on ? 'On your watchlist' : 'Add to your watchlist';
}

function loadWatchlist() {
    const id = currentFplId();
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
        + '<td class="wl-starcell">' + watchStar(p.code, p.web_name) + '</td>'
        + '<td class="ps-name">' + shirtImg(p.team_code, p.pos, 'shirt-sm')
        + '<span>' + name + '</span></td>'
        + '<td>' + esc(p.pos) + '</td>'
        + '<td class="text-muted">' + esc(p.team_name || '') + '</td>'
        + '<td class="text-end">' + (p.form != null ? esc(p.form) : '–') + '</td>'
        + '<td class="text-end">' + (p.cost != null ? '£' + p.cost.toFixed(1) + 'm' : '') + '</td>'
        + '<td class="wl-drift">' + priceDrift(p) + '</td>'
        + '<td class="text-end">' + (p.predicted != null ? p.predicted.toFixed(1) : '–') + '</td>'
        + '<td class="wl-fixtures">' + gws + '</td>'
        + '<td class="text-end"><button type="button" class="btn btn-link btn-sm p-0 wl-remove"'
        + ' data-code="' + esc(p.code) + '" aria-label="Remove ' + esc(p.web_name) + '">&times;</button></td>'
        + '</tr>';
}

// How close this player is to a price change: a bar and a word for it.
//
// The bar is the share of the threshold his net transfers have covered, and it
// is the same number against the same threshold that the price-changes page
// ranks its risers and fallers on - not a second estimate that could quietly
// disagree with the page that specialises in this.
//
// A player the model cannot measure - one snapshot, or too little ownership for
// the denominator to mean anything - gets a dash. "We cannot say" and "steady"
// are different answers and this column has to be able to give both.
function priceDrift(p) {
    if (!p.price_direction || p.price_progress == null) {
        return '<span class="wl-drift-none" title="Not enough price history for '
            + 'this player yet">–</span>';
    }
    const rising = p.price_direction === 'rise';
    const pct = Math.max(0, Math.min(100, p.price_progress));
    const label = pct >= 100 ? (rising ? 'due a rise' : 'due a fall')
        : pct >= 50 ? (rising ? 'rising' : 'falling')
            : 'steady';
    return '<span class="wl-bar" role="img" aria-label="' + esc(label) + ', '
        + Math.round(pct) + '% of the way to a price change">'
        + '<span class="wl-bar-fill ' + (rising ? 'is-rise' : 'is-fall') + '"'
        + ' style="width:' + pct.toFixed(0) + '%"></span></span>'
        + '<span class="wl-drift-label">' + esc(label) + '</span>';
}

function renderWatchlist(players) {
    const body = document.getElementById('watchlistBody');
    if (!players.length) {
        body.innerHTML = '<p class="text-muted small mb-0">Nothing on your watchlist '
            + 'yet. Open a player from any table and choose <strong>Watch</strong>.</p>';
        return;
    }
    body.innerHTML = '<div class="ps-list"><table class="table table-sm wl-table mb-0">'
        + '<thead><tr><th><span class="visually-hidden">Watching</span></th>'
        + '<th>Player</th><th>Pos</th><th>Club</th>'
        + '<th class="text-end">Form</th><th class="text-end">Price</th>'
        + '<th>Price change</th><th class="text-end">Next GW</th>'
        + '<th>Next 3</th><th></th></tr></thead>'
        + '<tbody>' + players.map(watchlistRow).join('') + '</tbody></table></div>';
    body.querySelectorAll('.wl-remove').forEach(btn =>
        btn.addEventListener('click', () => removeFromWatchlist(+btn.dataset.code)));
    bindWatchStars(body);
}

function removeFromWatchlist(code) {
    const id = currentFplId();
    if (!id) return;
    fetch('/api/watchlist/' + id + '/' + code, { method: 'DELETE' })
        .then(() => { watchedCodes.delete(code); loadWatchlist(); })
        .catch(() => {});
}

function toggleWatch(p, star) {
    const id = currentFplId();
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
            // Every star for this player, wherever it sits on the page. The
            // pop-up usually opens over the very row that produced it, and the
            // two must not disagree once it closes.
            document.querySelectorAll('.wl-star[data-code="' + p.code + '"]')
                .forEach(btn => paintStar(btn, !on));
            if (star) paintStar(star, !on);
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
    const id = currentFplId();
    if (!id) return Promise.resolve();
    watchedPrimed = true;
    return fetch('/api/watchlist/' + id)
        .then(r => r.json())
        .then(d => { watchedCodes = new Set((d.players || []).map(p => p.code)); })
        .catch(() => {});
}
