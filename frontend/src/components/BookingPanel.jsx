import { useEffect, useRef, useState } from "react";
import { ApiError, NetworkError, getPayment, payBooking } from "../api";
import {
  formatCountdown,
  formatDate,
  formatMoney,
  messageFor,
  newKey,
  reasonFor,
  secondsLeft,
} from "../format";

/** A clock that ticks once a second, so the hold countdown stays live. */
function useNow(intervalMs = 1000) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(timer);
  }, [intervalMs]);
  return now;
}

/**
 * Pay for a held seat.
 *
 * State flow:  idle -> paying -> success
 *                           \-> failed   (402: this attempt failed; Pay again = a NEW attempt)
 *                           \-> pending  (202: unresolved; we poll until it settles)
 *
 * Idempotency: the key is kept while the outcome is unknown (network drop, in-progress)
 * so pressing Pay again re-sends the SAME request and cannot charge twice. It is
 * discarded once the server gave a definite answer.
 */
export default function BookingPanel({ booking, session, onChanged, onError, pollMs = 3000 }) {
  const initial = booking.payment_status === "PENDING" ? "pending" : booking.payment_status === "SUCCESS" ? "success" : "idle";
  const [phase, setPhase] = useState(initial);
  const [message, setMessage] = useState("");
  const [expired, setExpired] = useState(false);
  const key = useRef(null);
  const now = useNow();

  const left = secondsLeft(booking.hold_expires_at, now);
  const confirmed = phase === "success" || booking.status === "CONFIRMED";

  // While a payment is unresolved, keep asking until it settles.
  useEffect(() => {
    if (phase !== "pending" || !pollMs) return undefined;
    let stopped = false;
    const timer = setInterval(async () => {
      try {
        const payment = await getPayment(session.token, booking.id);
        if (stopped || payment.status === "PENDING") return;
        key.current = null;
        if (payment.status === "SUCCESS") {
          setPhase("success");
          setMessage("");
        } else {
          setPhase("failed");
          setMessage(reasonFor(payment.last_error));
        }
        onChanged?.();
      } catch (e) {
        if (!(e instanceof NetworkError)) onError?.(e);
      }
    }, pollMs);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [phase, pollMs, session.token, booking.id, onChanged, onError]);

  async function pay() {
    if (phase === "paying") return;
    key.current = key.current ?? newKey();
    setPhase("paying");
    setMessage("");
    try {
      const { payment } = await payBooking(session.token, booking.id, key.current);
      if (payment.status === "SUCCESS") {
        key.current = null;
        setPhase("success");
      } else if (payment.status === "FAILED") {
        key.current = null; // the next press is a new attempt
        setPhase("failed");
        setMessage(reasonFor(payment.last_error));
      } else {
        setPhase("pending"); // keep the key: the same attempt is still in flight
      }
      onChanged?.();
    } catch (e) {
      if (e instanceof ApiError && e.detail?.startsWith("booking_not_payable")) {
        key.current = null;
        setExpired(true);
        setPhase("idle");
        onChanged?.();
        return;
      }
      setPhase("idle");
      if (e instanceof NetworkError) {
        setMessage("Connection lost. Press Pay again: you will not be charged twice.");
      } else {
        setMessage(messageFor(e));
        onError?.(e);
      }
    }
  }

  const seatLabel = `${booking.coach}-${booking.seat_no}`;
  const holdOver = (left <= 0 || expired) && phase !== "pending" && !confirmed;

  return (
    <section className="panel booking" aria-label="Your booking">
      <h3>Seat {seatLabel}</h3>
      <p className="muted">
        {booking.train_name} ({booking.train_number}) · {formatDate(booking.travel_date)}
      </p>
      <p className="fare">{formatMoney(booking.fare_paise)}</p>

      {confirmed ? (
        <p role="status" className="ok">Payment successful. Seat {seatLabel} is confirmed.</p>
      ) : holdOver ? (
        <p role="status" className="warn">This hold has expired. Pick the seat again to start over.</p>
      ) : (
        <>
          {phase !== "pending" && (
            <p className="countdown" aria-label="Time left to pay">
              Held for you: <strong>{formatCountdown(left)}</strong>
            </p>
          )}
          {phase === "pending" && (
            <p role="status" className="info">
              Payment in progress. We are confirming it with the bank and will update this automatically.
              You will not be charged twice.
            </p>
          )}
          {phase === "failed" && <p role="alert" className="warn">{message}</p>}
          {phase === "idle" && message && <p role="alert" className="warn">{message}</p>}
          {phase !== "pending" && (
            <button type="button" className="primary" onClick={pay} disabled={phase === "paying"}>
              {phase === "paying" ? "Processing…" : phase === "failed" ? `Try again, pay ${formatMoney(booking.fare_paise)}` : `Pay ${formatMoney(booking.fare_paise)}`}
            </button>
          )}
        </>
      )}
    </section>
  );
}
