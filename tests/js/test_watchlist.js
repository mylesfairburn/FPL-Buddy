/* Tests for static/watchlist.js.
 *
 * Written against what the feature promises, not against how it is written:
 *
 *   W1  Nothing can be watched without an FPL ID, because the list is stored
 *       against one.
 *   W2  The Watch button reflects whether the player is already on the list,
 *       the first time it is opened as well as after a change.
 *   W3  Watching posts, un-watching deletes, and both hit the id's own list.
 *   W4  A refusal from the server (the size cap) is surfaced, not swallowed.
 *   W5  A player who has left the game keeps his row rather than vanishing.
 *   W6  Names from the FPL API are escaped before they reach innerHTML.
 *   W7  Removing a row calls the delete endpoint for that player.
 */

'use strict';

const { group, expect, check, loadScript, flush } = require('./harness');
const { Element } = require('./dom');

const WATCHED = [
    {
        code: 111, available: true, web_name: 'Haaland', team_name: 'MCI',
        pos: 'FWD', cost: 15.0, predicted: 6.1, note: 'the obvious one',
        path: '/player/erling-haaland-111', team_code: 43,
        next_gameweeks: [{ event: 3, points: 6.1, difficulty: 2, opponent: 'BUR' }],
    },
    {
        // Left the league: the pool no longer carries him, so the server sends
        // the row back flagged rather than dropping it.
        code: 222, available: false, note: 'gone abroad',
    },
    {
        code: 333, available: true, web_name: '<script>boom()</script>',
        team_name: 'XYZ', pos: 'DEF', cost: 4.0, predicted: 1.0,
        note: '<b>injected</b>', path: '/player/x-333', team_code: 1,
        next_gameweeks: [],
    },
];

function boot({ savedId = '1234567', players = WATCHED } = {}) {
    return loadScript(['python/static/util.js', 'python/static/watchlist.js'], {
        exportNames: ['loadWatchlist', 'renderWatchlist', 'watchlistRow',
                      'removeFromWatchlist', 'toggleWatch', 'updateWatchButton',
                      'primeWatchedCodes', 'watchedCodes'],
        before(win) {
            // What watchlist.js takes from app.js. Stubbed rather than loading
            // all of app.js, which needs a whole document around it. esc()
            // comes from the real util.js, loaded above.
            win.getSavedId = () => savedId;
            win.fixtureTile = g => `<span class="mini-gw">${g.opponent}</span>`;
            win.shirtImg = () => '<img class="shirt-sm">';
            win.fetch.route(u => /^\/api\/watchlist\/\d+$/.test(u) && true,
                            { available: true, players });
            win.fetch.route(u => /^\/api\/watchlist\/\d+\/\d+$/.test(u), { ok: true });
        },
    });
}

const bodyHtml = s => s.document.getElementById('watchlistBody').innerHTML;

module.exports = async function run() {
    group('watchlist: without an ID', 'high');

    let { sandbox, exports } = boot({ savedId: null });
    await exports.loadWatchlist();
    await flush();
    check('the list asks for an FPL ID instead of loading',
          'no id in localStorage', 'a prompt mentioning the FPL ID',
          bodyHtml(sandbox), html => /FPL ID/i.test(html), 'high');
    expect('and no request is made for it', 'no id', [],
           sandbox.fetch.calls.map(c => c.url), 'high',
           'without an id there is no list to fetch, and /api/watchlist/null '
           + 'would be a 422');

    group('watchlist: loading', 'high');

    ({ sandbox, exports } = boot());
    await exports.loadWatchlist();
    await flush();
    expect('the list is fetched for the saved id', 'id 1234567',
           ['/api/watchlist/1234567'], sandbox.fetch.calls.map(c => c.url));

    const html = bodyHtml(sandbox);
    check('a watched player is listed by name', 'three entries',
          'Haaland appears', html, h => h.includes('Haaland'));
    check('with his price', 'three entries', '£15.0m shown', html,
          h => h.includes('£15.0m'));
    check('and his own note', 'note = the obvious one', 'note shown', html,
          h => h.includes('the obvious one'));

    // W5
    check('a player who has left the game keeps his row',
          'available: false', 'the row is still rendered', html,
          h => h.includes('no longer in the game'), 'high',
          'dropping it would look like the site had lost the entry rather '
          + 'than the player having gone');
    check('and his note survives with him', 'available: false',
          'gone abroad shown', html, h => h.includes('gone abroad'));

    // W6
    check('a hostile player name is escaped',
          'web_name = <script>boom()</script>',
          'no raw <script in the markup', html,
          h => !h.includes('<script>boom()'), 'high');
    check('and so is a hostile note', 'note = <b>injected</b>',
          'no raw <b>injected', html, h => !h.includes('<b>injected</b>'), 'high');
    check('the escaped forms are what appear', 'same',
          'contains &lt;script&gt;', html, h => h.includes('&lt;script&gt;'));

    // W7 - one remove control per entry, each carrying its own code.
    const removes = sandbox.document.getElementById('watchlistBody')
        .querySelectorAll('.wl-remove');
    expect('every entry has a remove control', 'three entries',
           3, removes.length);
    expect('each one carries its own player code', 'three entries',
           ['111', '222', '333'], removes.map(r => r.dataset.code));

    group('watchlist: changing it', 'high');

    // W7
    ({ sandbox, exports } = boot());
    await exports.loadWatchlist();
    await flush();
    sandbox.fetch.calls.length = 0;
    sandbox.document.getElementById('watchlistBody')
        .querySelectorAll('.wl-remove')[0].click();
    await flush();
    const del = sandbox.fetch.calls[0];
    expect('removing a row deletes that player from that id',
           'clicking the remove control on Haaland',
           '/api/watchlist/1234567/111', del.url, 'high');
    expect('using DELETE', 'same', 'DELETE', del.opts.method, 'high');

    // W3 - watching a player who is not on the list POSTs.
    ({ sandbox, exports } = boot({ players: [] }));
    await exports.primeWatchedCodes();
    await flush();
    sandbox.fetch.calls.length = 0;
    exports.toggleWatch({ code: 777, web_name: 'New' });
    await flush();
    let call = sandbox.fetch.calls[0];
    expect('watching a new player posts to the list', 'toggleWatch(code 777)',
           '/api/watchlist/1234567', call.url, 'high');
    expect('with POST', 'same', 'POST', call.opts.method, 'high');
    expect('and the player code in the body', 'same',
           { code: 777 }, JSON.parse(call.opts.body), 'high');

    // W3 - toggling the same player again deletes.
    ({ sandbox, exports } = boot({ players: [{ code: 777, available: true, web_name: 'New' }] }));
    await exports.primeWatchedCodes();
    await flush();
    sandbox.fetch.calls.length = 0;
    exports.toggleWatch({ code: 777, web_name: 'New' });
    await flush();
    call = sandbox.fetch.calls[0];
    expect('un-watching an already-watched player deletes it',
           'toggleWatch on a player already on the list',
           '/api/watchlist/1234567/777', call.url, 'high');
    expect('with DELETE', 'same', 'DELETE', call.opts.method, 'high');

    group('watchlist: the button', 'high');

    // W2 - the button has to be right the first time it is opened, which is
    // usually before the watchlist tab has ever been looked at.
    ({ sandbox, exports } = boot({ players: [{ code: 111, available: true, web_name: 'Haaland' }] }));
    const btn = sandbox.document.getElementById('pmWatch');
    await exports.primeWatchedCodes();
    await flush();
    exports.updateWatchButton({ code: 111 });
    expect('a player already on the list opens as Watching',
           'pop-up opened before the watchlist tab was ever visited',
           '★ Watching', btn.textContent, 'high');
    expect('and says so to assistive technology', 'same',
           'true', btn.getAttribute('aria-pressed'), 'high');

    exports.updateWatchButton({ code: 999 });
    expect('a player not on the list opens as Watch', 'code 999',
           '☆ Watch', btn.textContent);
    expect('with aria-pressed false', 'code 999', 'false',
           btn.getAttribute('aria-pressed'));

    group('watchlist: failures', 'high');

    // W1
    ({ sandbox, exports } = boot({ savedId: null }));
    exports.toggleWatch({ code: 111, web_name: 'Haaland' });
    await flush();
    check('watching without an ID tells the reader why',
          'no FPL ID saved', 'an alert mentioning the FPL ID',
          sandbox.alerts, a => a.length === 1 && /FPL ID/i.test(a[0]), 'high');
    expect('and makes no request', 'no FPL ID saved', [],
           sandbox.fetch.calls.map(c => c.url));

    // W4 - the server enforces the size cap; a refusal must reach the reader.
    const capped = loadScript(['python/static/util.js', 'python/static/watchlist.js'], {
        exportNames: ['toggleWatch', 'primeWatchedCodes'],
        before(win) {
            win.getSavedId = () => '1234567';
            win.fixtureTile = () => '';
            win.shirtImg = () => '';
            win.fetch.route(u => /^\/api\/watchlist\/\d+$/.test(u) && true, call =>
                (call.opts.method === 'POST'
                    ? { detail: 'A watchlist holds 30 players. Remove one first.' }
                    : { available: true, players: [] }));
        },
    });
    await capped.exports.primeWatchedCodes();
    await flush();
    capped.exports.toggleWatch({ code: 888, web_name: 'One too many' });
    await flush();
    check('a refusal from the server is shown, not swallowed',
          'POST returns a detail message',
          'the reader is told the list is full', capped.sandbox.alerts,
          a => a.length === 1 && /30 players/.test(a[0]), 'high',
          'the cap is enforced server-side, so silence here would look like '
          + 'the button simply not working');
};
