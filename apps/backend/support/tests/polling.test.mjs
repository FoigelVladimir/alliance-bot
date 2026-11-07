import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("../static/support/polling.js", import.meta.url), "utf8");
const { nextPollDelay, startPolling } = await import("data:text/javascript," + encodeURIComponent(source));

test("polling intervals, backoff, and Retry-After", () => {
  assert.equal(nextPollDelay(false, 0, 1000, 0, 2000), 4000);
  assert.equal(nextPollDelay(true, 0, 1000, 0, 2000), 29000);
  assert.equal(nextPollDelay(false, 1, 1000, 0, 2000), 9000);
  assert.equal(nextPollDelay(false, 2, 1000, 0, 2000), 19000);
  assert.equal(nextPollDelay(false, 4, 1000, 0, 2000), 29000);
  assert.equal(nextPollDelay(false, 0, 1000, 81000, 2000), 79000);
});

test("pending reads cannot repaint after mutation, and access loss stops the loop", async (t) => {
  let nextTimer = 0;
  let now = 1000;
  const timers = new Map();
  const documentEvents = {};
  const pending = [];
  const status = {};
  const renders = [];
  const list = {innerHTML: "original", contains: () => false, replaceWith: (value) => renders.push(value.innerHTML)};
  const root = {dataset: {queueUrl: "/support/fragments/tickets/"}, querySelector: () => list, hidden: false};

  function install(key, value) {
    const previous = Object.getOwnPropertyDescriptor(globalThis, key);
    Object.defineProperty(globalThis, key, {value, configurable: true});
    t.after(() => previous ? Object.defineProperty(globalThis, key, previous) : delete globalThis[key]);
  }

  t.mock.method(Date, "now", () => now);
  install("document", {
    hidden: false, querySelector: () => status,
    addEventListener: (name, callback) => { documentEvents[name] = callback; },
    removeEventListener: (name) => { delete documentEvents[name]; },
  });
  install("navigator", {onLine: true});
  install("window", {addEventListener() {}, removeEventListener() {}});
  install("location", {search: "?view=mine&q=test"});
  install("setTimeout", (fn, delay) => { timers.set(++nextTimer, {fn, delay}); return nextTimer; });
  install("clearTimeout", (id) => timers.delete(id));
  install("fetch", (path, options) => new Promise((resolve) => pending.push({path, options, resolve})));
  install("DOMParser", class {
    parseFromString(innerHTML) { return {querySelector: () => ({innerHTML})}; }
  });

  const flush = async () => { for (let i = 0; i < 15; i++) await Promise.resolve(); };
  function tick(delay) {
    const entry = [...timers].find(([, timer]) => timer.delay === delay);
    assert.ok(entry, `Expected timer for ${delay} ms`);
    timers.delete(entry[0]);
    now += delay;
    entry[1].fn();
  }
  function respond(read, code, html, retryAfter) {
    read.resolve({
      status: code, ok: code === 200,
      headers: new Headers({"Content-Type": "text/html", ...(retryAfter ? {"Retry-After": retryAfter} : {})}),
      text: async () => html,
    });
  }

  const controller = startPolling(root, {onStop() {}});
  tick(0);
  assert.equal(pending[0].path, "/support/fragments/tickets/?view=mine&q=test&page=1&review_page=1");
  document.hidden = true;
  documentEvents.visibilitychange();
  assert.equal(pending.length, 1);
  document.hidden = false;
  const release = controller.beginMutation();
  assert.equal(pending[0].options.signal.aborted, true);
  release(true);
  respond(pending[0], 200, "stale");
  await flush();
  assert.deepEqual(renders, []);
  tick(0);
  respond(pending[1], 200, "fresh");
  await flush();
  assert.deepEqual(renders, ["fresh"]);
  tick(5000);
  respond(pending[2], 503, "", "45");
  await flush();
  documentEvents.visibilitychange();
  tick(45000);
  respond(pending[3], 403, "");
  await flush();
  assert.equal(root.hidden, true);
  assert.equal(timers.size, 0);
});
