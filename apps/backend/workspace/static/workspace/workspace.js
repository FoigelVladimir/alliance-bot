import { requestJson } from "alliance/api";

document.addEventListener("submit", async (event) => {
  const form = event.target;
  if (!form.matches("[data-notification-read]")) return;
  event.preventDefault();
  const button = form.querySelector("button");
  if (button.disabled) return;
  button.disabled = true;
  const error = form.querySelector("[data-notification-error]");
  error.textContent = "";
  try {
    const result = await requestJson(form.action, {
      method: "POST", body: {}, csrfToken: new FormData(form).get("csrfmiddlewaretoken"),
    });
    const target = new URL(result.notification.target_url, window.location.origin);
    if (target.origin !== window.location.origin) throw new Error("Invalid target");
    window.location.assign(target.href);
  } catch {
    error.textContent = "Не удалось открыть уведомление. Проверьте доступ и повторите действие.";
    button.disabled = false;
  }
});

document.addEventListener("click", async (event) => {
  const link = event.target.closest("[data-home-refresh]");
  if (!link) return;
  event.preventDefault();
  if (link.getAttribute("aria-disabled") === "true") return;
  link.setAttribute("aria-disabled", "true");
  const section = link.closest("[data-home-section]");
  section.setAttribute("aria-busy", "true");
  try {
    const response = await fetch(link.dataset.homeRefresh, { credentials: "same-origin", redirect: "error" });
    if (!response.ok || !response.headers.get("content-type")?.includes("text/html")) throw new Error("Unavailable");
    const fragment = new DOMParser().parseFromString(await response.text(), "text/html");
    const updated = fragment.querySelector("[data-home-section]");
    if (!updated || updated.dataset.homeSection !== section.dataset.homeSection) throw new Error("Invalid section");
    section.replaceWith(updated);
  } catch {
    section.querySelector("[data-home-error]").textContent = "Не удалось обновить блок. Повторите загрузку.";
    link.removeAttribute("aria-disabled");
    section.removeAttribute("aria-busy");
  }
});
