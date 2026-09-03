/* Helpers shared by app.js and compare.js.
 *
 * These were copied into both and had already drifted: the "no difficulty"
 * colour was #eee in one file and hsl(210, 8%, 88%) in the other. Escaping
 * drifting the same way would be a security divergence rather than a cosmetic
 * one, which is why it lives here now.
 *
 * Loaded before either script on every page that uses one.
 */

'use strict';

// Every string from the FPL API passes through here before reaching innerHTML.
// The CSP blocks an injected handler from running; this stops markup from
// other people's league and player names tearing the page apart regardless.
function esc(value) {
    if (value === null || value === undefined) return '';
    return String(value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

// Grey, not green: a blank gameweek is "no fixture", not "an easy one".
// Mirrored by colour() in seo_tables.py - change the two together.
const NO_DIFFICULTY_COLOUR = 'hsl(210, 8%, 88%)';

// Green through amber to red across the difficulty range.
function colorFor(value, min, max) {
    if (value == null || !isFinite(value) || max === min) return NO_DIFFICULTY_COLOUR;
    return `hsl(${120 - ((value - min) / (max - min)) * 120}, 70%, 82%)`;
}

// The saved FPL id, read straight from localStorage. app.js has a richer
// getSavedId() that also adopts an id from the URL, and where it exists it
// wins - this is the fallback for the prose pages (a player profile, say) that
// load util.js but not app.js and still want the watchlist star to know whose
// list it is. One key name, defined once, so the two readers cannot drift onto
// different storage.
const FPL_ID_KEY = 'fpl_team_id';
function readSavedFplId() {
    try { return localStorage.getItem(FPL_ID_KEY); } catch (e) { return null; }
}
// The id whoever is looking at this page is identified by, from whichever
// reader the page has. Classic scripts resolve names at call time, so this
// picks app.js's version when it is loaded and falls back otherwise.
function currentFplId() {
    return (typeof getSavedId === 'function') ? getSavedId() : readSavedFplId();
}
