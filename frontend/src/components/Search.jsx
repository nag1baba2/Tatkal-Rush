import { useEffect, useState } from "react";
import { getSchedules, searchTrains } from "../api";
import { formatDate, formatMoney, formatTime } from "../format";

/** Search trains, pick one, then pick a date. Calls onSelect({ train, schedule }). */
export default function Search({ onSelect, onError, selectedScheduleId }) {
  const [source, setSource] = useState("");
  const [destination, setDestination] = useState("");
  const [trains, setTrains] = useState(null);
  const [train, setTrain] = useState(null);
  // null = still loading, [] = loaded and empty. They must look different to the user.
  const [schedules, setSchedules] = useState(null);

  async function runSearch(e) {
    e?.preventDefault();
    setTrain(null);
    setSchedules(null);
    try {
      setTrains(await searchTrains({ source, destination }));
    } catch (err) {
      onError?.(err);
    }
  }

  // Show every train on first load so there is something to click straight away.
  useEffect(() => {
    runSearch();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function pickTrain(t) {
    setTrain(t);
    setSchedules(null);
    try {
      setSchedules(await getSchedules(t.id));
    } catch (err) {
      setSchedules([]);
      onError?.(err);
    }
  }

  return (
    <section className="panel" aria-label="Find a train">
      <h2>Find a train</h2>
      <form className="search" onSubmit={runSearch}>
        <input
          aria-label="From"
          placeholder="From (e.g. Delhi)"
          value={source}
          onChange={(e) => setSource(e.target.value)}
        />
        <input
          aria-label="To"
          placeholder="To (e.g. Mumbai)"
          value={destination}
          onChange={(e) => setDestination(e.target.value)}
        />
        <button type="submit" className="primary">Search</button>
      </form>

      {trains && trains.length === 0 && <p className="muted">No trains match that search.</p>}
      <ul className="list">
        {(trains || []).map((t) => (
          <li key={t.id}>
            <button
              type="button"
              className={`row ${train?.id === t.id ? "active" : ""}`}
              onClick={() => pickTrain(t)}
            >
              <strong>{t.name}</strong> <span className="muted">({t.number})</span>
              <span>{t.source} → {t.destination}</span>
            </button>
          </li>
        ))}
      </ul>

      {train && schedules === null && <p className="muted">Loading dates…</p>}
      {train && schedules?.length === 0 && <p className="muted">No upcoming dates for this train.</p>}
      {train && schedules?.length > 0 && (
        <>
          <h3>Choose a date</h3>
          <ul className="list">
            {schedules.map((s) => (
              <li key={s.id}>
                <button
                  type="button"
                  className={`row ${selectedScheduleId === s.id ? "active" : ""}`}
                  onClick={() => onSelect({ train, schedule: s })}
                >
                  <strong>{formatDate(s.travel_date)}</strong>
                  <span>departs {formatTime(s.departs_at)} UTC · {formatMoney(s.fare_paise)}</span>
                  <span className="muted">{s.seats_available} of {s.seats_total} seats free</span>
                </button>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
