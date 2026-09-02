/* A minimal DOM, enough to load the site's real scripts under Node.
 *
 * Why this exists rather than jsdom: the project has no build step and no npm
 * dependencies at all - "vanilla JS, no build step" is a stated design choice,
 * and adding a node_modules tree to run three test files would be a bigger
 * change to the project than the tests are worth. This is about 200 lines and
 * has no dependencies.
 *
 * What it is honest about
 * -----------------------
 * Elements here are real objects with real state: setting `innerHTML` stores
 * the string, `classList` genuinely adds and removes, `querySelectorAll` on a
 * container genuinely scans the markup that was assigned to it. That matters,
 * because a stub that returned a shrug for everything would let a test pass
 * while the page was broken - which is the failure mode these tests exist to
 * catch, not to reproduce.
 *
 * What it cannot tell you
 * -----------------------
 * Nothing about layout, CSS, paint or real event dispatch order. It answers
 * "does this code build the right markup and call the right endpoints", which
 * is where the front-end bugs in this project have actually been. Anything
 * visual still has to be checked in the browser pane.
 */

'use strict';

const CLASS_RE = /class\s*=\s*"([^"]*)"/g;

class ClassList {
    constructor(el) { this.el = el; this._set = new Set(); }
    add(...names) { names.forEach(n => n && this._set.add(n)); }
    remove(...names) { names.forEach(n => this._set.delete(n)); }
    contains(name) { return this._set.has(name); }
    toggle(name, force) {
        const on = force === undefined ? !this._set.has(name) : !!force;
        if (on) this._set.add(name); else this._set.delete(name);
        return on;
    }
    get value() { return [...this._set].join(' '); }
    toString() { return this.value; }
}

class Element {
    constructor(tag = 'div', id = '') {
        this.tagName = String(tag).toUpperCase();
        this.id = id;
        this.classList = new ClassList(this);
        this.dataset = {};
        this.style = {};
        this.children = [];
        this.attributes = {};
        this.listeners = {};
        this.value = '';
        this.disabled = false;
        this.type = '';
        this._html = '';
        this._text = '';
        this.focused = false;
        // Non-null so `el.offsetParent !== null` visibility checks pass; the
        // stub has no layout, so everything is treated as visible.
        this.offsetParent = {};
    }

    // Assigning innerHTML replaces any parsed children. Reading it back gives
    // exactly what was assigned, which is what the markup assertions compare.
    set innerHTML(html) {
        this._html = String(html);
        this._text = this._html.replace(/<[^>]*>/g, '');
        this.children = parseFragment(this._html, this);
    }
    get innerHTML() { return this._html; }

    set textContent(text) {
        this._text = String(text);
        this._html = '';
        this.children = [];
    }
    get textContent() {
        if (this.children.length) {
            return this.children.map(c => c.textContent).join('');
        }
        return this._text;
    }

    setAttribute(name, value) {
        this.attributes[name] = String(value);
        if (name === 'id') this.id = String(value);
    }
    getAttribute(name) {
        return Object.prototype.hasOwnProperty.call(this.attributes, name)
            ? this.attributes[name] : null;
    }
    removeAttribute(name) { delete this.attributes[name]; }

    appendChild(child) { this.children.push(child); child.parent = this; return child; }
    remove() {
        if (this.parent) {
            this.parent.children = this.parent.children.filter(c => c !== this);
        }
    }

    addEventListener(type, fn) {
        (this.listeners[type] = this.listeners[type] || []).push(fn);
    }
    dispatchEvent(evt) {
        (this.listeners[evt.type] || []).forEach(fn => fn.call(this, evt));
        // `onclick =` is used in places instead of addEventListener.
        if (evt.type === 'click' && typeof this.onclick === 'function') {
            this.onclick.call(this, evt);
        }
        return true;
    }
    click() { this.dispatchEvent({ type: 'click', target: this, preventDefault() {} }); }
    focus() { this.focused = true; if (this.ownerDocument) this.ownerDocument.activeElement = this; }
    blur() { this.focused = false; }
    scrollIntoView() {}
    getBoundingClientRect() { return { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 }; }
    closest() { return null; }

    querySelectorAll(selector) { return matchIn(this, selector); }
    querySelector(selector) { return matchIn(this, selector)[0] || null; }
}

/* Parse the subset of markup these scripts generate: enough to know what
 * elements exist, what classes they carry and what their data attributes say.
 * Deliberately shallow - it flattens the tree rather than nesting it, because
 * every selector used by the code under test is a class or tag lookup that a
 * flat list answers identically. */
function parseFragment(html, owner) {
    const out = [];
    const tagRe = /<([a-zA-Z][a-zA-Z0-9]*)\b([^>]*)>/g;
    let m;
    while ((m = tagRe.exec(html)) !== null) {
        const [, tag, attrs] = m;
        const el = new Element(tag);
        el.parent = owner;
        CLASS_RE.lastIndex = 0;
        const cls = /class\s*=\s*"([^"]*)"/.exec(attrs);
        if (cls) cls[1].split(/\s+/).filter(Boolean).forEach(c => el.classList.add(c));
        const idm = /\bid\s*=\s*"([^"]*)"/.exec(attrs);
        if (idm) el.id = idm[1];
        let dm;
        const dataRe = /data-([a-zA-Z0-9-]+)\s*=\s*"([^"]*)"/g;
        while ((dm = dataRe.exec(attrs)) !== null) {
            const key = dm[1].replace(/-([a-z])/g, (_, c) => c.toUpperCase());
            el.dataset[key] = dm[2];
        }
        const am = /\baria-([a-zA-Z-]+)\s*=\s*"([^"]*)"/g;
        let aa;
        while ((aa = am.exec(attrs)) !== null) el.attributes['aria-' + aa[1]] = aa[2];
        // The text between this tag and the next one, which is what a cell's
        // textContent needs to report.
        const after = html.slice(m.index + m[0].length);
        const nextTag = after.search(/<[a-zA-Z\/]/);
        el._text = (nextTag === -1 ? after : after.slice(0, nextTag)).trim();
        out.push(el);
    }
    return out;
}

function matchIn(root, selector) {
    const all = [];
    const walk = node => node.children.forEach(c => { all.push(c); walk(c); });
    walk(root);
    return all.filter(el => matches(el, selector));
}

function matches(el, selector) {
    // Only the selector forms these scripts actually use.
    return String(selector).split(',').map(s => s.trim()).some(sel => {
        if (!sel) return false;
        if (sel.startsWith('.')) return el.classList.contains(sel.slice(1));
        if (sel.startsWith('#')) return el.id === sel.slice(1);
        const attr = /^\[([a-zA-Z-]+)\]$/.exec(sel);
        if (attr) return Object.prototype.hasOwnProperty.call(el.attributes, attr[1]);
        return el.tagName === sel.toUpperCase();
    });
}

class Document {
    constructor() {
        this.byId = new Map();
        this.body = new Element('body');
        this.body.ownerDocument = this;
        this.listeners = {};
        this.activeElement = this.body;
        this.title = '';
        this.hidden = false;
    }
    // Elements are created on demand and remembered, so the scripts' many
    // getElementById calls at load time do not each have to be anticipated -
    // but the SAME id always returns the SAME object, so state set on one call
    // is visible on the next. A fresh object each time would quietly break
    // every classList assertion.
    getElementById(id) {
        if (!this.byId.has(id)) {
            const el = new Element('div', id);
            el.ownerDocument = this;
            this.byId.set(id, el);
        }
        return this.byId.get(id);
    }
    register(id, el) { el.ownerDocument = this; this.byId.set(id, el); return el; }
    createElement(tag) {
        const el = new Element(tag);
        el.ownerDocument = this;
        return el;
    }
    querySelectorAll(selector) {
        const pool = [...this.byId.values()];
        const nested = [];
        pool.forEach(el => { const walk = n => n.children.forEach(c => { nested.push(c); walk(c); }); walk(el); });
        return [...pool, ...nested].filter(el => matches(el, selector));
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
    dispatchEvent(evt) { (this.listeners[evt.type] || []).forEach(fn => fn(evt)); return true; }
    contains() { return true; }
}

function makeStorage() {
    const store = new Map();
    return {
        getItem: k => (store.has(String(k)) ? store.get(String(k)) : null),
        setItem: (k, v) => { store.set(String(k), String(v)); },
        removeItem: k => { store.delete(String(k)); },
        clear: () => store.clear(),
    };
}

/* A fetch stub that records every call and answers from a routing table the
 * test installs. An unrouted URL is a loud failure rather than a silent empty
 * object: a test that thinks it exercised an endpoint it never reached is
 * exactly the kind of false pass this harness is for. */
function makeFetch() {
    const calls = [];
    const routes = [];
    const fetchFn = (url, opts) => {
        const call = { url: String(url), opts: opts || {} };
        calls.push(call);
        const hit = routes.find(r => r.test(call.url, call.opts));
        if (!hit) {
            return Promise.reject(new Error(`unrouted fetch: ${call.url}`));
        }
        const body = typeof hit.body === 'function' ? hit.body(call) : hit.body;
        return Promise.resolve({
            ok: true,
            status: 200,
            json: () => Promise.resolve(body),
            text: () => Promise.resolve(JSON.stringify(body)),
        });
    };
    fetchFn.calls = calls;
    fetchFn.route = (test, body) => routes.push({ test, body });
    fetchFn.reset = () => { calls.length = 0; routes.length = 0; };
    return fetchFn;
}

function createEnvironment(opts = {}) {
    const document = new Document();
    const fetchFn = makeFetch();
    const alerts = [];
    const timers = [];

    const win = {
        document,
        location: {
            pathname: opts.pathname || '/',
            search: opts.search || '',
            href: 'http://localhost' + (opts.pathname || '/') + (opts.search || ''),
            reload() {},
        },
        history: {
            entries: [],
            replaceState(state, title, url) { this.entries.push(['replace', url]); win.location.pathname = String(url).split('?')[0]; win.location.search = String(url).includes('?') ? '?' + String(url).split('?')[1] : ''; },
            pushState(state, title, url) { this.entries.push(['push', url]); win.location.pathname = String(url).split('?')[0]; },
            back() {}, forward() {},
        },
        localStorage: makeStorage(),
        sessionStorage: makeStorage(),
        fetch: fetchFn,
        alert: msg => alerts.push(String(msg)),
        setInterval: (fn, ms) => { timers.push({ fn, ms, kind: 'interval' }); return timers.length; },
        clearInterval: () => {},
        setTimeout: (fn, ms) => { timers.push({ fn, ms, kind: 'timeout' }); return timers.length; },
        clearTimeout: () => {},
        matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
        scrollTo() {},
        addEventListener(type, fn) { document.addEventListener(type, fn); },
        requestAnimationFrame: fn => { timers.push({ fn, ms: 0, kind: 'raf' }); return timers.length; },
        navigator: { userAgent: 'node-test' },
        console,
        URLSearchParams,
        Promise,
        JSON,
        Math,
        Date,
        Set,
        Map,
        Array,
        Object,
        String,
        Number,
        isFinite,
        parseInt,
        parseFloat,
        Error,
        RegExp,
        Event: class { constructor(type) { this.type = type; } },
        KeyboardEvent: class { constructor(type, init) { Object.assign(this, init || {}); this.type = type; this.preventDefault = () => {}; } },
    };
    win.window = win;
    win.globalThis = win;
    win.self = win;
    win.alerts = alerts;
    win.timers = timers;
    document.defaultView = win;
    return win;
}

module.exports = { createEnvironment, Element, Document, matches };
