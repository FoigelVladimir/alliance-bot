const status = document.querySelector("[data-participant-status]");
let timer;
let stopped = false;
let inFlight = false;
async function check() {
  if (stopped || inFlight || document.hidden || !status) return;
  inFlight = true;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 5000);
  try {
    const response = await fetch(status.dataset.participantStatus, {
      headers: { Accept: "application/json" }, credentials: "same-origin", signal: controller.signal,
    });
    if (!response.ok) throw new Error("status unavailable");
    const data = await response.json();
    if (data.status === "confirmed" && typeof data.next_url === "string") {
      const next = new URL(data.next_url, location.origin);
      if (next.origin === location.origin) {
        stopped = true;
        location.replace(next.href);
      }
    } else if (data.status === "expired") {
      stopped = true;
      status.textContent = "Подтверждение недоступно. Получите новую ссылку в боте.";
    }
  } catch {
    // Keep the explicit reload link usable when the connection drops.
  } finally {
    clearTimeout(timeout);
    inFlight = false;
    if (!stopped && !document.hidden) timer = setTimeout(check, 5000);
  }
}
document.addEventListener("visibilitychange", () => {
  clearTimeout(timer);
  if (!document.hidden) check();
});
check();
