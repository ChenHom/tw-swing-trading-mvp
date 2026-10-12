# PR-1 / PR-2 Quote Reliability and Retention — 2026-10-08

Status: OFFLINE_HARDENING_IMPLEMENTED; LIVE_SDK_NOT_VERIFIED. Both implementation pull requests stay Draft.

## Quote connection and provenance

- The adapter uses the official Shioaji quote event callback, without a second login or any trading CA/API.
- Transport codes 0 (up), 1 (down), 2 (connection error), 4 (subscription error), 5 (oversize/drop), 12 (reconnecting), 13 (reconnected), 17 (router changed), 19 (property failure) are processed in the worker, not in the SDK callback.
- Raw transport events are durably appended to data/raw/shioaji/session-events/YYYY-MM-DD/<session>.jsonl; event messages and credentials are not logged.
- Following 13, subscriptions are retried in the same SDK session with 3 bounded attempts (0.1s/0.2s backoff), for Tick and optional BidAsk.
- Missing data during disconnect is never invented or backfilled. gap_unresolved remains true / DEGRADED even after re-subscribe; canonical collector_session_id gains a new epoch suffix.
- Quote transport events are NOT regular heartbeats. No fake last_heartbeat_at or trading_session_verified flag is asserted; PR-4 cannot claim a real-time verified feed from these events alone.

## Atomic journal and maintenance

- Tick and BidAsk use a shared Linux flock protected append-only journal, O_APPEND plus per-event fsync, and an exclusive full-lifetime Collector lease.
- Offline compression refuses to run while a Collector is active, validates every complete JSONL line, creates gzip and SHA-256 manifest, verifies decompress/checksum, then deletes original only on success.
- Crash residue or partial gzip blocks maintenance rather than being silently overwritten; failed raw durability stops collection and unsubscribes.
- The new CLI command `python3 -m app market intraday-maintain --cache-dir data/raw --before-date YYYY-MM-DD` compresses files STRICTLY before the specified cutoff; it never installs a cron or service.
- Dedicated disk warning/stop threshold: 80% / 90% at maintenance; the raw writer defaults to a safer 75% stop if it shares the trading disk. A first-week storage/throughput measurement remains required.
- Retention audit checks at least 180 XTAI trading sessions; old archives are reported only. There is no automatic deletion of research inputs (D6).

## Verification and remaining limits

- Offline CI uses fake SDK events and synthetic raw files; it tests disconnects, resubscribe, archive/append locking, truncated raw, SHA manifest and preservation of files.
- Broker SDK 1.7.x live callbacks, subscription acknowledgements, limits, backpressure under realistic tick volume and overnight networking are NOT verified; live smoke needs separate approval.
- No unattended service, cron or daily live collection has been enabled. An independent approval is still required for live smoke and another one for daily service activation (D10).
- PR-3/4 are stacked on the pre-hardening PR-2 head and must later be refreshed; do not merge the stacks without replaying dependent CI.

Reference: https://sinotrade.github.io/zh/tutor/callback/event_cb/
