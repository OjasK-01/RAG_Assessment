// Thin API client. The token is held in memory and mirrored to sessionStorage so a refresh
// keeps you signed in; it is cleared on logout or any 401.
const KEY = "vectra_session";

export function loadSession() {
  try {
    const raw = sessionStorage.getItem(KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function saveSession(session) {
  try {
    if (session) sessionStorage.setItem(KEY, JSON.stringify(session));
    else sessionStorage.removeItem(KEY);
  } catch {
    /* storage unavailable: session lives in memory only */
  }
}

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

// Backend origin from frontend/.env (VITE_BASE_URL=http://127.0.0.1:8000). The browser then calls the API
// directly, so the backend must list this page's origin in CORS_ORIGINS. Empty = same origin: /api goes
// through the Vite dev proxy. Vite inlines VITE_* values into the public bundle: never put secrets here.
const BASE_URL = (import.meta.env.VITE_BASE_URL || "").replace(/\/+$/, "");

async function request(path, { token, body, method = "GET" } = {}) {
  let res;
  try {
    res = await fetch(BASE_URL + path, {
      method,
      headers: {
        ...(body ? { "Content-Type": "application/json" } : {}),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    // Network failure or a CORS rejection: fetch gives no detail, so say what to check.
    throw new ApiError(0, `Can't reach the server${BASE_URL ? ` at ${BASE_URL}` : ""}. Is the backend running?`);
  }
  let data = null;
  if (res.status === 204) return null;
  try {
    data = await res.json();
  } catch {
    /* non-JSON error body */
  }
  if (!res.ok) {
    const detail = data?.detail;
    const msg = Array.isArray(detail) ? detail.map((d) => d.msg).join("; ") : detail || res.statusText;
    throw new ApiError(res.status, msg);
  }
  return data;
}

export async function login(username, password) {
  const data = await request("/api/auth/login", { method: "POST", body: { username, password } });
  const session = { token: data.access_token, username: data.username, role: data.role,
                    expiresAt: Date.now() + data.expires_in * 1000 };
  saveSession(session);
  return session;
}

export function logout() {
  saveSession(null);
  saveChat(null);
}

export const ask = (token, question, conversationId) =>
  request("/api/ask", {
    method: "POST",
    token,
    body: conversationId ? { question, conversation_id: conversationId } : { question },
  });
export const deleteConversation = (token, id) => request(`/api/conversations/${id}`, { method: "DELETE", token });

// The open chat survives a page refresh (per tab only); the server holds the real history.
const CHAT_KEY = "vectra_chat";

export function loadChat(username) {
  try {
    const chat = JSON.parse(sessionStorage.getItem(CHAT_KEY) || "null");
    return chat && chat.username === username ? chat : null;
  } catch {
    return null;
  }
}

export function saveChat(chat) {
  try {
    if (chat) sessionStorage.setItem(CHAT_KEY, JSON.stringify(chat));
    else sessionStorage.removeItem(CHAT_KEY);
  } catch {
    /* storage unavailable: chat lives in memory only */
  }
}
export const listArticles = (token) => request("/api/kb/articles", { token });
export const reingest = (token) => request("/api/admin/ingest", { method: "POST", token });
export const auditLog = (token, limit = 20) => request(`/api/admin/audit?limit=${limit}`, { token });
