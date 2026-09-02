/* Runs the JavaScript suites and prints their rows as JSON on stdout.
 *
 * Invoked by tests/suite_js.py rather than by hand, so the results land in the
 * one report the Python runner writes and are gated by the same rule. A second
 * command to run is a command that stops being run.
 *
 * Anything that escapes a suite is reported as a failing row rather than as a
 * crash, matching harness.run() on the Python side: one broken suite should
 * cost its own rows, not the whole file's.
 */

'use strict';

const { ROWS, group, record } = require('./harness');

const SUITES = [
    ['compare.js', './test_compare'],
    ['watchlist.js', './test_watchlist'],
];

(async () => {
    for (const [label, mod] of SUITES) {
        try {
            // eslint-disable-next-line global-require, import/no-dynamic-require
            await require(mod)();
        } catch (err) {
            group(`${label} (suite)`, 'critical');
            record('the suite runs to completion', label,
                   'no exception escapes',
                   `${err && err.name}: ${err && err.message}`,
                   false, 'critical',
                   String((err && err.stack) || '').split('\n').slice(0, 4).join(' | '));
        }
    }
    process.stdout.write(JSON.stringify({ rows: ROWS }));
})().catch(err => {
    process.stdout.write(JSON.stringify({
        rows: [{
            group: 'javascript', name: 'the JS runner starts',
            input: 'node tests/js/run.js', expected: 'no exception',
            actual: `${err && err.message}`, ok: false,
            severity: 'critical', note: '',
        }],
    }));
});
