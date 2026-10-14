import { ApiError, NetworkError, RateLimitedError } from "./api";

/** 85000 -> "₹850.00". Money is integer paise everywhere; only this function formats it. */
export function formatMoney(paise) {
  const rupees = paise / 100;
  return `₹${rupees.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

/** 61 -> "1:01"; never negative. */
export function formatCountdown(totalSeconds) {
  const s = Math.max(0, Math.floor(totalSeconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

export function secondsLeft(expiresAtIso, nowMs = Date.now()) {
  if (!expiresAtIso) return 0;
  return Math.max(0, Math.ceil((new Date(expiresAtIso).getTime() - nowMs) / 1000));
}

/** "2026-10-15" -> "Thu, 15 Oct 2026" (UTC, so the day never shifts with the viewer's timezone). */
export function formatDate(isoDate) {
  return new Date(isoDate).toLocaleDateString("en-GB", {
    weekday: "short",
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  });
}

export function formatTime(isoDateTime) {
  return new Date(isoDateTime).toLocaleTimeString("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "UTC",
  });
}

/** A fresh idempotency key. Reuse the SAME key to retry an attempt; make a new one for a new attempt. */
export function newKey() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  return `k-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

const DETAIL_MESSAGES = {
  seat_unavailable: "Someone else just took that seat.",
  seat_not_found: "That seat no longer exists.",
  idempotency_key_reused_with_different_request: "That request was already used for a different seat. Please try again.",
  idempotency_key_already_used: "That payment request was already used. Please try again.",
  booking_not_found: "We couldn't find that booking.",
  email_already_registered: "That email is already registered. Try logging in.",
  invalid_credentials: "Wrong email or password.",
  invalid_or_missing_token: "Your session has expired. Please log in again.",
  server_busy: "The server is busy. Please try again in a moment.",
};

/** A human message for any error the API client can throw. */
export function messageFor(error) {
  if (error instanceof RateLimitedError) {
    return `You're going a little fast. Try again in ${error.retryAfter}s.`;
  }
  if (error instanceof NetworkError) {
    return "Can't reach the server. Is the API running on port 8000?";
  }
  if (error instanceof ApiError) {
    if (error.detail?.startsWith("booking_not_payable")) {
      return "This hold has expired and can no longer be paid.";
    }
    if (error.detail && DETAIL_MESSAGES[error.detail]) return DETAIL_MESSAGES[error.detail];
    if (error.status === 422) return "Please check what you entered (passwords need at least 8 characters).";
    return `Something went wrong (HTTP ${error.status}).`;
  }
  return "Something went wrong.";
}

const FAILURE_REASONS = {
  declined: "Your card was declined.",
  retries_exhausted: "The payment service didn't respond. You were not charged.",
  gateway_unavailable: "The payment service is temporarily unavailable. You were not charged.",
};

export function reasonFor(lastError) {
  return FAILURE_REASONS[lastError] || "The payment didn't go through. You were not charged.";
}

/** True when the server gave a final answer, so the idempotency key can be discarded.
 *  Network drops, 429 and 5xx leave the outcome unknown: keep the key and retry with it. */
export function isDefiniteOutcome(error) {
  return error instanceof ApiError && error.status < 500 && error.status !== 429;
}
