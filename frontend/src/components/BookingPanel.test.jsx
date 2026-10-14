import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, NetworkError, getPayment, payBooking } from "../api";
import BookingPanel from "./BookingPanel";

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal()),
  payBooking: vi.fn(),
  getPayment: vi.fn(),
}));

const SESSION = { email: "a@b.co", token: "tok" };

const booking = (over = {}) => ({
  id: 11,
  status: "HELD",
  coach: "S1",
  seat_no: 4,
  train_name: "Tatkal Express",
  train_number: "12951",
  travel_date: "2026-10-15",
  fare_paise: 85000,
  hold_expires_at: new Date(Date.now() + 5 * 60_000).toISOString(),
  payment_status: null,
  ...over,
});

const result = (status, extra = {}) => ({
  httpStatus: { SUCCESS: 200, PENDING: 202, FAILED: 402 }[status],
  payment: { id: 1, status, last_error: null, ...extra },
  replay: false,
});

function setup(over = {}, props = {}) {
  const onChanged = vi.fn();
  const onError = vi.fn();
  const utils = render(
    <BookingPanel
      booking={booking(over)}
      session={SESSION}
      onChanged={onChanged}
      onError={onError}
      pollMs={0}
      {...props}
    />,
  );
  return { onChanged, onError, user: userEvent.setup(), ...utils };
}

const payButton = (name = /^Pay ₹850\.00$/) => screen.findByRole("button", { name });
const keysUsed = () => payBooking.mock.calls.map((call) => call[2]);

beforeEach(() => vi.resetAllMocks());

describe("before paying", () => {
  it("shows the seat, the fare and a live hold countdown", async () => {
    setup();
    expect(screen.getByRole("heading", { name: "Seat S1-4" })).toBeInTheDocument();
    expect(screen.getByText(/Tatkal Express/)).toBeInTheDocument();
    expect(screen.getByText("₹850.00")).toBeInTheDocument();
    expect(screen.getByLabelText("Time left to pay")).toHaveTextContent(/[45]:\d\d/);
    expect(await payButton()).toBeEnabled();
  });

  it("says the hold has expired, with no Pay button, once the time is up", () => {
    setup({ hold_expires_at: new Date(Date.now() - 1000).toISOString() });
    expect(screen.getByRole("status")).toHaveTextContent(/hold has expired/i);
    expect(screen.queryByRole("button", { name: /pay/i })).not.toBeInTheDocument();
  });

  it("shows an already confirmed booking as confirmed", () => {
    setup({ status: "CONFIRMED", payment_status: "SUCCESS" });
    expect(screen.getByRole("status")).toHaveTextContent(/payment successful/i);
    expect(screen.queryByRole("button", { name: /pay/i })).not.toBeInTheDocument();
  });
});

describe("paying", () => {
  it("confirms the seat on success", async () => {
    payBooking.mockResolvedValue(result("SUCCESS"));
    const { user, onChanged } = setup();

    await user.click(await payButton());

    expect(await screen.findByRole("status")).toHaveTextContent(/payment successful.*S1-4 is confirmed/i);
    expect(payBooking).toHaveBeenCalledWith("tok", 11, expect.any(String));
    expect(onChanged).toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: /pay/i })).not.toBeInTheDocument();
  });

  it("sends only one request when Pay is pressed twice quickly", async () => {
    let finish;
    payBooking.mockReturnValue(new Promise((resolve) => (finish = resolve)));
    const { user } = setup();

    const button = await payButton();
    await user.click(button);
    await user.click(screen.getByRole("button", { name: /processing/i }));
    expect(payBooking).toHaveBeenCalledTimes(1);
    finish(result("SUCCESS"));
    await screen.findByText(/payment successful/i);
  });

  it("explains a decline, reassures the user, and retries as a NEW attempt", async () => {
    payBooking
      .mockResolvedValueOnce(result("FAILED", { last_error: "declined" }))
      .mockResolvedValueOnce(result("SUCCESS"));
    const { user } = setup();

    await user.click(await payButton());
    expect(await screen.findByRole("alert")).toHaveTextContent(/card was declined/i);

    await user.click(await payButton(/try again/i));
    await screen.findByText(/payment successful/i);

    const [first, second] = keysUsed();
    expect(second).not.toBe(first); // a decline is final, so the retry is a new attempt
  });

  it("tells the user they were not charged when the payment service is down", async () => {
    payBooking.mockResolvedValue(result("FAILED", { last_error: "retries_exhausted" }));
    const { user } = setup();
    await user.click(await payButton());
    expect(await screen.findByRole("alert")).toHaveTextContent(/not charged/i);
  });

  it("re-sends the SAME request after a network drop, so it cannot charge twice", async () => {
    payBooking.mockRejectedValueOnce(new NetworkError()).mockResolvedValueOnce(result("SUCCESS"));
    const { user } = setup();

    await user.click(await payButton());
    expect(await screen.findByRole("alert")).toHaveTextContent(/not be charged twice/i);

    await user.click(await payButton());
    await screen.findByText(/payment successful/i);
    const [first, second] = keysUsed();
    expect(second).toBe(first);
  });

  it("reports an expired hold from the server", async () => {
    payBooking.mockRejectedValue(new ApiError(409, "booking_not_payable:expired"));
    const { user, onChanged } = setup();
    await user.click(await payButton());
    expect(await screen.findByRole("status")).toHaveTextContent(/hold has expired/i);
    expect(onChanged).toHaveBeenCalled();
  });

  it("passes other errors up to the app", async () => {
    payBooking.mockRejectedValue(new ApiError(404, "booking_not_found"));
    const { user, onError } = setup();
    await user.click(await payButton());
    await waitFor(() => expect(onError).toHaveBeenCalled());
  });
});

describe("a payment that is still in progress", () => {
  it("says so, hides the Pay button, and polls until it settles", async () => {
    payBooking.mockResolvedValue(result("PENDING"));
    getPayment
      .mockResolvedValueOnce({ status: "PENDING" })
      .mockResolvedValue({ status: "SUCCESS" });
    const { user, onChanged } = setup({}, { pollMs: 20 });

    await user.click(await payButton());
    expect(await screen.findByRole("status")).toHaveTextContent(/payment in progress/i);
    expect(screen.queryByRole("button", { name: /pay/i })).not.toBeInTheDocument();

    expect(await screen.findByText(/payment successful/i)).toBeInTheDocument();
    expect(getPayment.mock.calls.length).toBeGreaterThanOrEqual(2);
    expect(getPayment).toHaveBeenCalledWith("tok", 11);
    expect(onChanged).toHaveBeenCalled();
  });

  it("shows the reason and offers a retry if it settles as failed", async () => {
    payBooking.mockResolvedValue(result("PENDING"));
    getPayment.mockResolvedValue({ status: "FAILED", last_error: "declined" });
    const { user } = setup({}, { pollMs: 20 });

    await user.click(await payButton());
    expect(await screen.findByRole("alert")).toHaveTextContent(/card was declined/i);
    expect(await payButton(/try again/i)).toBeEnabled();
  });

  it("keeps waiting, not 'expired', for an overdue hold whose payment is still pending", async () => {
    getPayment.mockResolvedValue({ status: "PENDING" });
    setup(
      { hold_expires_at: new Date(Date.now() - 60_000).toISOString(), payment_status: "PENDING" },
      { pollMs: 20 },
    );
    expect(screen.getByRole("status")).toHaveTextContent(/payment in progress/i);
    expect(screen.queryByText(/hold has expired/i)).not.toBeInTheDocument();
    await waitFor(() => expect(getPayment).toHaveBeenCalled());
  });

  it("survives a dropped connection while polling", async () => {
    payBooking.mockResolvedValue(result("PENDING"));
    getPayment.mockRejectedValueOnce(new NetworkError()).mockResolvedValue({ status: "SUCCESS" });
    const { user, onError } = setup({}, { pollMs: 20 });
    await user.click(await payButton());
    expect(await screen.findByText(/payment successful/i)).toBeInTheDocument();
    expect(onError).not.toHaveBeenCalled(); // a blip while polling is not shown as an error
  });
});
