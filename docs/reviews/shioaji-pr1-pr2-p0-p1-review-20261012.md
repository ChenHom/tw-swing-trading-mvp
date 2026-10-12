# PR-1 / PR-2 P0–P1 Review — Shioaji 2327 quote capture

Date: 2026-10-12. Scope: `feat/shioaji-bidask-observations-pr2` (stacked PR-2, incorporates PR-1).
Status: **P0/P1 code + synthetic regressions completed; P0 operator-host offline raw audit PASS_RAW_ONLY (81/81).** Live SDK reconnect / soak / production market permission still unverified. No PR merge, deploy, cron or broker trading API.

## Observed smoke evidence (from operator's 2026-10-12 console)

- Shioaji `simulation=True`, symbol 2327, 30s, Tick+BidAsk, BOARD+ODD, initial login successful.
- SDK printout `Session up`; live market data **not** authorized/verified (simulation only).
- Collector health after stop `CLOSED`, raw_tick accepted/written 15, BidAsk accepted/written 66, queue 0, last_error empty.
- File line counts captured by operator (not opened by CI): Tick BOARD **9**, ODD **6**; BidAsk BOARD **60**, ODD **6**; **81** total.
- Transport initial event absent from registered callback (`transport_state=UNVERIFIED`, `connection_verified=false`); early SDK `Session up` is not evidence of an audited quote transport session.
- **Operator subsequently ran the strict offline audit on the original host (2026-10-12, commit `8606b090`)**: `status=PASS_RAW_ONLY`, `total_raw_lines=81`, `total_accepted=81`, all four streams `errors=[]`, `warnings=[]`, `rejected={}`. SHA-256 file digests recorded below; these raw files remain on the operator's machine and were **not independently read by GitHub CI**. No exchange-level feed completeness claim is made.

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

## P0 host audit of original 81 lines — completed (operator evidence)

The operator executed the command below in detached smoke worktree at `8606b090` on 2026-10-12. The audit is fully offline and does not need `.env`:

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

**Observed result:** `PASS_RAW_ONLY`, 81 raw lines / 81 canonical accepted, no errors/warnings/rejections across all four files. This closes P0 *observed raw integrity* acceptance; independent exchange tape and transport continuity are still `UNVERIFIED`. See the per-stream SHA-256 and timing details immediately below.

### Operator-reported per-file quality evidence

| Stream | Raw / accepted | SHA-256 (file bytes) | Reported max event→receipt delay | Errors / warnings / rejects |
|---|---:|---|---:|---|
| Tick BOARD | 9 / 9 | `09985b529d75ff738487c1c7f85de0964d8804e325d8f5b97cbb1dd0d72245b8` | -0.002441 s | 0 / 0 / 0 |
| Tick ODD | 6 / 6 | `624bf29e9d5262dd802d93151bc74b94a4285284ad779149868b90738fdcebe3` | +0.190472 s | 0 / 0 / 0 |
| BidAsk BOARD | 60 / 60 | `be57117a14a41612fe687e7d93ec5f548e1e99a190ce739a1fb7a32d4ec0fa5f` | +0.163159 s | 0 / 0 / 0 |
| BidAsk ODD | 6 / 6 | `6fa0c1969e3733471b39da5074163329fa56730ef2100183e5f8967205136b3c` | +0.190694 s | 0 / 0 / 0 |

- All four streams have `errors=[]`, `warnings=[]`, `rejected={}` and timestamps on **2026-10-12** (Asia/Taipei).
- Tick BOARD reported max delay is **negative**: all observed event timestamps were a few milliseconds ahead of their receipt timestamps. Small permitted skew is not proof of zero transport delay.
- Reported sample units: Tick BOARD **1,000 shares**, ODD **220 shares**; BidAsk best bid volumes BOARD **27,000 shares**, ODD **4,705 shares**. These represent separate markets and are not pooled.
- Four digests are stored as operator-provided checkpoints, **not independently recomputed** by GitHub. The per-event stream audit exercises canonical conversion and validation but does not validate broker-vs-exchange end-to-end completeness or full-day coverage.

## Final PR boundary and P1 release-gate check (2026-10-12)

The checked branch graph is a valid stack:

- PR #2: `master` <- `feat/shioaji-tick-collector-pr1`; stand-alone Tick baseline (35 isolated offline tests).
- PR #3: `feat/shioaji-tick-collector-pr1` <- `feat/shioaji-bidask-observations-pr2`; BidAsk + P0/P1 hardening and SDK adaptation. PR #3 includes PR #2 by ancestry; no need for an additional PR.
- **Important:** the newest shared Collector hardening is currently in PR #3; PR #2 by itself does **not** contain all P0/P1 fixes. Do not claim PR #2 alone is equivalent to the validated stacked build.
- PR #2 and PR #3 remain Draft, not merged. Recommended merge order once release gate is satisfied: **#2 into master first, then retarget #3 to master and run full CI again, then #3**. A commit/diff audit must ensure neither the shared Collector code nor the BidAsk files are accidentally dropped. No force pushing / branch deletion.

During final review the one-off smoke CLI was further hardened to **exit non-zero** on a worker failure, outstanding SDK subscription, undrained queue, loss detected or missing observed Tick/BidAsk stream; it now prints `smoke_status` and `smoke_checks` after successful logout. Previously, a degraded Collector could return 0, which was a release-blocking false-positive. Synthetic regressions cover all negative cases, including legacy SDK fake behavior.

The user supplied the post-hardening 60-second isolated host run and **all six smoke checks passed**. The exact command and checks are in `docs/operations/shioaji-2327-live-smoke-checklist.md` under 'P1 最終實機驗收'. The final healthy start/stop host gate is now satisfied; remaining actual disconnect/soak/live permission gates stay separate from code merge and runtime deployment.

## Final 60-second P1 host smoke acceptance (2026-10-12, operator report)

User executed the latest one-off Shioaji SDK **1.3.2** on `tw-day-trading-smoke`:
`simulation=True`, symbol 2327, `--with-bidask`, `--duration-seconds 60`, isolated
`$HOME/shioaji-2327-final-smoke` root. The CLI reported:

- `smoke_status=PASS_CAPTURE_ONLY`, six `smoke_checks` all `true`.
- Tick 22 accepted / raw written; BidAsk 135 accepted / raw written; **157 total**.
- `health.state=CLOSED`, `lifecycle_status=STOPPED`,
  `active_subscription_count=0`, `queue_depth=0`, `last_error=""`.
- Four topics configured, no per-lot count in this report: **do not claim every lot/topic observed data in this 60-second run**.
- `transport_verification_status=UNVERIFIED`, `data_quality_status=UNVERIFIED`,
  `market_completeness_status=UNVERIFIED`: lack of an independently audited
  transport session or exchange-completeness evidence is intentional.
- The separate 30-second 81/81 raw audit from earlier remains the P0
  structure/value evidence; this second 157-line capture has not been individually
  audited and is not merged with the earlier raw files.

**Acceptance:** P1 isolated SDK healthy start / raw capture / unsubscribe /
shutdown criterion passed with operator-provided console output. True
forced disconnect, extended multi-hour reliability, official production market feed
and long-term cron/systemd remain outside this acceptance.

### PR stacking / integration

- PR #2 Tick base was merged to `master` as merge commit
  `752d713e59ab9a05507ff0bba88a5751efbde023`.
- PR #3 was subsequently retargeted from PR #2's former head to `master`.
  GitHub reports `mergeable_state=clean` and exactly 17 PR-2 delta files;
  the previous head ancestry remains intact.
- This review-file update intentionally triggers a new full PR-2 offline CI at
  the rebased PR target before the second merge. **Never change production
  Shioaji mode or enable a collector service as part of the code merge.**

## Merge / deployment gate

- PR-1 (#2) was merged to master after successful offline checks; PR-2 (#3) will pass an additional retargeted CI before code merge. Simulation 60s **is not** a true live production-feed smoke or daylong soak.
- **P0 host raw-content audit accepted from operator-provided output** (81/81 valid with SHA-256 per file). This is not equivalent to confirming the complete market feed; the original files were not transferred to CI.
- P1 SDK **real disconnect/reconnect**, unrecoverable subscribe, long-duration capacity, broker permission and runtime versions remain untested on operator's host. Do not forcibly disconnect the service or activate broker trading.
- No new service/cron enabled; original master working tree / `app.db` untouched. Do not merge dependent PR-3/PR-4 until stack refreshed and revalidated.
