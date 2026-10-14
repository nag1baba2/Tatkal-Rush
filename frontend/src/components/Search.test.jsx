import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, getSchedules, searchTrains } from "../api";
import Search from "./Search";

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal()),
  searchTrains: vi.fn(),
  getSchedules: vi.fn(),
}));

const TRAIN = { id: 12, number: "12951", name: "Tatkal Express", source: "New Delhi", destination: "Mumbai Central" };
const SCHEDULE = {
  id: 23,
  train_id: 12,
  travel_date: "2026-10-15",
  departs_at: "2026-10-15T16:35:00Z",
  fare_paise: 85000,
  seats_total: 72,
  seats_available: 70,
};

function setup() {
  const onSelect = vi.fn();
  const onError = vi.fn();
  render(<Search onSelect={onSelect} onError={onError} />);
  return { onSelect, onError, user: userEvent.setup() };
}

beforeEach(() => {
  vi.resetAllMocks();
  searchTrains.mockResolvedValue([TRAIN]);
});

describe("Search", () => {
  it("lists every train on first load", async () => {
    setup();
    expect(await screen.findByText("Tatkal Express")).toBeInTheDocument();
    expect(searchTrains).toHaveBeenCalledWith({ source: "", destination: "" });
  });

  it("searches with what the user typed", async () => {
    const { user } = setup();
    await screen.findByText("Tatkal Express");
    await user.type(screen.getByLabelText("From"), "Delhi");
    await user.type(screen.getByLabelText("To"), "Mumbai");
    await user.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(searchTrains).toHaveBeenLastCalledWith({ source: "Delhi", destination: "Mumbai" }));
  });

  it("says so when nothing matches", async () => {
    searchTrains.mockResolvedValue([]);
    setup();
    expect(await screen.findByText(/no trains match/i)).toBeInTheDocument();
  });

  it("shows the dates with fare and free seats, and reports the chosen one", async () => {
    getSchedules.mockResolvedValue([SCHEDULE]);
    const { user, onSelect } = setup();

    await user.click(await screen.findByRole("button", { name: /Tatkal Express/ }));
    const date = await screen.findByRole("button", { name: /Thu, 15 Oct 2026/ });
    expect(date).toHaveTextContent("₹850.00");
    expect(date).toHaveTextContent("70 of 72 seats free");

    await user.click(date);
    expect(onSelect).toHaveBeenCalledWith({ train: TRAIN, schedule: SCHEDULE });
  });

  it("says 'Loading' while the dates load, never 'No upcoming dates' (a bug found in the browser)", async () => {
    let finish;
    getSchedules.mockReturnValue(new Promise((resolve) => (finish = resolve)));
    const { user } = setup();

    await user.click(await screen.findByRole("button", { name: /Tatkal Express/ }));
    expect(screen.getByText(/loading dates/i)).toBeInTheDocument();
    expect(screen.queryByText(/no upcoming dates/i)).not.toBeInTheDocument();

    finish([SCHEDULE]);
    expect(await screen.findByRole("button", { name: /Thu, 15 Oct 2026/ })).toBeInTheDocument();
    expect(screen.queryByText(/loading dates/i)).not.toBeInTheDocument();
  });

  it("says 'No upcoming dates' only once it knows the list is empty", async () => {
    getSchedules.mockResolvedValue([]);
    const { user } = setup();
    await user.click(await screen.findByRole("button", { name: /Tatkal Express/ }));
    expect(await screen.findByText(/no upcoming dates/i)).toBeInTheDocument();
    expect(screen.queryByText(/loading dates/i)).not.toBeInTheDocument();
  });

  it("reports a failure to load dates and stops showing 'Loading'", async () => {
    getSchedules.mockRejectedValue(new ApiError(500));
    const { user, onError } = setup();
    await user.click(await screen.findByRole("button", { name: /Tatkal Express/ }));
    await waitFor(() => expect(onError).toHaveBeenCalled());
    expect(screen.queryByText(/loading dates/i)).not.toBeInTheDocument();
  });
});
