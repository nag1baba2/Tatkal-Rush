import { formatDate, formatMoney } from "../format";

const LABELS = {
  HELD: "Held",
  CONFIRMED: "Confirmed",
  EXPIRED: "Expired",
  CANCELLED: "Cancelled",
};

/** The user's recent bookings. A held one can be selected to pay for. */
export default function MyBookings({ bookings, activeId, onSelect }) {
  if (!bookings.length) return null;
  return (
    <section className="panel" aria-label="My bookings">
      <h3>My bookings</h3>
      <ul className="list">
        {bookings.map((b) => (
          <li key={b.id} className="booking-row">
            <span>
              <strong>{b.coach}-{b.seat_no}</strong> · {b.train_name} · {formatDate(b.travel_date)}
              <span className="muted"> · {formatMoney(b.fare_paise)}</span>
            </span>
            <span className={`chip ${b.status.toLowerCase()}`}>{LABELS[b.status] || b.status}</span>
            {b.status === "HELD" && b.id !== activeId && (
              <button type="button" onClick={() => onSelect(b.id)}>
                Pay for this
              </button>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
