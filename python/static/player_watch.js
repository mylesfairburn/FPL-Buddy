/* The watchlist star on a player's own profile page.
 *
 * The prose pages load util.js but not app.js (see pages/_layout.html), so the
 * pop-up and its Watch button don't exist here - this is the one place the star
 * has to stand on its own. It reuses watchlist.js wholesale: the star is an
 * ordinary .wl-star button, so bindWatchStars wires its click and toggleWatch
 * repaints it exactly as it does for a star in a table. The button on a profile
 * and the button in a table are the same control, backed by the same state and
 * the same endpoint.
 *
 * Loaded AFTER util.js and watchlist.js; both resolve at call time through the
 * global scope classic scripts share.
 */

'use strict';

(function () {
    const btn = document.querySelector('.player-title-row .wl-star');
    if (!btn) return;
    const code = btn.dataset.code === '' ? null : +btn.dataset.code;
    if (code === null) return;   // no season-stable code, nothing to watch

    // Wire the click the same way every table does.
    bindWatchStars(document);

    // Server-rendered as unwatched. Once we know whose list this is, repaint it
    // to the truth - which is the whole reason it couldn't be settled on the
    // server. primeWatchedCodes is a single fetch, shared and idempotent.
    primeWatchedCodes().then(function () {
        paintStar(btn, watchedCodes.has(code));
    });
})();
