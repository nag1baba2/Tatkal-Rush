from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://tatkal:tatkal@localhost:5433/tatkal"
    redis_url: str = "redis://localhost:6379/0"

    # JWT. The default secret is for local development only.
    jwt_secret: str = "dev-only-secret-change-me-0123456789abcdef"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60

    # Database connection pool, PER API PROCESS. Keep
    #   api_workers x (db_pool_size + db_max_overflow) + other clients < Postgres max_connections
    # or Postgres refuses connections ("too many clients"). Compose sets max_connections=200.
    db_pool_size: int = 20
    db_max_overflow: int = 20
    db_pool_timeout: int = 10   # seconds to wait for a free connection before answering 503

    # How long a seat stays HELD while the user pays (seconds).
    hold_seconds: int = 300

    # Simulated payment gateway. Rates are probabilities per call.
    gateway_fail_rate: float = 0.3            # rejected before processing; no charge
    gateway_lost_response_rate: float = 0.1   # charged, but the response is lost (timeout)
    gateway_decline_rate: float = 0.05        # card declined; not retryable
    gateway_latency_ms: int = 0

    # Retries with exponential backoff around the gateway.
    retry_max_attempts: int = 4
    retry_base_delay: float = 0.1
    retry_max_delay: float = 2.0

    # Circuit breaker around the gateway.
    breaker_failure_threshold: int = 5
    breaker_recovery_seconds: float = 10.0

    # A PENDING payment older than this may be re-driven (same gateway key).
    payment_stale_seconds: int = 30

    # Background worker (Celery). Broker defaults to Redis database 1 so it never
    # mixes with the gateway's keys in database 0.
    celery_broker_url: str | None = None
    expiry_sweep_seconds: int = 10
    sweep_batch_size: int = 200
    # Log a warning for payments unresolved for longer than this.
    payment_alert_seconds: int = 600

    # Browser origins allowed to call the API (comma separated). The Vite dev server.
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"


    # Rate limiting (token bucket in Redis). Each request costs one token.
    rate_limit_enabled: bool = True
    # Per identity: the user (from the JWT) or, when not logged in, the client IP.
    rate_limit_capacity: int = 10                  # burst size
    rate_limit_refill_per_second: float = 5.0
    # Per client IP, across all identities: a ceiling against one IP making many accounts.
    rate_limit_ip_capacity: int = 100
    rate_limit_ip_refill_per_second: float = 50.0
    # Only set behind a proxy you control: X-Forwarded-For is trivially spoofable otherwise.
    trust_forwarded_for: bool = False


settings = Settings()
