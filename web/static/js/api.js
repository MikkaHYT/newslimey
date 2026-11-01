// Thin fetch wrapper: JSON in, JSON out, one error type, and a global signal
// when the session expires so the shell can show the login screen again.

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

let onUnauthorised = null;

export function setUnauthorisedHandler(handler) {
  onUnauthorised = handler;
}

async function request(path, options) {
  let response;
  try {
    response = await fetch(path, { credentials: "same-origin", ...options });
  } catch (error) {
    throw new ApiError("The bot's dashboard server is not reachable", 0);
  }

  let payload = null;
  const type = response.headers.get("content-type") || "";
  if (type.includes("application/json")) {
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
  }

  if (response.status === 401) {
    if (onUnauthorised) onUnauthorised();
    throw new ApiError(payload?.error || "Not signed in", 401);
  }

  if (!response.ok) {
    throw new ApiError(payload?.error || `Request failed (${response.status})`, response.status);
  }

  return payload;
}

export function get(path) {
  return request(path);
}

export function post(path, body) {
  return request(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
}

export function patch(path, body) {
  return request(path, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
}

export function put(path, body) {
  return request(path, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
}

export function del(path) {
  return request(path, { method: "DELETE" });
}

export function upload(path, formData) {
  return request(path, { method: "POST", body: formData });
}
