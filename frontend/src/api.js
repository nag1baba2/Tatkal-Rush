// A thin client for the Tatkal-Rush API.
//
// Conventions that matter for correctness:
//  * Booking and payment calls take an Idempotency-Key supplied by the CALLER. The
//    caller reuses a key when retrying the same attempt (so a retry can never create a
//    second booking or charge) and makes a new one for a genuinely new attempt.
//  * Payment responses 202 (in progress) and 402 (attempt failed) are results with a
//    body, not exceptions, so payBooking returns them instead of throwing.

const BASE = import.meta.env.VITE_API_URL || "http://localhost:8000";

export class ApiError extends Error {
  constructor(status, detail) {
    super(detail || `HTTP ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

export class RateLimitedError extends ApiError {
  constructor(retryAfter) {
    super(429, "rate_limited");
    this.name = "RateLimitedError";
    this.retryAfter = retryAfter;
  }
}

export class NetworkError extends Error {
  constructor() {
    super("network_error");
    this.name = "NetworkError";
  }
}

async function send(path, { method = "GET", body, token, idempotencyKey } = {}) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (token) headers.Authorization = `Bearer ${token}`;
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;

  let res;
  try {
    res = await fetch(`${BASE}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new NetworkError();
  }
  let data = null;
  try {
    data = await res.json();
  } catch {
    /* an empty or non-JSON body */
  }
  return { status: res.status, data, headers: res.headers };
}

function fail({ status, data, headers }) {
  if (status === 429) {
    throw new RateLimitedError(Number(headers.get("Retry-After")) || 1);
  }
  // FastAPI sends a string detail for our errors and a list for validation errors.
  throw new ApiError(status, typeof data?.detail === "string" ? data.detail : undefined);
}

async function expectOk(promise) {
  const response = await promise;
  if (response.status >= 200 && response.status < 300) return response;
  return fail(response);
}

// ---- auth ----------------------------------------------------------------------

export async function register(email, password) {
  return (await expectOk(send("/auth/register", { method: "POST", body: { email, password } }))).data;
}

export async function login(email, password) {
  const { data } = await expectOk(send("/auth/login", { method: "POST", body: { email, password } }));
  return data.access_token;
}

// ---- catalog -------------------------------------------------------------------------

export async function searchTrains({ source = "", destination = "" } = {}) {
  const params = new URLSearchParams();
  if (source.trim()) params.set("source", source.trim());
  if (destination.trim()) params.set("destination", destination.trim());
  const query = params.toString();
  return (await expectOk(send(`/trains${query ? `?${query}` : ""}`))).data;
}

export async function getSchedules(trainId) {
  return (await expectOk(send(`/trains/${trainId}/schedules`))).data;
}

export async function getSeats(scheduleId) {
  return (await expectOk(send(`/schedules/${scheduleId}/seats`))).data;
}

// ---- bookings and payments -------------------------------------------------------------

export async function getMyBookings(token) {
  return (await expectOk(send("/bookings", { token }))).data;
}

export async function bookSeat(token, scheduleId, seatId, idempotencyKey) {
  const { status, data } = await expectOk(
    send("/bookings", {
      method: "POST",
      token,
      idempotencyKey,
      body: { schedule_id: scheduleId, seat_id: seatId },
    }),
  );
  return { booking: data, replay: status === 200 };
}

/** Pay for a held booking. Returns { httpStatus, payment, replay } for 200 (paid),
 *  202 (in progress) and 402 (this attempt failed); throws for anything else. */
export async function payBooking(token, bookingId, idempotencyKey) {
  const response = await send(`/bookings/${bookingId}/pay`, {
    method: "POST",
    token,
    idempotencyKey,
  });
  if ([200, 202, 402].includes(response.status)) {
    return {
      httpStatus: response.status,
      payment: response.data,
      replay: response.headers.get("Idempotent-Replay") === "true",
    };
  }
  return fail(response);
}

export async function getPayment(token, bookingId) {
  return (await expectOk(send(`/bookings/${bookingId}/payment`, { token }))).data;
}
