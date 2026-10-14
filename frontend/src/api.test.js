import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  NetworkError,
  RateLimitedError,
  bookSeat,
  getMyBookings,
  login,
  payBooking,
  searchTrains,
} from "./api";

function reply(status, body, headers = {}) {
  return new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

let fetchMock;
beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

const lastCall = () => fetchMock.mock.calls.at(-1);

describe("requests", () => {
  it("sends the token and the Idempotency-Key on a booking", async () => {
    fetchMock.mockResolvedValue(reply(201, { id: 7 }));
    const { booking, replay } = await bookSeat("tok", 3, 9, "key-1");

    const [url, init] = lastCall();
    expect(url).toMatch(/\/bookings$/);
    expect(init.method).toBe("POST");
    expect(init.headers.Authorization).toBe("Bearer tok");
    expect(init.headers["Idempotency-Key"]).toBe("key-1");
    expect(JSON.parse(init.body)).toEqual({ schedule_id: 3, seat_id: 9 });
    expect(booking.id).toBe(7);
    expect(replay).toBe(false);
  });

  it("flags a 200 booking response as a replay of an earlier request", async () => {
    fetchMock.mockResolvedValue(reply(200, { id: 7 }));
    expect((await bookSeat("tok", 3, 9, "key-1")).replay).toBe(true);
  });

  it("builds the search query and leaves blank filters out", async () => {
    fetchMock.mockResolvedValue(reply(200, []));
    await searchTrains({ source: "  New Delhi ", destination: "" });
    expect(lastCall()[0]).toMatch(/\/trains\?source=New\+Delhi$/);
    await searchTrains({});
    expect(lastCall()[0]).toMatch(/\/trains$/);
  });

  it("sends no Authorization header when logged out", async () => {
    fetchMock.mockResolvedValue(reply(200, []));
    await searchTrains({});
    expect(lastCall()[1].headers.Authorization).toBeUndefined();
  });

  it("returns the access token from login", async () => {
    fetchMock.mockResolvedValue(reply(200, { access_token: "abc", token_type: "bearer" }));
    expect(await login("a@b.co", "pw")).toBe("abc");
  });
});

describe("errors", () => {
  it("turns a 429 into RateLimitedError carrying Retry-After", async () => {
    fetchMock.mockResolvedValue(reply(429, { detail: "rate_limited" }, { "Retry-After": "4" }));
    const error = await getMyBookings("tok").catch((e) => e);
    expect(error).toBeInstanceOf(RateLimitedError);
    expect(error.retryAfter).toBe(4);
  });

  it("turns other failures into ApiError with the server's detail", async () => {
    fetchMock.mockResolvedValue(reply(409, { detail: "seat_unavailable" }));
    const error = await bookSeat("tok", 1, 1, "k").catch((e) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(409);
    expect(error.detail).toBe("seat_unavailable");
  });

  it("ignores FastAPI's list-shaped validation detail", async () => {
    fetchMock.mockResolvedValue(reply(422, { detail: [{ msg: "bad" }] }));
    const error = await login("x", "y").catch((e) => e);
    expect(error.status).toBe(422);
    expect(error.detail).toBeUndefined();
  });

  it("turns a failed fetch into NetworkError", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
    await expect(searchTrains({})).rejects.toBeInstanceOf(NetworkError);
  });

  it("copes with an empty or non-JSON error body", async () => {
    fetchMock.mockResolvedValue(new Response("Bad gateway", { status: 502 }));
    const error = await searchTrains({}).catch((e) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(502);
  });
});

describe("payBooking", () => {
  const payment = (status) => ({ id: 1, status, last_error: null });

  it.each([
    [200, "SUCCESS"],
    [202, "PENDING"],
    [402, "FAILED"],
  ])("returns HTTP %i as a result instead of throwing", async (httpStatus, status) => {
    fetchMock.mockResolvedValue(reply(httpStatus, payment(status)));
    const result = await payBooking("tok", 5, "pay-key");
    expect(result.httpStatus).toBe(httpStatus);
    expect(result.payment.status).toBe(status);
    expect(lastCall()[1].headers["Idempotency-Key"]).toBe("pay-key");
  });

  it("reports an Idempotent-Replay response", async () => {
    fetchMock.mockResolvedValue(reply(200, payment("SUCCESS"), { "Idempotent-Replay": "true" }));
    expect((await payBooking("tok", 5, "k")).replay).toBe(true);
  });

  it("throws for a booking that can no longer be paid", async () => {
    fetchMock.mockResolvedValue(reply(409, { detail: "booking_not_payable:expired" }));
    const error = await payBooking("tok", 5, "k").catch((e) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error.detail).toBe("booking_not_payable:expired");
  });
});
