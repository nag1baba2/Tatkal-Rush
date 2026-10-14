import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, NetworkError, RateLimitedError, bookSeat, getSeats } from "../api";
import SeatMap from "./SeatMap";

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal()),
  getSeats: vi.fn(),
  bookSeat: vi.fn(),
}));

const SEATS = [
  { id: 1, coach: "S1", seat_no: 1, status: "AVAILABLE" },
  { id: 2, coach: "S1", seat_no: 2, status: "HELD" },
  { id: 3, coach: "S1", seat_no: 3, status: "BOOKED" },
  { id: 4, coach: "S2", seat_no: 1, status: "AVAILABLE" },
];
const SESSION = { email: "a@b.co", token: "tok" };
const SCHEDULE = { id: 5 };

function setup(props = {}) {
  const onBooked = vi.fn();
  const onError = vi.fn();
  const utils = render(
    <SeatMap
      schedule={SCHEDULE}
      session={SESSION}
      myBookings={[]}
      onBooked={onBooked}
      onError={onError}
      pollMs={0}
      {...props}
    />,
  );
  return { onBooked, onError, user: userEvent.setup(), ...utils };
}

const seat = (name) => screen.findByRole("button", { name });

beforeEach(() => {
  vi.resetAllMocks();
  getSeats.mockResolvedValue(SEATS);
});

describe("rendering", () => {
  it("groups seats by coach and says what each one is", async () => {
    setup();
    expect(await seat(/Seat S1-1, available/)).toBeEnabled();
    expect(await seat(/Seat S1-2, held by someone/)).toBeDisabled();
    expect(await seat(/Seat S1-3, booked/)).toBeDisabled();
    expect(await seat(/Seat S2-1, available/)).toBeEnabled();
    expect(screen.getByRole("heading", { name: "Coach S1" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Coach S2" })).toBeInTheDocument();
  });

  it("marks the user's own seats", async () => {
    setup({ myBookings: [{ schedule_id: 5, seat_id: 2, status: "HELD" }] });
    expect(await seat(/Seat S1-2, yours, held/)).toBeInTheDocument();
  });

  it("ignores bookings for other schedules and finished ones", async () => {
    setup({
      myBookings: [
        { schedule_id: 99, seat_id: 1, status: "HELD" },
        { schedule_id: 5, seat_id: 4, status: "EXPIRED" },
      ],
    });
    expect(await seat(/Seat S1-1, available/)).toBeInTheDocument();
    expect(await seat(/Seat S2-1, available/)).toBeInTheDocument();
  });

  it("refreshes on a timer so other people's holds show up", async () => {
    setup({ pollMs: 20 });
    await seat(/Seat S1-1/);
    await waitFor(() => expect(getSeats.mock.calls.length).toBeGreaterThanOrEqual(3));
  });
});

describe("when the connection drops", () => {
  it("does not raise an error banner for a failed background refresh, and recovers by itself", async () => {
    getSeats
      .mockResolvedValueOnce(SEATS)
      .mockRejectedValueOnce(new NetworkError())
      .mockResolvedValue(SEATS);
    const { onError } = setup({ pollMs: 30 });

    await seat(/Seat S1-1, available/);
    expect(await screen.findByText(/connection problem/i)).toBeInTheDocument();
    expect(onError).not.toHaveBeenCalled(); // no sticky banner in the app
    expect(screen.getByRole("button", { name: /Seat S1-1/ })).toBeInTheDocument(); // seats stay visible

    await waitFor(() => expect(screen.queryByText(/connection problem/i)).not.toBeInTheDocument());
  });

  it("says it is retrying if the very first load fails", async () => {
    getSeats.mockRejectedValueOnce(new NetworkError()).mockResolvedValue(SEATS);
    const { onError } = setup({ pollMs: 30 });
    expect(await screen.findByText(/can't reach the server\. retrying/i)).toBeInTheDocument();
    expect(onError).not.toHaveBeenCalled();
    expect(await seat(/Seat S1-1, available/)).toBeInTheDocument();
  });

  it("still reports real errors, such as a missing schedule", async () => {
    getSeats.mockRejectedValue(new ApiError(404, "schedule_not_found"));
    const { onError } = setup();
    await waitFor(() => expect(onError).toHaveBeenCalled());
  });
});

describe("booking a seat", () => {
  it("books with the token and a fresh idempotency key, then reports it", async () => {
    const booking = { id: 77 };
    bookSeat.mockResolvedValue({ booking, replay: false });
    const { user, onBooked } = setup();

    await user.click(await seat(/Seat S1-1, available/));

    await waitFor(() => expect(onBooked).toHaveBeenCalledWith(booking));
    expect(bookSeat).toHaveBeenCalledWith("tok", 5, 1, expect.any(String));
    expect(bookSeat.mock.calls[0][3].length).toBeGreaterThan(8);
  });

  it("asks a logged-out visitor to log in instead of booking", async () => {
    const { user } = setup({ session: null });
    await user.click(await seat(/Seat S1-1, available/));
    expect(bookSeat).not.toHaveBeenCalled();
    expect(screen.getByRole("status")).toHaveTextContent(/log in/i);
  });

  it("does not book twice when the seat is clicked twice quickly", async () => {
    let finish;
    bookSeat.mockReturnValue(new Promise((resolve) => (finish = resolve)));
    const { user } = setup();

    const button = await seat(/Seat S1-1, available/);
    await user.click(button);
    await user.click(button);
    expect(bookSeat).toHaveBeenCalledTimes(1);

    // every seat is locked while a booking is in flight
    expect(await seat(/Seat S2-1, available/)).toBeDisabled();
    finish({ booking: { id: 1 }, replay: false });
    await waitFor(() => expect(screen.getByRole("button", { name: /Seat S2-1/ })).toBeEnabled());
  });

  it("explains a lost race and refreshes the map", async () => {
    bookSeat.mockRejectedValue(new ApiError(409, "seat_unavailable"));
    const { user, onError } = setup();
    const callsBefore = getSeats.mock.calls.length;

    await user.click(await seat(/Seat S1-1, available/));

    expect(await screen.findByRole("status")).toHaveTextContent(/someone else just took that seat/i);
    expect(onError).toHaveBeenCalled();
    await waitFor(() => expect(getSeats.mock.calls.length).toBeGreaterThan(callsBefore));
  });
});

describe("idempotency keys across retries", () => {
  async function clickTwice(firstError) {
    bookSeat.mockRejectedValueOnce(firstError).mockResolvedValueOnce({ booking: { id: 9 }, replay: false });
    const { user } = setup();
    await user.click(await seat(/Seat S1-1, available/));
    await waitFor(() => expect(bookSeat).toHaveBeenCalledTimes(1));
    await user.click(await seat(/Seat S1-1, available/));
    await waitFor(() => expect(bookSeat).toHaveBeenCalledTimes(2));
    return bookSeat.mock.calls.map((call) => call[3]);
  }

  it("reuses the SAME key after a network drop (the outcome is unknown)", async () => {
    const [first, second] = await clickTwice(new NetworkError());
    expect(second).toBe(first);
  });

  it("reuses the same key after a 429 or a 503", async () => {
    const [a, b] = await clickTwice(new RateLimitedError(2));
    expect(b).toBe(a);
    vi.resetAllMocks();
    getSeats.mockResolvedValue(SEATS);
    document.body.innerHTML = "";
    const [c, d] = await clickTwice(new ApiError(503, "server_busy"));
    expect(d).toBe(c);
  });

  it("uses a NEW key after a definite answer such as 'seat taken'", async () => {
    const [first, second] = await clickTwice(new ApiError(409, "seat_unavailable"));
    expect(second).not.toBe(first);
  });

  it("uses a different key for a different seat", async () => {
    bookSeat.mockResolvedValue({ booking: { id: 1 }, replay: false });
    const { user } = setup();
    await user.click(await seat(/Seat S1-1, available/));
    await waitFor(() => expect(bookSeat).toHaveBeenCalledTimes(1));
    await user.click(await seat(/Seat S2-1, available/));
    await waitFor(() => expect(bookSeat).toHaveBeenCalledTimes(2));
    expect(bookSeat.mock.calls[1][3]).not.toBe(bookSeat.mock.calls[0][3]);
  });
});
