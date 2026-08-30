/* FPL Companion front end.
 *
 * Served as a static file rather than inlined in the template, so the
 * browser can cache it between page loads. It contains no Jinja
 * variables - everything it needs comes from the /api endpoints.
 *
 * The markup it drives lives in templates/panes/ (one file per tab) and
 * templates/partials/ (navbar, tabs, footer, player modal).
 */

// ---- Broken-image fallbacks ----
// Images that should quietly disappear rather than show a broken-image icon
// carry data-onerror="hide" (removes the space too) or "invisible" (keeps the
// layout). One delegated listener rather than inline onerror="" attributes.
//
// Inline handlers had to go before a Content-Security-Policy could be added:
// script-src blocks an on* attribute whatever its content, and it blocks it
// wherever it came from - including strings built in this file and assigned
// through innerHTML. So the two in here were as much of a problem as the two in
// the templates, and less obvious, because the security test only reads the
// markup the server sends.
//
// Registered in the CAPTURE phase, which is load-bearing: `error` events from
// an <img> do not bubble, so a listener on document would never see one. They
// do capture.
document.addEventListener('error', (e) => {
    const el = e.target;
    if (!(el instanceof HTMLImageElement)) return;
    const mode = el.dataset.onerror;
    if (mode === 'hide') el.style.display = 'none';
    else if (mode === 'invisible') el.style.visibility = 'hidden';
}, true);

// ---- Tabs ----
// Each tab is a real URL served by the server, not a fragment of one document.
// Switching tabs still happens entirely in the browser - nothing is re-fetched -
// but the address bar changes to the actual path, so the page can be linked to,
// bookmarked, shared and indexed. A '#pane-players' fragment could do none of
// those: it never reaches the server, so every tab looked like the same page.
//
// The map has to match the routes in main.py. It's short and it changes about
// once a year, which is why it's a literal here rather than something generated.
const SCROLL_KEY = 'fpl_scroll_pos';
const PANE_PATHS = {
    'pane-team': '/my-team',
    'pane-ai-teams': '/ai-teams',
    'pane-players': '/players',
    'pane-rotator': '/fixture-rotator',
};
const PATH_PANES = {};
Object.keys(PANE_PATHS).forEach(p => { PATH_PANES[PANE_PATHS[p]] = p; });
const PANES = Object.keys(PANE_PATHS);

function activatePane(pane, opts) {
    opts = opts || {};
    if (!PANES.includes(pane)) pane = 'pane-team';
    const btn = document.querySelector(`#mainTabs [data-pane="${pane}"]`);
    if (!btn) return;
    document.querySelectorAll('#mainTabs [data-pane]').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-pane').forEach(p => p.classList.add('d-none'));
    btn.classList.add('active');
    document.getElementById(pane).classList.remove('d-none');
    // pushState, not replaceState: each tab is a place you've been, so Back
    // should return to the previous tab rather than leaving the site. Skipped
    // when the path already matches - on first load, and when popstate is what
    // moved us here, in which case the browser has already changed the URL.
    const path = PANE_PATHS[pane];
    if (path && history.pushState && location.pathname !== path) {
        history.pushState({ pane: pane }, '', path);
    }
    // The URL changed, so the title has to as well. Otherwise the browser tab -
    // and any bookmark taken from it - keeps the name of whichever page you
    // first landed on, which is the wrong page by the time you've switched
    // twice. The server puts each tab's real <title> on the link.
    if (btn.dataset.title) document.title = btn.dataset.title;
    // Same reasoning for the visible heading. It's the page's <h1>, so leaving
    // it on the tab you first landed on would have every page after the first
    // headed with the wrong tool's name.
    const heading = document.getElementById('pageHeading');
    if (heading && btn.dataset.h1) heading.textContent = btn.dataset.h1;
    if (pane === 'pane-players') ensurePlayers().then(() => playersTabSearch.refresh());
    if (pane === 'pane-ai-teams') showAiView(currentAiView);
    // Only reset scroll on a real click; a restore wants to keep it.
    if (!opts.restoring) window.scrollTo(0, 0);
}

// [data-pane], not .nav-link: the bar also carries the Gameweek briefings and
// Players A–Z links, which are separate documents rather than panes of this
// one. Without the attribute filter they'd be intercepted here, have their
// navigation prevented, and silently open the My Team tab instead.
document.querySelectorAll('#mainTabs [data-pane]').forEach(btn => {
    btn.addEventListener('click', e => {
        // The tabs are anchors so crawlers can follow them and so middle-click
        // still opens a new tab - but a plain left-click should switch panes
        // instantly rather than reload the whole document. Modified clicks are
        // left alone: the user is asking for a new tab or window.
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
        e.preventDefault();
        activatePane(btn.dataset.pane);
    });
});

// Back/forward between tabs. Without this the URL would change and the page
// would not, which is worse than having no history at all.
window.addEventListener('popstate', () => {
    const pane = PATH_PANES[location.pathname];
    if (pane) activatePane(pane, { restoring: true });
});

// Throttled so a scroll doesn't hit storage on every frame.
let scrollSaveTimer = null;
window.addEventListener('scroll', () => {
    if (scrollSaveTimer) return;
    scrollSaveTimer = setTimeout(() => {
        scrollSaveTimer = null;
        try { sessionStorage.setItem(SCROLL_KEY, String(window.scrollY)); } catch (e) {}
    }, 200);
});

// The two AI squads answer different questions, so they stay separate views -
// but they're one destination, not two top-level tabs. Which view you were on
// is remembered alongside the tab.
const AI_VIEW_KEY = 'fpl_ai_view';
let currentAiView = 'mgr';
try { currentAiView = sessionStorage.getItem(AI_VIEW_KEY) || 'mgr'; } catch (e) {}

function showAiView(view) {
    currentAiView = (view === 'xi') ? 'xi' : 'mgr';
    try { sessionStorage.setItem(AI_VIEW_KEY, currentAiView); } catch (e) {}
    document.querySelectorAll('#aiViewTabs .nav-link').forEach(b =>
        b.classList.toggle('active', b.dataset.view === currentAiView));
    document.getElementById('aiViewMgr').classList.toggle('d-none', currentAiView !== 'mgr');
    document.getElementById('aiViewXi').classList.toggle('d-none', currentAiView !== 'xi');
    // Loaded lazily, so opening the tab doesn't solve both squads at once.
    if (currentAiView === 'mgr') ensureMgr(); else ensureAi();
}

document.querySelectorAll('#aiViewTabs .nav-link').forEach(btn =>
    btn.addEventListener('click', () => showAiView(btn.dataset.view)));

function restoreView() {
    // The URL is the answer. The server has already told us which tab this path
    // is (and rendered it open, so there's no flash of the wrong pane); reading
    // location.pathname is the fallback for a cached document served before
    // that variable existed.
    const pane = window.__INITIAL_PANE__ || PATH_PANES[location.pathname] || 'pane-team';
    activatePane(pane, { restoring: true });

    let y = 0;
    try { y = parseInt(sessionStorage.getItem(SCROLL_KEY) || '0', 10) || 0; } catch (e) {}
    if (!y) return;
    // Content loads asynchronously, so the page is short at first and a
    // single scrollTo would be clamped. Retry briefly as it grows, and
    // stop early if the user scrolls themselves.
    let tries = 0;
    let interrupted = false;
    const stop = () => { interrupted = true; };
    window.addEventListener('wheel', stop, { once: true, passive: true });
    window.addEventListener('touchstart', stop, { once: true, passive: true });
    const tick = setInterval(() => {
        if (interrupted || ++tries > 20 || Math.abs(window.scrollY - y) < 2) {
            clearInterval(tick);
            return;
        }
        if (document.documentElement.scrollHeight - window.innerHeight >= y) {
            window.scrollTo(0, y);
            clearInterval(tick);
        }
    }, 100);
}

// ---- Shirt kits ----
// shirtImg() now lives in kits.js, which draws each club's shirt as inline SVG
// from a colour map. It replaced PNGs hotlinked from fantasy.premierleague.com:
// those were someone else's artwork served off someone else's bandwidth, and a
// path change at their end would have broken every player card here at once.
// kits.js is loaded before this file in index.html.

// Home/away marker. Bracketed everywhere - "ARS H" reads like a scoreline,
// "ARS (H)" reads as a venue, which is what it is.
function haTag(g) {
    if (!g || g.was_home == null) return '';
    return g.was_home ? '(H)' : '(A)';
}

// ---- Availability banner ------------------------------------------------
// FPL flags a player with a status letter and, usually, a percentage chance of
// featuring. That percentage is the useful number - "doubtful" could mean 75%
// or 25%, and those are very different decisions - so it's shown directly
// rather than hidden behind a tooltip on a small coloured dot.
//
// Colour tracks the chance so the pitch is scannable at a glance: you should be
// able to see you have a problem without reading anything.
function availabilityBand(p) {
    const status = (p.status || 'a').toLowerCase();
    const chance = p.chance_of_playing_next_round;

    // Every player carries a band, so the absence of a warning is itself
    // information: "100%" says this player was checked and is fit, where a blank
    // space could equally mean nobody has looked.
    let pct = (chance == null) ? null : Math.max(0, Math.min(100, Number(chance)));
    if (pct == null) {
        // FPL omits the percentage for most players, and what that implies
        // depends on the status letter: 'a' means fully fit, injured/suspended/
        // unavailable means not playing. Only a bare 'd' is a real unknown -
        // there a number would be invented rather than inferred, so it keeps a
        // word instead.
        if (status === 'a') pct = 100;
        else if (status !== 'd') pct = 0;
    }

    let tone;                                  // drives the colour band
    if (pct == null) tone = 'doubt';
    else if (pct >= 100) tone = 'full';
    else if (pct >= 75) tone = 'likely';
    else if (pct >= 50) tone = 'doubt';
    else if (pct >= 25) tone = 'unlikely';
    else tone = 'out';

    const STATUS_WORD = { i: 'Injured', s: 'Suspended', u: 'Unavailable',
                          n: 'Not in squad', d: 'Doubtful' };
    const label = pct != null ? `${pct}%` : (STATUS_WORD[status] || 'Doubtful');
    // The news line is the reason behind the number - kept as the tooltip.
    const title = (p.news || STATUS_WORD[status] || 'No injury news').replace(/"/g, '&quot;');
    return { tone, label, title };
}

function availabilityBandHtml(p) {
    const band = availabilityBand(p);
    if (!band) return '';
    return `<div class="avail-band avail-${band.tone}" title="${band.title}">${band.label}</div>`;
}

// Small clear-button helper for search inputs.
function wireClear(input, btn, cb) {
    const upd = () => { btn.style.display = input.value ? '' : 'none'; };
    input.addEventListener('input', upd);
    btn.addEventListener('click', () => { input.value = ''; upd(); if (cb) cb(); });
    upd();
}

// =====================================================================
//  MY TEAM
// =====================================================================
const FPL_ID_KEY = 'fpl_team_id';
// Saves carry no write token. A per-id secret in localStorage would load the
// squad anywhere but let it be EDITED only on the browser that first saved it,
// which defeats keeping drafts on the server. See the drafts.py docstring.
const idPrompt = document.getElementById('idPrompt');
const idInput = document.getElementById('idInput');
const idSave = document.getElementById('idSave');
const idError = document.getElementById('idError');
const teamContent = document.getElementById('teamContent');

let teamView = null;
let workingSquad = null;
let captainId = null, viceId = null;
let selectedEvent = null;
let subSource = null;   // id of the player currently being substituted
let subEligible = new Set();
let pendingOuts = [];   // players marked for transfer out (multi-select, all live at once —
                         // e.g. a marked-out DEF and MID both show DEF/MID candidates together)
let pendingIn = null;   // player being transferred in (choose who to drop)
let tinEligible = new Set();
let transfersUsed = 0;   // real (non-empty-slot) transfers made this preview session

// ---- Live gameweek scoring -------------------------------------------------
// A gameweek that has kicked off is a RESULT, not a plan: the team is locked in
// the real game, so the pitch shows what each player actually scored and the
// editing controls step aside. The upcoming gameweek is the editable one.
let liveScores = null;      // { [element_id]: points } for the viewed gameweek
let liveMeta = null;        // { provisional, in_progress }
// Team ids with a match under way or finished. null means the server
// couldn't tell us, in which case every player is treated as having
// played \u2014 the old behaviour, and the safe one: it shows a real score
// where the alternative would blank the whole pitch on a fixtures outage.
let liveStarted = null;
// { [team_id]: [{opponent, was_home, difficulty, started, finished}] } for the
// gameweek in play. The projection horizon starts at the round whose deadline
// hasn't passed, so a round under way is in nobody's next_gameweeks and this is
// the only place the pitch can learn who anyone is playing. See fixtureForEvent.
let liveFixtures = null;
let livePollTimer = null;
const LIVE_POLL_MS = 60000; // matches finish in minutes, not seconds

function gameweekIsLocked() {
    // Locked = the deadline has gone. currentEvent is the live one; anything at
    // or before it can no longer be changed.
    return !!(teamView && !teamView.built && selectedEvent && teamView.current_event
              && selectedEvent <= teamView.current_event);
}

// The AI tabs' answer to gameweekIsLocked(). Same question - is this a plan
// or a result - asked where there is no manager squad to lock.
//
// Decided from the season clock rather than from `stored`, because the deadline
// watcher commits both AI squads up to 100 minutes BEFORE a deadline (see
// imminent_deadlines): for that window the next gameweek is stored and still
// upcoming, and reading `stored` alone would flip the whole page into
// result-mode while the round had not kicked off. `stored` is the fallback for
// when /api/ai/status never answered and there are no bounds to compare to.
function aiGwIsUpcoming(gameweek, bounds, d) {
    if (bounds && bounds.max != null) return gameweek >= bounds.max;
    return !!(d && d.stored === false);
}

function stopLivePolling() {
    if (livePollTimer) { clearInterval(livePollTimer); livePollTimer = null; }
}

function loadLiveScores(gameweek, opts) {
    opts = opts || {};
    if (!gameweek) return Promise.resolve(null);
    return fetch(`/api/live/${gameweek}`, { cache: 'no-store' })
        .then(r => r.json())
        .then(d => {
            if (selectedEvent !== gameweek) return null;   // user moved on mid-flight
            if (!d.available) {
                liveScores = null; liveMeta = null; liveStarted = null; liveFixtures = null;
                return null;
            }
            liveScores = d.points || {};
            liveMeta = { provisional: d.provisional, in_progress: d.in_progress };
            liveStarted = Array.isArray(d.started_teams) ? new Set(d.started_teams) : null;
            liveFixtures = d.fixtures || null;
            renderPitch();
            renderLiveBanner();
            // Only poll while matches are actually being played.
            if (d.in_progress && !livePollTimer) {
                livePollTimer = setInterval(() => loadLiveScores(selectedEvent, { quiet: true }), LIVE_POLL_MS);
            }
            if (!d.in_progress) stopLivePolling();
            return d;
        })
        .catch(() => null);
}

// The one line saying what you are looking at. Two states, and they cannot
// both be true: a locked gameweek is a RESULT and gets the live score; an
// editable one carried forward from last week is a PLAN and has to say so.
// Unlabelled, a carried-forward squad reads as a confirmed team, which is
// the one misreading this view can cause.
function renderLiveBanner() {
    const banner = document.getElementById('liveBanner');
    if (!banner) return;
    if (!liveScores || !gameweekIsLocked()) {
        // Your own saved team takes precedence over the carry-forward note:
        // once you have saved, this is no longer last week's squad, it is
        // yours, and the one thing worth saying about it is that it may now
        // differ from what the official app holds.
        if (teamView && teamView.from_draft && !gameweekIsLocked()) {
            const when = teamView.draft_saved_at
                ? new Date(teamView.draft_saved_at) : null;
            const stamp = when && !isNaN(when)
                ? ' on ' + when.toLocaleString([], { day: 'numeric', month: 'short',
                                                     hour: '2-digit', minute: '2-digit' })
                : '';
            banner.innerHTML =
                `<strong>GW${selectedEvent}</strong> — showing the team you saved here${stamp}. `
                + `It stays put while you look around, and it is a preview only — apply the `
                + `changes in the official app. `
                + `<button type="button" class="btn btn-sm btn-outline-primary ms-1" `
                + `id="discardDraftBtn">Use my actual FPL team</button>`;
            banner.classList.remove('d-none');
            const discard = document.getElementById('discardDraftBtn');
            if (discard) discard.addEventListener('click', discardSavedTeam);
            return;
        }
        const from = teamView && teamView.carried_from;
        if (from && !gameweekIsLocked()) {
            banner.innerHTML =
                `<strong>GW${selectedEvent}</strong> \u2014 starting from your GW${from} squad. `
                + `FPL doesn\u2019t publish a gameweek\u2019s picks until its deadline, so this is `
                + `what you own going into it. Edit freely \u2014 then apply the changes in the `
                + `official app.`;
            banner.classList.remove('d-none');
            return;
        }
        banner.classList.add('d-none');
        return;
    }
    // Who is scoring this week. Ordinarily the starting eleven; under a Bench
    // Boost all fifteen, which is the entire point of the chip and which this
    // used to ignore - so the one week the bench counted was the one week the
    // banner left it out, and the headline read several players short of the
    // score in the official app.
    const chip = (teamView && teamView.gw && teamView.gw.active_chip) || null;
    const counting = (workingSquad || []).filter(p => p.starting || chip === 'bboost');
    const capMult = captainMultiplier();
    // Only players whose match has begun, so the banner and the pitch add up
    // to the same thing \u2014 a starter kicking off tomorrow contributes his
    // eventual score to neither.
    const started = counting.filter(hasKickedOff);
    const total = started
        .reduce((sum, p) => sum + (liveScores[p.id] || 0) * (p.id === captainId ? capMult : 1), 0);
    const waiting = counting.length - started.length;
    const label = chip === 'bboost' ? 'Your fifteen have' : 'Starting XI has';
    banner.innerHTML =
        `<strong>GW${selectedEvent}</strong> &mdash; your team is locked. `
        + `${label} scored <strong>${total}</strong> pts`
        + (waiting > 0 ? ` from ${started.length} of ${counting.length} \u2014 ${waiting} still to play.` : '')
        + (liveMeta && liveMeta.provisional
            ? ' <span class="live-prov">(provisional &mdash; bonus points aren\u2019t final yet)</span>'
            : '')
        + (liveMeta && liveMeta.in_progress ? ' <span class="live-dot"></span>updating' : '');
    banner.classList.remove('d-none');
}

// Throw the saved preview away and go back to whatever FPL holds. The only way
// back: with a draft stored, every load of this gameweek shows it, which is the
// entire point - so there has to be one deliberate action that says "forget it".
// Confirmed, because it is the one control here that destroys work.
function discardSavedTeam() {
    const id = getSavedId();
    if (!id) return;
    if (!confirm('Discard the team you saved and go back to your actual FPL squad?')) return;
    fetch(`/api/draft/${id}`, { method: 'DELETE' })
        .then(() => { savedDraft = null; loadTeam(); })
        .catch(() => alert('Couldn’t discard the saved team. Try again in a moment.'));
}

function getSavedId() { return localStorage.getItem(FPL_ID_KEY); }
function showResetBtn() { document.getElementById('resetBtn').classList.remove('d-none'); }

// "Change ID" lives in the navbar now, so it's visible from any tab —
// but it only makes sense once an ID has been entered, and it would be
// pointing at the form you're already looking at while the prompt is up.
const changeIdBtn = document.getElementById('changeId');
function showChangeId(on) { changeIdBtn.classList.toggle('d-none', !on); }

// The server-rendered explainer above the ID form. It exists so this pane is
// readable to someone (or something) with no FPL ID, which before was every
// crawler and every first-time visitor - so it belongs with the prompt, and
// goes away with it once a real squad is on screen.
const toolIntro = document.getElementById('toolIntro');
function showToolIntro(on) { if (toolIntro) toolIntro.classList.toggle('d-none', !on); }

function showPrompt() {
    idPrompt.classList.remove('d-none');
    teamContent.classList.add('d-none');
    showToolIntro(true);
    showChangeId(false);
}

idSave.addEventListener('click', () => {
    const val = idInput.value.trim();
    if (!/^\d+$/.test(val)) { idError.textContent = 'Enter a numeric FPL ID.'; return; }
    idError.textContent = '';
    localStorage.setItem(FPL_ID_KEY, val);
    idPrompt.classList.add('d-none');
    showToolIntro(false);
    savedDraft = null;      // different manager, different saved team
    selectedEvent = null;
    loadTeam();
});
idInput.addEventListener('keydown', e => { if (e.key === 'Enter') idSave.click(); });

document.getElementById('changeId').addEventListener('click', () => {
    idInput.value = getSavedId() || '';
    showPrompt();
});

function loadTeam() {
    const id = getSavedId();
    if (!id) { showPrompt(); return; }
    const evParam = selectedEvent ? `&event=${selectedEvent}` : '';
    fetch(`/api/team?team_id=${id}${evParam}`)
        .then(res => res.json())
        .then(renderTeam)
        .catch(() => { idError.textContent = 'Failed to load team.'; showPrompt(); });
}

// Editing controls only make sense for the gameweek you can still change.
function applyLockedState() {
    const locked = gameweekIsLocked();
    ['saveTeamBtn', 'optimiseBtn', 'resetBtn', 'refreshTransfersBtn'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.classList.toggle('d-none', locked);
    });
    const recCol = document.getElementById('recCol');
    if (recCol) recCol.classList.toggle('d-none', locked);
    const searchCol = document.getElementById('searchCol');
    if (searchCol) searchCol.classList.toggle('d-none', locked);
    document.getElementById('teamBody').classList.toggle('gw-locked', locked);

    if (locked) {
        loadLiveScores(selectedEvent);
    } else {
        liveScores = null; liveMeta = null; liveStarted = null;
        stopLivePolling();
        renderLiveBanner();
    }
}

function renderTeam(view) {
    teamView = view;
    liveScores = null; liveMeta = null; liveStarted = null; stopLivePolling();
    teamContent.classList.remove('d-none');
    idPrompt.classList.add('d-none');
    showChangeId(true);
    exitSubMode();
    closeModal();
    pendingOuts = [];
    pendingIn = null;
    tinEligible = new Set();
    transfersUsed = 0;
    document.getElementById('transferBanner').classList.add('d-none');

    const h = view.header || {};
    document.getElementById('teamName').textContent = h.name || 'Your team';
    document.getElementById('managerName').textContent = h.manager || '';

    // Gameweek nav is always visible once a team is being viewed \u2014 it's
    // just disabled with a neutral label when there's no live gameweek
    // to navigate (preseason / still-being-built squad).
    const gwNav = document.getElementById('gwNav');
    gwNav.classList.remove('d-none');
    if (view.built) {
        // Name the gameweek being picked for. "Preseason" is a state, not a
        // destination - it doesn't say which gameweek your squad is actually for.
        document.getElementById('gwLabel').textContent = `GW${view.next_event || 1}`;
        document.getElementById('gwPrev').disabled = true;
        document.getElementById('gwNext').disabled = true;
    } else {
        selectedEvent = view.gw ? view.gw.event
            : (selectedEvent || view.next_event || view.current_event);
        document.getElementById('gwLabel').textContent = selectedEvent ? `GW${selectedEvent}` : 'GW\u2013';
        // Forward stops at the gameweek being PICKED for, not the one in
        // play. Capping at current_event put the arrow one short of the
        // only round still editable \u2014 the tab could reach every
        // gameweek except the one you came here to change.
        const maxE = view.max_event || view.next_event || view.current_event;
        const minE = view.min_event || 1;
        document.getElementById('gwPrev').disabled = !selectedEvent || selectedEvent <= minE;
        document.getElementById('gwNext').disabled = !selectedEvent || !maxE || selectedEvent >= maxE;
    }

    const unavailable = document.getElementById('teamUnavailable');
    const teamBody = document.getElementById('teamBody');

    renderLeagues(view.leagues || {});

    if (!view.available) {
        renderStatChips(null);
        if (!view.header) {
            // Genuine error (bad ID, fetch failure) \u2014 nothing to build on.
            unavailable.innerHTML = `<div class="mb-2">${view.detail || 'Team not available.'}</div>`;
            unavailable.classList.remove('d-none');
            teamBody.classList.add('d-none');
            return;
        }
        // Valid manager, but no live gameweek data yet (preseason).
        // Load the squad saved against this FPL ID on the server \u2014 the
        // same team from any device \u2014 or show empty slots to pick into.
        unavailable.classList.add('d-none');
        ensurePlayers()
            .then(loadDraft)
            .then(draft => {
                const squad = (draft && draft.squad && draft.squad.length === 15)
                    ? draft.squad : emptySquad();
                showBuiltTeam(squad, view.leagues, view.header);
            });
        return;
    }
    unavailable.classList.add('d-none');
    teamBody.classList.remove('d-none');

    renderStatChips(view.gw);
    teamView._gw0 = view.gw ? { ...view.gw } : null;   // snapshot for reset

    workingSquad = view.squad.map(p => ({ ...p }));
    const cap = view.squad.find(p => p.is_captain);
    const vice = view.squad.find(p => p.is_vice_captain);
    captainId = cap ? cap.id : null;
    viceId = vice ? vice.id : null;
    document.getElementById('resetBtn').classList.add('d-none');

    teamView.transfer_recs = (view.transfer_recs || []).slice();
    teamView._recs0 = (view.transfer_recs || []).slice();
    renderPitch();
    renderTransfers(teamView.transfer_recs);
    updatePredicted();
    renderChips(view.gw);
    updateTransferBanner();
    applyLockedState();

    ensurePlayers().then(() => playerSearch.refresh());
}

function renderStatChips(gw) {
    const el = document.getElementById('statChips');
    const h = teamView.header || {};
    const rating = ratingChip(teamRating(workingSquad));
    if (teamView.built && gw) {
        el.innerHTML = chip('Squad value', '\u00a3' + h.value + 'm')
                     + bankChip(gw.bank)
                     + freeTransfersChip(gw)
                     + chip('Predicted', gw.predicted_points, true)
                     + rating;
        return;
    }
    if (!gw) {
        el.innerHTML = chip('Total pts', h.total_points ?? '\u2013')
                     + chip('Squad value', h.value != null ? '\u00a3' + h.value + 'm' : '\u2013')
                     + (h.bank != null ? bankChip(h.bank) : chip('In the bank', '\u2013'));
        return;
    }
    // A gameweek already played is a RESULT, and a result is a short story:
    // what you scored, what we thought you would score, and whether you spent
    // a chip on it. Bank, free transfers, squad value and team rating all
    // describe decisions that are still open, and none of them is still open
    // once the deadline has gone - printing them against a locked round
    // invites reading them as things you could act on.
    if (gameweekIsLocked()) {
        el.innerHTML =
              chip('GW points', gw.points ?? '\u2013')
            + chip('Predicted', gw.predicted_points ?? '\u2013', true)
            + chipPlayedChip(gw.active_chip);
        return;
    }
    // The gameweek being picked is a PLAN, and a plan is about what you have
    // left to work with. Same containers in the same order as the AI Manager's
    // preview, so the two pages can be read against each other line by line.
    el.innerHTML =
          chip('Total points', gw.total_points ?? h.total_points ?? '\u2013')
        + rating
        + chip('Predicted', gw.predicted_points ?? '\u2013', true)
        + chip('Squad value', (gw.value ?? h.value) != null
                              ? '\u00a3' + Number(gw.value ?? h.value).toFixed(1) + 'm' : '\u2013')
        + bankChip(gw.bank)
        + freeTransfersChip(gw)
        + costChip(transferCost(gw));
}
// The chip spent on a round already played, or nothing at all when none was.
// FPL names these in its own codes - '3xc', 'bboost' - which are fine as keys
// and unreadable as a value on a card, so they go through the same CHIP_NAMES
// map the AI Manager's chip cards use.
function chipPlayedChip(activeChip) {
    if (!activeChip) return '';
    return chip('Chip played', CHIP_NAMES[activeChip] || activeChip);
}
// Free transfers remaining this preview session, next to the bank chip.
// Once transfersUsed exceeds the free allowance, each extra manual
// transfer is a -4 point hit (reflected here and in computePredicted).
// Preseason / a locally-built draft has no real gameweek deadline yet,
// so \u2014 same as the real FPL app before the season starts \u2014 transfers
// there are unlimited and never incur a hit.
function transferHitPoints() {
    if (teamView && teamView.built) return 0;
    const free = (teamView && teamView.gw && typeof teamView.gw.free_transfers_est === 'number')
        ? teamView.gw.free_transfers_est : 1;
    return Math.max(0, transfersUsed - free) * 4;
}
// How many free transfers are left right now, after whatever this session has
// already spent. Infinity in a preseason/draft team, where transfers are
// unlimited and nothing can be a hit.
//
// Extracted because two things need the same answer: the chip below and the -4
// accounting in transferHitPoints. It once fed a free/hit tag on each
// recommended transfer as well; see renderTransfers for why that tag is gone.
function freeTransfersLeft() {
    if (teamView && teamView.built) return Infinity;
    const free = (teamView && teamView.gw && typeof teamView.gw.free_transfers_est === 'number')
        ? teamView.gw.free_transfers_est : 1;
    return Math.max(0, free - transfersUsed);
}

function freeTransfersChip(gw) {
    if (teamView && teamView.built) {
        return `<div class="stat-chip"><span class="stat-label">Free transfers</span><span class="stat-value">Unlimited</span></div>`;
    }
    // Just the count. This chip used to carry the hit as well - "0 (-4 hit)" -
    // because it was the only place a hit could be shown. Cost is that place
    // now, and the same -4 stated in two containers side by side reads as two
    // separate deductions.
    const remaining = freeTransfersLeft();
    const spent = remaining === 0;
    // Flagged as derived, because it is the one figure on this row that FPL
    // does not publish. Every other number here is read straight out of the
    // API; this one is replayed from the transfers and chips in your history,
    // by the same rule the game uses. It has matched in every case tested, but
    // "we worked it out" and "they told us" are different claims and the chip
    // should not make them look alike.
    return `<div class="stat-chip${spent ? ' stat-neg' : ''}" title="${FT_TIP}">`
         + `<span class="stat-label">Free transfers</span>`
         + `<span class="stat-value">${remaining}</span></div>`;
}
// What this gameweek has cost in points, which is a different question from
// how many transfers were made \u2014 and the only one of the two that moves
// your score. Two free transfers and no transfers at all both cost nothing,
// so a count of them told you nothing; a hit is what there is to know.
//
// Zero is stated rather than hidden. "0 pts" is the reassurance someone
// mid-transfer is looking for, and a chip that disappears when the news is
// good is a chip whose absence you have to remember the meaning of.
function transferCost(gw) {
    // A locked gameweek has FPL's own figure and it cannot change. An
    // editable one is whatever this preview session has run up so far.
    if (gameweekIsLocked()) return (gw && gw.transfers_cost) || 0;
    return transferHitPoints();
}
function costChip(cost) {
    const pts = Number(cost) || 0;
    const val = pts ? `\u2212${pts} pts` : '0 pts';
    return `<div class="stat-chip${pts ? ' stat-neg' : ''}"><span class="stat-label">Cost</span><span class="stat-value">${val}</span></div>`;
}
function bankChip(bank) {
    const neg = bank < 0;
    const val = (neg ? '-\u00a3' + Math.abs(bank).toFixed(1) : '\u00a3' + Number(bank).toFixed(1)) + 'm';
    const style = neg ? ' style="color:#e03131"' : '';
    return `<div class="stat-chip${neg ? ' stat-neg' : ''}"><span class="stat-label">In the bank</span><span class="stat-value"${style}>${val}</span></div>`;
}
// `tip` becomes a title attribute rather than one of the .info-icon tooltips.
// Those are wired once at load by a querySelectorAll over the document, and
// attachTip() appends a div to document.body per call - chips are re-rendered
// on every lineup change, so they'd either never be wired or leak a tip div per
// render. A title needs neither.
function chip(label, value, accent, tip) {
    const t = tip ? ` title="${tip}"` : '';
    return `<div class="stat-chip${accent ? ' accent' : ''}"${t}><span class="stat-label">${label}</span><span class="stat-value">${value}</span></div>`;
}
// A squad's rating out of 100: the mean of its starting XI's ratings. Same
// rule as squad_optimiser.team_rating() on the server, which is where the
// reasoning for "starters only" is written down.
//
// Computed here rather than read from /api/team because My Team is editable:
// optimise the lineup or preview a transfer and the eleven changes, so a
// figure fixed at load would be describing a squad that's no longer on screen.
// The AI squads aren't editable and use the server's number.
function teamRating(squad) {
    const starters = (squad || []).filter(p => p.starting);
    if (starters.length !== 11) return null;
    const ratings = starters.map(p => p.rating).filter(r => r != null);
    if (ratings.length !== 11) return null;
    return Math.round(ratings.reduce((a, b) => a + b, 0) / 11);
}
// The chip itself, or nothing at all when there's no honest number to show -
// a part-built squad, or a stored gameweek the server declined to rate.
//
// The tooltip is the short version of what /about and the FAQ say at length: a
// rating is a percentile within a position, so averaging eleven of them is a
// squad-quality figure and not a points forecast. It sits on the chip because
// that is where the misreading happens - a number out of 100 next to a
// predicted-points number invites being read as the better of the two.
// Said on the containers that carry a live score rather than in a banner:
// the number itself is what gets read and quoted, so the caveat belongs on
// it. Bonus points are not settled until FPL checks the round, so a total
// shown mid-Saturday genuinely will move.
const PROVISIONAL_TIP = 'Provisional \u2014 the gameweek is still being played '
    + 'and bonus points are not final until FPL checks the round.';
const FT_TIP = 'Estimated. The public FPL API doesn’t publish your free '
    + 'transfer count, so this is replayed from your transfer and chip history '
    + 'using the game’s own rule: one a week, banked up to five, and a '
    + 'wildcard or free hit week costs none.';
const RATING_TIP = 'Squad quality, not a points forecast. Each rating is a '
    + "player's projected points ranked within his own position, so the best "
    + 'keeper and the best forward both read 100. Predicted points is the '
    + 'figure for this gameweek.';
function ratingChip(rating) {
    return rating == null ? '' : chip('Team rating', rating + '/100', false, RATING_TIP);
}

// ---- Pitch ----
function fixtureTile(g, opts) {
    opts = opts || {};
    const color = g.difficulty != null ? colorFor(g.difficulty, 1, 5) : '#eee';
    const ha = haTag(g);
    // The number under the opponent is a PROJECTION, and a round that has
    // kicked off no longer has one - it has a result, which the card shows
    // instead the moment there is one. So a live tile keeps the opponent and
    // holds the second line open with a non-breaking space rather than
    // printing a dash, which would read as "nothing expected of him".
    const pts = opts.live ? '&nbsp;'
              : (g.points != null ? Number(g.points).toFixed(1) : '-');
    return `<span class="mini-gw" style="background:${color}" title="${opts.title || ('GW' + g.event)}">`
        // No space before the bracket: these tiles are ~21px wide while
        // "ARS (H)" needs ~24px, so the separator is the one character
        // that can go without shrinking the label below legibility.
        + `<b>${g.opponent || ''}${ha}</b>${pts}</span>`;
}

function miniFixtures(p) {
    return (p.next_gameweeks || []).slice(0, 3).map(fixtureTile).join('');
}

// The single fixture a player is about to play, for a gameweek already under
// way. Three tiles is planning information - the two after this one are rounds
// you can still do something about, and on a locked gameweek you cannot, so
// they are two thirds of the card spent saying nothing you can act on. The one
// that matters is the match he is waiting to play.
//
// A blank gameweek gets a neutral tile rather than an empty string: an element
// with no content has no line box, so the card would lose ~18px of height and
// sit out of line with the ten around it.
// A gameweek under way is never in `next_gameweeks`. Every projection on the
// site runs from the round whose deadline has NOT passed, so the moment this
// one kicked off it left that list - and this function, which only ever looked
// there, had nothing to find for the one round the pitch was showing. Every
// player still waiting to play got the no-fixture tile: a dash, on a Saturday
// morning, for eleven players who all had a match that afternoon.
//
// So the round in play is read from `liveFixtures`, which /api/live serves
// alongside the scores and which the pitch is already polling. `next_gameweeks`
// remains the source for every other gameweek, where it is the only one.
function fixtureForEvent(p, event) {
    const planned = (p.next_gameweeks || []).find(x => x.event === event);
    if (planned) return fixtureTile(planned);
    // A double gameweek gives a club two of these. The one still to be played
    // is the one worth naming; if both are done the player has a score and this
    // branch was never reached.
    const live = liveFixtures && p.team != null
        ? (liveFixtures[p.team] || []).find(f => !f.finished) : null;
    if (live) return fixtureTile(live, { live: true, title: `GW${event}` });
    // A blank gameweek gets the neutral tile it always did - by here it really
    // is a player with no match, not one we simply couldn't look up.
    return `<span class="mini-gw" style="background:#eee" title="No fixture this gameweek">`
        + `<b>\u2013</b>&nbsp;</span>`;
}

// Has this player's gameweek begun? A player whose match hasn't kicked off
// has no score yet \u2014 and "0" is not the same statement as "hasn't
// played", though on a pitch they look identical. Owning six players who
// have scored and five who kick off tomorrow read as a disaster.
//
// A double gameweek counts as begun once either fixture has: a real score
// from the first match is worth more than a fixture tile for the second.
function hasKickedOff(p) {
    return !liveStarted || p.team == null || liveStarted.has(p.team);
}
// What the armband is worth this week. Two, unless a Triple Captain is on -
// in which case the card and the banner both have to say three, or they
// disagree with each other and with the official app.
function captainMultiplier() {
    return (teamView && teamView.gw && teamView.gw.active_chip === '3xc') ? 3 : 2;
}
function playerCard(p, opts) {
    opts = opts || {};
    const isEmpty = p.id < 0;
    const posLabel = opts.bench ? `<div class="bench-pos">${p.pos}</div>` : '';
    let cls = 'player';
    if (opts.subActive) {
        if (opts.source) cls += ' sub-source';
        else cls += opts.eligible ? ' sub-eligible' : ' sub-ineligible';
    }
    if (isEmpty) {
        // Empty squad slot — tap it to search for a player to fill it.
        // All pending-out slots are equally "live" at once now (a
        // multitransfer isn't limited to one at a time), so every one
        // marked gets the highlighted look, not just the last-marked.
        cls += ' empty-slot' + (opts.pendingOut ? ' empty-active' : '');
        return `<div class="${cls}" data-id="${p.id}" style="position:relative">
            ${posLabel}
            <div class="empty-slot-icon">+</div>
            <div class="player-name-pill">Add ${p.pos}</div>
        </div>`;
    }
    const bstyle = 'position:absolute;top:3px;right:3px;left:auto;bottom:auto;z-index:4';
    let badge = '';
    if (p.id === captainId) badge = `<span class="cap-badge" style="${bstyle}">C</span>`;
    else if (p.id === viceId) badge = `<span class="cap-badge vice" style="${bstyle}">V</span>`;
    // Replaces the old corner dot: a full-width band above the name, so a
    // flagged starter is obvious rather than something you have to hover to find.
    const availBand = availabilityBandHtml(p);
    if (opts.pendingOut) cls += ' pending-out pending-active';
    const plus = opts.pendingOut ? '<div class="out-plus">+</div>' : '';
    // Three states, and which one applies is a question about the gameweek
    // rather than about the player.
    //
    //   locked + played      -> what he scored. The projection is history.
    //   locked + not yet     -> the one fixture he is about to play.
    //   not locked (planning) -> the next three, which is what you pick on.
    //
    // The captain's number is the doubled one, printed plainly. It used to
    // read "8 \u00d72", which is the arithmetic rather than the answer, and
    // left the reader to decide whether 8 or 16 was the figure that counted
    // towards the total in the banner above. The C badge already says who
    // the captain is.
    const locked = gameweekIsLocked();
    const showLive = liveScores && liveScores[p.id] != null && hasKickedOff(p);
    const gws = showLive
        ? `<span class="live-pts${p.id === captainId ? ' live-cap' : ''}">`
          + `${liveScores[p.id] * (p.id === captainId ? captainMultiplier() : 1)}</span>`
        : (locked ? fixtureForEvent(p, selectedEvent) : miniFixtures(p));
    const live = `<div class="player-gws">${gws}</div>`;
    return `<div class="${cls}" data-id="${p.id}" style="position:relative">
        ${posLabel}${badge}
        <div class="player-kit">${shirtImg(p.team_code, p.pos, 'kit')}${plus}</div>
        ${availBand}
        <div class="player-name-pill">${p.web_name}</div>
        ${live}
    </div>`;
}

function renderPitch() {
    const pitch = document.getElementById('pitch');
    const benchEl = document.getElementById('bench');
    const subActive = subSource != null;
    const tinActive = pendingIn != null;
    const starters = workingSquad.filter(p => p.starting);
    const bench = workingSquad.filter(p => !p.starting).sort((a, b) => a.position - b.position);
    const cardOpts = (p, onBench) => ({
        bench: onBench,
        subActive: subActive || tinActive,
        source: p.id === subSource,
        eligible: subActive ? subEligible.has(p.id) : (tinActive ? tinEligible.has(p.id) : false),
        pendingOut: pendingOuts.some(o => o.id === p.id)
    });

    pitch.innerHTML = ['GK', 'DEF', 'MID', 'FWD'].map(pos => {
        const line = starters.filter(p => p.pos === pos).sort((a, b) => a.position - b.position);
        if (!line.length) return '';
        return `<div class="pitch-row">${line.map(p => playerCard(p, cardOpts(p, false))).join('')}</div>`;
    }).join('');

    benchEl.innerHTML = `<div class="bench-label">Bench</div>
        <div class="bench-row">${bench.map(p => playerCard(p, cardOpts(p, true))).join('')}</div>`;

    [pitch, benchEl].forEach(container =>
        container.querySelectorAll('.player[data-id]').forEach(el =>
            el.addEventListener('click', () => onPlayerClick(+el.dataset.id))));
}

// ---- Player click / substitution ----
function onPlayerClick(id) {
    // A locked gameweek is a result. Editing it here would imply a change you
    // can't actually make in the real game.
    if (gameweekIsLocked()) {
        const p = workingSquad.find(x => x.id === id);
        // Read-only, not merely unowned. `false` here meant "a player from the
        // search table", which is the branch that offers "Transfer in" - so
        // tapping one of your own locked players offered to buy him, into a
        // gameweek whose deadline has already gone.
        if (p) openPlayerModal(p, false, { readOnly: true });
        return;
    }
    if (subSource != null) {
        if (id === subSource) { exitSubMode(); return; }   // tap the same player again to call the sub off
        if (!subEligible.has(id)) return;                  // only legal targets
        const src = subSource; exitSubMode(); attemptSub(src, id); return;
    }
    if (pendingIn != null) {
        if (!tinEligible.has(id)) return;
        const inp = pendingIn; exitTransferInMode(); performTransfer(id, inp); return;
    }
    if (pendingOuts.some(o => o.id === id)) { removePendingOut(id); return; }   // tap greyed → drop it from the multitransfer
    const p = workingSquad.find(x => x.id === id);
    if (p && p.id < 0) { markTransferOut(p); return; }   // empty slot — jump straight into "fill this" mode
    if (p) openPlayerModal(p, true);
}

function isLegalXI(squad) {
    const s = squad.filter(p => p.starting);
    if (s.length !== 11) return false;
    const c = { GK: 0, DEF: 0, MID: 0, FWD: 0 };
    s.forEach(p => c[p.pos]++);
    return c.GK === 1 && c.DEF >= 3 && c.DEF <= 5 && c.MID >= 2 && c.MID <= 5 && c.FWD >= 1 && c.FWD <= 3;
}
function normalizePositions(squad) {
    const order = { GK: 0, DEF: 1, MID: 2, FWD: 3 };
    squad.filter(p => p.starting)
         .sort((a, b) => (order[a.pos] - order[b.pos]) || (b.predicted - a.predicted))
         .forEach((p, i) => p.position = i + 1);
    const bench = squad.filter(p => !p.starting);
    const gk = bench.filter(p => p.pos === 'GK');
    const rest = bench.filter(p => p.pos !== 'GK').sort((a, b) => a.position - b.position);
    [...gk, ...rest].forEach((p, i) => p.position = 12 + i);
}
function attemptSub(aId, bId) {
    const a = workingSquad.find(p => p.id === aId), b = workingSquad.find(p => p.id === bId);
    if (!a || !b || a === b) return;
    if (a.starting === b.starting) {
        const t = a.position; a.position = b.position; b.position = t;
    } else {
        a.starting = !a.starting; b.starting = !b.starting;
        if (!isLegalXI(workingSquad)) {
            a.starting = !a.starting; b.starting = !b.starting;
            alert('That swap would break the formation (need exactly 1 GK, and at least 3 DEF, 2 MID, 1 FWD).');
            return;
        }
    }
    normalizePositions(workingSquad);
    showResetBtn();
    renderPitch();
    updatePredicted();
}
// Which players can legally swap with `srcId`. Starter <-> bench swaps
// must keep the resulting XI legal. Bench <-> bench is always legal
// (it's just a reorder of who's next in line) so those are always offered.
function computeSubEligible(srcId) {
    const src = workingSquad.find(p => p.id === srcId);
    const set = new Set();
    if (!src) return set;
    workingSquad.forEach(t => {
        if (t.id === srcId) return;
        if (t.starting === src.starting) {
            if (!src.starting) set.add(t.id);   // bench <-> bench reorder
            return;
        }
        src.starting = !src.starting; t.starting = !t.starting;
        if (isLegalXI(workingSquad)) set.add(t.id);
        src.starting = !src.starting; t.starting = !t.starting;
    });
    return set;
}
function startSub(id) {
    closeModal();
    subEligible = computeSubEligible(id);
    if (!subEligible.size) { alert('No legal swaps available for that player.'); return; }
    subSource = id;
    const p = workingSquad.find(x => x.id === id);
    const banner = document.getElementById('subBanner');
    banner.innerHTML = `Swapping <strong>${p ? p.web_name : ''}</strong> &mdash; tap a highlighted player to swap, `
        + `or tap <strong>${p ? p.web_name : 'them'}</strong> again to cancel. `
        + `<button id="subCancel" class="btn btn-link btn-sm p-0 align-baseline">cancel</button>`;
    banner.classList.remove('d-none');
    document.getElementById('subCancel').onclick = exitSubMode;
    document.getElementById('teamBody').classList.add('sub-mode');
    renderPitch();
}
function exitSubMode() {
    subSource = null;
    subEligible = new Set();
    document.getElementById('subBanner').classList.add('d-none');
    document.getElementById('teamBody').classList.remove('sub-mode');
    if (workingSquad) renderPitch();
}

// ---- Player modal ----
function closeModal() { document.getElementById('playerModal').classList.add('d-none'); }
document.getElementById('pmClose').addEventListener('click', closeModal);
document.getElementById('pmBackdrop').addEventListener('click', closeModal);

// `opts.readOnly` opens the pop-up with no action buttons at all, for a player
// on somebody else's pitch - the AI squads. Without it a card on the AI Manager
// tab offered "Transfer in", which would have quietly edited YOUR team from a
// page about the bot's.
function openPlayerModal(p, owned, opts) {
    opts = opts || {};
    document.getElementById('pmKit').innerHTML = shirtImg(p.team_code, p.pos, 'shirt');
    document.getElementById('pmName').textContent = p.web_name;
    document.getElementById('pmSub').textContent =
        `${p.pos}${p.team_name ? ' \u00b7 ' + p.team_name : ''}`
        + `${p.cost != null ? ' \u00b7 \u00a3' + p.cost.toFixed(1) + 'm' : ''}`
        + `${p.rating != null ? ' \u00b7 rating ' + Math.round(p.rating) : ''}`;

    const actions = document.getElementById('pmActions');
    if (opts.readOnly) {
        actions.innerHTML = '';
    } else if (owned) {
        const capBtns = p.starting
            ? `<button class="btn btn-sm btn-primary pm-btn" id="pmCap">Captain</button>
               <button class="btn btn-sm btn-outline-primary pm-btn" id="pmVice">Vice</button>` : '';
        actions.innerHTML = capBtns +
            `<button class="btn btn-sm btn-outline-primary pm-btn" id="pmSubBtn">Substitute</button>
             <button class="btn btn-sm btn-outline-primary pm-btn" id="pmTransferBtn">Transfer</button>`;
        if (p.starting) {
            document.getElementById('pmCap').onclick = () => {
                const oldCap = captainId;
                if (viceId === p.id) viceId = oldCap;   // was vice — swap, don't duplicate
                captainId = p.id;
                showResetBtn(); renderPitch(); updatePredicted(); closeModal();
            };
            document.getElementById('pmVice').onclick = () => {
                const oldVice = viceId;
                if (captainId === p.id) captainId = oldVice;   // was captain — swap, don't duplicate
                viceId = p.id;
                showResetBtn(); renderPitch(); updatePredicted(); closeModal();
            };
        }
        document.getElementById('pmSubBtn').onclick = () => startSub(p.id);
        document.getElementById('pmTransferBtn').onclick = () => markTransferOut(p);
    } else {
        // A player from the search table: offer to transfer them in.
        if (workingSquad) {
            actions.innerHTML = `<button class="btn btn-sm btn-primary pm-btn" id="pmTransferIn">Transfer in</button>`;
            document.getElementById('pmTransferIn').onclick = () => markTransferIn(p);
        } else {
            actions.innerHTML = '';
        }
    }
    const tbox = document.getElementById('pmTransfer');
    tbox.classList.add('d-none'); tbox.innerHTML = '';

    // The pop-up is a summary; the profile page is the whole thing. The path is
    // supplied by /api/all_players rather than built here, so this link always
    // matches the canonical URL instead of 301-ing on every click. It's hidden
    // where there's no path - a squad pick loaded from /api/team hasn't been
    // through that enrichment.
    const profile = document.getElementById('pmProfile');
    profile.classList.toggle('d-none', !p.path);
    if (p.path) profile.href = p.path;

    renderUpcoming(p);
    renderForm(p);
    document.getElementById('playerModal').classList.remove('d-none');
}

function renderUpcoming(p) {
    const el = document.getElementById('pmUpcoming');
    const gws = p.next_gameweeks || [];
    if (!gws.length) { el.innerHTML = '<span class="text-muted small">No upcoming fixtures.</span>'; return; }
    el.innerHTML = gws.slice(0, 3).map(g => {
        const color = g.difficulty != null ? colorFor(g.difficulty, 1, 5) : '#eee';
        const pts = g.points != null ? Number(g.points).toFixed(1) : '-';
        return `<span class="pm-fix" style="background:${color}"><b>${g.opponent || ''} ${haTag(g)}</b><span>${pts} pts</span></span>`;
    }).join('');
}

function renderForm(p) {
    const el = document.getElementById('pmForm');
    el.innerHTML = '<span class="text-muted small">Loading\u2026</span>';
    fetch(`/api/player/${p.id}`).then(r => r.json()).then(d => {
        if (!d.available) { el.innerHTML = '<span class="text-muted small">No data available.</span>'; return; }
        let html = '';
        if (d.history && d.history.length) {
            html += `<table class="table table-sm pm-form-table mb-1"><tbody>${d.history.map(hh => `
                <tr><td>GW${hh.event}</td><td>${hh.opponent || ''} ${hh.was_home ? 'H' : 'A'}</td>
                <td>${hh.minutes}'</td><td class="pm-pts">${hh.points}</td></tr>`).join('')}</tbody></table>`;
        } else {
            html += '<span class="text-muted small">No games this season yet.</span>';
        }
        el.innerHTML = html;
    }).catch(() => { el.innerHTML = '<span class="text-muted small">Couldn\u2019t load form.</span>'; });
}

// ---- Manual transfers (preview only) — mark players out, then replace ----
function updateTransferBanner() {
    const banner = document.getElementById('transferBanner');
    if (!pendingOuts.length) {
        // Mid initial pick: no player has been marked out, but there are
        // still empty slots to fill, and tapping the list fills them in
        // order — say so, since nothing else on screen explains it.
        const empties = emptySlots();
        if (!empties.length) { banner.classList.add('d-none'); return; }
        const counts = {};
        empties.forEach(s => counts[s.pos] = (counts[s.pos] || 0) + 1);
        const need = ['GK', 'DEF', 'MID', 'FWD'].filter(p => counts[p])
            .map(p => `${counts[p]} ${p}`).join(', ');
        const left = teamView && teamView.gw ? (teamView.gw.bank || 0) : 0;
        banner.innerHTML = `Still to pick: <strong>${need}</strong> `
            + `(£${left.toFixed(1)}m left) &mdash; tap a player below and they'll go straight `
            + `into the next free slot for their position.`;
        banner.classList.remove('d-none');
        return;
    }
    const bank = teamView.gw ? (teamView.gw.bank || 0) : 0;
    const totalBudget = bank + pendingOuts.reduce((s, o) => s + o.cost, 0);
    const labels = pendingOuts.map(o => o.id < 0 ? o.pos : o.web_name);
    const text = pendingOuts.length === 1
        ? `Replacing <strong>${labels[0]}</strong>`
        : `Replacing <strong>${pendingOuts.length}</strong> players (${labels.join(', ')})`;
    banner.innerHTML = `${text} `
        + `(\u00a3${totalBudget.toFixed(1)}m total to spend) `
        + `&mdash; tap a highlighted player below to fill a slot, or tap a greyed player to drop it. `
        + `<button id="transferCancel" class="btn btn-link btn-sm p-0 align-baseline">cancel all</button>`;
    banner.classList.remove('d-none');
    document.getElementById('transferCancel').onclick = cancelTransfers;
}
function markTransferOut(p) {
    closeModal();
    if (!pendingOuts.some(o => o.id === p.id)) pendingOuts.push(p);
    renderPitch();            // grey the player out straight away
    updateTransferBanner();
    ensurePlayers().then(() => playerSearch.refresh());
}
function cancelTransfers() {
    pendingOuts = [];
    document.getElementById('transferBanner').classList.add('d-none');
    playerSearch.refresh();
    renderPitch();
}
function removePendingOut(id) {
    pendingOuts = pendingOuts.filter(o => o.id !== id);
    updateTransferBanner();
    playerSearch.refresh();
    renderPitch();
}
// Empty (unfilled) squad slots for a position, in pitch order. During the
// initial pick these are what an incoming player drops into.
function emptySlots(pos) {
    return (workingSquad || []).filter(p => p.id < 0 && (!pos || p.pos === pos))
                               .sort((a, b) => a.position - b.position);
}
function hasEmptySlots() { return emptySlots().length > 0; }

function resolveTransfer(inp) {
    // Match the incoming player's position to the right slot. Several
    // different positions can be queued at once, so it isn't necessarily
    // "the most recently marked" one. Where more than one slot of the
    // position is open — a multi-transfer of two MIDs, or the initial
    // pick with five empty MID slots — the first one just gets used
    // rather than making the user nominate which of the identical slots
    // they meant.
    const out = pendingOuts.find(o => o.pos === inp.pos) || emptySlots(inp.pos)[0];
    if (!out) return;
    if (!performTransfer(out.id, inp)) return;   // blocked (e.g. club limit) — keep it queued
    pendingOuts = pendingOuts.filter(o => o.id !== out.id);
    updateTransferBanner();
    refreshTransferTags();
    playerSearch.refresh();
    renderPitch();
}

// Redraw the recommendation tags after anything that spends or restores a free
// transfer. Cheap - it re-renders three cards from a list already in memory -
// and it is what keeps "free" honest once a transfer has been made somewhere
// else on the pitch.
function refreshTransferTags() {
    if (teamView && teamView.transfer_recs && teamView.transfer_recs.length) {
        renderTransfers(teamView.transfer_recs);
    }
}

// Predicted GW points, with the captain counting double, minus any
// -4 hits from exceeding free transfers this session. Recomputed
// client-side so captaincy / lineup / transfer changes stay in sync.
function computePredicted() {
    if (!workingSquad) return null;
    const raw = workingSquad.filter(p => p.starting)
        .reduce((s, p) => s + (p.predicted || 0) * (p.id === captainId ? 2 : 1), 0);
    return +(raw - transferHitPoints()).toFixed(1);
}
function updatePredicted() {
    if (gameweekIsLocked()) { renderLiveBanner(); return; }
    if (teamView && teamView.gw) {
        teamView.gw.predicted_points = computePredicted();
        renderStatChips(teamView.gw);
    }
}

// Transfer a searched player in: let the user pick who to drop. Any
// OTHER players already marked for transfer-out (mid multi-transfer,
// not yet resolved) are left alone rather than wiped \u2014 they won't be
// left in the team either, so they're excluded from the incoming
// player's club-limit check too, and aren't offered as a second drop
// candidate for this same incoming player.
function markTransferIn(inp) {
    if (!workingSquad) return;
    if (workingSquad.some(p => p.id === inp.id)) { alert('That player is already in your squad.'); return; }
    closeModal();
    // A slot is already waiting for this position — either one the user
    // marked for transfer out, or an empty slot from the initial pick —
    // so fill it straight away. Only ask "who goes out?" when the squad
    // is full and nothing has been queued.
    if (pendingOuts.some(o => o.pos === inp.pos) || emptySlots(inp.pos).length) {
        resolveTransfer(inp);
        return;
    }
    const leaving = new Set(pendingOuts.map(o => o.id));
    tinEligible = new Set(workingSquad
        .filter(p => p.pos === inp.pos && !leaving.has(p.id)
            && workingSquad.filter(x => x.team === inp.team && x.id !== p.id && !leaving.has(x.id)).length < 3)
        .map(p => p.id));
    if (!tinEligible.size) { alert(`No eligible ${inp.pos} to swap out for ${inp.web_name}.`); return; }
    pendingIn = inp;
    const banner = document.getElementById('transferBanner');
    banner.innerHTML = `Transferring in <strong>${inp.web_name}</strong> \u2014 tap a highlighted player to swap out. `
        + `<button id="tinCancel" class="btn btn-link btn-sm p-0 align-baseline">cancel</button>`;
    banner.classList.remove('d-none');
    document.getElementById('tinCancel').onclick = exitTransferInMode;
    renderPitch();
    banner.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}
function exitTransferInMode() {
    pendingIn = null; tinEligible = new Set();
    if (pendingOuts.length) { updateTransferBanner(); }   // restore the "Replacing X" banner if one was in progress
    else { document.getElementById('transferBanner').classList.add('d-none'); }
    renderPitch();
}

function performTransfer(outId, inp) {
    const idx = workingSquad.findIndex(p => p.id === outId);
    if (idx < 0) return false;
    // Enforce max 3 players from any single club, counting only players who
    // will actually remain in the team — other slots already marked for
    // transfer out (mid multi-transfer) don't count against the incoming club.
    const leaving = new Set([...pendingOuts.map(o => o.id), outId]);
    if (workingSquad.filter(p => p.team === inp.team && !leaving.has(p.id)).length >= 3) {
        alert(`You can only have 3 players from one club.`);
        return false;
    }
    const old = workingSquad[idx];
    if (old.id > 0) transfersUsed++;   // replacing an empty slot (initial pick) isn't a "transfer"
    if (teamView.gw) teamView.gw.bank = +((teamView.gw.bank || 0) - (inp.cost - old.cost)).toFixed(1);
    workingSquad[idx] = { ...inp, position: old.position, starting: old.starting,
        is_captain: old.is_captain, is_vice_captain: old.is_vice_captain,
        multiplier: old.multiplier, status: 'a', news: '' };
    if (teamView.header) teamView.header.value = +workingSquad.reduce((s, p) => s + (p.cost || 0), 0).toFixed(1);
    if (captainId === old.id) captainId = inp.id;
    if (viceId === old.id) viceId = inp.id;
    if (teamView.transfer_recs) {
        teamView.transfer_recs = teamView.transfer_recs.filter(
            r => workingSquad.some(p => p.id === r.out.id));
        renderTransfers(teamView.transfer_recs);
    }
    showResetBtn();
    closeModal();
    updatePredicted();
    renderPitch();
    updateTransferBanner();
    playerSearch.refresh();
    return true;
}

// ---- Optimise / reset ----
// One button: the best XI and the armband are the same decision. Picking the
// lineup and then separately picking a captain from it is two clicks for one
// outcome, and leaves you with a captain chosen from the OLD eleven if you
// only press one of them.
//
// This recomputes from the CURRENT workingSquad rather than reading the
// `optimised` payload the server sent at load. That payload is a snapshot of
// the squad as it was: after any transfer its player ids no longer match, so
// the incoming players were skipped and the lineup silently came out wrong (or
// not at all). Recomputing also means it keeps working while you're still
// building a squad, where the server sends no `optimised` at all.
document.getElementById('optimiseBtn').addEventListener('click', () => {
    if (!workingSquad || workingSquad.some(p => p.id < 0)) {
        alert('Fill every slot first — tap the remaining "+" slots to pick players.');
        return;
    }
    const opt = optimiseSquad(workingSquad);
    if (!opt) { alert('Need a full 15-man squad to optimise.'); return; }

    const orderMap = {};
    opt.starting.forEach((id, i) => orderMap[id] = { starting: true, position: i + 1 });
    opt.bench.forEach((id, i) => orderMap[id] = { starting: false, position: 12 + i });
    workingSquad.forEach(p => { const o = orderMap[p.id]; if (o) { p.starting = o.starting; p.position = o.position; } });

    // Armband goes to the two highest-projected STARTERS, decided after the
    // lineup so it can never land on someone who's just been benched.
    const ranked = workingSquad.filter(p => p.starting)
                               .sort((a, b) => (b.predicted || 0) - (a.predicted || 0));
    if (ranked[0]) captainId = ranked[0].id;
    if (ranked[1]) viceId = ranked[1].id;

    showResetBtn();
    renderPitch();
    updatePredicted();
});

document.getElementById('resetBtn').addEventListener('click', () => {
    workingSquad = teamView.squad.map(p => ({ ...p }));
    const cap = teamView.squad.find(p => p.is_captain);
    const vice = teamView.squad.find(p => p.is_vice_captain);
    captainId = cap ? cap.id : null;
    viceId = vice ? vice.id : null;
    pendingOuts = [];
    transfersUsed = 0;
    document.getElementById('transferBanner').classList.add('d-none');
    if (teamView.header) teamView.header.value = +workingSquad.reduce((s, p) => s + (p.cost || 0), 0).toFixed(1);
    if (teamView._gw0) { teamView.gw = { ...teamView._gw0 }; }
    teamView.transfer_recs = (teamView._recs0 || []).slice();
    document.getElementById('resetBtn').classList.add('d-none');
    renderTransfers(teamView.transfer_recs);
    renderPitch();
    updatePredicted();
    playerSearch.refresh();
});

// ---- Refresh recommended transfers (recompute from the current squad) ----
document.getElementById('refreshTransfersBtn').addEventListener('click', () => {
    if (!workingSquad) return;
    const rtIcon = document.querySelector('#refreshTransfersBtn .rt-icon');
    if (rtIcon) rtIcon.classList.add('spinning');
    ensurePlayers().then(() => {
        const bank = teamView.gw ? (teamView.gw.bank || 0) : 0;
        // Preseason / draft => unlimited transfers, so nothing is a points hit.
        const ft = teamView.built ? Infinity
            : ((teamView.gw && typeof teamView.gw.free_transfers_est === 'number') ? teamView.gw.free_transfers_est : 1);
        teamView.transfer_recs = computeTransfers(workingSquad, allPlayers, bank, ft);
        teamView._recs0 = teamView.transfer_recs.slice();
        renderTransfers(teamView.transfer_recs);
    }).finally(() => { if (rtIcon) rtIcon.classList.remove('spinning'); });
});

// ---- Chips strip above the pitch (image cards + hover/press info) ----
let chipTips = [];
function renderChips(gw) {
    const bar = document.getElementById('chipsBar');
    const info = document.getElementById('chipInfo');
    chipTips.forEach(t => t.remove()); chipTips = [];
    info.classList.add('d-none'); info.textContent = ''; delete info.dataset.openFor;
    // chips_available carries display NAMES ('Bench Boost'), not codes, so a
    // card is matched on either. chip_advice is keyed on the code and only
    // contains chips still in hand, which makes it the better signal when the
    // server sent one.
    const avail = (gw && gw.chips_available) || [];
    const advice = {};
    ((gw && gw.chip_advice) || []).forEach(a => { advice[a.chip] = a; });

    const bench = (workingSquad || []).filter(p => !p.starting);
    const benchPts = +bench.reduce((s, p) => s + (p.predicted || 0), 0).toFixed(1);
    const cap = (workingSquad || []).find(p => p.id === captainId);
    const capPts = cap ? +(cap.predicted || 0).toFixed(1) : 0;

    const CHIPS = [
        { key: 'bboost', name: 'Bench Boost', what: 'your bench also scores',
          fallback: `Bench projected ${benchPts} pts this gameweek.` },
        { key: '3xc', name: 'Triple Captain', what: 'your captain scores x3',
          fallback: cap ? `${cap.web_name} projected ${capPts} pts (x3 = ${(capPts * 3).toFixed(1)}).`
                        : 'Pick a strong captain first.' },
        { key: 'wildcard', name: 'Wildcard', what: 'unlimited free transfers',
          fallback: 'Best when the squad has drifted - injuries, bad runs, or players not contributing.' },
        { key: 'freehit', name: 'Free Hit', what: 'a different team for one gameweek',
          fallback: 'Best for a blank gameweek your squad cannot cover.' }
    ];

    // A note that says what the chip is worth, and against what. A projection
    // on its own is not information - "14 points" only means something next to
    // what the same chip returned in weeks gone by.
    function noteFor(c) {
        const a = advice[c.key];
        if (!a) return `${c.name}: ${c.what}. ${c.fallback} Refreshes at gameweek 19.`;
        let note = `${c.name}: ${c.what}. ${a.detail}.`;
        if (a.realised_median != null) {
            note += ` The median ${c.name} in our 2025-26 simulation returned ${a.realised_median} pts.`;
        }
        if (a.percentile != null) {
            note += ` That is better than about ${a.percentile}% of simulated weeks.`;
        }
        if (a.verdict === 'play') note += ' Worth playing this week.';
        else if (a.verdict === 'hold') note += ' Worth holding for a better week.';
        if (a.context === 'double') note += ' This is a double gameweek.';
        if (a.context === 'blank') note += ' This is a blank gameweek.';
        return note + ' Refreshes at gameweek 19.';
    }

    // The card says whether you HAVE the chip, and nothing more. It used to
    // read "Play it" and light up whenever a projection cleared a floor,
    // which is a recommendation dressed as a status \u2014 and one good week
    // for a captain is a thin basis on which to spend something that has to
    // last half a season. The full argument is still one tap away in the
    // note; it is offered there rather than pushed here.
    bar.innerHTML = CHIPS.map(c => {
        const a = advice[c.key];
        const available = a ? true : (avail.includes(c.key) || avail.includes(c.name));
        const status = available ? 'Available' : 'Used';
        return `<div class="chip-card ${available ? 'chip-avail' : 'chip-unavail'}" tabindex="0" data-i="${c.key}">
            <img class="chip-img" src="/static/${c.key}.svg" alt="${c.name}" data-onerror="invisible">
            <div class="chip-card-name">${c.name}</div>
            <div class="chip-status">${status}</div>
        </div>`;
    }).join('');
    bar.querySelectorAll('.chip-card').forEach(card => {
        const c = CHIPS.find(x => x.key === card.dataset.i);
        chipTips.push(attachTip(card, c ? noteFor(c) : '', info));
    });
}

// ---- Save team (persisted server-side against the FPL ID) ----
// Stored on the server rather than in localStorage, so the same squad
// loads on any device you enter this FPL ID on. It gets replaced by
// your real picks once the gameweek deadline passes.
document.getElementById('saveTeamBtn').addEventListener('click', () => {
    if (!workingSquad || workingSquad.length !== 15) return;
    if (workingSquad.some(p => p.id < 0)) {
        alert('Fill every slot before saving — tap the remaining "+" slots to pick players.');
        return;
    }
    if (teamView.gw && teamView.gw.bank < 0) {
        alert(`You're £${Math.abs(teamView.gw.bank).toFixed(1)}m over budget — sort your transfers before saving.`);
        return;
    }
    const id = getSavedId();
    if (!id) { alert('Enter your FPL ID first.'); return; }

    const snap = workingSquad.map(p => ({
        ...p, is_captain: p.id === captainId, is_vice_captain: p.id === viceId
    }));
    const btn = document.getElementById('saveTeamBtn');
    const label = btn.textContent;
    btn.textContent = 'Saving…'; btn.disabled = true;

    fetch(`/api/draft/${id}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            // Stamped with the gameweek on screen rather than left to default
            // server-side. A draft only applies to the round it was built for,
            // so which round that was is part of the save, not something to be
            // re-derived from the clock afterwards.
            gameweek: selectedEvent || null,
            bank: teamView.gw ? teamView.gw.bank : null,
            picks: snap.map(p => ({
                element_id: p.id, position: p.position,
                is_captain: p.is_captain, is_vice_captain: p.is_vice_captain,
                cost: p.cost
            }))
        })
    })
    .then(r => r.json().then(d => ({ ok: r.ok, d })))
    .then(({ ok, d }) => {
        if (!ok) throw new Error(d.detail || 'save failed');
        savedDraft = null;   // force a re-read next time the team loads
        // The saved state becomes the new "actual" that Reset reverts to, and
        // the banner has to switch over to saying so - from here on this
        // gameweek loads your team rather than FPL's.
        teamView.squad = snap.map(p => ({ ...p }));
        teamView.from_draft = true;
        teamView.draft_saved_at = new Date().toISOString();
        renderLiveBanner();   // from_draft outranks the carry-forward note
        if (teamView.gw) teamView._gw0 = { ...teamView.gw };
        teamView._recs0 = (teamView.transfer_recs || []).slice();
        document.getElementById('resetBtn').classList.add('d-none');
        btn.textContent = 'Saved ✓';
        setTimeout(() => { btn.textContent = label; btn.disabled = false; }, 1500);
        showSaveFeedback();
    })
    .catch(e => {
        alert(`Couldn't save your team: ${e.message}`);
        btn.textContent = label; btn.disabled = false;
    });
});

// ---- Save feedback ---------------------------------------------------------
// Two forms, deliberately different in weight:
//
//   FIRST save of a session -> a centred dialog that stays until it's closed by
//   hand. It's the one moment the site has visibly done something for you, so
//   it's the one moment worth interrupting for. Shown once per session and
//   never again, which is what earns the interruption.
//
//   EVERY save after that -> the small bottom toast, with the same Ko-fi link
//   in it. Someone tinkering with their squad saves half a dozen times in a
//   sitting; a dialog each time would be intolerable, but a toast they can
//   ignore is not.
//
// Both confirm the save itself, because that part is genuine feedback and the
// button briefly reading "Saved ✓" is easy to miss on a phone.
const KOFI_PROMPTED_KEY = 'fpl_kofi_prompted';
let saveToastTimer = null;

function showSaveFeedback() {
    let prompted = false;
    try { prompted = sessionStorage.getItem(KOFI_PROMPTED_KEY) === '1'; } catch (e) {}

    // The dialog only exists when a Ko-fi handle is configured; without one
    // there's nothing to interrupt for, so every save gets the toast.
    if (!prompted && document.getElementById('kofiModal')) {
        try { sessionStorage.setItem(KOFI_PROMPTED_KEY, '1'); } catch (e) {}
        openKofiModal();
        return;
    }
    showSaveToast();
}

// ---- Bottom toast ----
function hideSaveToast() {
    const t = document.getElementById('saveToast');
    if (t) t.classList.add('d-none');
    if (saveToastTimer) { clearTimeout(saveToastTimer); saveToastTimer = null; }
}

function showSaveToast() {
    const t = document.getElementById('saveToast');
    if (!t) return;

    t.classList.remove('d-none');
    // Restart the animation on a repeat save, otherwise the second toast
    // appears fully-formed with no movement and reads as a stuck element.
    t.classList.remove('toast-in');
    void t.offsetWidth;
    t.classList.add('toast-in');

    if (saveToastTimer) clearTimeout(saveToastTimer);
    // Long enough to notice a link, read it and decide to click it.
    saveToastTimer = setTimeout(hideSaveToast, document.getElementById('toastKofi') ? 7000 : 4000);
}

// ---- First-save dialog ----
// Closed by the X or Escape only. Not by clicking the backdrop: that's the
// dismissal people trigger by accident, and this is meant to be read once.
let kofiLastFocus = null;

function openKofiModal() {
    const m = document.getElementById('kofiModal');
    if (!m) return;
    kofiLastFocus = document.activeElement;
    m.classList.remove('d-none');
    // Stops the page scrolling behind the dialog, which on a phone otherwise
    // looks like the dialog itself is broken.
    document.body.classList.add('modal-open');
    const close = document.getElementById('kofiClose');
    if (close) close.focus();
}

function closeKofiModal() {
    const m = document.getElementById('kofiModal');
    if (!m || m.classList.contains('d-none')) return;
    m.classList.add('d-none');
    document.body.classList.remove('modal-open');
    // Put focus back where it was, so a keyboard user isn't dumped at the top
    // of the document.
    if (kofiLastFocus && kofiLastFocus.focus) kofiLastFocus.focus();
    kofiLastFocus = null;
}

(function () {
    const close = document.getElementById('toastClose');
    if (close) close.addEventListener('click', hideSaveToast);
    // Clicking through to Ko-fi opens a new tab; clear the toast behind it so
    // it isn't still sitting there on return.
    const kofi = document.getElementById('toastKofi');
    if (kofi) kofi.addEventListener('click', hideSaveToast);

    const modalClose = document.getElementById('kofiClose');
    if (modalClose) modalClose.addEventListener('click', closeKofiModal);

    document.addEventListener('keydown', e => {
        if (e.key !== 'Escape') return;
        const m = document.getElementById('kofiModal');
        if (m && !m.classList.contains('d-none')) closeKofiModal();
    });
}());

// ---- Recommended transfers ----
// No free/-4 tag on these, deliberately.
//
// It used to carry one, decided by position in the list: the first few were
// captioned "free" and the rest "-4 hit". That is not a property of a
// recommendation, it is a property of the ORDER YOU HAPPEN TO MAKE THEM IN -
// these are three independent upgrades, not a sequence, and the third one is
// free if it is the only one you make. Worse, the list is not a plan the reader
// has agreed to: captioning the bottom entry "-4 hit" told someone about to
// make one transfer that it would cost them four points, which was simply
// untrue.
//
// What the allowance actually is stays on screen: the header carries the free
// transfer count, and the pitch charges a hit as soon as spending one would
// really incur it.
function renderTransfers(recs) {
    const el = document.getElementById('transferRecs');
    if (!recs.length) { el.innerHTML = '<p class="text-muted small">No upgrades found within budget.</p>'; return; }
    el.innerHTML = recs.map((r, i) => {
        const cost = r.cost_change === 0 ? '\u00b10.0' : (r.cost_change > 0 ? '+' : '') + r.cost_change.toFixed(1);
        return `<div class="transfer-rec">
            <div class="transfer-line">
                <span class="tr-out">${shirtImg(r.out.team_code, r.out.pos, 'shirt-sm')}${r.out.web_name}</span>
                <span class="tr-arrow">&rarr;</span>
                <span class="tr-in">${shirtImg(r.in.team_code, r.in.pos, 'shirt-sm')}${r.in.web_name}</span>
            </div>
            <div class="transfer-meta">
                <span>rating +${r.rating_gain}</span><span>\u00a3${cost}m</span>
            </div>
            <button class="btn btn-sm btn-primary make-tr w-100 mt-1" data-i="${i}">Make transfer</button>
        </div>`;
    }).join('');
    el.querySelectorAll('.make-tr').forEach(btn =>
        btn.addEventListener('click', () => performTransfer(recs[btn.dataset.i].out.id, recs[btn.dataset.i].in)));
}

// ---- Gameweek navigation ----
document.getElementById('gwPrev').addEventListener('click', () => {
    if (selectedEvent > (teamView.min_event || 1)) { selectedEvent--; loadTeam(); }
});
document.getElementById('gwNext').addEventListener('click', () => {
    // max_event, not current_event. The button's disabled state was moved to
    // max_event when the next gameweek became reachable, but this guard was
    // left behind - so stepping back to the round in play left an arrow that
    // looked live and did nothing, because `1 < 1` is false. Two places
    // deciding the same thing, and they disagreed.
    if (selectedEvent < (teamView.max_event || teamView.current_event)) {
        selectedEvent++; loadTeam();
    }
});

// ---- Leagues (accordion: opening one closes the others) ----
// News list scrolls internally, capped to the leagues LIST's actual
// rendered height (not the whole leagues column — both columns have
// their own equal-height heading above the list, so matching the
// content divs is what makes the two column bottoms line up). A pure
// flex-stretch approach doesn't work here: stretch sizes the row to
// the TALLEST natural content, so nothing would ever need to scroll.
// Re-run whenever either column's height can change.
function syncNewsHeight() {
    const leaguesSection = document.getElementById('leaguesSection');
    const newsList = document.getElementById('newsList');
    if (!leaguesSection || !newsList) return;
    if (window.matchMedia('(min-width: 992px)').matches) {
        // newsList's own top (not its heading's bottom — there's a
        // margin gap between them) is a stable reference regardless of
        // any height set on a previous call, since it's positioned by
        // the content above it, not by its own size. getBoundingClientRect
        // (not offsetHeight) so a child's bottom margin — e.g. the
        // "No leagues found." <p> — is accounted for. A fixed height
        // (not max-height) so the bottom lines up even when news has
        // FEWER items than leagues has height for.
        const contentHeight = leaguesSection.getBoundingClientRect().bottom
            - newsList.getBoundingClientRect().top;
        newsList.style.height = Math.max(0, contentHeight) + 'px';
    } else {
        newsList.style.height = '';   // stacked on mobile — CSS media query caps it instead
    }
}
window.addEventListener('resize', syncNewsHeight);

function renderLeagues(groups) {
    const el = document.getElementById('leaguesList');
    groups = groups || {};
    const order = [['personal', 'Personal'], ['general', 'General'], ['broadcaster', 'Broadcaster']];
    let html = '';
    order.forEach(([key, label]) => {
        const list = groups[key] || [];
        if (!list.length) return;
        html += `<div class="league-group-label">${label}</div>`;
        html += list.map(l => `
            <div class="league-row" data-id="${l.id}">
                <span class="league-name">${l.name}</span>
                <span class="league-rank">${l.rank != null ? 'Rank ' + l.rank.toLocaleString() : ''}</span>
                <span class="league-caret">&#9662;</span>
                <div class="league-standings d-none"></div>
            </div>`).join('');
    });
    el.innerHTML = html || '<p class="text-muted small">No leagues found.</p>';
    el.querySelectorAll('.league-row').forEach(row => {
        row.querySelector('.league-name').addEventListener('click', () => toggleLeague(row));
        row.querySelector('.league-caret').addEventListener('click', () => toggleLeague(row));
    });
    syncNewsHeight();
}
function toggleLeague(row) {
    const box = row.querySelector('.league-standings');
    const isOpen = !box.classList.contains('d-none');
    document.querySelectorAll('.league-standings').forEach(b => { if (b !== box) b.classList.add('d-none'); });
    if (isOpen) { box.classList.add('d-none'); syncNewsHeight(); return; }
    if (box.dataset.loaded) { box.classList.remove('d-none'); syncNewsHeight(); return; }
    box.innerHTML = '<div class="text-muted small p-2">Loading\u2026</div>';
    box.classList.remove('d-none');
    syncNewsHeight();
    fetch(`/api/league/${row.dataset.id}`)
        .then(res => res.json())
        .then(data => {
            if (!data.available) { box.innerHTML = '<div class="text-muted small p-2">Unavailable.</div>'; syncNewsHeight(); return; }
            box.dataset.loaded = '1';
            box.innerHTML = `<table class="table table-sm league-table mb-0"><tbody>${
                data.standings.map(s => `<tr>
                    <td class="ls-rank">${s.rank}</td>
                    <td>${s.entry_name}<div class="ls-manager">${s.manager}</div></td>
                    <td class="ls-total">${s.total}</td>
                </tr>`).join('')}</tbody></table>`;
            syncNewsHeight();
        });
}

// ---- News feed (injury/transfer blurbs, straight from FPL's own data) ----
// FPL stamps `news_added` only when a player's flag actually CHANGES, so a
// quiet stretch legitimately produces no new items. What DID make it look
// frozen is that this only ever ran once per page load, and the response
// was cacheable — so it now polls, sends a cache-buster, and shows when it
// last checked, so "no new news" is distinguishable from "not updating".
const NEWS_POLL_MS = 5 * 60 * 1000;
let newsLoading = false;

// "3h ago" / "2d ago" for anything recent, an absolute date beyond a week.
function newsStamp(iso) {
    if (!iso) return '';
    const t = new Date(iso);
    if (isNaN(t)) return '';
    const mins = Math.round((Date.now() - t.getTime()) / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m ago`;
    if (mins < 60 * 24) return `${Math.floor(mins / 60)}h ago`;
    if (mins < 60 * 24 * 7) return `${Math.floor(mins / 1440)}d ago`;
    return t.toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
}
// Exact date+time for the hover title, so the relative stamp is checkable.
function newsExact(iso) {
    if (!iso) return '';
    const t = new Date(iso);
    return isNaN(t) ? '' : t.toLocaleString();
}

function loadNews() {
    if (newsLoading) return;
    newsLoading = true;
    const el = document.getElementById('newsList');
    const stampEl = document.getElementById('newsUpdated');
    const btn = document.getElementById('newsRefresh');
    btn.classList.add('spinning');
    // Cache-busting param as well as no-store: some mobile browsers ignore
    // the header on a back/forward restore.
    fetch(`/api/news?_=${Date.now()}`, { cache: 'no-store' })
        .then(res => res.json())
        .then(data => {
            if (!data.available || !data.stories.length) {
                el.innerHTML = '<p class="text-muted small mb-0">No injury or transfer news right now.</p>';
            } else {
                el.innerHTML = data.stories.map(s => {
                    const rel = newsStamp(s.added || s.date);
                    const exact = newsExact(s.added || s.date);
                    return `
                    <div class="news-item">
                        ${shirtImg(s.team_code, '', 'shirt-sm')}
                        <div class="news-body">
                            <div class="news-head">
                                <span class="news-who"><span class="news-player">${s.player}</span>
                                    <span class="news-team">${s.team || ''}</span></span>
                                <span class="news-date"${exact ? ` title="${exact}"` : ''}>${rel}</span>
                            </div>
                            <div class="news-text">${s.headline}</div>
                        </div>
                    </div>`;
                }).join('');
            }
            stampEl.textContent = 'checked ' + new Date().toLocaleTimeString(
                undefined, { hour: '2-digit', minute: '2-digit' });
            syncNewsHeight();
        })
        .catch(() => { el.innerHTML = '<p class="text-muted small mb-0">Couldn’t load news.</p>'; })
        .finally(() => { newsLoading = false; btn.classList.remove('spinning'); });
}
document.getElementById('newsRefresh').addEventListener('click', loadNews);
setInterval(loadNews, NEWS_POLL_MS);
// Coming back to a tab that's been open for hours should show current news,
// not whatever was on screen when it was backgrounded.
document.addEventListener('visibilitychange', () => {
    if (document.hidden) { stopLivePolling(); return; }
    loadNews();
    if (gameweekIsLocked()) loadLiveScores(selectedEvent);
});

// ---- Under/overperforming players (actual returns vs xG/xGC) ----
//
// One code path for both tables. They differ only in which endpoint they read
// and which empty-state sentence they print — the rows, the attacker/defender
// split and the sorting are identical, and the server has already signed `diff`
// so that bigger always means "more of whatever this table is about".
const PERF_VIEWS = {
    underperforming: {
        api: '/api/underperforming', body: 'underperfBody',
        tabs: 'underperfPosTabs', view: 'underperformingView',
        empty: 'No underperforming players found.',
    },
    overperforming: {
        api: '/api/overperforming', body: 'overperfBody',
        tabs: 'overperfPosTabs', view: 'overperformingView',
        empty: 'No overperforming players found.',
    },
};
// 'attackers' (MID/FWD, goals vs xG) or 'defenders' (GK/DEF, conceded vs xGC).
// Per view, not shared: switching tabs should not silently move the other
// table's position filter under the user.
const perfState = {
    underperforming: { loaded: false, data: [], group: 'attackers' },
    overperforming: { loaded: false, data: [], group: 'attackers' },
};
function renderPerfRows(key) {
    const cfg = PERF_VIEWS[key], st = perfState[key];
    const body = document.getElementById(cfg.body);
    const rows = st.data.filter(p =>
        st.group === 'attackers' ? (p.pos === 'MID' || p.pos === 'FWD') : (p.pos === 'DEF' || p.pos === 'GK'));
    body.innerHTML = rows.length ? rows.map(p => `
        <tr class="ps-row">
            <td class="ps-name">${shirtImg(p.team_code, p.pos, 'shirt-sm')}<span>${p.web_name}</span></td>
            <td>${p.pos}</td>
            <td>${p.team_name || ''}</td>
            <td>${p.metric}${p.season === 'last season' ? ' <span class="text-muted">(LS)</span>' : ''}</td>
            <td>${p.expected}</td>
            <td>${p.actual}</td>
            <td><span class="rating-badge underperf-diff">+${p.diff}</span></td>
            <td>£${p.cost != null ? p.cost.toFixed(1) : '–'}</td>
            <td><div class="player-gws">${miniFixtures(p)}</div></td>
        </tr>`).join('')
        : `<tr><td colspan="9" class="text-muted small p-2">${cfg.empty}</td></tr>`;
}
function loadPerfView(key) {
    const cfg = PERF_VIEWS[key], st = perfState[key];
    if (st.loaded) { renderPerfRows(key); return; }
    st.loaded = true;
    fetch(cfg.api)
        .then(res => res.json())
        .then(data => {
            st.data = data.results || [];
            renderPerfRows(key);
        })
        .catch(() => {
            // Reset so the next click retries rather than showing the error for
            // the rest of the session.
            st.loaded = false;
            document.getElementById(cfg.body).innerHTML =
                '<tr><td colspan="9" class="text-muted small p-2">Couldn’t load this table.</td></tr>';
        });
}
Object.entries(PERF_VIEWS).forEach(([key, cfg]) => {
    document.querySelectorAll(`#${cfg.tabs} .nav-link`).forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll(`#${cfg.tabs} .nav-link`).forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            perfState[key].group = btn.dataset.group;
            renderPerfRows(key);
        });
    });
});
document.querySelectorAll('#playersViewTabs .nav-link').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('#playersViewTabs .nav-link').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        const chosen = btn.dataset.view;            // 'all' | 'underperforming' | 'overperforming'
        // Every view is hidden and then exactly one shown, rather than toggled
        // against a single boolean. With three views a toggle would have left
        // two of them visible at once on the first switch between them.
        Object.entries(PERF_VIEWS).forEach(([key, cfg]) => {
            document.getElementById(cfg.view).classList.toggle('d-none', key !== chosen);
        });
        document.getElementById('playersTabSearch').classList.toggle('d-none', chosen !== 'all');
        if (PERF_VIEWS[chosen]) loadPerfView(chosen);
    });
});

// =====================================================================
//  PLAYER POOL — builder, picker, shared filters
// =====================================================================
// The saved squad now lives on the server keyed by FPL ID (see
// /api/draft), not in localStorage — so it follows you between
// devices instead of being stranded on the one you built it on.
let savedDraft = null;
function loadDraft() {
    const id = getSavedId();
    if (!id) return Promise.resolve(null);
    if (savedDraft) return Promise.resolve(savedDraft);
    return fetch(`/api/draft/${id}`)
        .then(r => r.json())
        .then(d => { savedDraft = d.available ? d : null; return savedDraft; })
        .catch(() => null);
}
const SQUAD_REQ = { GK: 2, DEF: 5, MID: 5, FWD: 3 };
const BUDGET = 100.0;
let allPlayers = null;


function ensurePlayers() {
    if (allPlayers) return Promise.resolve(allPlayers);
    return fetch('/api/all_players').then(r => r.json()).then(d => { allPlayers = d.players || []; return allPlayers; });
}
// Empty starting squad (4-4-2, GK/DEF/MID/FWD bench) so the normal pitch view
// shows "Add player" slots when there is nothing to load yet.
function emptySquad() {
    const rows = [
        { pos: 'GK', starting: true }, { pos: 'GK', starting: false },
        { pos: 'DEF', starting: true }, { pos: 'DEF', starting: true }, { pos: 'DEF', starting: true }, { pos: 'DEF', starting: true }, { pos: 'DEF', starting: false },
        { pos: 'MID', starting: true }, { pos: 'MID', starting: true }, { pos: 'MID', starting: true }, { pos: 'MID', starting: true }, { pos: 'MID', starting: false },
        { pos: 'FWD', starting: true }, { pos: 'FWD', starting: true }, { pos: 'FWD', starting: false },
    ];
    let startN = 0, benchN = 0;
    return rows.map((r, i) => ({
        id: -(i + 1), web_name: '', pos: r.pos, team: null, team_code: null,
        cost: 0, rating: 0, predicted: 0, form: null, status: 'a', news: '',
        next_gameweeks: [], starting: r.starting,
        position: r.starting ? ++startN : 11 + (++benchN),
        is_captain: false, is_vice_captain: false, multiplier: 1,
    }));
}
function setupPriceRange(which, onChange) {
    const min = document.getElementById(which + 'PriceMin');
    const max = document.getElementById(which + 'PriceMax');
    const label = document.getElementById(which + 'PriceLabel');
    const fill = document.getElementById(which + 'PriceFill');
    const lo0 = parseFloat(min.min), hi0 = parseFloat(min.max);
    const upd = (fire) => {
        let lo = parseFloat(min.value), hi = parseFloat(max.value);
        if (lo > hi) {   // stop the two handles crossing
            if (document.activeElement === min) { hi = lo; max.value = hi; }
            else { lo = hi; min.value = lo; }
        }
        label.textContent = `\u00a3${lo.toFixed(1)}m \u2013 \u00a3${hi.toFixed(1)}m`;
        if (fill) {
            fill.style.left = ((lo - lo0) / (hi0 - lo0) * 100) + '%';
            fill.style.right = (100 - (hi - lo0) / (hi0 - lo0) * 100) + '%';
        }
        if (fire !== false) onChange();
    };
    min.addEventListener('input', () => upd(true));
    max.addEventListener('input', () => upd(true));
    upd(false);
}
// ---- Reusable player search (sortable table + filters) ----
function createPlayerSearch(cfg) {
    const c = cfg.container;
    const pfx = cfg.sliderPrefix || 'ps';
    // Anything already in the list came from the server (see
    // partials/ssr_players.html). Overwriting the container is the first thing
    // this function does, so without capturing them here the server-rendered
    // rows would be destroyed on the very first frame - a crawler would still
    // read them, but a person with a slow connection would watch a full table
    // of ratings vanish and be replaced by an empty box until /api/all_players
    // came back. Held instead, and shown until there is real data to replace
    // them with.
    const seededList = c.querySelector('.ps-list');
    const seededRows = seededList ? seededList.innerHTML : '';
    c.innerHTML = `
        <div class="ps-controls">
            <div class="search-clear-wrap ps-search">
                <input class="form-control form-control-sm ps-q" placeholder="Search player...">
                <button class="search-clear" type="button">&times;</button>
            </div>
            <select class="form-select form-select-sm ps-pos">
                <option value="All">All positions</option>
                <option>GK</option><option>DEF</option><option>MID</option><option>FWD</option>
            </select>
            <select class="form-select form-select-sm ps-team"><option value="All">All teams</option></select>
            <button class="btn btn-sm btn-outline-primary ps-reset">Reset</button>
            <div class="price-range ps-price">
                <div class="price-label">Price: <span id="${pfx}PriceLabel"></span></div>
                <div class="range-wrap">
                    <div class="range-fill" id="${pfx}PriceFill"></div>
                    <input type="range" id="${pfx}PriceMin" min="3.5" max="17" step="0.5" value="3.5">
                    <input type="range" id="${pfx}PriceMax" min="3.5" max="17" step="0.5" value="17">
                </div>
            </div>
        </div>
        <div class="ps-rec"></div>
        <div class="ps-list"></div>`;
    const qEl = c.querySelector('.ps-q');
    const posEl = c.querySelector('.ps-pos');
    const teamEl = c.querySelector('.ps-team');
    const listEl = c.querySelector('.ps-list');
    const recEl = c.querySelector('.ps-rec');
    // Put the server's rows straight back into the rebuilt shell, so the table
    // is populated in the same frame the controls appear rather than blinking
    // empty until the pool arrives.
    if (seededRows) listEl.innerHTML = seededRows;
    const state = { sortKey: 'rating', sortDir: 'desc', teamsFilled: false };

    // Ownership is on the Players tab and deliberately not on My Team. Browsing
    // the whole game, "who else owns him" is half the decision - it is what
    // separates a template pick from a differential, and the FAQ has been
    // telling people to look for low ownership on a table that didn't show it.
    // On My Team the same widget is a transfer picker in a narrow column beside
    // the pitch, where an eighth column costs width the fixtures need and
    // answers a question you have already asked by the time you are picking a
    // replacement.
    const COLS = [
        { key: 'web_name', label: 'Player', noSort: true },
        { key: 'pos', label: 'Pos', noSort: true },
        { key: 'team_name', label: 'Team', noSort: true },
        { key: 'form', label: 'Form', num: true },
        { key: 'rating', label: 'Rtg', num: true },
        { key: 'cost', label: '\u00a3m', num: true },
        ...(cfg.showOwnership ? [{ key: 'owned', label: 'Own %', num: true }] : []),
        { key: 'fixtures', label: 'Next 3', noSort: true }
    ];
    // Widths come from these classes rather than from column position, because
    // the two instances no longer have the same columns - and because the
    // server renders this same table too (partials/ssr_players.html), from the
    // same keys.
    const tableClass = 'table table-sm ps-table mb-0' + (cfg.showOwnership ? ' ps-table-own' : '');

    function pool() { return cfg.pool() || []; }
    function ensureTeams() {
        if (state.teamsFilled) return;
        const names = [...new Set(pool().map(p => p.team_name).filter(Boolean))].sort();
        if (!names.length) return;
        teamEl.insertAdjacentHTML('beforeend', names.map(n => `<option>${n}</option>`).join(''));
        state.teamsFilled = true;
    }
    function bounds() {
        const mn = parseFloat(document.getElementById(pfx + 'PriceMin').value);
        const mx = parseFloat(document.getElementById(pfx + 'PriceMax').value);
        return [Math.min(mn, mx), Math.max(mn, mx)];
    }
    function sortRows(rows) {
        const k = state.sortKey, dir = state.sortDir === 'asc' ? 1 : -1;
        const num = (COLS.find(x => x.key === k) || {}).num;
        return rows.slice().sort((a, b) => {
            if (num) { const va = a[k] == null ? -Infinity : a[k], vb = b[k] == null ? -Infinity : b[k]; return (va - vb) * dir; }
            const va = (a[k] || '').toString().toLowerCase(), vb = (b[k] || '').toString().toLowerCase();
            return va < vb ? -dir : va > vb ? dir : 0;
        });
    }
    function rowHtml(p) {
        const form = p.form != null ? p.form.toFixed(1) : '\u2013';
        const disabled = cfg.rowDisabled ? cfg.rowDisabled(p) : false;
        const dattr = disabled
            ? ' style="opacity:0.4;cursor:not-allowed" title="Max 3 players from one club"'
            : '';
        // FPL leaves ownership out for a player it has no figure for, so this
        // is an en dash rather than "0.0%" - which would read as "nobody owns
        // him", a different and much more interesting claim.
        const owned = p.owned != null ? p.owned.toFixed(1) : '\u2013';
        const ownedCell = cfg.showOwnership ? `<td class="col-owned">${owned}</td>` : '';
        return `<tr class="ps-row${disabled ? ' ps-disabled' : ''}"${dattr} data-id="${p.id}">
            <td class="ps-name col-web_name">${shirtImg(p.team_code, p.pos, 'shirt-sm')}<span>${p.web_name}</span></td>
            <td class="col-pos">${p.pos}</td>
            <td class="col-team_name">${p.team_name || ''}</td>
            <td class="col-form">${form}</td>
            <td class="col-rating"><span class="rating-badge">${Math.round(p.rating)}</span></td>
            <td class="col-cost">${p.cost.toFixed(1)}</td>
            ${ownedCell}
            <td class="col-fixtures"><div class="player-gws">${miniFixtures(p)}</div></td>
        </tr>`;
    }
    function headHtml() {
        const arrow = c => c.noSort ? '' : (state.sortKey === c.key ? (state.sortDir === 'asc' ? ' \u25B2' : ' \u25BC') : ' <span class="ps-arrow">\u21C5</span>');
        const ths = COLS.map(col => `<th class="col-${col.key}${col.noSort ? '' : ' ps-sortable'}" data-key="${col.key}">${col.label}${arrow(col)}</th>`).join('');
        return `<thead><tr>${ths}</tr></thead>`;
    }
    function render() {
        // No pool yet. Leave the server-rendered rows where they are rather
        // than replacing them with "No players match." - which would be a
        // false statement about an empty pool, not an empty result.
        if (!pool().length && seededRows) { listEl.innerHTML = seededRows; return; }
        ensureTeams();
        const transfer = cfg.isTransferMode();
        const [lo, hi] = bounds();
        const qq = qEl.value.trim().toLowerCase();
        let rows = pool().filter(p =>
            (posEl.value === 'All' || p.pos === posEl.value) &&
            (teamEl.value === 'All' || p.team_name === teamEl.value) &&
            p.cost >= lo && p.cost <= hi &&
            (!qq || p.web_name.toLowerCase().includes(qq)));
        if (transfer) rows = rows.filter(cfg.transferCandidate);
        rows = sortRows(rows).slice(0, 80);

        if (transfer) {
            const rec = pool().filter(cfg.transferCandidate).sort((a, b) => b.rating - a.rating).slice(0, 3);
            recEl.innerHTML = rec.length
                ? `<div class="ps-rec-label">Recommended \u2014 tap to transfer in</div>
                   <div class="ps-list"><table class="${tableClass}">${headHtml()}<tbody>${rec.map(rowHtml).join('')}</tbody></table></div>`
                : '';
        } else { recEl.innerHTML = ''; }

        listEl.innerHTML = rows.length
            ? `<table class="${tableClass}${transfer ? ' ps-transfer' : ''}">${headHtml()}<tbody>${rows.map(rowHtml).join('')}</tbody></table>`
            : '<p class="text-muted small p-2">No players match.</p>';
        listEl.scrollTop = 0; listEl.scrollLeft = 0;

        c.querySelectorAll('.ps-sortable').forEach(th => th.addEventListener('click', () => {
            const k = th.dataset.key;
            if (state.sortKey === k) state.sortDir = state.sortDir === 'asc' ? 'desc' : 'asc';
            else { state.sortKey = k; state.sortDir = 'desc'; }
            render();
        }));
        // Whole row is clickable: transfer the player in, or open their card.
        c.querySelectorAll('.ps-row').forEach(r => r.addEventListener('click', () => {
            if (r.classList.contains('ps-disabled')) return;
            const p = pool().find(x => x.id === +r.dataset.id);
            if (!p) return;
            if (transfer) cfg.onTransfer(p); else cfg.onBrowse(p);
        }));
    }

    qEl.addEventListener('input', render);
    posEl.addEventListener('change', render);
    teamEl.addEventListener('change', render);
    wireClear(qEl, c.querySelector('.ps-search .search-clear'), render);
    setupPriceRange(pfx, render);
    c.querySelector('.ps-reset').addEventListener('click', () => {
        qEl.value = ''; posEl.value = 'All'; teamEl.value = 'All';
        c.querySelector('.ps-search .search-clear').style.display = 'none';
        const mn = document.getElementById(pfx + 'PriceMin'), mx = document.getElementById(pfx + 'PriceMax');
        mn.value = 3.5; mx.value = 17; mn.dispatchEvent(new Event('input'));
    });
    return { refresh: render };
}

const playerSearch = createPlayerSearch({
    container: document.getElementById('playerSearch'),
    sliderPrefix: 'ps',
    pool: () => allPlayers || [],
    // Open slots (marked-out players OR unfilled slots from the initial
    // pick) put the table into transfer mode, so tapping a row drops the
    // player straight into a matching slot instead of opening their card.
    isTransferMode: () => pendingOuts.length > 0 || hasEmptySlots(),
    transferCandidate: (p) => {
        // Every open position is fair game at once — e.g. mark a DEF and
        // a MID out, and both DEF and MID candidates show up together,
        // not just whichever was marked most recently.
        const positions = new Set(pendingOuts.map(o => o.pos));
        emptySlots().forEach(s => positions.add(s.pos));
        if (!positions.size) return true;
        if (!positions.has(p.pos)) return false;
        const ownedIds = new Set(workingSquad.map(x => x.id));
        return !ownedIds.has(p.id);
    },
    rowDisabled: (p) => {
        if (!pendingOuts.length && !hasEmptySlots()) return false;
        // Other slots already marked for transfer out don't count against the
        // club limit — they won't be left in the team once resolved.
        const leaving = new Set(pendingOuts.map(o => o.id));
        return workingSquad.filter(x => x.team === p.team && !leaving.has(x.id)).length >= 3;
    },
    onTransfer: (p) => resolveTransfer(p),
    onBrowse: (p) => {
        const owned = workingSquad && workingSquad.find(x => x.id === p.id);
        openPlayerModal(owned || p, !!owned);
    }
});

// Same search table powers the Players tab (browse only), with the ownership
// column on - see the note beside COLS. The server-rendered copy this replaces
// (partials/ssr_players.html) carries the same column, so the swap doesn't
// change the table's shape under the reader.
const playersTabSearch = createPlayerSearch({
    container: document.getElementById('playersTabSearch'),
    sliderPrefix: 'pt',
    pool: () => allPlayers || [],
    showOwnership: true,
    isTransferMode: () => false,
    transferCandidate: () => true,
    onTransfer: () => {},
    onBrowse: (p) => {
        const owned = workingSquad && workingSquad.find(x => x.id === p.id);
        openPlayerModal(owned || p, !!owned);
    }
});

// (Building a squad is now done directly on the pitch: empty slots are
// filled via the same transfer-in flow used for swaps, then "Save team"
// persists it \u2014 see emptySquad() / saveTeamBtn.)

// ---- Client-side optimise / transfers for a built team ----
function optimiseSquad(squad) {
    const gks = squad.filter(p => p.pos === 'GK').sort((a, b) => b.predicted - a.predicted);
    const outs = { DEF: [], MID: [], FWD: [] };
    squad.filter(p => p.pos !== 'GK').forEach(p => { if (outs[p.pos]) outs[p.pos].push(p); });
    Object.values(outs).forEach(a => a.sort((x, y) => y.predicted - x.predicted));
    if (gks.length < 2 || (outs.DEF.length + outs.MID.length + outs.FWD.length) < 10) return null;
    const startGk = gks[0], benchGk = gks[1];
    const MIN = { DEF: 3, MID: 2, FWD: 1 }, MAX = { DEF: 5, MID: 5, FWD: 3 };
    let starters = [], counts = { DEF: 0, MID: 0, FWD: 0 };
    ['DEF', 'MID', 'FWD'].forEach(pos => { starters = starters.concat(outs[pos].slice(0, MIN[pos])); counts[pos] = MIN[pos]; });
    let pool = [];
    ['DEF', 'MID', 'FWD'].forEach(pos => { pool = pool.concat(outs[pos].slice(MIN[pos])); });
    pool.sort((a, b) => b.predicted - a.predicted);
    for (const p of pool) { if (starters.length >= 10) break; if (counts[p.pos] < MAX[p.pos]) { starters.push(p); counts[p.pos]++; } }
    const starterIds = new Set(starters.map(p => p.id)); starterIds.add(startGk.id);
    const benchOut = squad.filter(p => !starterIds.has(p.id) && p.pos !== 'GK').sort((a, b) => b.predicted - a.predicted);
    const order = { GK: 0, DEF: 1, MID: 2, FWD: 3 };
    const starting = [startGk].concat(starters).sort((a, b) => (order[a.pos] - order[b.pos]) || (b.predicted - a.predicted));
    return { starting: starting.map(p => p.id), bench: [benchGk.id].concat(benchOut.map(p => p.id)) };
}
function computeTransfers(squad, pool, bank, freeTransfers, maxRecs) {
    maxRecs = maxRecs || 3;
    const owned = new Set(squad.map(p => p.id));
    const byPos = {};
    pool.forEach(p => { (byPos[p.pos] = byPos[p.pos] || []).push(p); });
    Object.values(byPos).forEach(a => a.sort((x, y) => y.rating - x.rating));
    let budget = bank, recs = [];
    const weak = [...squad].sort((a, b) => a.rating - b.rating);
    for (const w of weak) {
        if (recs.length >= maxRecs) break;
        const afford = w.cost + budget;
        for (const c of (byPos[w.pos] || [])) {
            if (owned.has(c.id)) continue;
            if (c.rating <= w.rating) break;
            if (c.cost <= afford) {
                // No `free` flag: renderTransfers decides that from the
                // allowance remaining when it draws, which is the only moment
                // the answer is current.
                recs.push({ out: w, in: c, rating_gain: +(c.rating - w.rating).toFixed(1),
                            cost_change: +(c.cost - w.cost).toFixed(1) });
                owned.add(c.id); budget -= (c.cost - w.cost); break;
            }
        }
    }
    return recs;
}
function showBuiltTeam(picked, leagues, header) {
    const byId = new Map((allPlayers || []).map(p => [p.id, p]));
    const squad = picked.map(p => {
        const fresh = byId.get(p.id) || {};
        return { ...p, ...fresh, is_captain: false, is_vice_captain: false, multiplier: 1 };
    });

    // Keep a saved lineup if present and legal; otherwise auto-optimise.
    let useSaved = picked.some(p => 'starting' in p) && picked.filter(p => p.starting).length === 11;
    if (useSaved) {
        picked.forEach(sp => { const p = squad.find(x => x.id === sp.id); if (p) { p.starting = !!sp.starting; p.position = sp.position; } });
        useSaved = isLegalXI(squad);
    }
    if (!useSaved) {
        const opt = optimiseSquad(squad);
        if (opt) {
            const map = {};
            opt.starting.forEach((id, i) => map[id] = { starting: true, position: i + 1 });
            opt.bench.forEach((id, i) => map[id] = { starting: false, position: 12 + i });
            squad.forEach(p => { const o = map[p.id]; if (o) { p.starting = o.starting; p.position = o.position; } });
        } else {
            squad.forEach((p, i) => { p.starting = i < 11; p.position = i + 1; });
        }
    }

    const savedCap = picked.find(p => p.is_captain);
    const savedVice = picked.find(p => p.is_vice_captain);
    const ranked = [...squad].sort((a, b) => b.predicted - a.predicted);
    const capId = savedCap ? savedCap.id : (ranked[0] ? ranked[0].id : null);
    const vId = savedVice ? savedVice.id : (ranked[1] ? ranked[1].id : null);
    if (capId) { const cc = squad.find(p => p.id === capId); if (cc) cc.is_captain = true; }
    if (vId) { const vv = squad.find(p => p.id === vId); if (vv) vv.is_vice_captain = true; }

    const spent = +squad.reduce((s, p) => s + p.cost, 0).toFixed(1);
    const bank = +(BUDGET - spent).toFixed(1);
    const predGw = +squad.filter(p => p.starting).reduce((s, p) => s + p.predicted * (p.is_captain ? 2 : 1), 0).toFixed(1);
    // Still has empty slots — suppress optimise/recommendation noise
    // built for a complete squad; filling slots is done via the pitch.
    const isBuilding = squad.some(p => p.id < 0);
    renderTeam({
        available: true, built: true,
        // Use the manager's real team name from the FPL entry - it's available
        // even in preseason, and "Pick your squad" told you nothing you didn't
        // already know from the empty slots in front of you.
        header: { name: (header && header.name) || 'Your team',
                  manager: (header && header.manager) || '',
                  value: spent, bank: bank },
        gw: { event: null, points: null, predicted_points: predGw, bank: bank, value: spent,
              chips_available: ['wildcard', 'freehit', 'bboost', '3xc'] },
        squad: squad,
        recommended: isBuilding ? { captain: null, vice: null } : { captain: capId, vice: vId },
        optimised: isBuilding ? null : optimiseSquad(squad),
        // Preseason/draft = unlimited transfers, so nothing is a hit here.
        transfer_recs: isBuilding ? [] : computeTransfers(squad, allPlayers, bank, Infinity),
        leagues: leagues || {}, current_event: null, min_event: 1
    });
}

// ---- Reusable tooltip (body-level so nothing traps it) ----
// `panel`, if given, is an element that takes the text INLINE on mobile
// instead of the floating tip — used by the chips so their explanation
// appears directly under the chips row rather than over the page middle.
function attachTip(el, text, panel) {
    const tip = document.createElement('div');
    tip.className = 'info-tip';
    tip.textContent = text;
    tip.style.display = 'none';
    document.body.appendChild(tip);
    const isMobile = () => window.matchMedia('(max-width: 767.98px)').matches;
    const usePanel = () => !!panel && isMobile();
    const panelKey = el.dataset.i || text;
    function show() {
        if (usePanel()) {
            tip.style.display = 'none';
            panel.textContent = text;
            panel.dataset.openFor = panelKey;
            panel.classList.remove('d-none');
            return;
        }
        tip.style.display = 'block';
        if (window.matchMedia('(max-width: 576px)').matches) {
            Object.assign(tip.style, {
                position: 'fixed', top: '50%', left: '50%', right: 'auto', bottom: 'auto',
                transform: 'translate(-50%, -50%)', width: '88vw', maxWidth: '320px', zIndex: '3000'
            });
        } else {
            const r = el.getBoundingClientRect();
            Object.assign(tip.style, {
                position: 'fixed', top: (r.bottom + 6) + 'px', left: r.left + 'px',
                right: 'auto', bottom: 'auto', transform: 'none', width: '240px', zIndex: '3000'
            });
        }
    }
    function hide() {
        tip.style.display = 'none';
        // Only close the shared panel if it's showing THIS tip's text —
        // another chip may have taken it over since.
        if (panel && panel.dataset.openFor === panelKey) {
            panel.classList.add('d-none');
            panel.textContent = '';
            delete panel.dataset.openFor;
        }
    }
    function isOpen() {
        return usePanel() ? panel.dataset.openFor === panelKey : tip.style.display !== 'none';
    }
    // Hover is a desktop affordance; on mobile a tap fires mouseenter first,
    // which would open the panel and let the click immediately close it.
    el.addEventListener('mouseenter', () => { if (!usePanel()) show(); });
    el.addEventListener('mouseleave', () => { if (!window.matchMedia('(max-width: 576px)').matches) hide(); });
    el.addEventListener('click', e => { e.stopPropagation(); isOpen() ? hide() : show(); });
    document.addEventListener('click', hide);
    window.addEventListener('resize', hide);
    return tip;
}
document.querySelectorAll('.info-icon').forEach(el => attachTip(el, el.dataset.tip));

// =====================================================================
//  FIXTURE ROTATOR
// =====================================================================
const rotationHeader = document.getElementById('rotationHeader');
const rotationBody = document.getElementById('rotationBody');
const pairsContainer = document.getElementById('pairsContainer');
let currentCategory = 'defender';
let selectedTeams = new Set();
let latestRotationData = null;

// Grey, not green, when every fixture scores the same. A flat range means the
// strength data behind the grid is missing, and green is the one answer that
// cannot be right for all twenty clubs at once - it reads as "every fixture is
// a banker" rather than "we don't know yet". Mirrored exactly by colour() in
// seo_tables.py; change the two together.
const NO_DIFFICULTY_COLOUR = 'hsl(210, 8%, 88%)';

function colorFor(value, min, max) {
    if (value == null || !isFinite(value)) return NO_DIFFICULTY_COLOUR;
    if (max === min) return NO_DIFFICULTY_COLOUR;
    const ratio = (value - min) / (max - min);
    const hue = 120 - (ratio * 120);
    return `hsl(${hue}, 70%, 82%)`;
}

function allDifficulties(data) {
    const values = [];
    data.teams.forEach(t => Object.values(t.fixtures).forEach(f => values.push(f.difficulty)));
    return values;
}

function fixtureCell(fixture, min, max) {
    if (!fixture) return '<td></td>';
    const color = colorFor(fixture.difficulty, min, max);
    return `<td><span class="fixture-cell" style="background-color:${color}">${fixture.opponent}</span></td>`;
}

function renderPairRow(teamName, teamCode, fixtures, gameweeks, min, max) {
    let cells = `<span class="pair-team-label">${shirtImg(teamCode, null, 'shirt-sm')}${teamName}</span>`;
    gameweeks.forEach(gw => {
        const f = fixtures[gw];
        if (!f) { cells += '<span style="width:44px;"></span>'; return; }
        const color = colorFor(f.difficulty, min, max);
        cells += `<span class="fixture-cell" style="background-color:${color}; width:44px; text-align:center;">${f.opponent}</span>`;
    });
    return `<div class="pair-row">${cells}</div>`;
}

function recSlot(pl) {
    if (!pl) return '<span class="rec-empty">&mdash;</span>';
    return `<span class="rec-slot">
        ${shirtImg(pl.team_code, pl.position, 'shirt-sm')}
        <span class="player-name">${pl.web_name ?? ''}</span>
        <span class="rec-rating">${pl.rating != null ? Math.round(pl.rating) : '-'}</span>
        <span class="rec-cost">${pl.cost != null ? '£' + pl.cost.toFixed(1) + 'm' : ''}</span>
    </span>`;
}

function recPlayersHtml(positionPairs) {
    if (!positionPairs || !positionPairs.length) return '';
    const rows = positionPairs.map(pp => `
        <span class="rec-pos">${pp.label}</span>
        ${recSlot(pp.player_a)}
        <span class="rec-plus">+</span>
        ${recSlot(pp.player_b)}
    `).join('');
    return `<div class="rec-grid">${rows}</div>`;
}

function renderRotationTable() {
    if (!latestRotationData) return;
    const data = latestRotationData;
    const gameweeks = data.gameweeks.map(String);
    const values = allDifficulties(data);
    const min = Math.min(...values);
    const max = Math.max(...values);

    rotationHeader.innerHTML = '<th class="team-col">Team</th>';
    gameweeks.forEach(gw => {
        const th = document.createElement('th');
        th.textContent = `GW${gw}`;
        rotationHeader.appendChild(th);
    });

    const sortedTeams = [...data.teams].sort((a, b) => {
        const aSel = selectedTeams.has(a.team_name);
        const bSel = selectedTeams.has(b.team_name);
        if (aSel && !bSel) return -1;
        if (!aSel && bSel) return 1;
        return 0;
    });

    rotationBody.innerHTML = '';
    sortedTeams.forEach(team => {
        const tr = document.createElement('tr');
        if (selectedTeams.has(team.team_name)) tr.classList.add('selected-team');
        let rowHtml = `<td class="team-col">${shirtImg(team.team_code, null, 'shirt-sm')} ${team.team_name}</td>`;
        gameweeks.forEach(gw => { rowHtml += fixtureCell(team.fixtures[gw], min, max); });
        tr.innerHTML = rowHtml;
        tr.querySelector('.team-col').addEventListener('click', () => {
            if (selectedTeams.has(team.team_name)) selectedTeams.delete(team.team_name);
            else selectedTeams.add(team.team_name);
            renderRotationTable();
        });
        rotationBody.appendChild(tr);
    });
}

function loadRotation() {
    fetch(`/api/rotation?category=${currentCategory}`)
        .then(res => res.json())
        .then(data => {
            latestRotationData = data;
            const gameweeks = data.gameweeks.map(String);
            const values = allDifficulties(data);
            const min = Math.min(...values);
            const max = Math.max(...values);

            pairsContainer.innerHTML = '';
            const buildPairCard = (pair) => {
                const card = document.createElement('div');
                card.className = 'pair-card';
                const positionPairs = pair.position_pairs || [];
                card.innerHTML = `
                    ${renderPairRow(pair.team_a, pair.team_a_code, pair.team_a_fixtures, gameweeks, min, max)}
                    ${renderPairRow(pair.team_b, pair.team_b_code, pair.team_b_fixtures, gameweeks, min, max)}
                    ${positionPairs.length ? `<div class="rec-players">
                        <div class="pair-meta">Rotate these players</div>
                        ${recPlayersHtml(positionPairs)}
                    </div>` : ''}
                `;
                return card;
            };

            const INITIAL_PAIRS = 2;
            data.pairs.slice(0, INITIAL_PAIRS).forEach(pair => pairsContainer.appendChild(buildPairCard(pair)));

            const remaining = data.pairs.slice(INITIAL_PAIRS);
            if (remaining.length) {
                const toggleBtn = document.createElement('button');
                toggleBtn.className = 'btn btn-outline-primary btn-sm';
                let expanded = false;
                let extraCards = [];
                const renderToggle = () => { toggleBtn.textContent = expanded ? 'Show less' : `Show all (${data.pairs.length})`; };
                toggleBtn.addEventListener('click', () => {
                    if (expanded) { extraCards.forEach(c => c.remove()); extraCards = []; }
                    else { extraCards = remaining.map(pair => { const card = buildPairCard(pair); pairsContainer.insertBefore(card, toggleBtn); return card; }); }
                    expanded = !expanded;
                    renderToggle();
                });
                renderToggle();
                pairsContainer.appendChild(toggleBtn);
            }

            renderRotationTable();
        });
}

document.querySelectorAll('#rotationTabs .nav-link').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('#rotationTabs .nav-link').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        currentCategory = btn.dataset.category;
        loadRotation();
    });
});

// =====================================================================
//  AI BEST XI
// =====================================================================
// Stateless per gameweek: the server solves a fresh budget-constrained
// optimum and freezes it at the deadline. A stored snapshot is the
// record of what was predicted BEFORE the gameweek, so it's shown in
// preference to re-solving; only the upcoming gameweek is solved live.
let aiGw = null;            // gameweek currently on screen
let aiBounds = { min: 1, max: null };
let aiLoaded = false;
let aiReqSeq = 0;           // guards against out-of-order fetch responses

// The one fixture this squad is picked FOR - opponent, home/away, and the same
// difficulty colour the rest of the app uses. Without it a projected score is a
// bare number with no way to judge whether it looks reasonable.
// One box per player, same visual language as the My Team pitch: coloured by
// fixture difficulty, projection on top, opponent underneath. Two separate
// pills sitting side by side read as clutter at this size.
function aiFixtureBox(p, gameweek, opts) {
    opts = opts || {};
    // A score only replaces the projection once the player's match has
    // actually begun - otherwise a mid-round pitch reads as though the AI's
    // squad had blanked when half of it hasn't kicked off. That decision is
    // the server's: it fills actual_points only for players whose fixture
    // has started, so an absent score here already means "hasn't played".
    //
    // Once he HAS played, the opponent stops being the point and the score is
    // the whole of it - so this is the same pill the My Team pitch uses, not a
    // fixture box with a number tucked inside it. Two pages showing a score two
    // different ways is two conventions to learn for one fact.
    if (p.actual_points != null) {
        const mult = p.is_captain ? (opts.chip === '3xc' ? 3 : 2) : 1;
        return `<span class="live-pts${p.is_captain ? ' live-cap' : ''}">`
            + `${p.actual_points * mult}</span>`;
    }

    const gws = p.next_gameweeks || [];
    const g = (gameweek != null && gws.find(x => x.event === gameweek)) || gws[0];

    // Hasn't played, and the round is under way: the one fixture he is waiting
    // on, drawn in the same tile My Team draws it in.
    if (!opts.upcoming) return fixtureForEvent(p, gameweek);

    // Hasn't played because the round hasn't started - the planning view.
    // Opponent on top, projection underneath.
    const colour = (g && g.difficulty != null) ? colorFor(g.difficulty, 1, 5) : '#eee';
    const val = `<span>${p.predicted != null ? p.predicted.toFixed(1) : '\u2013'}</span>`;
    const fix = g ? `<b>${g.opponent || ''} ${haTag(g)}</b>` : '<b>&nbsp;</b>';
    const title = g ? `GW${g.event}` : '';
    return `<span class="ai-mini" style="background:${colour}" title="${title}">${fix}${val}</span>`;
}

function aiPlayerCard(p, onBench, gameweek, opts) {
    // is_captain has already been moved onto whoever actually wore the armband
    // for a scored gameweek - see effectiveLineup.
    const badge = p.is_captain ? '<span class="cap-badge">C</span>'
                : (p.is_vice_captain ? '<span class="cap-badge vice">V</span>' : '');
    const posLabel = onBench ? `<div class="bench-pos">${p.pos || ''}</div>` : '';
    // Who came on for whom. Flagged rather than reordered - see live_overlay:
    // moving the substitute up onto the pitch would redraw the formation under
    // the reader mid-round, where a mark on both cards explains the total and
    // leaves the squad the bot picked still recognisable.
    const sub = p.auto_sub_in ? '<span class="auto-sub in" title="Came on as an automatic substitute">&#9650;</span>'
              : (p.auto_sub_out ? '<span class="auto-sub out" title="Didn’t play — substituted automatically">&#9660;</span>' : '');
    // Availability only means something for a squad that hasn't played yet, and
    // it's the more interesting number here than on My Team: it shows whether
    // the AI knowingly picked someone doubtful. Stored snapshots don't carry a
    // status, and a finished gameweek has actual points - in either case today's
    // fitness would say nothing true about a squad that already played.
    const band = (p.status !== undefined && p.actual_points == null)
        ? availabilityBandHtml(p) : '';
    // Tappable, for the same reason the My Team pitch is: these are the same
    // players, and "who is that and what sort of form is he in" is the first
    // question the bot's squad provokes. The skeleton placeholders carry a
    // negative id and are deliberately left inert.
    const clickable = p.id > 0 ? ` data-ai-id="${p.id}"` : '';
    return `<div class="player${clickable ? ' player-tappable' : ''}"${clickable} style="position:relative">
        ${posLabel}${badge}${sub}
        <div class="player-kit">${shirtImg(p.team_code, p.pos, 'kit')}</div>
        ${band}
        <div class="player-name-pill">${p.web_name}</div>
        <div class="player-gws">${aiFixtureBox(p, gameweek, opts)}</div>
    </div>`;
}

function renderAiPitch(squad, gameweek, opts) { renderAiPitchInto('aiPitch', 'aiBench', squad, gameweek, opts); }

// A full-size pitch of blank cards, drawn before either AI endpoint has
// answered so the block occupies its final height from the very first frame.
//
// It uses emptySquad() - the same fifteen placeholders the My Team tab builds a
// squad from - and the same card renderer as the real thing, which is the point:
// a skeleton hand-built from divs would drift out of step with the real card the
// first time one gained a row, and a skeleton that is the wrong height is worse
// than none at all. The placeholders keep their 'a' status so the availability
// band renders and the cards stand exactly as tall as they will with real
// players in them; .ai-skeleton in the stylesheet is what blanks the text.
function renderAiSkeletons() {
    // A non-breaking space rather than emptySquad()'s empty name. An element
    // with no text has no line box, so the name pill collapses from ~18px to
    // its padding - and across four pitch rows plus the bench that was 186px
    // of the block missing, which is a shift of exactly the kind this is meant
    // to prevent. The character is invisible; the height it holds is the point.
    // Written as an escape, not typed: a plain space would be collapsed away
    // by HTML and the pill would silently go back to 2px, and the two look
    // identical in the source.
    const squad = emptySquad().map(p => ({ ...p, web_name: ' ' }));
    // upcoming: true so the placeholders draw the full-height .ai-mini box
    // rather than the shorter fixture tile - reserving the height the real
    // cards will need is the entire job of this pass.
    renderAiPitchInto('mgrPitch', 'mgrBench', squad, null, { upcoming: true });
    renderAiPitchInto('aiPitch', 'aiBench', squad, null, { upcoming: true });
}

// Data has arrived: stop blanking the text. Idempotent, so every load path can
// call it without checking.
function clearAiSkeleton(id) {
    const el = document.getElementById(id);
    if (el) el.classList.remove('ai-skeleton');
}

// Shared by the Best XI and AI Manager tabs - same card, same layout.
// `opts` carries the two things a card cannot work out for itself: whether
// this gameweek is still to be played, and which chip is active - the latter
// because actual_points is the player's raw score and a tripled captain is only
// tripled by the chip. live_overlay applies the same multiplier when it totals
// the squad, so the pitch and the points container agree by construction rather
// than by coincidence.
// The eleven that actually counted, which is not always the eleven that was
// picked. A starter who recorded no minutes is replaced by the first bench
// player who did, so the pitch shows him ON it and the man he replaced below -
// the same swap the total was built from, and the same one the official app
// shows once a round settles.
//
// Only ever applied to a substitution that is already FINAL: the server marks
// one only once the club's fixtures are over (see live_overlay), so this never
// shuffles the formation around mid-match. The arrows on both cards stay, so
// the change is explained rather than just silently applied.
// The armband is normalised here too, and only once the round has been scored.
// `wore_armband` is set on exactly one player then, and it is the vice-captain
// on any week the captain didn't play - so the C moves to whoever's points were
// actually doubled. Decided for the squad rather than per card, because a card
// cannot see that someone else has the armband: a captain who blanked without
// being substituted still looks like the captain from inside his own card, and
// judging it there put a C on him AND on the vice who really wore it.
function effectiveLineup(squad) {
    const armband = squad.find(p => p.wore_armband);
    return squad.map(p => ({
        ...p,
        starting: p.auto_sub_in ? true : (p.auto_sub_out ? false : p.starting),
        is_captain: armband ? p === armband : p.is_captain,
        is_vice_captain: armband ? false : p.is_vice_captain,
    }));
}

function renderAiPitchInto(pitchId, benchId, rawSquad, gameweek, opts) {
    const squad = effectiveLineup(rawSquad);
    const starters = squad.filter(p => p.starting);
    const bench = squad.filter(p => !p.starting);
    document.getElementById(pitchId).innerHTML =
        ['GK', 'DEF', 'MID', 'FWD'].map(pos => {
            const line = starters.filter(p => p.pos === pos);
            return line.length
                ? `<div class="pitch-row">${line.map(p => aiPlayerCard(p, false, gameweek, opts)).join('')}</div>`
                : '';
        }).join('');
    document.getElementById(benchId).innerHTML =
        `<div class="bench-label">Bench</div>
         <div class="bench-row">${bench.map(p => aiPlayerCard(p, true, gameweek, opts)).join('')}</div>`;

    // Read-only: the modal opens with `owned` false and no squad to transfer
    // into, so it shows fixtures, form and the profile link and offers no
    // buttons. That is exactly the right pop-up here - the AI's team isn't
    // yours to edit.
    const byId = {};
    squad.forEach(p => { byId[p.id] = p; });
    [pitchId, benchId].forEach(id =>
        document.getElementById(id).querySelectorAll('.player[data-ai-id]').forEach(el =>
            el.addEventListener('click', () => {
                const p = byId[+el.dataset.aiId];
                if (p) openPlayerModal(p, false, { readOnly: true });
            })));
}

function renderAiSquadTable(squad) {
    document.getElementById('aiSquadBody').innerHTML = squad.map(p => {
        const arm = p.is_captain ? ' <span class="ai-arm">C</span>'
                  : (p.is_vice_captain ? ' <span class="ai-arm vice">V</span>' : '');
        return `<tr class="${p.starting ? '' : 'ai-benched'}">
            <td class="ps-name">${shirtImg(p.team_code, p.pos, 'shirt-sm')}<span>${p.web_name}</span>${arm}</td>
            <td>${p.pos || ''}</td>
            <td>${p.team_name || ''}</td>
            <td>${p.cost != null ? p.cost.toFixed(1) : '–'}</td>
            <td>${p.predicted != null ? p.predicted.toFixed(1) : '–'}</td>
            <td>${p.actual_points != null ? p.actual_points : '–'}</td>
        </tr>`;
    }).join('');
}

function renderAiChips(d) {
    const el = document.getElementById('aiChips');
    const spare = (d.budget != null && d.squad_cost != null)
        ? (d.budget - d.squad_cost) : null;
    // Two sets, same split as My Team.
    //
    // A gameweek already played is a result: what the squad was projected to
    // score and what it actually scored. What it cost to assemble and what was
    // left over were decisions, and they were taken weeks ago - stating them
    // over a finished round invites reading a budget as though it were still
    // there to spend. (A stored snapshot carries no team rating either, by
    // design: ratings move nightly, so one derived now would describe today's
    // players rather than the squad as picked.)
    //
    // The upcoming gameweek is the opposite: the budget is the interesting part
    // and there is no score yet, so a GW points container would be a dash
    // occupying a slot.
    if (!aiGwIsUpcoming(d.gameweek, aiBounds, d)) {
        el.innerHTML =
              chip('Predicted', d.predicted_points != null ? d.predicted_points.toFixed(1) : '–', true)
            + chip('GW points', d.actual_points != null ? d.actual_points : '–',
                   false, d.provisional ? PROVISIONAL_TIP : '');
        return;
    }
    el.innerHTML =
          chip('Squad cost', d.squad_cost != null ? '£' + d.squad_cost.toFixed(1) + 'm' : '–')
        + chip('Unspent', spare != null ? '£' + spare.toFixed(1) + 'm' : '–')
        + chip('Predicted', d.predicted_points != null ? d.predicted_points.toFixed(1) : '–', true)
        + ratingChip(d.team_rating);
}

function loadAi(gw) {
    const stateEl = document.getElementById('aiState');
    const content = document.getElementById('aiContent');
    const q = gw ? `?gameweek=${gw}` : '';
    stateEl.classList.add('d-none');
    // Step the label immediately rather than on response: otherwise two
    // quick taps on the arrow both read the pre-fetch gameweek and ask
    // for the same one twice.
    const seq = ++aiReqSeq;
    if (gw) { aiGw = gw; updateAiNav(); }
    fetch(`/api/ai/best_xi${q}`)
        .then(r => r.json())
        .then(d => {
            if (seq !== aiReqSeq) return;   // superseded by a newer request
            if (!d.available) {
                content.classList.add('d-none');
                stateEl.textContent = d.detail || 'No AI squad available.';
                stateEl.classList.remove('d-none');
                if (d.gameweek) { aiGw = d.gameweek; updateAiNav(); }
                return;
            }
            aiGw = d.gameweek;
            updateAiNav();
            content.classList.remove('d-none');
            clearAiSkeleton('aiContent');
            renderAiChips(d);
            // The Best XI plays no chips, so there is none to pass.
            renderAiPitch(d.squad, d.gameweek,
                          { upcoming: aiGwIsUpcoming(d.gameweek, aiBounds, d) });
            renderAiSquadTable(d.squad);
        })
        .catch(() => {
            if (seq !== aiReqSeq) return;
            content.classList.add('d-none');
            stateEl.textContent = 'Couldn’t load the AI squad.';
            stateEl.classList.remove('d-none');
        });
}

function updateAiNav() {
    document.getElementById('aiGwLabel').textContent = aiGw ? `GW${aiGw}` : 'GW–';
    document.getElementById('aiPrev').disabled = !aiGw || aiGw <= aiBounds.min;
    document.getElementById('aiNext').disabled = !aiGw || (aiBounds.max != null && aiGw >= aiBounds.max);
}
document.getElementById('aiPrev').addEventListener('click', () => { if (aiGw > aiBounds.min) loadAi(aiGw - 1); });
document.getElementById('aiNext').addEventListener('click', () => { if (aiBounds.max == null || aiGw < aiBounds.max) loadAi(aiGw + 1); });

// How many track-record rows are drawn before the "Show all" button. Matches
// TRACK_RECORD_ROWS in seo_tables.py, so the server-rendered table and the one
// the script replaces it with are the same length and nothing jumps on load.
//
// Capped at all because these tables grow by a row a week: by May they are
// thirty-eight rows apiece, two of them stacked under a full pitch, and on a
// phone that was the longest thing on the page. Newest first, so the cap hides
// the oldest gameweeks rather than the ones anyone is looking for.
const TRACK_RECORD_ROWS = 10;

// Same expand/collapse the rotation pairs use. Appended after the table rather
// than inside it so it isn't a seventh column, and it removes itself when there
// is nothing left to show.
function attachRowToggle(tbody, total, render) {
    const host = tbody.closest('.ps-list') || tbody.closest('table');
    const existing = host && host.parentNode
        && host.parentNode.querySelector('.history-toggle');
    if (existing) existing.remove();
    if (!host || !host.parentNode || total <= TRACK_RECORD_ROWS) return;

    const btn = document.createElement('button');
    btn.className = 'btn btn-outline-primary btn-sm mt-2 history-toggle';
    let expanded = false;
    const paint = () => {
        btn.textContent = expanded ? 'Show less' : `Show all (${total})`;
        btn.setAttribute('aria-expanded', expanded ? 'true' : 'false');
    };
    btn.addEventListener('click', () => {
        expanded = !expanded;
        render(expanded);
        paint();
    });
    paint();
    host.parentNode.insertBefore(btn, host.nextSibling);
}

function loadAiHistory() {
    fetch('/api/ai/history').then(r => r.json()).then(d => {
        const body = document.getElementById('aiHistoryBody');
        const rows = d.snapshots || [];
        if (!rows.length) {
            body.innerHTML = '<tr><td colspan="6" class="text-muted small p-2">'
                + 'No gameweeks recorded yet &mdash; the first snapshot is frozen when the next deadline passes.</td></tr>';
            return;
        }
        const rowHtml = s => {
            const diff = (s.actual_points != null && s.predicted_points != null)
                ? (s.actual_points - s.predicted_points) : null;
            const diffCls = diff == null ? '' : (diff >= 0 ? 'ai-over' : 'ai-under');
            return `<tr>
                <td>GW${s.gameweek}</td>
                <td>${s.formation}</td>
                <td>£${s.squad_cost.toFixed(1)}m</td>
                <td>${s.predicted_points.toFixed(1)}</td>
                <td>${s.actual_points != null ? s.actual_points : '<span class="text-muted">pending</span>'}</td>
                <td>${diff == null ? '–' : `<span class="${diffCls}">${diff >= 0 ? '+' : ''}${diff.toFixed(1)}</span>`}</td>
            </tr>`;
        };
        const render = all => {
            body.innerHTML = (all ? rows : rows.slice(0, TRACK_RECORD_ROWS))
                .map(rowHtml).join('');
        };
        render(false);
        attachRowToggle(body, rows.length, render);
    }).catch(() => {});
}

function ensureAi() {
    if (aiLoaded) return;
    // Latch only once the clock has actually answered. Setting it up front
    // means a single failed request leaves the view permanently blank, because
    // every later visit short-circuits on a load that never happened.
    // The season clock decides which gameweek to open on, and caps the
    // forward arrow at the one currently being picked.
    fetch('/api/ai/status').then(r => r.json()).then(s => {
        aiLoaded = true;
        aiBounds.max = s.next_gameweek || s.current_gameweek || null;
        loadAi(s.next_gameweek || s.current_gameweek || null);
    }).catch(() => loadAi(null));
    loadAiHistory();
}


// =====================================================================
//  AI MANAGER
// =====================================================================
let mgrGw = null, mgrBounds = { min: 1, max: null }, mgrLoaded = false, mgrSeq = 0;

// Is this gameweek's decision still provisional? A stored week has been
// committed by the deadline watcher and cannot change; a simulated one is
// re-solved on every data refresh, so everything on the page - the chip, the
// transfers, the eleven - is what the bot WOULD do if the deadline were now.
function mgrIsPreview(d) {
    return d && d.stored === false;
}

function renderMgrPreview(d) {
    const el = document.getElementById('mgrPreview');
    if (!el) return;
    if (!mgrIsPreview(d)) { el.classList.add('d-none'); return; }
    // The deadline in the reader's own timezone. Absent when the season clock
    // couldn't be reached, in which case the sentence still works without it.
    let when = '';
    if (d.deadline) {
        const dt = new Date(d.deadline);
        if (!isNaN(dt)) {
            when = ' the GW' + d.gameweek + ' deadline (' + dt.toLocaleString([], {
                weekday: 'short', day: 'numeric', month: 'short',
                hour: '2-digit', minute: '2-digit' }) + ')';
        }
    }
    el.innerHTML = `<strong>GW${d.gameweek} preview.</strong> Nothing here is committed yet. `
        + `The bot re-runs this every time the projections are rebuilt, and the squad `
        + `it actually plays is the one it commits shortly before${when || ' the deadline'} `
        + `&mdash; so the chip and the transfers below can still change.`;
    el.classList.remove('d-none');
}

const CHIP_MOVE_LABEL = { wildcard: 'Wildcard rebuild', freehit: 'Free Hit side' };

function renderMgrMoves(d) {
    const el = document.getElementById('mgrMoves');
    const moves = d.transfers || [];
    // A chip week is not a quiet week. A wildcard replaces the squad outright
    // and a free hit fields a rented one, so neither goes through the transfer
    // evaluator - which is where `transfers` used to come from, and why the
    // busiest weeks of the bot's season reported that it had done nothing.
    const chip = d.active_chip || d.chip;
    const heading = CHIP_MOVE_LABEL[chip];
    if (!moves.length) {
        el.innerHTML = heading
            ? `<p class="text-muted small mb-0">${heading}: the squad came back unchanged &mdash; `
              + 'nothing in the pool beat what it already owns.</p>'
            : '<p class="text-muted small mb-0">No transfer was worth making &mdash; '
              + 'nothing cleared the projected-gain threshold, so the free transfer is banked.</p>';
        return;
    }
    const intro = heading
        ? `<p class="text-muted small mb-2">${heading} &mdash; ${moves.length} change`
          + `${moves.length === 1 ? '' : 's'}, no hit taken.</p>`
        : '';
    el.innerHTML = intro + moves.map(t => `
        <div class="transfer-rec">
            <div class="transfer-line">
                <span class="tr-out">${t.out}</span>
                <span class="tr-arrow">&rarr;</span>
                <span class="tr-in">${t.in}</span>
            </div>
            <div class="transfer-meta">
                ${t.free ? '<span class="ft-tag free">free</span>'
                         : '<span class="ft-tag hit">-4 hit</span>'}
                ${t.gain != null
                    ? `<span>${t.gain > 0 ? '+' : ''}${t.gain} projected</span>` : ''}
            </div>
            <div class="mgr-why">${t.rationale || ''}</div>
        </div>`).join('');
}

const CHIP_NAMES = { bboost: 'Bench Boost', '3xc': 'Triple Captain',
                     wildcard: 'Wildcard', freehit: 'Free Hit' };

function renderMgrChipPlan(d) {
    const el = document.getElementById('mgrChipPlan');
    const plan = d.chip_plan;
    if (!plan) { el.innerHTML = '<p class="text-muted small mb-0">No chip data recorded.</p>'; return; }

    // Same chip cards as My Team, and in the same place - above the pitch, so
    // the squad and the chips available to it read as one block.
    const used = plan.used || [];
    const available = plan.available || [];
    const bar = document.getElementById('mgrChipsBar');
    if (bar) {
        bar.innerHTML = Object.keys(CHIP_NAMES).map(key => {
            const isAvailable = available.includes(key) && !used.includes(key);
            const playing = d.chip === key;
            return `<div class="chip-card ${isAvailable ? 'chip-avail' : 'chip-unavail'}${playing ? ' chip-playing' : ''}" data-i="${key}">
                <img class="chip-img" src="/static/${key}.svg" alt="${CHIP_NAMES[key]}"
                     data-onerror="invisible">
                <div class="chip-card-name">${CHIP_NAMES[key]}</div>
                <div class="chip-status">${playing ? 'Playing' : (isAvailable ? 'Available' : 'Used')}</div>
            </div>`;
        }).join('');
    }

    let html = '';
    if (d.chip) html += `<div class="mgr-chip-play">Playing <strong>${CHIP_NAMES[d.chip] || d.chip}</strong> this gameweek</div>`;
    html += (plan.notes || []).map(n => `
        <div class="mgr-chip-note ${n.ready ? 'ready' : ''}">
            <span class="mgr-chip-name">${CHIP_NAMES[n.chip] || n.chip}</span>
            <span class="mgr-chip-detail">${n.detail}</span>
        </div>`).join('');

    // The plan for the chips it is NOT playing. Every one has to be spent
    // before the gameweek they all reset on, so where the rest are going is
    // the more interesting half of the decision.
    const sched = (plan.schedule || []).filter(s => s.chip !== d.chip);
    if (sched.length) {
        html += '<div class="mgr-chip-schedule">Planned: '
            + sched.map(s => `${CHIP_NAMES[s.chip] || s.chip} in GW${s.gameweek}`).join(', ')
            + (plan.deadline ? ` — all of them reset after GW${plan.deadline}.` : '')
            + '</div>';
    }
    const up = plan.upcoming || [];
    if (up.length) {
        html += '<div class="mgr-upcoming">Watching: '
            + up.map(o => `GW${o.gameweek} ${o.is_double ? 'double' : 'blank'}`).join(', ')
            + '</div>';
    }
    el.innerHTML = html || '<p class="text-muted small mb-0">All chips held.</p>';
}

function loadMgr(gw) {
    const stateEl = document.getElementById('mgrState');
    const content = document.getElementById('mgrContent');
    const seq = ++mgrSeq;
    if (gw) { mgrGw = gw; updateMgrNav(); }
    stateEl.classList.add('d-none');
    fetch(`/api/ai/manager${gw ? '?gameweek=' + gw : ''}`)
        .then(r => r.json())
        .then(d => {
            if (seq !== mgrSeq) return;
            if (!d.available) {
                content.classList.add('d-none');
                document.getElementById('mgrPreview').classList.add('d-none');
                stateEl.textContent = d.detail || 'No AI Manager data.';
                stateEl.classList.remove('d-none');
                if (d.gameweek) { mgrGw = d.gameweek; updateMgrNav(); }
                return;
            }
            mgrGw = d.gameweek; updateMgrNav();
            content.classList.remove('d-none');
            clearAiSkeleton('mgrContent');
            const value = d.squad_cost != null ? d.squad_cost : d.value;
            const tip = d.provisional ? PROVISIONAL_TIP : '';
            const mgrChipsEl = document.getElementById('mgrChips');
            // The same two sets My Team uses, container for container, so the
            // bot's week and yours can be read side by side without first
            // working out which page calls which number what.
            if (!aiGwIsUpcoming(d.gameweek, mgrBounds, d)) {
                mgrChipsEl.innerHTML =
                      chip('GW points', d.points != null ? d.points : '–', false, tip)
                    + chip('Predicted', d.predicted_points != null ? d.predicted_points.toFixed(1) : '–', true)
                    + chipPlayedChip(d.active_chip || d.chip);
            } else {
                mgrChipsEl.innerHTML =
                      chip('Total points', d.total_points != null ? d.total_points : '–', false, tip)
                    + ratingChip(d.team_rating)
                    + chip('Predicted', d.predicted_points != null ? d.predicted_points.toFixed(1) : '–', true)
                    + chip('Squad value', value != null ? '£' + value.toFixed(1) + 'm' : '–')
                    + bankChip(d.bank)
                    + chip('Free transfers', d.free_transfers != null ? d.free_transfers : '–')
                    + costChip(d.hits);
            }
            renderAiPitchInto('mgrPitch', 'mgrBench', d.squad || [], d.gameweek,
                              { upcoming: aiGwIsUpcoming(d.gameweek, mgrBounds, d),
                                chip: d.active_chip || d.chip });
            renderMgrPreview(d);
            renderMgrMoves(d);
            renderMgrChipPlan(d);
        })
        .catch(() => {
            if (seq !== mgrSeq) return;
            content.classList.add('d-none');
            stateEl.textContent = 'Couldn’t load the AI Manager.';
            stateEl.classList.remove('d-none');
        });
}

function updateMgrNav() {
    document.getElementById('mgrGwLabel').textContent = mgrGw ? `GW${mgrGw}` : 'GW–';
    document.getElementById('mgrPrev').disabled = !mgrGw || mgrGw <= mgrBounds.min;
    document.getElementById('mgrNext').disabled = !mgrGw || (mgrBounds.max != null && mgrGw >= mgrBounds.max);
}
document.getElementById('mgrPrev').addEventListener('click', () => { if (mgrGw > mgrBounds.min) loadMgr(mgrGw - 1); });
document.getElementById('mgrNext').addEventListener('click', () => { if (mgrBounds.max == null || mgrGw < mgrBounds.max) loadMgr(mgrGw + 1); });

function loadMgrHistory() {
    fetch('/api/ai/manager/history').then(r => r.json()).then(d => {
        const body = document.getElementById('mgrHistoryBody');
        const rows = d.history || [];
        if (!rows.length) {
            body.innerHTML = '<tr><td colspan="6" class="text-muted small p-2">'
                + 'No gameweeks played yet &mdash; the bot commits its first squad '
                + 'when the next deadline passes.</td></tr>';
            return;
        }
        const rowHtml = h => `
            <tr>
                <td>GW${h.gameweek}</td>
                <td>${h.value != null ? '£' + h.value.toFixed(1) + 'm' : '–'}</td>
                <td>${h.bank != null ? '£' + h.bank.toFixed(1) + 'm' : '–'}</td>
                <td>${h.active_chip || '–'}</td>
                <td>${h.predicted_points != null ? h.predicted_points.toFixed(1) : '–'}</td>
                <td>${h.points != null ? h.points : '<span class="text-muted">pending</span>'}</td>
            </tr>`;
        const render = all => {
            body.innerHTML = (all ? rows : rows.slice(0, TRACK_RECORD_ROWS))
                .map(rowHtml).join('');
        };
        render(false);
        attachRowToggle(body, rows.length, render);
    }).catch(() => {});
}

function ensureMgr() {
    if (mgrLoaded) return;
    // Same reasoning as ensureAi: latch on success, not on intent.
    fetch('/api/ai/status').then(r => r.json()).then(s => {
        mgrLoaded = true;
        mgrBounds.max = s.next_gameweek || s.current_gameweek || null;
        loadMgr(s.next_gameweek || s.current_gameweek || null);
    }).catch(() => loadMgr(null));
    loadMgrHistory();
}

// (Preseason/in-season is decided server-side from the first gameweek
// deadline — see detect_mode() — so there's no toggle to render here.)

// ---- Initial load ----
// First, and synchronously. app.js is a blocking script at the end of <body>,
// so anything done here happens before the browser's first paint - which is the
// whole point of the AI skeletons: they have to be in the layout in the frame
// the reader first sees, not one network round trip later.
renderAiSkeletons();
restoreView();
// Hidden here rather than inside loadTeam()'s callback: that lands a network
// round trip later, by which point the browser has painted the explainer and
// removing it is a visible jump.
if (getSavedId()) { showToolIntro(false); loadTeam(); } else showPrompt();
ensurePlayers().then(() => playersTabSearch.refresh());
loadRotation();
loadNews();
