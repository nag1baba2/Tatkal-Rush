import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearSession, loadSession, saveSession } from "./session";

function tokenExpiringIn(seconds) {
  const b64 = (obj) => btoa(JSON.stringify(obj)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const exp = Math.floor(Date.now() / 1000) + seconds;
  return `${b64({ alg: "HS256", typ: "JWT" })}.${b64({ sub: "1", exp })}.signature`;
}

beforeEach(() => localStorage.clear());
afterEach(() => vi.restoreAllMocks());

describe("session storage", () => {
  it("remembers a login across a reload", () => {
    const session = { email: "a@b.co", token: tokenExpiringIn(3600) };
    saveSession(session);
    expect(loadSession()).toEqual(session);
  });

  it("forgets on logout", () => {
    saveSession({ email: "a@b.co", token: tokenExpiringIn(3600) });
    clearSession();
    expect(loadSession()).toBeNull();
  });

  it("drops an expired token instead of using it", () => {
    saveSession({ email: "a@b.co", token: tokenExpiringIn(-10) });
    expect(loadSession()).toBeNull();
    expect(localStorage.getItem("tatkal.session")).toBeNull(); // and cleans up
  });

  it("drops garbage that is not a session", () => {
    localStorage.setItem("tatkal.session", "{not json");
    expect(loadSession()).toBeNull();
    localStorage.setItem("tatkal.session", JSON.stringify({ email: "x" }));
    expect(loadSession()).toBeNull();
    localStorage.setItem("tatkal.session", JSON.stringify({ email: "x", token: "not-a-jwt" }));
    expect(loadSession()).toBeNull();
  });

  it("does not crash when localStorage is unavailable", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(loadSession()).toBeNull();
    expect(() => saveSession({ email: "a@b.co", token: "t" })).not.toThrow();
    expect(() => clearSession()).not.toThrow();
  });
});
