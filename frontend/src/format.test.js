import { describe, expect, it } from "vitest";
import { ApiError, NetworkError, RateLimitedError } from "./api";
import {
  formatCountdown,
  formatDate,
  formatMoney,
  isDefiniteOutcome,
  messageFor,
  newKey,
  reasonFor,
  secondsLeft,
} from "./format";

describe("formatMoney", () => {
  it("formats integer paise as rupees with two decimals", () => {
    expect(formatMoney(85000)).toBe("₹850.00");
    expect(formatMoney(5)).toBe("₹0.05");
    expect(formatMoney(123456789)).toBe("₹12,34,567.89"); // Indian digit grouping
  });
});

describe("countdown", () => {
  it("formats minutes and padded seconds, and never goes negative", () => {
    expect(formatCountdown(300)).toBe("5:00");
    expect(formatCountdown(61)).toBe("1:01");
    expect(formatCountdown(9)).toBe("0:09");
    expect(formatCountdown(-5)).toBe("0:00");
  });

  it("computes the seconds left until an expiry, rounding up, floor of zero", () => {
    const now = Date.parse("2026-10-14T10:00:00Z");
    expect(secondsLeft("2026-10-14T10:05:00Z", now)).toBe(300);
    expect(secondsLeft("2026-10-14T10:00:00.400Z", now)).toBe(1);
    expect(secondsLeft("2026-10-14T09:59:00Z", now)).toBe(0);
    expect(secondsLeft(null, now)).toBe(0);
  });
});

describe("formatDate", () => {
  it("does not shift the day with the viewer's timezone", () => {
    expect(formatDate("2026-10-15")).toBe("Thu, 15 Oct 2026");
  });
});

describe("newKey", () => {
  it("makes a different key every time", () => {
    const keys = new Set(Array.from({ length: 200 }, newKey));
    expect(keys.size).toBe(200);
  });
});

describe("messageFor", () => {
  it("explains the errors a user can actually hit", () => {
    expect(messageFor(new ApiError(409, "seat_unavailable"))).toMatch(/someone else/i);
    expect(messageFor(new ApiError(401, "invalid_or_missing_token"))).toMatch(/log in again/i);
    expect(messageFor(new ApiError(401, "invalid_credentials"))).toMatch(/wrong email or password/i);
    expect(messageFor(new ApiError(409, "email_already_registered"))).toMatch(/already registered/i);
    expect(messageFor(new ApiError(409, "booking_not_payable:held"))).toMatch(/expired/i);
    expect(messageFor(new ApiError(503, "server_busy"))).toMatch(/busy/i);
    expect(messageFor(new ApiError(422))).toMatch(/8 characters/);
  });

  it("tells the user how long to wait when rate limited", () => {
    expect(messageFor(new RateLimitedError(3))).toMatch(/3s/);
  });

  it("explains a network failure and falls back gracefully", () => {
    expect(messageFor(new NetworkError())).toMatch(/can't reach the server/i);
    expect(messageFor(new ApiError(500))).toMatch(/HTTP 500/);
    expect(messageFor(new Error("weird"))).toBe("Something went wrong.");
  });
});

describe("reasonFor", () => {
  it("always reassures the user that they were not charged", () => {
    for (const reason of ["declined", "retries_exhausted", "gateway_unavailable", "something_new", null]) {
      const text = reasonFor(reason);
      expect(text.length).toBeGreaterThan(10);
    }
    expect(reasonFor("retries_exhausted")).toMatch(/not charged/i);
    expect(reasonFor("unknown_reason")).toMatch(/not charged/i);
  });
});

describe("isDefiniteOutcome (may the idempotency key be discarded?)", () => {
  it("is true for a final answer from the server", () => {
    expect(isDefiniteOutcome(new ApiError(409, "seat_unavailable"))).toBe(true);
    expect(isDefiniteOutcome(new ApiError(404))).toBe(true);
    expect(isDefiniteOutcome(new ApiError(422))).toBe(true);
  });

  it("is false when the outcome is unknown, so the key must be kept", () => {
    expect(isDefiniteOutcome(new NetworkError())).toBe(false);
    expect(isDefiniteOutcome(new RateLimitedError(2))).toBe(false);
    expect(isDefiniteOutcome(new ApiError(503, "server_busy"))).toBe(false);
    expect(isDefiniteOutcome(new ApiError(500))).toBe(false);
  });
});
