import { useCallback, useEffect, useRef, useState } from "react";
import { NetworkError, bookSeat, getSeats } from "../api";
import { isDefiniteOutcome, messageFor, newKey } from "../format";

const STATUS_TEXT = { AVAILABLE: "available", HELD: "held by someone", BOOKED: "booked" };

function groupByCoach(seats) {
  const coaches = new Map();
  for (const seat of seats) {
    if (!coaches.has(seat.coach)) coaches.set(seat.coach, []);
    coaches.get(seat.coach).push(seat);
  }
  return [...coaches.entries()];
}

/**
 * The seat map for one schedule. Refreshes itself every `pollMs` (0 turns that off),
 * so other people's holds appear without a reload.
 *
 * Booking and idempotency: each seat attempt gets an Idempotency-Key. If the request
 * fails in a way that leaves the outcome unknown (network drop, 429, 5xx) the SAME key
 * is reused when the user tries again, so a retry can never book twice. A definite
 * answer (success, or a 4xx such as "seat taken") discards the key.
 */
export default function SeatMap({ schedule, session, myBookings, onBooked, onError, pollMs = 3000 }) {
  const [seats, setSeats] = useState([]);
  const [loaded, setLoaded] = useState(false);
  const [offline, setOffline] = useState(false);
  const [busySeat, setBusySeat] = useState(null);
  const [hint, setHint] = useState("");
  const keys = useRef(new Map());

  const load = useCallback(async () => {
    try {
      setSeats(await getSeats(schedule.id));
      setLoaded(true);
      setOffline(false);
    } catch (e) {
      // A dropped connection during a background refresh is not worth a sticky error
      // banner: show a small note that clears itself as soon as a refresh succeeds.
      if (e instanceof NetworkError) setOffline(true);
      else onError?.(e);
    }
  }, [schedule.id, onError]);

  useEffect(() => {
    setLoaded(false);
    setSeats([]);
    load();
    if (!pollMs) return undefined;
    const timer = setInterval(load, pollMs);
    return () => clearInterval(timer);
  }, [load, pollMs]);

  const mine = new Map();
  for (const b of myBookings || []) {
    if (b.schedule_id === schedule.id && (b.status === "HELD" || b.status === "CONFIRMED")) {
      mine.set(b.seat_id, b.status);
    }
  }

  async function choose(seat) {
    if (!session) {
      setHint("Log in to book a seat.");
      return;
    }
    if (seat.status !== "AVAILABLE" || busySeat !== null) return;
    setHint("");

    const key = keys.current.get(seat.id) ?? newKey();
    keys.current.set(seat.id, key);
    setBusySeat(seat.id);
    try {
      const { booking } = await bookSeat(session.token, schedule.id, seat.id, key);
      keys.current.delete(seat.id);
      onBooked?.(booking);
    } catch (e) {
      if (isDefiniteOutcome(e)) keys.current.delete(seat.id);
      onError?.(e);
      setHint(messageFor(e));
    } finally {
      setBusySeat(null);
      load();
    }
  }

  if (!loaded) {
    return <p className="muted">{offline ? "Can't reach the server. Retrying…" : "Loading seats…"}</p>;
  }

  return (
    <div className="seatmap">
      {offline && (
        <p role="status" className="hint">
          Connection problem, retrying. The seats shown may be out of date.
        </p>
      )}
      <ul className="legend" aria-label="Seat colours">
        <li><span className="swatch available" /> Available</li>
        <li><span className="swatch held" /> Held</li>
        <li><span className="swatch booked" /> Booked</li>
        <li><span className="swatch mine" /> Yours</li>
      </ul>
      {groupByCoach(seats).map(([coach, coachSeats]) => (
        <section key={coach} className="coach" aria-label={`Coach ${coach}`}>
          <h4>Coach {coach}</h4>
          <div className="seats">
            {coachSeats.map((seat) => {
              const yours = mine.get(seat.id);
              const state = yours ? "mine" : seat.status.toLowerCase();
              const text = yours ? (yours === "HELD" ? "yours, held" : "yours, booked") : STATUS_TEXT[seat.status];
              return (
                <button
                  key={seat.id}
                  type="button"
                  className={`seat ${state}`}
                  disabled={seat.status !== "AVAILABLE" || busySeat !== null}
                  aria-label={`Seat ${seat.coach}-${seat.seat_no}, ${text}`}
                  onClick={() => choose(seat)}
                >
                  {seat.seat_no}
                </button>
              );
            })}
          </div>
        </section>
      ))}
      {hint && <p role="status" className="hint">{hint}</p>}
    </div>
  );
}
