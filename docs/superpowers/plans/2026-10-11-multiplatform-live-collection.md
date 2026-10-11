# Multi-platform Live Collection Implementation Plan

> **For agentic workers:** Execute this plan incrementally. Write a failing test before changing behavior, keep each slice passing, and run the repository engineering review before reporting completion.

**Goal:** Add a CLI entry that attempts live collection across selected platforms, clearly reports login/authorization/platform limits, and stores only data actually collected in this run.

**Architecture:** Add `collect-live` to the existing `main.py` CLI. Reuse `ADAPTERS`, `PLATFORM_ORDER`, `platform_service.collect_platform_comments()`, existing XHS browser extraction, and `unified_store`; do not add another platform registry or data layer. XHS receives a manual-login gate and real `AccessResult` implementations; Bilibili continues through its public API; other adapters return their real current access status and do not fabricate data.

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

- [ ] Add parser test for `collect-live --platform all --keyword 露营 --limit 10` and for a single-platform `--content-id` request.
- [ ] Add argument validation test: exactly one of `--keyword` or `--content-id` is required; reject both and neither before creating a database or adapter.
- [ ] Add orchestration test by monkeypatching `platform_service.collect_platform_comments`: a failed first platform must not prevent calls for later platforms, and each platform's status/count/reason must appear in the summary.
- [ ] Implement the smallest CLI handler: loop over `PLATFORM_ORDER` for `all`, otherwise use the selected platform; call the existing service once per platform; print explicit per-platform results and an overall `completed/partial` summary. Do not claim all-platform success when any platform is unavailable.
- [ ] Run `pytest tests/test_live_collection.py -q --basetemp=<fresh path>` and then the existing offline suite.

### Task 2: Make XHS authorized-session collection real and explicit

**Files:**
- Modify: `platforms/xiaohongshu.py`
- Test: `tests/test_live_collection.py`

- [ ] Add tests first for: logged-out search prompts once for manual login; still-logged-out/challenge state returns a non-OK result; successful authenticated search returns only real extracted result cards; detail and comments map to the existing unified record fields; a headless run does not claim a manual login is ready.
- [ ] Implement the XHS `search_content`, `get_content_detail`, and `list_comments` methods so they return real `AccessResult` payloads instead of treating “Chrome driver exists” as proof of login or returning status-only results.
- [ ] Reuse `open_search_page`, `fetch_item`, `fetch_comments`, `CrawlGuard`, and `wait_user_login`; after manual confirmation, check the page state once. If login is still required, or a captcha/security/rate-limit state remains, stop this platform and return the observed status.
- [ ] Set `access_mode=AUTHORIZED_SESSION` on successful XHS responses; include a safe source/evidence reference but never cookies, page storage, passwords, or full browser profile paths in evidence.
- [ ] Run the focused XHS adapter tests with network disabled; no live XHS request is part of automated tests.

### Task 3: Audit actual requests and stop on restrictions

**Files:**
- Modify: `platform_service.py`
- Test: `tests/test_live_collection.py`

- [ ] Add tests first to assert each actual search/detail/comments/replies attempt is appended to `access_records` with status, access mode, source, record count, and failure reason when applicable.
- [ ] Add a test where comments return `RATE_LIMITED`/`BLOCKED`/`AUTH_REQUIRED`: the service makes no further request for that platform and records a partial/failed run rather than marking it as an unqualified success.
- [ ] Add a successful fake-adapter test proving real-format items are upserted into `platform_content` and `platform_comments`, while an unsupported adapter adds no fake rows.
- [ ] Implement a small private helper in `platform_service.py` to map an `AccessResult` into the existing append-only `record_access()` contract. Record only calls actually made; do not record a probe as a successful fetch.
- [ ] On restriction statuses, stop remaining capability requests for that platform while allowing the outer CLI loop to continue to the next platform.
- [ ] Run focused service tests and the existing `tests/test_platform_access.py` offline selection.

### Task 4: Document the command and platform limits

**Files:**
- Modify: `README.md`
- Modify: `USAGE.md`
- Test: CLI help assertions in `tests/test_live_collection.py`

- [ ] Update README with a natural Chinese section and one command example for `collect-live --platform all --keyword ...`.
- [ ] State plainly that XHS live search/comments require user login in a visible local Chrome; login and verification are manual; local history remains usable without login but is not live collection.
- [ ] Describe the other platforms by their actual supported mode; explicitly say adapters do not imply all seven can collect live and that limits may change at runtime.
- [ ] Clarify that HTTP 401/403/412/429 or login/challenge pages stop that platform without retries or circumvention.
- [ ] Verify `python main.py collect-live --help` and `git diff --check`.

### Task 5: Review, verify, and publish the feature branch

**Files:**
- Review all changed files and any new tests/docs.

- [ ] Run `ruff check .`.
- [ ] Run all offline tests with `.venv/Scripts/python.exe -m pytest tests/ -q --basetemp=<fresh unique path>`; the basetemp parent must already exist and the target directory must not.
- [ ] Run `python scripts/smoke_test.py` once. If Bilibili returns HTTP 412/403/429, record the result and do not retry it.
- [ ] Run `PLATFORM_LIVE_TESTS=1 pytest tests/test_platform_access.py` only if this does not repeat a just-observed blocked endpoint; never retry merely to obtain a passing result.
- [ ] Run the repository engineering-review commands from `AGENTS.md`; inspect `git status`, `git diff --check`, and the full diff; scan the staged source/history for secrets. Do not add `.provider_key`, `.env`, `*.key`, `config.yaml`, `data/`, `outputs/`, or the Chrome profile.
- [ ] Keep the original `origin` untouched. Add/use the user-requested repository `https://github.com/tyzy1314/social-content-research-agent.git` as a separate publication remote and push only `codex/multiplatform-live-collection` after the checks.
