import { useCallback, useEffect, useState } from "react";
import { ApiError, getMyBookings } from "./api";
import AuthBar from "./components/AuthBar";
import BookingPanel from "./components/BookingPanel";
import MyBookings from "./components/MyBookings";
import Search from "./components/Search";
import SeatMap from "./components/SeatMap";
import { formatDate, messageFor, secondsLeft } from "./format";
import { clearSession, loadSession, saveSession } from "./session";

export default function App() {
  const [session, setSession] = useState(loadSession);
  const [selection, setSelection] = useState(null); // { train, schedule }
  const [bookings, setBookings] = useState([]);
  const [activeId, setActiveId] = useState(null);
  const [notice, setNotice] = useState("");

  const logout = useCallback((message = "") => {
    clearSession();
    setSession(null);
    setBookings([]);
    setActiveId(null);
    setNotice(message);
  }, []);

  const handleError = useCallback(
    (e) => {
      if (e instanceof ApiError && e.status === 401) {
        logout(messageFor(e));
        return;
      }
      setNotice(messageFor(e));
    },
    [logout],
  );

  const refreshBookings = useCallback(async () => {
    if (!session) return;
    try {
      setBookings(await getMyBookings(session.token));
    } catch (e) {
      handleError(e);
    }
  }, [session, handleError]);

  useEffect(() => {
    if (!session) {
      setBookings([]);
      return;
    }
    refreshBookings();
  }, [session, refreshBookings]);

  function login(next) {
    saveSession(next);
    setSession(next);
    setNotice("");
  }

  // The booking shown in the payment panel: the one the user picked, else the newest
  // seat still held for them (a hold with a payment in flight counts even if overdue).
  const payable = (b) =>
    b.status === "HELD" && (secondsLeft(b.hold_expires_at) > 0 || b.payment_status === "PENDING");
  const active =
    bookings.find((b) => b.id === activeId && b.status !== "EXPIRED") ||
    bookings.find(payable) ||
    bookings.find((b) => b.id === activeId) ||
    null;

  function onBooked(booking) {
    setActiveId(booking.id);
    refreshBookings();
  }

  return (
    <div className="app">
      <header className="top">
        <h1>Tatkal Rush</h1>
        <AuthBar session={session} onLogin={login} onLogout={() => logout()} />
      </header>

      {notice && (
        <div role="alert" className="notice">
          <span>{notice}</span>
          <button type="button" aria-label="Dismiss" onClick={() => setNotice("")}>×</button>
        </div>
      )}

      <main className="columns">
        <div>
          <Search
            onSelect={setSelection}
            onError={handleError}
            selectedScheduleId={selection?.schedule.id}
          />
          {selection && (
            <section className="panel" aria-label="Choose a seat">
              <h2>Choose a seat</h2>
              <p className="muted">
                {selection.train.name} · {selection.train.source} → {selection.train.destination} ·{" "}
                {formatDate(selection.schedule.travel_date)}
              </p>
              <SeatMap
                key={selection.schedule.id}
                schedule={selection.schedule}
                session={session}
                myBookings={bookings}
                onBooked={onBooked}
                onError={handleError}
              />
            </section>
          )}
        </div>

        <aside>
          {session ? (
            <>
              {active ? (
                <BookingPanel
                  key={active.id}
                  booking={active}
                  session={session}
                  onChanged={refreshBookings}
                  onError={handleError}
                />
              ) : (
                <section className="panel">
                  <h3>Your booking</h3>
                  <p className="muted">Pick a seat and it is held for you while you pay.</p>
                </section>
              )}
              <MyBookings bookings={bookings} activeId={active?.id} onSelect={setActiveId} />
            </>
          ) : (
            <section className="panel">
              <h3>Your booking</h3>
              <p className="muted">Log in or register to book a seat.</p>
            </section>
          )}
        </aside>
      </main>
    </div>
  );
}
