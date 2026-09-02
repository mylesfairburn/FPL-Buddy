/* The JS half of the test harness.
 *
 * Mirrors tests/harness.py deliberately - same row shape, same severities - so
 * the results drop into the one report the Python runner writes rather than
 * being a second set of results somebody has to remember to look at. A test
 * suite nobody runs is worse than no test suite, and a second command to run is
 * how that happens.
 */

'use strict';

const path = require('path');
const fs = require('fs');
const vm = require('vm');

const { createEnvironment } = require('./dom');

const ROWS = [];
let current = { group: '?', severity: 'medium' };

function group(name, severity = 'medium') {
    current = { group: name, severity };
}

function trim(value, limit = 220) {
    let text;
    if (typeof value === 'string') text = value;
    else {
        try { text = JSON.stringify(value); } catch (e) { text = String(value); }
    }
    text = String(text).replace(/\s+/g, ' ');
    if (text.length <= limit) return text;
    const keep = Math.floor((limit - 5) / 2);
    return `${text.slice(0, keep)} ... ${text.slice(-keep)}`;
}

function record(name, input, expected, actual, ok, severity, note) {
    ROWS.push({
        group: current.group,
        name,
        input: trim(input),
        expected: trim(expected),
        actual: trim(actual),
        ok: !!ok,
        severity: severity || current.severity,
        note: note || '',
    });
    return ok;
}

/* Deep equality, so an assertion can be about a whole structure rather than
 * about one field at a time - the same thing `expect` in the Python harness
 * gets for free from ==. */
function same(a, b) {
    if (a === b) return true;
    if (typeof a !== typeof b) return false;
    if (a && b && typeof a === 'object') {
        if (Array.isArray(a) !== Array.isArray(b)) return false;
        const ka = Object.keys(a), kb = Object.keys(b);
        if (ka.length !== kb.length) return false;
        return ka.every(k => same(a[k], b[k]));
    }
    return false;
}

function expect(name, input, expected, actual, severity, note) {
    return record(name, input, expected, actual, same(expected, actual), severity, note);
}

function check(name, input, expectedDesc, actual, predicate, severity, note) {
    let ok;
    try {
        ok = !!predicate(actual);
    } catch (e) {
        ok = false;
        actual = `${e.name}: ${e.message}`;
    }
    return record(name, input, expectedDesc, actual, ok, severity, note);
}

/* Load the site's REAL script files into a fresh stub environment.
 *
 * The shipped files are executed, not copies: a test against a re-typed version
 * of the logic proves only that the copy is self-consistent. `exportNames`
 * becomes an epilogue, because lexical declarations never land on the global
 * object and so cannot be read off the sandbox.
 */
function loadScript(relPaths, { exportNames = [], env = {}, before } = {}) {
    const root = path.resolve(__dirname, '..', '..');
    const paths = Array.isArray(relPaths) ? relPaths : [relPaths];
    // Concatenated into one script so a `const` in an earlier file is in scope
    // for a later one - which is how the browser treats them, and what lets
    // util.js supply esc() to both app.js and compare.js.
    const source = paths
        .map(rel => fs.readFileSync(path.join(root, rel), 'utf8'))
        .join(String.fromCharCode(10) + ';' + String.fromCharCode(10));

    const sandbox = createEnvironment(env);
    if (before) before(sandbox);
    vm.createContext(sandbox);

    const epilogue = exportNames.length
        ? `;globalThis.__exports = { ${exportNames.map(n => `${n}: typeof ${n} !== 'undefined' ? ${n} : undefined`).join(', ')} };`
        : '';
    vm.runInContext(source + String.fromCharCode(10) + epilogue, sandbox,
                    { filename: paths.join(' + ') });
    return { sandbox, exports: sandbox.__exports || {} };
}

/* Let queued promise callbacks run. The scripts under test are full of
 * `.then()` chains, and an assertion made before those have settled is testing
 * the moment before the work happened. */
function flush(times = 6) {
    let p = Promise.resolve();
    for (let i = 0; i < times; i += 1) p = p.then(() => {});
    return p;
}

function summary() {
    const failed = ROWS.filter(r => !r.ok);
    return { total: ROWS.length, passed: ROWS.length - failed.length, failed: failed.length };
}

module.exports = { group, expect, check, record, loadScript, flush, ROWS, summary, same };
