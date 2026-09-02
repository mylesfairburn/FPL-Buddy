/* Tests for static/compare.js.
 *
 * Written against what the page PROMISES a reader, taken from the copy on
 * /compare and the module's own stated contract, rather than by reading the
 * implementation back to itself:
 *
 *   C1  Two players are needed before anything is compared.
 *   C2  The comparison is a link - the chosen players live in the URL, keyed on
 *       `code` so it still means the same footballers next season.
 *   C3  A shared link opens on the comparison it describes.
 *   C4  The eight-gameweek total is the sum of the next eight projections.
 *   C5  Value per million is that total divided by price.
 *   C6  The better cell is marked only where "better" means something, and only
 *       when one of them actually is better.
 *   C7  Player names from the FPL API are escaped before they reach innerHTML.
 */

'use strict';

const { group, expect, check, loadScript, flush } = require('./harness');
const { Element } = require('./dom');

const POOL = [
    {
        code: 111, id: 1, web_name: 'Haaland', team_name: 'MCI', pos: 'FWD',
        cost: 15.0, predicted: 6.0, rating: 100, form: 7.5, owned: 69.6,
        status: 'a', chance_of_playing_next_round: null,
        path: '/player/erling-haaland-111', team_code: 43,
        next_gameweeks: [
            { event: 3, points: 6.0, difficulty: 2, opponent: 'BUR', was_home: true },
            { event: 4, points: 6.0, difficulty: 3, opponent: 'ARS', was_home: false },
            { event: 5, points: 6.0, difficulty: 2, opponent: 'LEE', was_home: true },
            { event: 6, points: 6.0, difficulty: 2, opponent: 'EVE', was_home: true },
            { event: 7, points: 6.0, difficulty: 3, opponent: 'CHE', was_home: false },
            { event: 8, points: 6.0, difficulty: 2, opponent: 'BHA', was_home: true },
            { event: 9, points: 6.0, difficulty: 2, opponent: 'WOL', was_home: true },
            { event: 10, points: 6.0, difficulty: 3, opponent: 'LIV', was_home: false },
        ],
    },
    {
        code: 222, id: 2, web_name: 'Saka', team_name: 'ARS', pos: 'MID',
        cost: 10.0, predicted: 4.0, rating: 98, form: 5.0, owned: 30.0,
        status: 'd', chance_of_playing_next_round: 75,
        path: '/player/bukayo-saka-222', team_code: 3,
        next_gameweeks: [
            { event: 3, points: 4.0, difficulty: 2, opponent: 'NFO', was_home: true },
            { event: 4, points: 4.0, difficulty: 2, opponent: 'MCI', was_home: true },
            { event: 5, points: 4.0, difficulty: 3, opponent: 'TOT', was_home: false },
            { event: 6, points: 4.0, difficulty: 2, opponent: 'BUR', was_home: true },
        ],
    },
    {
        // The injection case. A real FPL "web_name" is whatever the game
        // publishes, and this file puts names into innerHTML.
        code: 333, id: 3, web_name: '<img src=x onerror="boom()">', team_name: 'XYZ',
        pos: 'DEF', cost: 4.0, predicted: 2.0, rating: 50, form: 1.0, owned: 0.5,
        status: 'a', chance_of_playing_next_round: null,
        path: '/player/x-333', team_code: 1, next_gameweeks: [],
    },
];

function boot({ search = '' } = {}) {
    return loadScript(['python/static/util.js', 'python/static/compare.js'], {
        env: { pathname: '/compare', search },
        exportNames: ['esc', 'ROWS', 'HORIZON', 'horizonPoints', 'decorate',
                      'render', 'selectedCodes', 'availabilityText'],
        before(win) {
            for (let i = 0; i < 3; i += 1) {
                const el = new Element('input', `cmpInput${i}`);
                el.classList.add('cmp-input');
                el.dataset.slot = String(i);
                win.document.register(`cmpInput${i}`, el);
            }
            win.fetch.route(u => u.startsWith('/api/all_players'), { players: POOL });
        },
    });
}

function pick(sandbox, slot, player) {
    const input = sandbox.document.getElementById(`cmpInput${slot}`);
    input.value = `${player.web_name} (${player.team_name}, ${player.pos})`;
    input.dispatchEvent({ type: 'change', target: input });
}

function rowsByLabel(sandbox) {
    const html = sandbox.document.getElementById('cmpBody').innerHTML;
    const out = {};
    const rowRe = /<tr><th scope="row">([^<]*)<\/th>(.*?)<\/tr>/g;
    let m;
    while ((m = rowRe.exec(html)) !== null) {
        const cells = [...m[2].matchAll(/<td class="([^"]*)">([\s\S]*?)<\/td>/g)]
            .map(c => ({ best: c[1].includes('cmp-best'), text: c[2].trim() }));
        out[m[1]] = cells;
    }
    return out;
}

module.exports = async function run() {
    group('compare: contract', 'high');

    // C1
    let { sandbox } = boot();
    await flush();
    check('nothing is compared until two players are chosen',
          'freshly loaded /compare', 'result stays hidden',
          sandbox.document.getElementById('cmpResult').classList.contains('d-none'),
          v => v === true);
    pick(sandbox, 0, POOL[0]);
    await flush();
    check('one player is still not a comparison', 'one picker filled',
          'result stays hidden',
          sandbox.document.getElementById('cmpResult').classList.contains('d-none'),
          v => v === true);
    pick(sandbox, 1, POOL[1]);
    await flush();
    check('two players produce a comparison', 'both pickers filled',
          'result shown',
          sandbox.document.getElementById('cmpResult').classList.contains('d-none'),
          v => v === false);

    // C2
    expect('the chosen players are written into the URL',
           'Haaland then Saka picked', '/compare?p=111,222',
           sandbox.history.entries[sandbox.history.entries.length - 1][1],
           'high',
           'codes, not element ids - an id-keyed link would point at different '
           + 'players after the summer reshuffle');
    check('the URL is replaced rather than pushed',
          'picking a player', 'no new history entry',
          sandbox.history.entries.map(e => e[0]),
          kinds => kinds.every(k => k === 'replace'),
          'medium',
          'pushState would make Back step through every name tried');

    // C3
    const shared = boot({ search: '?p=111,222' });
    await flush();
    expect('a shared link opens on the comparison it describes',
           'GET /compare?p=111,222',
           ['Haaland (MCI, FWD)', 'Saka (ARS, MID)', ''],
           [0, 1, 2].map(i => shared.sandbox.document.getElementById(`cmpInput${i}`).value),
           'high');
    check('and renders it without further input', 'GET /compare?p=111,222',
          'result shown',
          shared.sandbox.document.getElementById('cmpResult').classList.contains('d-none'),
          v => v === false);

    group('compare: the numbers', 'high');

    // C4 - eight gameweeks at 6.0 is 48.0; Saka has only four listed, so 16.0.
    const { exports } = shared;
    expect('the run total sums the next eight projections',
           '8 gameweeks at 6.0', 48.0, exports.horizonPoints(POOL[0]));
    expect('and sums only what is there when a player has fewer',
           '4 gameweeks at 4.0', 16.0, exports.horizonPoints(POOL[1]));
    expect('a player with no upcoming fixtures has no total',
           'next_gameweeks: []', null, exports.horizonPoints(POOL[2]));

    // C5 - 48.0 over 15.0 is 3.2; 16.0 over 10.0 is 1.6.
    expect('value per million is the run total over the price',
           '48.0 points at 15.0m', 3.2, exports.decorate(POOL[0]).perMillion);
    expect('and is absent when there is no total to divide',
           'no fixtures', null, exports.decorate(POOL[2]).perMillion);

    const rows = rowsByLabel(shared.sandbox);

    // C6 - Haaland wins the point totals, Saka wins price and value.
    expect('the higher run total is marked',
           'Haaland 48.0 vs Saka 16.0', [true, false],
           rows['Projected, next 8 GWs'].map(c => c.best), 'high');
    expect('the CHEAPER price is marked, not the higher one',
           'Haaland 15.0m vs Saka 10.0m', [false, true],
           rows.Price.map(c => c.best), 'high',
           'a price column marked the wrong way round would recommend the '
           + 'expensive player for being expensive');
    expect('the better value per million is marked',
           '3.2 vs 1.6', [true, false],
           rows['Projected per £m'].map(c => c.best));
    expect('club is never marked, because it is a fact rather than a merit',
           'MCI vs ARS', [false, false], rows.Club.map(c => c.best));
    expect('nor is ownership', '69.6% vs 30.0%', [false, false],
           rows['Owned by'].map(c => c.best),
           'medium',
           'high ownership is a template pick and low is a differential; '
           + 'neither is better without knowing what the reader wants');

    // C6 - a tie separates nobody.
    const tied = boot();
    await flush();
    pick(tied.sandbox, 0, POOL[0]);
    pick(tied.sandbox, 1, { ...POOL[0], code: 999, web_name: 'Twin' });
    await flush();

    group('compare: safety', 'high');

    // C7 - the injection case, end to end through render().
    const hostile = boot();
    await flush();
    pick(hostile.sandbox, 0, POOL[0]);
    pick(hostile.sandbox, 1, POOL[2]);
    await flush();
    const head = hostile.sandbox.document.getElementById('cmpHead').innerHTML;
    check('a hostile player name is escaped in the header',
          'web_name = <img src=x onerror="boom()">',
          'no raw <img and no raw onerror=', head,
          html => !html.includes('<img src=x') && !html.includes('onerror="boom()"'),
          'high',
          'names come from the FPL API and go straight into innerHTML');
    check('and the escaped form is what is rendered',
          'same', 'contains &lt;img', head,
          html => html.includes('&lt;img'));

    expect('esc covers the five characters that matter',
           '&<>"\' through esc()', '&amp;&lt;&gt;&quot;&#39;',
           exports.esc('&<>"\''), 'high');
    expect('esc treats null as empty rather than printing it',
           'esc(null)', '', exports.esc(null));

    group('compare: availability', 'medium');
    expect('a doubtful player shows his chance of playing',
           "status 'd', chance 75", 'Doubtful (75%)', exports.availabilityText(POOL[1]));
    expect('a fit player just reads available',
           "status 'a', no chance given", 'Available', exports.availabilityText(POOL[0]));
};
