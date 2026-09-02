/* Player comparison.
 *
 * Its own file: app.js wires up the pitch and the tab panes, none of which
 * exist here, and would throw on the first missing element.
 *
 * Runs entirely off /api/all_players, which the Players tab already loads.
 * The selection lives in the query string and is not server-rendered - a
 * crawler following a few of those would index near-identical pages.
 */

const SLOTS = 3;
const HORIZON = 8;                       // gameweeks summed for the run total

const inputs = [...document.querySelectorAll('.cmp-input')];
const statusEl = document.getElementById('cmpStatus');
const resultEl = document.getElementById('cmpResult');
const headEl = document.getElementById('cmpHead');
const bodyEl = document.getElementById('cmpBody');
const noteEl = document.getElementById('cmpNote');

let pool = [];
let byCode = new Map();

// `better`: 1 higher wins, -1 lower wins, 0 the row is a fact rather than a
// merit. Declared as data so the highlight is one rule, not a case per cell.
const ROWS = [
    { key: 'team_name', label: 'Club', better: 0, fmt: v => esc(v || '—') },
    { key: 'pos', label: 'Position', better: 0, fmt: v => esc(v || '—') },
    { key: 'cost', label: 'Price', better: -1, fmt: v => v == null ? '—' : `£${v.toFixed(1)}m` },
    { key: 'horizon', label: `Projected, next ${HORIZON} GWs`, better: 1,
      fmt: v => v == null ? '—' : v.toFixed(1) },
    { key: 'predicted', label: 'Projected, next GW', better: 1,
      fmt: v => v == null ? '—' : v.toFixed(1) },
    { key: 'perMillion', label: 'Projected per £m', better: 1,
      fmt: v => v == null ? '—' : v.toFixed(2) },
    { key: 'rating', label: 'Rating (vs position)', better: 1,
      fmt: v => v == null ? '—' : String(Math.round(v)) },
    { key: 'form', label: 'Form', better: 1, fmt: v => v == null ? '—' : v.toFixed(1) },
    { key: 'owned', label: 'Owned by', better: 0, fmt: v => v == null ? '—' : `${v.toFixed(1)}%` },
    { key: 'availability', label: 'Availability', better: 0, fmt: v => esc(v) },
    { key: 'fixtures', label: `Next ${HORIZON} fixtures`, better: 0, fmt: v => v },
];

const STATUS_TEXT = {
    a: 'Available', d: 'Doubtful', i: 'Injured', s: 'Suspended', u: 'Unavailable',
};

function horizonPoints(p) {
    const gws = (p.next_gameweeks || []).slice(0, HORIZON);
    const pts = gws.map(g => g.points).filter(v => v != null);
    return pts.length ? pts.reduce((a, b) => a + b, 0) : null;
}

function availabilityText(p) {
    const base = STATUS_TEXT[(p.status || 'a').toLowerCase()] || 'Unknown';
    if (p.chance_of_playing_next_round != null) {
        return `${base} (${p.chance_of_playing_next_round}%)`;
    }
    return base;
}

function fixturesHtml(p) {
    const gws = (p.next_gameweeks || []).slice(0, HORIZON);
    if (!gws.length) return '<span class="text-muted">—</span>';
    return gws.map(g => {
        const ha = g.was_home === true ? '(H)' : g.was_home === false ? '(A)' : '';
        return `<span class="mini-gw" style="background:${colorFor(g.difficulty, 1, 5)}"`
            + ` title="GW${esc(g.event)}"><b>${esc(g.opponent || '?')}${ha}</b></span>`;
    }).join('');
}

function decorate(p) {
    const horizon = horizonPoints(p);
    return {
        ...p,
        horizon,
        // Value - the comparison a price column alone cannot make.
        perMillion: (horizon != null && p.cost) ? horizon / p.cost : null,
        availability: availabilityText(p),
    };
}

// ---- URL state ----
// ?p=code,code so a comparison is a link. Codes, not ids: FPL reassigns `id`
// every summer, so an id-keyed link would point at different players.
function selectedCodes() {
    const raw = new URLSearchParams(location.search).get('p') || '';
    return raw.split(',').map(s => parseInt(s, 10))
        .filter(n => Number.isFinite(n)).slice(0, SLOTS);
}

function writeUrl(codes) {
    if (!history.replaceState) return;
    const query = codes.filter(Boolean).join(',');
    // replaceState: picking a player refines one view. pushState would make
    // Back step through every name tried.
    history.replaceState(null, '', query ? `/compare?p=${query}` : '/compare');
}

// ---- Rendering ----
function currentCodes() {
    return inputs.map(input => {
        const code = parseInt(input.dataset.code || '', 10);
        return Number.isFinite(code) ? code : null;
    });
}

function render() {
    const codes = currentCodes();
    const chosen = codes.map(c => (c == null ? null : byCode.get(c)))
                        .filter(Boolean).map(decorate);
    writeUrl(codes);

    if (chosen.length < 2) {
        resultEl.classList.add('d-none');
        statusEl.textContent = 'Pick two players to compare.';
        statusEl.classList.remove('d-none');
        return;
    }
    statusEl.classList.add('d-none');
    resultEl.classList.remove('d-none');

    headEl.innerHTML = '<th scope="col"></th>' + chosen.map(p =>
        `<th scope="col">${p.path ? `<a href="${esc(p.path)}">${esc(p.web_name)}</a>`
                                  : esc(p.web_name)}</th>`).join('');

    bodyEl.innerHTML = ROWS.map(row => {
        const values = chosen.map(p => p[row.key]);
        // Ties stay unmarked: highlighting both would claim the row separates
        // them when it does not.
        let best = -1;
        if (row.better !== 0) {
            const numeric = values.map(v => (typeof v === 'number' ? v : null));
            const present = numeric.filter(v => v != null);
            if (present.length > 1) {
                const target = row.better === 1 ? Math.max(...present) : Math.min(...present);
                if (present.filter(v => v === target).length === 1) {
                    best = numeric.indexOf(target);
                }
            }
        }
        const cells = chosen.map((p, i) => {
            const rendered = row.key === 'fixtures' ? fixturesHtml(p) : row.fmt(values[i]);
            return `<td class="${i === best ? 'cmp-best' : ''}">${rendered}</td>`;
        }).join('');
        return `<tr><th scope="row">${esc(row.label)}</th>${cells}</tr>`;
    }).join('');

    noteEl.textContent = 'Highlighted cells are the better of the two on that row, '
        + 'where "better" means something — club, position and ownership are facts '
        + 'rather than merits, so nothing is marked on those.';
}

// ---- Pickers ----
function fillOptions(input) {
    const term = input.value.trim().toLowerCase();
    const list = document.getElementById(`cmpOptions${input.dataset.slot}`);
    if (!term || term.length < 2) { list.innerHTML = ''; return; }
    const matches = pool
        .filter(p => (p.web_name || '').toLowerCase().includes(term))
        .slice(0, 20);
    list.innerHTML = matches.map(p =>
        `<option value="${esc(p.web_name)} (${esc(p.team_name)}, ${esc(p.pos)})"></option>`).join('');
}

function resolve(input) {
    // The datalist value carries the club and position so two players with the
    // same surname are distinguishable; match on the whole label first, then
    // fall back to a plain name match for anyone typing it out by hand.
    const typed = input.value.trim().toLowerCase();
    if (!typed) { delete input.dataset.code; return; }
    const label = p => `${p.web_name} (${p.team_name}, ${p.pos})`.toLowerCase();
    const hit = pool.find(p => label(p) === typed)
             || pool.find(p => (p.web_name || '').toLowerCase() === typed);
    if (hit) input.dataset.code = String(hit.code);
    else delete input.dataset.code;
}

inputs.forEach(input => {
    input.addEventListener('input', () => { fillOptions(input); resolve(input); render(); });
    input.addEventListener('change', () => { resolve(input); render(); });
});

fetch('/api/all_players')
    .then(r => r.json())
    .then(d => {
        pool = (d.players || []).filter(p => p.code != null);
        byCode = new Map(pool.map(p => [p.code, p]));
        // Prefill from the URL, so a shared link opens on the comparison it
        // describes rather than on an empty form.
        selectedCodes().forEach((code, i) => {
            const p = byCode.get(code);
            if (!p || !inputs[i]) return;
            inputs[i].value = `${p.web_name} (${p.team_name}, ${p.pos})`;
            inputs[i].dataset.code = String(code);
        });
        render();
    })
    .catch(() => {
        statusEl.textContent = 'Couldn’t load the player list. Try reloading the page.';
    });
