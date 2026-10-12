# PR-1 / PR-2 P0–P1 Review — Shioaji 2327 quote capture

Date: 2026-10-12. Scope: `feat/shioaji-bidask-observations-pr2` (stacked PR-2, incorporates PR-1).
Status: **P0/P1 code + synthetic regressions completed; 2327 host audit and live reconnect still unverified.** No PR merge, deploy, cron or broker trading API.

## Observed smoke evidence (from operator's 2026-10-12 console)

- Shioaji `simulation=True`, symbol 2327, 30s, Tick+BidAsk, BOARD+ODD, initial login successful.
- SDK printout `Session up`; live market data **not** authorized/verified (simulation only).
- Collector health after stop `CLOSED`, raw_tick accepted/written 15, BidAsk accepted/written 66, queue 0, last_error empty.
- File line counts captured by operator (not opened by CI): Tick BOARD **9**, ODD **6**; BidAsk BOARD **60**, ODD **6**; **81** total.
- Transport initial event absent from registered callback (`transport_state=UNVERIFIED`, `connection_verified=false`); early SDK `Session up` is not evidence of an audited quote transport session.
- **Cannot assert raw values, sha256, latency, or replay are correct until the host runs the new offline audit.** The original 81 raw lines are not in GitHub.

## P0 — implemented

- `src/market_data/intraday_audit.py`: strictly reads only the named day/symbol's four stream/lot raw files; supports .jsonl and .jsonl.gz, detects conflict, missing files, truncation, invalid JSON, wrong symbol/lot/date/type/source, duplicate/non-increasing per-session sequence, malformed identity, time anomalies and normalization rejects. Checks raw volume conversion (BOARD ×1000, ODD ×1), fixed-price scale and BidAsk top-of-book constraints. No SDK, DB, keys or network.
- `market intraday-audit`: optional 4-count input only if **all four** provided; `--output` JSON audit artifact, exit 1 for `FAIL`/`INCOMPLETE`/`REVIEW`. Captured counts are an **independent baseline only** (not exchange-completeness evidence).
- Result classifications: `PASS_RAW_ONLY` = structural / canonical contents pass for observed files; `REVIEW` = quality warning or normalization rejects; `INCOMPLETE` = a required stream file absent; `FAIL` = mismatch/corruption. All outputs explicitly say `market_completeness=UNVERIFIED`.
- Collector health now separately reports `collection_status` (`RAW_CAPTURED`, `NO_RAW_OBSERVED`, `LOSS_DETECTED`), `transport_verification_status` (`UNVERIFIED`, `EVENT_VERIFIED`, `GAP_UNRESOLVED`), `data_quality_status=UNVERIFIED`, `market_completeness_status=UNVERIFIED`, `lifecycle_status`. Captured raws never upgrade transport health or guarantee end-to-end market completeness. The legacy `state` field remains fail closed for existing consumers.

## P1 — implemented (offline fault injection)

- SDK init uses explicit quote-only `simulation=True`, supports original installed SDK's `api.quote.subscribe()/unsubscribe(..., version=QuoteVersion.v1)` and alternative SDK surface; four topics/rollback/unsubscribe covered.
- Abnormal quote worker termination stops accepting, unsubscribes without placing orders, preserves FAILED and holds collector lease until worker join on `stop()`.
- Exhausted bounded reconnection retries (`3`) fail closed and unsubscribe; never silently claim recovery.
- SDK unsubscribe exceptions never erase a still-active topic: the Collector retains failed topics, keeps its lease, reports `unsubscribe_error`, exposes `active_subscription_count`, and retries on the next `stop()`. Quote resubscription and unsubscribe are serialized with a lock; a stop request forbids subsequent resubscribes.
- Stop timeout likewise keeps the lease until the worker exits, preventing maintenance or another Collector from racing an unfinished raw write.
- Each new disconnect epoch increments `gap_count` and yields unique `collector_session_id` suffix, even when a historical gap is still unresolved. A lone SDK event 13 also opens a new gap epoch. Raw/session gaps are never synthesized or silently cleared.
- Broker login failures are sanitized before raising to avoid replaying SDK exception fields such as account identifiers; finally path attempts SDK logout. Failed stop still triggers writing a health snapshot when Collector was constructed.
- Fake SDK regression tests cover initial quote subscriptions, failure rollback, successful resubscribe, terminal retry exhaustion, two distinct disconnect epochs, worker crashes, quote-only SDK version compatibility, login error redaction, locks/retention and no trading API use.

## P0 host audit of original 81 lines — required next step

Execute **in detached smoke worktree** after `git fetch` / `git switch --detach origin/feat/shioaji-bidask-observations-pr2`. The audit is fully offline and does not need `.env`:

```bash
python3 -m app market intraday-audit \
  --cache-dir "$HOME/shioaji-2327-smoke" \
  --symbol 2327 \
  --date 2026-10-12 \
  --expected-tick-board 9 \
  --expected-tick-odd 6 \
  --expected-bidask-board 60 \
  --expected-bidask-odd 6 \
  --output "$HOME/shioaji-2327-20261012-quality.json"
```

Verify JSON status `PASS_RAW_ONLY`, `total_raw_lines=81`, `total_accepted=81`, each four rows have zero errors/warnings/rejections, and raw SHA-256 values. If not, inspect the error per file; do not edit raw to force green. Compare provider's independent market tape if true completeness is required.

## Merge / deployment gate

- PR-1 and PR-2 stay Draft. CI green + simulation 30s **is not** a true live production-feed smoke or daylong soak.
- Must review **actual** 81-line quality output before claiming P0 host data validation.
- P1 SDK **real disconnect/reconnect**, unrecoverable subscribe, long-duration capacity, broker permission and runtime versions remain untested on operator's host. Do not forcibly disconnect the service or activate broker trading.
- No new service/cron enabled; original master working tree / `app.db` untouched. Do not merge dependent PR-3/PR-4 until stack refreshed and revalidated.
