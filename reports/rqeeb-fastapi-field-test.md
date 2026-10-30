# Redline real-project evaluation and release follow-up

Date: 2026-10-29. Target: `/Volumes/Data/Dev/rqeeb-backend-fastapi`.

## Current rating: 8.0 / 10 for prepared 0.5.0a1

Baseline rating: **6.5/10**; the first correction release `0.4.0a2` scored **7.5/10**.
The prepared `0.5.0a1` adds import-aware analysis, safer SQL recovery, optional SDK/task
capture and visible integration coverage. Native-browser checks now cover offline
snapshots and the health UI. The score remains limited by unresolved dynamic SQL and
clean online installation/live infrastructure checks. This is an engineering assessment,
not a measured percentage of all defects detected. Historical baseline evidence below
is retained; current release results are at the end.

## Verdict before fixes: 6.5 / 10

The runtime profiler is useful on a substantial FastAPI application. Its static analysis currently produces too much noise and misses important helper-mediated queries. This is a development evaluation, not production qualification or a complete security/performance audit.

| Area | Score | Evidence |
|---|---:|---|
| Installation and onboarding | 7/10 provisional | Public-source wheels build and install; bundled shell and API respond. Normal network installation and browser rendering could not be verified in this sandbox. |
| Runtime capture and detectors | 8/10 | Real requests and SQL captured; seeded probes trigger expected detectors; incorrect blocker attribution found. |
| Static detection usefulness | 4/10 | 26 clear false query-in-loop warnings in the first 31 query warnings; real helper-mediated queries missed. |
| Real-project coverage | 6/10 | Default 200-file ceiling covers 200 of this app's 599 Python files; SQL checker cannot resolve many dynamic expressions. |
| Guidance and honesty | 7.5/10 | Explicit incomplete-scan exit and documented integration limitations are good; SQLAlchemy warnings recommend Django methods. |

Equal-weight arithmetic mean: 6.5/10. Installation score is provisional; no PyPI release success is claimed.

## Method and boundaries

- Tested the current public `main` source archive, downloaded from GitHub on this date. All package Python files matched local commit `6cc9e60f97a23f229ab60d6810b164445d623af5`; versions are `0.4.0a1`.
- Initial web retrieval returned an older README recommending two GitHub requirements. The downloaded current README recommends `pip install --pre fastapi-redline`; both flows were attempted. Both failed on host DNS resolution. These failures belong to the execution environment, not Redline.
- Built ordinary core and FastAPI wheels from the public source using cached build tooling, then installed those wheels with pip in `/Volumes/Data/apps/redline-field-test/.venv`. No editable Redline source imports were used for the baseline tests.
- Reused the project's existing Python 3.12 dependency packages read-only through a `.pth` file because package hosts were unavailable. This is not a clean end-to-end resolver test. `pip check` reported existing target-project metadata mismatches: installed FastAPI 0.135.4 and Starlette 1.3.1 versus metadata requesting 0.142.1 and 1.8.0. Redline's requirements were satisfied.
- Actual app/router/service/model code and the project's existing disposable SQLite fixtures were used. No production database, customer records, real credentials, Redis, S3, billing or outbound provider was contacted. Original project source/environment remained unchanged; its pre-existing `DU scripts/seed_perf.py` state was preserved.
- App installation followed `install(app, engine=..., settings={"access":"local"})`, with fixture engines attached to the installed handle. Explicit `project_root` and an isolated trace store were necessary for this external harness. Fixtures replace only database dependencies with the project's normal test setup.
- Exercised ASGI requests through HTTPX, including Redline's shell/API. A real Uvicorn process was attempted on loopback port 8917; sandbox socket binding failed with EPERM. Browser interaction, real network load testing, process topology, live MySQL plans and production overhead therefore remain unverified.
- External harness mistakes (fixture returns an engine/token tuple; trace-list items are dictionaries) were corrected and are not counted as package bugs.
- Smaller-model reviews covered documentation, a manual source-finding sample and focused regression tests. Runtime measurements and implementation were verified independently.

## What Redline found

### Real endpoints

14 initial requests across seven paths, each with and without a synthetic fixture bearer token:

| Path | Anonymous / authenticated status |
|---|---|
| `/health` | 200 / 200 |
| `/api/customer/categories` | 200 / 200 |
| `/api/customer/categories/single?id=1` | 200 / 200 |
| `/api/customer/packages` | 200 / 200 |
| `/api/customer/packages/single?id=1` | 200 / 200 |
| `/api/customer/services` | 401 / 403 |
| `/does-not-exist` | 404 / 404 |

Stored all 14 traces, 21 SQL spans, three transaction spans, two serializer spans and one blocking span. The authenticated services call was denied because that fixture user lacks the permission; this is not a Redline failure. Small seeded read endpoints did not expose real N+1 problems at runtime. Absence of runtime findings does not mean the wider application is optimized.

Redline shell, traces, issues, endpoints and config API all returned 200 with a loopback client. A request to `/api/database` returned 404 because that is not the documented SQL API route; it is not counted as a bug. Asset rendering was not browser-verified.

### Controlled probes in the real application's test harness

These routes were added only in the external harness, not to the project. Their problems were deliberately injected and must not be presented as discovered production defects.

| Probe | Observed baseline result |
|---|---|
| Nine repeated SQL reads on `users` | `n_plus_one`; query count 9 |
| Recursive read returning 1,100 rows without LIMIT | Both `large_result` and `unbounded_query`; row count 1,100 |
| 80 ms synchronous sleep inside async handler | `blocking_in_async`; culprit incorrectly named `runpy._run_module_as_main` because probe source lies outside configured project root |
| Three identical mock outbound HTTP requests, quickstart only | No outbound spans: HTTPX integration is opt-in, as documented |
| Same requests after `instrument_httpx()` | `duplicate_external` |
| Synthetic secret in bearer header, query token and JSON password | Synthetic secret absent from serialized stored traces |
| Loopback API access / hostile Host / untrusted forwarding header | 200 / 403 / 403 |

### Source scans

Default `redline analyze app --json`: 200 files, 43 findings, zero parse errors, `limited=true`, exit 2. Breakdown: 31 query-in-loop, six list-membership, five Python-filtering and one Python-aggregation candidates. Only 33.4% of the app's Python files were covered.

Manual review of that sample found 26 definite query-in-loop false positives: the `execute()` call supplies the iterable and runs once, not once per row. Examples: `core/client_resource.py:360`, `core/entitlement_permissions.py:61,68,72`, and `modules/analytics/service.py:639,655`. The five remaining query-loop candidates include chunked batching and write loops, so even a syntactically correct warning is not proof of a harmful N+1. Four of six list candidates may merit optimization; two operate on small validation-rule lists. Filtering/aggregation findings require semantic review; no unsupported precision percentage is claimed for them.

Default `redline check-sql app --dialect mysql --json`: 200 files, 31 extracted statements, zero findings and 187 unresolved entries including a project-limit marker. Its exit is 2, not a false success. SQLAlchemy expressions and unresolved SQL variables are not validated by this offline mode.

Diagnostic file-by-file pass (explicitly bypassing the project cap, not default CLI behavior): all 599 files parsed, 209 static candidates (121 query-in-loop, 42 aggregation, 22 membership, 17 filtering, seven sorting). SQL checker still found zero lint findings and reported 426 unresolved expressions. These counts describe coverage, not confirmed application defects.

## Confirmed issues and improvements

| ID | Priority | Evidence / user impact | Required release action |
|---|---|---|---|
| F01 | High | First iterable expressions of comprehensions/for loops are treated as repeated bodies. 26 clear false SQL warnings in first sample. | Model evaluation position: outer iterable executes once; later nested iterables can repeat. Preserve real calls in loop bodies, conditions and inner generators. |
| F02 | High | `session.get()` and `session.scalar()` omitted from session query recognition. `_country`/`_city` helpers in `modules/admin/client_employees/service.py:210–219` use `get`. | Recognize these SQLAlchemy operations without treating ordinary mapping `.get()` as SQL. |
| F03 | High | Calls through local query helpers missed: `core/permissions.py:109–110` → `user_has_role`; `modules/admin/client_employees/service.py:204–205` → `_country`/`_city`; `modules/customer/smart_scheduler/scheduler.py:175` → `_score_worker` → several query helpers. | Bounded, same-module helper summaries; report call sites as candidates, including identity-map/early-return caveats. Cross-module and dynamic dispatch remain limitations. |
| F04 | Medium | Default 200-file / 2 MB cap prevents whole-project evaluation; no CLI override. This is documented behavior, but impractical for this project. | Add explicit bounded CLI scan-budget options to source and SQL checks; preserve safe UI defaults and incomplete-coverage signaling. |
| F05 | Medium | FastAPI query suggestions say only `prefetch_related/select_related`. | Include SQLAlchemy batching/loader guidance, with model/context caveats. |
| F06 | Medium | Missing project frame lets blocker name fall back to outer runner (`runpy`); cold async SQLAlchemy CPU work was labeled `SQLAlchemy (sync)` and advised AsyncSession although app already uses it. | Avoid invented culprit names without project evidence; distinguish sampled SQLAlchemy work from confirmed synchronous-driver spans. |

## Capability gaps and release boundaries

| Gap | Assessment and next step |
|---|---|
| Most dynamic SQL/ORM expressions unresolved offline | Coverage gap, not proof those queries are correct. Keep explicit unresolved reports; online compilation needs an isolated MySQL copy. Do not evaluate unknown Python expressions to recover SQL. |
| Cross-module helper and dynamic session-factory inference | Not covered by a same-module summary. Requires a separate import/alias/call-graph design with bounded analysis and confidence reporting. |
| Redis/HTTPX not automatic in minimal quickstart | Documented opt-in integrations. Add real-app wiring example; default silence should not be interpreted as zero cache/external activity. |
| boto3, file/PDF/XLSX CPU work | No automatic SDK span coverage. Several inspected project paths correctly use `run_in_threadpool`; no unsafe blocking call was confirmed there. Manual spans remain the available option. Do not flag correctly offloaded work. |
| Background tasks and scheduler/voice processes | Request tracing does not automatically stitch the separate runtime or arbitrary background work; explicit job instrumentation is required. Distributed tracing is not established by this test. |
| ORM loader suggestions | Raw SQL helpers/batch projections often need a manual `IN` query, not a relationship loader. Suggestions must remain reviewable hypotheses. |
| Database index/EXPLAIN quality | No MySQL server/copy available to validate real plans, index advice, reconnects or asyncmy compatibility at runtime. SQLite probes do not qualify MySQL support. |
| Production/performance | No browser rendering, live TCP requests, real load knee, multiworker SQLite contention, profiling extra or release publication was validated here. Do not raise the score on these claims. |

## Release follow-up

Work branch: `release/v0.4.0a2-field-test`, based on `6cc9e60`. Existing unrelated untracked `.serena/` and `HANDOFF.md` are preserved. Release fixes, test results and remaining limitations are recorded below as work completes. No tag, merge, push or publication is implied by preparing a release branch.

Evidence directory (local, excluded from release): `/Volumes/Data/apps/redline-field-test/evidence`. Files include install logs, public source wheels, static JSON reports, real-request/probe trace exports, test logs and server-bind failure. These are synthetic fixture traces; do not replace them with customer traffic in a commit.


## Completed corrections and observed improvement

All six confirmed release items F01–F06 are implemented on the release branch. The
larger capability gaps above remain explicitly unqualified; no automatic boto3 adapter,
cross-module whole-program analyzer or production certification is claimed.

| Check | Before | Prepared 0.4.0a2 |
|---|---|---|
| Default/explicit source coverage | 200/599 files with default budget | 599/599 with `--max-files 1000 --max-project-bytes 16000000`; zero errors/skips, `limited=false` |
| 26 confirmed sample false query warnings | All present | All removed; the other five original loop-query candidates retained |
| Whole-app query candidates | 121, including first-iterable false positives | 122: 97 old query warnings removed and 98 new candidates added, largely through helper summaries |
| Sorting in outer loop iterables | Seven repeated-sort candidates | Removed: those sorts execute once to supply the iterable |
| Known local helper paths | Missed | Permission loop, employee country/city helpers and smart-scheduler helper chain reported |
| SQL checker coverage | 200 files / 31 extracted statements | 599 files / 38 statements; still zero lint findings and 426 unresolved expressions, exit 2 |
| Blocker without project frame | Runner could be named | Culprit stays unknown; the event-loop stall itself is still reported |
| Sampled SQLAlchemy CPU work | Assumed sync driver | Labeled `SQLAlchemy work`, low-confidence profiling guidance; explicit sync-driver spans remain specific |

The final full scan emits 203 syntax candidates: 122 query loops, 42 aggregations,
22 list-membership checks and 17 filtering candidates. New helper findings have not all
been manually audited. This count must not be used as a confirmed defect count or a
recall/precision measurement.

Two previously missed helper paths were also exercised using **actual project helper
implementations** and synthetic inputs: six missing-role checks performed six SQL
queries and triggered `n_plus_one`; six country/city lookup pairs performed 12 SQL
queries and produced two N+1 groups. Smart-scheduler detection was source-verified;
its full MySQL-dependent workload was not run.

An app-lifespan test served both bundled JavaScript and CSS with HTTP 200 and called
`POST /_redline/api/explain` on a real categories query. SQLite returned HTTP 200 with
`SCAN categories` and suggested `(active, deleted_at)`. This confirms the explain path
and advice plumbing; a full scan on a tiny fixture table is not proof an index is
needed in production. No index was created.

### Final validation

| Validation | Result |
|---|---|
| Existing target catalog/branches/auth query-count tests, baseline | 138 passed |
| Installed 0.4.0a2 target regression run including request/probe tests | 140 passed; zero failures/errors |
| Final-wheel real-helper, detector, lifespan/assets/EXPLAIN smoke | Three passed |
| New focused source/budget/blocker regressions | 41 cases included in core suite |
| Core suite supported by this sandbox | 1,558 passed, six skipped (optional integrations/services); zero failures/errors |
| Full core suite attempt | 21 setup errors from prohibited socket binding; network check/load tests cannot run here |
| FastAPI adapter suite | 125 passed |
| Django adapter suite | 202 passed, one skipped |
| Flask adapter suite | 132 passed |
| Litestar adapter suite | 114 passed |
| Frontend | 35 tests passed; production build passed; existing >500 kB chunk warning remains a future UI optimization |
| Documentation | Strict MkDocs build passed using cached documentation dependencies |
| Packaging | All five ordinary 0.4.0a2 wheels built; core/FastAPI wheels installed and tested on target dependency environment |
| Versions and hygiene | Five lockstep versions/core pins, Ruff check/format and `git diff --check` passed |
| Original project | No source/environment changes; pre-existing conflicted/deleted seed script preserved |

The supported core run excludes the three files whose HTTP server fixtures cannot bind
in this sandbox (`tests/check/test_cli.py`, `tests/load/test_cli.py`,
`tests/load/test_engine.py`). Their full-suite errors are recorded, not hidden as
successful tests. Native supported-Python/framework CI, clean online install, live
MySQL/Redis/Postgres checks and fresh full-stack qualification remain release gates.

Post-correction rubric: installation 7 (provisional), runtime 8.5, static usefulness 6.5,
coverage 7.5, guidance 8. Equal-weight mean: **7.5/10**. The largest next improvements
are safe dynamic-SQL/schema coverage and bounded cross-module helper inference, followed
by real deployment and browser qualification.

The current public [repository README](https://github.com/Pythondeveloper6/redline)
explicitly says published packages may lag development. The retrieved
[PyPI project description](https://pypi.org/project/fastapi-redline/) still contains an
older “not on PyPI yet” installation paragraph despite being hosted there. The source
README is corrected; existing published metadata requires a future authorized upload
or project-description update. This test does not establish the latest installable
PyPI version.

The release is prepared locally. No push, merge, tag or package publication was performed.


## 0.5.0a1 follow-up: 2026-10-30

Branch: `release/v0.5.0a1-real-project-coverage`, based on `434b739`.
User decisions: use recommended implementation choices and smaller models for small,
independent work. The prior corrections remain in this branch; this follow-up targets
the remaining actionable coverage gaps.

### Implemented scope and remaining boundaries

| Report gap | Current implementation | Boundary retained |
|---|---|---|
| Helpers behind module imports | Eight-pass bounded summaries for explicit aliases, relative imports and stable package re-exports | Only already-scanned files; dynamic imports/wildcards/class dispatch remain uncertain |
| Session types and factories | Optional/Annotated/Union/string forms plus known SQLAlchemy sessionmaker/async_sessionmaker and context bindings | Unknown application-specific factories such as get_sessionmaker are not guessed |
| Recoverable SQL templates | Whitelisted literal AST operations, stable SQLAlchemy text clauses/wrappers, pure literal-return helpers and imported constants | No arbitrary evaluation/imports, ORM synthesis or certification of unresolved SQL |
| Missing HTTP/cache/SDK coverage visibility | Installed/loaded/instrumented statuses and warnings in Recording health/API | Hooks stay opt-in; active does not imply observed calls or coverage of other processes |
| boto3 SDK activity | Optional botocore operation hook; bounded service/operation/status/duration only | No argument/response/error text, bucket/key or signed-URL storage; streaming body work is outside operation duration |
| FastAPI in-process background work | Explicit add_background_task helper creates a separate correlated job for async/sync/callable tasks | No automatic cross-process stitching; arbitrary schedulers/voice workers still need explicit job instrumentation |
| Raw SQL loader advice | Existing batching guidance retained; import warnings advise IN queries/eager loading only as applicable | Actual raw SQL/response-preserving rewrite remains an application change requiring review |
| File/PDF/XLSX CPU work | Existing manual spans and profiler remain the supported tools | No unsupported automatic identification of all SDK/CPU work; inspected offloaded paths are not flagged as blocking |
| Large UI bundle | Route chunks, stable shell while loading, eager palette metadata | All chunks still ship; lazy loading reduces initial transfer rather than eliminating total code |
| Offline lazy chunks | URL preload helpers disabled; import-map snapshot loads native modules | Verified native Safari, not an exhaustive browser matrix |
| Deployment and plans | Existing isolated release qualification harness retained | Docker API access and socket binding are denied here; live MySQL/Redis/Postgres and fresh full-stack qualification remain mandatory elsewhere |

### Real-project rescan and runtime results

The installed 0.5.0a1 wheels scan all **599 files**, with no parse errors, file skips or
inference-limit truncation. Import inference converges in three passes. There are
**264 source candidates**, compared with 203 after the first corrections; the 61 extra
candidates include imported helpers and newly understood session provenance. They are
not 61 confirmed defects. Examples manually checked include:

- `modules/admin/settings/router.py:110`: a settings loop invokes imported `service.upsert`,
  which can query/write for each setting.
- `modules/cameras/validation.py:215`: per-service validation awaits imported `_exists_scalar`.
- `modules/customer/employees/service.py:1053`: row imports call a validation helper that
  can perform database work per row.

All 26 original confirmed iteration false positives remain corrected. The scanner does
not execute any target module, inspect `.env` or follow imports outside the admitted files.

SQL extraction improves from **38 to 40 checked literal statements**, while unresolved
inputs decline from **426 to 410** on the whole project: 254 are recognized ORM
constructor expressions requiring runtime capture/explicit compilation, and 156 remain
dynamic SQL. Most remaining shapes need runtime capture or isolated compilation. This is deliberately a conservative improvement;
zero lint findings still exits **2** because coverage is incomplete. Unsupported ORM
constructor expressions are labeled as needing capture/compilation when recognized.

The installed-wheel feature smoke uses the real target FastAPI app and dependency
environment. A real boto3 S3 client runs under botocore Stubber with synthetic credentials
and no network; ListBuckets produces the expected external-operation span. An explicit
background task produces a distinct successful TASK trace linked to its parent request,
without storing its argument. Recording health correctly reports the installed active
boto3 hook and inactive optional integrations.

Five wheel smoke tests passed (features, requests, probes, real helper query counts and
lifespan/assets/SQLite EXPLAIN). Two final-wheel feature/EXPLAIN smoke tests also passed
after the last SDK safety fix. Existing **138 target project tests passed** with 0.5.0a1.
The target repository's source, environment and pre-existing seed-script conflict remain
unchanged.

### Browser findings and fixes during this release

Native Safari initially showed a blank exported snapshot. Vite's lazy-chunk preload map
attempted URL resolution against embedded data modules, which does not honor the import
map. Disabling URL module preloads fixed the issue; a generated self-contained production
snapshot then visibly rendered its request timeline and SQL spans without a server.
The real-bundle snapshot regression test now guards against the incompatible preload map.

The production health view was rendered using synthetic, locally embedded API fixtures
from the tested app. Screenshots at **390 CSS px and 1440 CSS px** showed wrapping status
rows and no clipped integration content; keyboard focus was visibly outlined on the
coverage disclosure. These are browser viewport layout checks, not physical mobile-device
or complete accessibility qualification. The temporary browser tab was closed afterward.

The largest production JavaScript chunk is **229,283 bytes** (previous monolithic entry
exceeded 500 kB). No >500 kB build warning remains. Command-palette metadata is loaded
eagerly so unvisited lazy routes remain discoverable; the navigation shell stays mounted
while page chunks load.

### Additional safety regressions caught and fixed

- The background-task helper initially inherited exception-detail/local capture. It now
  suppresses details per recording, preserving only exception type/outcome, and never
  mutates global settings. Default capture-failure handling remains unchanged; a core
  regression caught and prevented changing that behavior.
- The initial SDK no-trace fast path could catch an application SDK exception and call
  the operation a second time. Review caught it before release: the original SDK call
  is now outside the setup exception handler. A regression asserts exactly one call and
  preservation of the original exception. Metadata/recording failures cannot replace a
  successful SDK result.
- SDK resource identity is deliberately not captured. The integration therefore uses
  SDK-prefixed methods, contributes latency/error/count totals and skips duplicate/retry
  inference. A regression prevents three reads of different object keys from becoming
  a false duplicate-call finding or duplicate/retry aggregate count.
- SQL template evaluation now rejects shadowed/rebound helpers, side-effecting/decorated
  functions and invalid argument binding. UTF-8 size is checked during f-string assembly.
  Imported constants respect source budgets, scope shadowing, cycles and path boundaries.

### Current release validation

| Check | Result |
|---|---|
| Core supported in this sandbox | 1,605 passed, six optional/service skips |
| FastAPI | 130 passed |
| Django | 202 passed, one skip |
| Flask | 132 passed |
| Litestar | 114 passed |
| Package Python total | **2,183 passed**, seven skips, no remaining failures/errors in supported run |
| Target project regressions | 138 passed |
| Installed-wheel smoke | Five passed; final SDK/lifespan smoke two passed |
| Frontend | 35 tests, TypeScript and production build passed |
| Browser | Offline production snapshot and health fixtures visibly rendered; 390/1440 px layouts and keyboard focus reviewed |
| Docs and hygiene | Strict MkDocs, Ruff check/format, version/pin checks and git diff --check passed |
| Packaging | All five ordinary wheels built; core/FastAPI installed locally; all five wheels' Python files match final source |
| Infrastructure tests | The same 21 socket-dependent tests remain unavailable; optional database/Redis checks skipped; Docker API access denied |
| Required dependencies/schema | No new required dependency and no storage/database migration |

Current assessment: installation 7 (still provisional), runtime 8.5, static usefulness 7,
coverage 8, guidance 9.5. Equal-weight mean: **8.0/10**. The score is not a claim that
all dynamic queries are analyzable or that this release is production-qualified.
Clean online installation, live database-driver plans/reconnects, network load testing,
full Compose qualification and the supported-version CI matrix remain release gates.

No push, merge, tag or publication was performed. The development branch is prepared
for review; these remaining environment-dependent gates are explicitly not marked fixed.
