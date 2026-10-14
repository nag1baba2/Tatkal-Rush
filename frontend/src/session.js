// The login session lives in localStorage so it survives a page refresh.
//
// Trade-off (also noted in the README): anything in localStorage can be read by
// JavaScript running on the page, so an XSS bug would leak the token. React escapes
// rendered text and the token expires after 60 minutes, which is a reasonable
// trade for a project like this. A production app would prefer an httpOnly cookie.
//
// localStorage can throw (private windows, blocked site data), so every access is
// wrapped and the app works, just without remembering you, when it is unavailable.

const KEY = "tatkal.session";

function tokenIsExpired(token) {
  try {
    const payload = JSON.parse(atob(token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
    return typeof payload.exp === "number" && payload.exp * 1000 <= Date.now();
  } catch {
    return true; // not a token we can read: treat as unusable
  }
}

export function loadSession() {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return null;
    const session = JSON.parse(raw);
    if (!session?.token || !session?.email || tokenIsExpired(session.token)) {
      localStorage.removeItem(KEY);
      return null;
    }
    return session;
  } catch {
    return null;
  }
}

export function saveSession(session) {
  try {
    localStorage.setItem(KEY, JSON.stringify(session));
  } catch {
    /* storage unavailable: stay logged in for this page load only */
  }
}

export function clearSession() {
  try {
    localStorage.removeItem(KEY);
  } catch {
    /* nothing to clear */
  }
}
