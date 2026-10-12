# Multi-platform Live Collection Implementation Plan

> **For agentic workers:** Execute this plan incrementally. Write a failing test before changing behavior, keep each slice passing, and run the repository engineering review before reporting completion.

**Goal:** Add a CLI entry that attempts live collection across selected platforms, clearly reports login/authorization/platform limits, and stores only data actually collected in this run.

**Architecture:** Add `collect-live` to the existing `main.py` CLI. Reuse `ADAPTERS`, `PLATFORM_ORDER`, `platform_service.collect_platform_comments()`, existing XHS browser extraction, and `unified_store`; do not add another platform registry or data layer. XHS reuses the existing `CrawlerEngine.run_xhs_test_stream()` browser path with a manual-login gate, then calls the existing idempotent `import_xhs_history()` to sync real legacy rows into unified tables; do not invent platform comment IDs. Bilibili and the remaining adapters use `platform_service.collect_platform_comments()` and return their real access status.

**Tech Stack:** Python 3.10, argparse, Selenium already used by the XHS adapter, SQLite unified store, pytest, ruff.

## Global Constraints

- Only collect from platform-visible data through existing authorized or public access paths.
- XHS login and any verification are completed manually by the user in visible Chrome; no password or browser session is logged, exported, or committed.
- Stop the current platform on login wall, challenge, 401/403/412/429, or equivalent restriction; do not retry, rotate IP/UA/accounts, forge requests, or crack signatures.
- `--platform all` isolates failures and reports every selected platform; local history is not counted as data collected in this live run.
- No new runtime dependency, platform registry, database schema, or secrets.
- Keep the pre-existing dirty `main` worktree untouched; work only on `codex/multiplatform-live-collection`.

---

### Task 1: CLI contract and platform-by-platform result summary

**Files:**
- Modify: `main.py`
- Test: `tests/test_live_collection.py`

- [x] Add parser test for `collect-live --platform all --keyword 露营 --limit 10` and for a single-platform `--content-id` request.
- [x] Add argument validation test: exactly one of `--keyword` or `--content-id` is required; reject both and neither before creating a database or adapter. Reject `--content-id` with `all` and with XHS, whose existing live stream is keyword-based.
- [x] Add orchestration test by monkeypatching `platform_service.collect_platform_comments`: a failed first platform must not prevent calls for later platforms, and each platform's status/count/reason must appear in the summary.
- [x] Implement the smallest CLI handler: loop over `PLATFORM_ORDER` for `all`, otherwise use the selected platform; call the existing service once per platform; print explicit per-platform results and an overall `completed/partial` summary. Do not claim all-platform success when any platform is unavailable.
- [x] Run `pytest tests/test_live_collection.py -q --basetemp=<fresh path>` and then the existing offline suite.

### Task 2: Make XHS authorized-session collection real and explicit

**Files:**
- Modify: `platforms/xiaohongshu.py`
- Test: `tests/test_live_collection.py`

- [x] Add tests first for: a login/captcha status on the search page prompts once for manual user action; re-checking a still-blocked page ends the XHS run; an unblocked search continues; an EOF/non-interactive terminal stops cleanly.
- [x] In `crawl_engine.py`, reuse the existing `run_xhs_test_stream()` route; after opening the search page, if `CrawlGuard` reports `login_required` or `captcha_required`, call the existing manual `wait_user_login()` once and re-check. Do not pause/retry `rate_limited`, `blocked`, or `access_forbidden`.
- [x] In `platform_service.py`, add a narrow XHS branch that runs this existing stream with a visible browser, a bounded target/candidate/comment limit, DB persistence enabled, AI summary disabled, and closes the browser in `finally`.
- [x] After the XHS stream, call existing `import_xhs_history()` so this run's real content/comments appear in unified tables. Preserve original legacy rows. Never synthesize a platform-native comment ID to fill a missing field.
- [x] Record XHS search/content/comments outcomes in append-only `access_records` with `access_mode=AUTHORIZED_SESSION`; do not include cookies, page storage, passwords, token query parameters, or full browser profile paths.
- [x] Run focused XHS path tests with fake drivers and network disabled; no live XHS request is part of automated tests.

### Task 3: Audit actual requests and stop on restrictions

**Files:**
- Modify: `platform_service.py`
- Test: `tests/test_live_collection.py`

- [x] Add tests first to assert each actual search/detail/comments/replies attempt is appended to `access_records` with status, access mode, source, record count, and failure reason when applicable.
- [x] Add a test where comments return `RATE_LIMITED`/`BLOCKED`/`AUTH_REQUIRED`: the service makes no further request for that platform and records a partial/failed run rather than marking it as an unqualified success.
- [x] Add a successful fake-adapter test proving real-format items are upserted into `platform_content` and `platform_comments`, while an unsupported adapter adds no fake rows.
- [x] Implement a small private helper in `platform_service.py` to map an `AccessResult` into the existing append-only `record_access()` contract. Record only calls actually made; do not record a probe as a successful fetch.
- [x] On restriction statuses, stop remaining capability requests for that platform while allowing the outer CLI loop to continue to the next platform.
- [x] Run focused service tests and the existing `tests/test_platform_access.py` offline selection.

### Task 4: Document the command and platform limits

**Files:**
- Modify: `README.md`
- Modify: `USAGE.md`
- Test: CLI help assertions in `tests/test_live_collection.py`

- [x] Update README with a natural Chinese section and one command example for `collect-live --platform all --keyword ...`.
- [x] State plainly that XHS live search/comments require user login in a visible local Chrome; login and verification are manual; local history remains usable without login but is not live collection.
- [x] Describe the other platforms by their actual supported mode; explicitly say adapters do not imply all seven can collect live and that limits may change at runtime.
- [x] Clarify that HTTP 401/403/412/429 or login/challenge pages stop that platform without retries or circumvention.
- [x] Verify `python main.py collect-live --help` and `git diff --check`.

### Task 5: Review, verify, and publish the feature branch

**Files:**
- Review all changed files and any new tests/docs.

- [x] Run `ruff check .`.
- [x] Run all offline tests with `.venv/Scripts/python.exe -m pytest tests/ -q --basetemp=<fresh unique path>`; the basetemp parent must already exist and the target directory must not.
- [ ] Run `python scripts/smoke_test.py` once. If Bilibili returns HTTP 412/403/429, record the result and do not retry it.
- [ ] Run `PLATFORM_LIVE_TESTS=1 pytest tests/test_platform_access.py` only if this does not repeat a just-observed blocked endpoint; never retry merely to obtain a passing result.
- [x] Run the repository engineering-review commands from `AGENTS.md`; inspect `git status`, `git diff --check`, and the full diff; scan the staged source/history for secrets. Do not add `.provider_key`, `.env`, `*.key`, `config.yaml`, `data/`, `outputs/`, or the Chrome profile.
- [x] Kept the original `origin` untouched, used a separate `social-content` remote for `https://github.com/tyzy1314/social-content-research-agent.git`, and pushed only `codex/multiplatform-live-collection`.


Live-only checks are not executed from this worktree: its ignored local database is not present, the last recorded Bilibili live result was HTTP 412 (2026-10-11), and no XHS login session was provided. Do not copy the user database or retry the blocked endpoint just to obtain a green result.
