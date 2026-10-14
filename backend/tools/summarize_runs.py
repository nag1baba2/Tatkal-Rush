"""Turn the run reports in /loadtest-data into a Markdown results table.

  docker compose exec api python -m tools.summarize_runs [name-prefix ...]

Every number comes from a *.report.json written by tools.check_invariants, so nothing
in the README has to be typed by hand. With several runs of the same scenario
(names like 100x-a, 100x-b) each run gets its own row.
"""

import json
import pathlib
import sys

DATA = pathlib.Path("/loadtest-data")


def fmt_ms(value):
    return "-" if value is None else (f"{value / 1000:.1f} s" if value >= 1000 else f"{value:.0f} ms")


def row(report: dict) -> list[str]:
    env = report["environment"]
    s = report["summary"]
    agg = (report.get("locust") or {}).get("Aggregated") or {}
    outcomes = report.get("outcomes") or {}
    checks = report["checks"]
    passed = sum(1 for c in checks if c["ok"])
    sold = s["seats_sold"]
    ok = outcomes.get("outcomes", {})
    limiter = s.get("rate_limiter_stats", {})
    return [
        report["name"],
        str(s["users"]),
        str(env["api_workers"]),
        str(env["rate_limiter"]),
        f"{sold} / {s['seats']}",
        str(agg.get("requests", "-")),
        f"{agg.get('failures', '-')}",
        f"{agg['rps']:.0f}" if agg else "-",
        fmt_ms(agg.get("median_ms")),
        fmt_ms(agg.get("p95_ms")),
        fmt_ms(agg.get("p99_ms")),
        str(limiter.get("denied", 0)),
        f"{passed}/{len(checks)}",
    ]


def main() -> None:
    prefixes = sys.argv[1:]
    reports = []
    for path in sorted(DATA.glob("*.report.json")):
        if prefixes and not any(path.name.startswith(p) for p in prefixes):
            continue
        reports.append(json.loads(path.read_text()))
    header = ["run", "users", "API workers", "limiter", "seats sold", "requests", "failed", "req/s",
              "median", "p95", "p99", "429s", "invariants"]
    print("| " + " | ".join(header) + " |")
    print("|" + "|".join("---" for _ in header) + "|")
    for report in reports:
        print("| " + " | ".join(row(report)) + " |")
    print()
    print("What the simulated users went through (and what the system did about it)\n")
    header2 = ["run", "users told confirmed", "sold out", "gave up on payment", "double-clicks sent",
               "same-key retries", "gateway calls", "real charges", "payments failed (reason)"]
    print("| " + " | ".join(header2) + " |")
    print("|" + "|".join("---" for _ in header2) + "|")
    for report in reports:
        o = report.get("outcomes") or {}
        res = o.get("outcomes", {})
        s2 = report["summary"]
        dup = o.get("duplicates_sent", {})
        retries = o.get("retries", {})
        gave_up = res.get("payment_abandoned", 0) + res.get("payment_error", 0) + res.get("payment_unresolved", 0)
        print("| " + " | ".join([
            report["name"],
            str(res.get("confirmed", 0)),
            str(res.get("sold_out", 0)),
            str(gave_up),
            f"{dup.get('booking', 0)} booking / {dup.get('payment', 0)} payment",
            f"{retries.get('booking', 0)} booking / {retries.get('payment', 0)} payment",
            str(s2["gateway_stats"].get("calls", 0)),
            str(s2["real_charges_on_gateway_ledger"]),
            str(s2.get("failed_payment_reasons") or "none"),
        ]) + " |")
    print()
    for report in reports:
        cpu = DATA / f"{report['name']}.peak_cpu.txt"
        host = DATA / f"{report['name']}.host.txt"
        print(f"{report['name']}: peak CPU (100% = one core): {cpu.read_text().strip() if cpu.exists() else 'n/a'}")
    if reports and (DATA / f"{reports[0]['name']}.host.txt").exists():
        print("\n" + (DATA / f"{reports[0]['name']}.host.txt").read_text().strip())


if __name__ == "__main__":
    main()
