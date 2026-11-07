const SECOND = 1000;

export function nextPollDelay(hidden, failures, startedAt, retryAt, now) {
  const interval = hidden ? 30 : failures ? Math.min(30, failures * 10) : 5;
  return Math.max(0, startedAt + interval * SECOND - now, retryAt - now);
}

function retryDeadline(value, now) {
  if (!value) return 0;
  const deadline = /^\d+$/.test(value) ? now + Number(value) * SECOND : Date.parse(value);
  return Number.isFinite(deadline) ? deadline : 0;
}

function parseFragment(text, selector) {
  const doc = new DOMParser().parseFromString(text, "text/html");
  const element = doc.querySelector(selector);
  if (!element) throw new Error("Missing workspace fragment");
  return element;
}

function historySignature(element) {
  return JSON.stringify(Array.from(element.querySelectorAll("[data-history-key], [data-delivery-state], [data-attachment-key]"),
    (node) => [node.dataset.historyKey, node.dataset.historyStatus, node.dataset.deliveryState, node.dataset.attachmentKey]));
}

export function startPolling(root, { onHeader, onState, onHistory, onStop }) {
  const status = document.querySelector("[data-poll-status]");
  const queue = root.dataset.queueUrl;
  const stateUrl = root.dataset.ticketStateUrl;
  if (!queue && !stateUrl) return null;

  const params = new URLSearchParams(location.search);
  let active, timer, startedAt, latestSignature;
  let generation = 0;
  let failures = 0;
  let retryAt = 0;
  let immediate = false;
  let holds = 0;
  let stopped = false;
  let dormant = false;
  let historyStopped = false;

  function notice(message) {
    status.textContent = message;
    status.hidden = !message;
  }

  function stop(message) {
    stopped = true;
    generation += 1;
    clearTimeout(timer);
    active?.abort();
    removeListeners();
    root.hidden = true;
    onStop();
    notice(message);
  }

  function schedule(now = Date.now()) {
    clearTimeout(timer);
    if (stopped || dormant || holds || active || !navigator.onLine) return;
    const delay = immediate && !document.hidden
      ? Math.max(0, retryAt - now)
      : nextPollDelay(document.hidden, failures, startedAt ?? now, retryAt, now);
    timer = setTimeout(() => void cycle(), Math.min(delay, 2147483647));
  }

  async function read(path, type, signal) {
    const response = await fetch(path, {
      signal, credentials: "same-origin", redirect: "error", cache: "no-store",
      headers: { Accept: type },
    });
    if (signal.aborted) throw signal.reason;
    if ([401, 403].includes(response.status)) {
      stop("Доступ к поддержке недоступен. Открой кабинет сотрудников и войди снова.");
      throw new Error("Access ended");
    }
    if (response.status === 404 && stateUrl) {
      stop("Обращение больше недоступно.");
      throw new Error("Ticket unavailable");
    }
    if ([429, 503].includes(response.status)) {
      retryAt = Math.max(retryAt, retryDeadline(response.headers.get("Retry-After"), Date.now()));
    }
    if (!response.ok) {
      const error = new Error("Workspace read failed");
      error.code = (response.headers.get("Content-Type")?.includes("application/json")
        ? await response.json() : null)?.error?.code;
      throw error;
    }
    if (response.headers.get("Content-Type")?.split(";")[0] !== type) throw new Error("Unexpected content type");
    return type === "application/json" ? response.json() : response.text();
  }

  function replace(selector, replacement, { anchor = false } = {}) {
    const previous = root.querySelector(selector);
    if (!previous) return false;
    const focused = document.activeElement;
    let nextFocus;
    if (previous.contains(focused)) {
      if (focused.tagName !== "A") return false;
      nextFocus = Array.from(replacement.querySelectorAll("a")).find((link) => link.href === focused.href);
      if (!nextFocus) return false;
    }
    const visible = anchor && Array.from(previous.querySelectorAll("[data-history-key]"))
      .find((item) => item.getBoundingClientRect().bottom > 0);
    const offset = visible && visible.getBoundingClientRect().top;
    previous.replaceWith(replacement);
    nextFocus?.focus({preventScroll: true});
    if (visible) {
      const match = Array.from(replacement.querySelectorAll("[data-history-key]"))
        .find((item) => item.dataset.historyKey === visible.dataset.historyKey);
      if (match) window.scrollBy(0, match.getBoundingClientRect().top - offset);
    }
    return true;
  }

  async function readTicket(signal, current) {
    const state = await read(stateUrl, "application/json", signal);
    if (!current()) return;
    onState(state);
    const header = root.querySelector("[data-ticket-header]");
    if (header.dataset.stateKey !== state.state_key) {
      const focused = document.activeElement;
      if (header.contains(focused) && focused.closest("form")) focused.blur();
      for (const control of header.querySelectorAll("button, select")) control.disabled = true;
    }
    const older = params.has("before");
    const history = root.querySelector("[data-ticket-history]");
    const results = await Promise.allSettled([
      header.dataset.stateKey === state.state_key ? null : read(root.dataset.headerUrl, "text/html", signal),
      historyStopped ? null : read(root.dataset.historyUrl + (older ? "?" + new URLSearchParams({before: params.get("before")}) : ""), "text/html", signal),
      older && !historyStopped ? read(root.dataset.historyUrl, "text/html", signal) : null,
    ]);
    if (!current()) return;
    const [newHeader, newHistory, latest] = results;
    if (newHeader.status === "fulfilled" && newHeader.value) {
      const replacement = parseFragment(newHeader.value, "[data-ticket-header]");
      if (replace("[data-ticket-header]", replacement)) await onHeader(replacement, state, signal, read, current);
    } else if (header.dataset.stateKey === state.state_key) {
      await onHeader(header, state, signal, read, current);
    }
    if (!current()) return;
    if (newHistory.status === "fulfilled" && newHistory.value) {
      const replacement = parseFragment(newHistory.value, "[data-ticket-history]");
      if (historySignature(history) !== historySignature(replacement)) replace("[data-ticket-history]", replacement, {anchor: true});
      await onHistory(replacement, async (path) => {
        const data = await read(path, "application/json", signal);
        if (!current()) throw new Error("Stale workspace read");
        return data;
      }, current);
    } else if (newHistory.reason?.code === "invalid_cursor") {
      historyStopped = true;
      root.querySelector("[data-new-history]").hidden = false;
    }
    if (!current()) return;
    if (latest.status === "fulfilled" && latest.value && !historyStopped) {
      const latestFragment = parseFragment(latest.value, "[data-ticket-history]");
      const signature = historySignature(latestFragment);
      if (latestSignature !== undefined && latestSignature !== signature) root.querySelector("[data-new-history]").hidden = false;
      latestSignature = signature;
      await onHistory(latestFragment, async (path) => {
        const data = await read(path, "application/json", signal);
        if (!current()) throw new Error("Stale workspace read");
        return data;
      }, current);
    }
    if (results.some((result) => result.status === "rejected" && result.reason?.code !== "invalid_cursor")) {
      throw new Error("Workspace read failed");
    }
  }

  async function cycle() {
    if (stopped || dormant || active || holds || !navigator.onLine) return;
    startedAt = Date.now();
    if (startedAt < retryAt) {
      schedule();
      return;
    }
    immediate = false;
    const controller = new AbortController();
    active = controller;
    const epoch = generation;
    const current = () => epoch === generation && !controller.signal.aborted;
    const timeout = setTimeout(() => controller.abort(), 12 * SECOND);
    try {
      if (queue) {
        const path = queue + "?" + new URLSearchParams({
          view: params.get("view") ?? "queue", q: params.get("q") ?? "", page: params.get("page") ?? "1",
          review_page: params.get("review_page") ?? "1",
        });
        const html = await read(path, "text/html", controller.signal);
        if (current()) {
          const fragment = parseFragment(html, "[data-ticket-list]");
          if (root.querySelector("[data-ticket-list]").innerHTML !== fragment.innerHTML) replace("[data-ticket-list]", fragment);
        }
      } else {
        await readTicket(controller.signal, current);
      }
      if (current()) {
        failures = 0;
        notice("");
      } else if (epoch === generation && controller.signal.aborted) {
        throw new Error("Workspace read timed out");
      }
    } catch {
      if (epoch === generation) {
        failures += 1;
        notice("Не удалось обновить данные. На экране остаётся последняя загруженная версия.");
      }
    } finally {
      clearTimeout(timeout);
      active = null;
      schedule();
    }
  }

  function wake() {
    immediate = true;
    schedule();
  }

  function suspend() {
    generation += 1;
    clearTimeout(timer);
    active?.abort();
  }

  function visibilityChanged() {
    if (document.hidden) schedule();
    else wake();
  }

  function disconnected() {
    suspend();
    notice("Нет связи. На экране остаётся последняя загруженная версия.");
  }

  function pageHidden() {
    dormant = true;
    suspend();
    removeListeners();
  }

  function pageShown(event) {
    if (!stopped && event.persisted) {
      dormant = false;
      addListeners();
      wake();
    }
  }

  function addListeners() {
    document.addEventListener("visibilitychange", visibilityChanged);
    window.addEventListener("online", wake);
    window.addEventListener("offline", disconnected);
    window.addEventListener("pagehide", pageHidden);
  }

  function removeListeners() {
    document.removeEventListener("visibilitychange", visibilityChanged);
    window.removeEventListener("online", wake);
    window.removeEventListener("offline", disconnected);
    window.removeEventListener("pagehide", pageHidden);
    if (stopped) window.removeEventListener("pageshow", pageShown);
  }

  window.addEventListener("pageshow", pageShown);
  addListeners();
  immediate = true;
  if (document.hidden) void cycle();
  else schedule();

  return {
    beginMutation() {
      holds += 1;
      suspend();
      return (success) => {
        holds -= 1;
        if (success) {
          immediate = true;
          if (document.hidden && !active && !holds && Date.now() >= retryAt) void cycle();
          else schedule();
        } else schedule();
      };
    },
  };
}
