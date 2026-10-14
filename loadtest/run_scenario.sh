#!/usr/bin/env bash
# Run one load scenario end to end and judge it.
#
#   loadtest/run_scenario.sh NAME USERS SPAWN_RATE MAX_SECONDS LIMITER WORKERS [SEATS] [ROUNDS]
#
#   NAME         label for the output files in loadtest/data/
#   USERS        simulated users (10 = "1x", 1000 = "100x")
#   SPAWN_RATE   users started per second (200 = a near-instant rush)
#   MAX_SECONDS  hard cap on the run (it ends sooner once every user is done)
#   LIMITER      "off" = rate limiting bypassed (correctness/throughput runs)
#                "on"  = rate limiting active (shows bots being blocked)
#   WORKERS      uvicorn worker processes for the API
#   SEATS        hot seats (default 40)
#   ROUNDS       bookings each user makes one after another (default 1)
#
# Examples:
#   loadtest/run_scenario.sh 1x   10   10  180 off 1 400 20
#   loadtest/run_scenario.sh 100x 1000 200 180 off 1
#   loadtest/run_scenario.sh 100x-4w 1000 200 180 off 4
#   loadtest/run_scenario.sh 100x-limited 1000 200 180 on 1
#
# Set KEEP=1 to keep the run's rows in the database afterwards.
set -euo pipefail

export MSYS_NO_PATHCONV=1   # stop Git Bash rewriting /container/paths into C:\ paths

NAME=${1:?name}; USERS=${2:?users}; RATE=${3:?spawn rate}; MAXS=${4:?max seconds}
LIMITER=${5:?on|off}; WORKERS=${6:?workers}; SEATS=${7:-40}; ROUNDS=${8:-1}

cd "$(dirname "$0")/.."

# Conditions of the load test: short holds so seats recycle during the run, and a short
# "stale payment" threshold so the background worker settles unresolved payments quickly.
export HOLD_SECONDS=${HOLD_SECONDS:-20}
export PAYMENT_STALE_SECONDS=${PAYMENT_STALE_SECONDS:-10}
export UVICORN_ARGS="--workers ${WORKERS}"

echo "=== scenario '${NAME}': ${USERS} users x ${ROUNDS} booking(s) each, ${SEATS} seats, ${WORKERS} API worker(s), rate limiter ${LIMITER}"
echo "    hold=${HOLD_SECONDS}s stale-payment=${PAYMENT_STALE_SECONDS}s, random gateway (30% reject / 10% lost response / 5% decline)"

docker compose up -d >/dev/null
# Always start from fresh processes: it picks up code changes (compose alone would not restart
# an unchanged service) and gives every scenario a fresh circuit breaker and connection pool.
docker compose restart api worker beat >/dev/null
for _ in $(seq 1 60); do
  curl -sf -m 3 http://localhost:8000/health >/dev/null && break
  sleep 2
done
curl -sf -m 3 http://localhost:8000/health >/dev/null || { echo "API did not become healthy"; exit 1; }
sleep 3   # let the worker and beat finish starting

{
  echo "date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "docker VM: $(docker info --format '{{.NCPU}} CPUs, {{.MemTotal}} bytes RAM')"
  echo "uvicorn: ${UVICORN_ARGS}"
} > "loadtest/data/${NAME}.host.txt"

if [ "$LIMITER" = "off" ]; then
  docker compose exec -T api python -c "from app.ratelimit import set_bypass; set_bypass(1800)"
else
  docker compose exec -T api python -c "from app.ratelimit import clear_bypass; clear_bypass()"
fi

docker compose exec -T -e "UVICORN_ARGS=${UVICORN_ARGS}" -e "API_WORKERS=${WORKERS}" -e "LIMITER_MODE=${LIMITER}" \
  api python -m tools.provision --name "$NAME" --users "$USERS" --seats "$SEATS" --rounds "$ROUNDS" \
  --out "/loadtest-data/${NAME}.run.json"

# Sample every container's CPU while the load runs, to see what is actually saturated.
CPU_FILE="loadtest/data/${NAME}.cpu.csv"
( while :; do docker stats --no-stream --format '{{.Name}},{{.CPUPerc}},{{.MemUsage}}'; sleep 2; done ) > "$CPU_FILE" 2>/dev/null &
SAMPLER_PID=$!

START=$(date +%s)
docker compose --profile loadtest run --rm \
  -e "RUN_FILE=/loadtest-data/${NAME}.run.json" -e "OUT_PREFIX=/loadtest-data/${NAME}" -e "TOTAL_USERS=${USERS}" \
  locust -f /mnt/locustfile.py --headless -u "$USERS" -r "$RATE" -t "${MAXS}s" --stop-timeout 10 \
  --csv "/loadtest-data/${NAME}" --only-summary --exit-code-on-error 0
echo "    load phase took $(( $(date +%s) - START ))s wall-clock"
kill "$SAMPLER_PID" 2>/dev/null || true
wait "$SAMPLER_PID" 2>/dev/null || true
# Peak CPU per service. 100% = one full core (the Docker VM has several).
awk -F, '{ gsub("%", "", $2); n=$1; sub(/^tatkal-rush-/, "", n); if (n ~ /^locust-run/) n="locust"; sub(/-[0-9]+$/, "", n);
           if ($2+0 > max[n]) max[n] = $2+0 }
         END { for (n in max) printf "%s=%.0f%% ", n, max[n]; printf "\n" }' "$CPU_FILE" > "loadtest/data/${NAME}.peak_cpu.txt"
echo "    peak CPU (100% = one core): $(cat "loadtest/data/${NAME}.peak_cpu.txt")"

docker compose exec -T api python -c "from app.ratelimit import clear_bypass; clear_bypass()"

CLEANUP=--cleanup
[ "${KEEP:-0}" = "1" ] && CLEANUP=""
set +e
docker compose exec -T api python -m tools.check_invariants \
  --run "/loadtest-data/${NAME}.run.json" --name "$NAME" --locust-prefix "/loadtest-data/${NAME}" \
  --wait-settle 90 --report "/loadtest-data/${NAME}.report.json" $CLEANUP
STATUS=$?
echo "=== invariant check exit code: ${STATUS} (0 = every check passed)"
exit $STATUS
