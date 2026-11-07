export class ApiError extends Error {
  constructor(status, payload) {
    super(`HTTP ${status}`);
    this.status = status;
    this.payload = payload;
  }
}

export async function requestJson(path, { method = "GET", body, csrfToken, signal } = {}) {
  const url = new URL(path, window.location.origin);
  if (url.origin !== window.location.origin) {
    throw new TypeError("The API URL must use the same origin");
  }
  const headers = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (csrfToken) headers["X-CSRFToken"] = csrfToken;
  const response = await fetch(url, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
    credentials: "same-origin",
    redirect: "error",
  });
  const payload = response.headers.get("content-type")?.includes("application/json")
    ? await response.json()
    : null;
  if (!response.ok) throw new ApiError(response.status, payload);
  return payload;
}
