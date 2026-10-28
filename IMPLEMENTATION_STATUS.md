# v0.3 selected feature expansion

Implementation is present on `release/v0.3`, version `0.3.0a1`.
No merge, tag, publication or current CI success is claimed. `HANDOFF.md` is unchanged.

**Deferred to the next release:** F03, F08, F14 and F16. They are not implemented here.

The user guide and test instructions are in [Performance workflows](docs/performance-workflows.md).
The selection is recorded in [Feature candidates](FEATURE_CANDIDATES.md).

## Where to test the selected features

| Selection | Implemented surface | UI workflow |
|---|---|---|
| F01 | Auto-discovered pytest plugin; query/outbound-call/issue assertions; context-local capture | Checks request editor generates pytest snippets; Analysis → Recording health links stored test requests |
| F02 | Absolute per-request budgets with missing-data failures | Checks → Budgets and response rules; suite badges, summary and expanded report show budget failures |
| F04, F05 | Keyed arrays and explicit numeric tolerances | Checks request editor; report shows applied rules and invalid identities; save/edit/export preserve rules |
| F06 | Observed target Python/framework metadata and declared environment labels | Suite environment editor; report warnings; request runtime details |
| F07 | Coverage against retained traffic, including common route parameters | Analysis → Suite coverage |
| F09–F12 | Repeated writes, observed OFFSET, cumulative round trips and pool waits | Requests → Issues; Try detector thresholds previews existing evidence |
| F13 | Route no-SQL budgets, pytest max_queries=0 and explicit no-query blocks | Checks policies; recorded SQL and exceptions show unexpected access |
| F15 | Adapter-observed response byte budgets/findings | Request issues and threshold preview |
| F17 | Observed cumulative issue cost and occurrence ranking | Issues → ordering controls; sampling/overlap caveat remains visible |
| F18 | Supporting spans, rule metadata, thresholds and completeness | Request Issues tab and span details |
| F19 | Persistent exact-match suppressions with reason and expiry | Analysis → Suppressions; Issues → Show suppressed; raw CI/test findings remain available |
| F20 | Recording health, drop/profile indicators and test-request links | Analysis → Recording health; request recording diagnostics |
| F21 | Interval-based time attribution without double counting | Request → Time breakdown and recording health |
| F22 | Real issue → seeded check suite with forbidden issue kind | Issues → Verify a fix; existing baseline/compare workflow checks responses, metrics and resolution |
| P01, P07 | Existing runtime N+1/index advice retained; Python loop scan added | Existing Issues/Database pages; Analysis → Python source |
| P02, P06, P11, P12, P14 | Bounded AST candidates for aggregation/filtering/repeated scans/sorting/membership | Analysis → Python source: paste/open source, load example or scan configured project |
| P03 | Repeated aggregate scan candidates | Request Issues and threshold preview |
| P04, P08 | Wide-column and covering-index catalog candidates | Expand a real SQL group → Inspect columns / indexes |
| P05 | Django construction / SQLAlchemy session-load counters | Request Issues; real /patterns demos and synthetic model-count sample |
| P09 | Actual filtered-row plan evidence plus inspectable existing indexes | Existing EXPLAIN UI and on-demand catalog inspection; no claim that an unused index is faster |
| P10 | Exact generated JSON expression candidates and sampled JSON decode hotspots | SQL catalog inspection; profiled request Issues and synthetic JSON sample |
| P13 | Known binary cache sizes and explicit decode-block timings | Request Issues, threshold preview and synthetic cache sample |

Developer integrations have inspectable UI policies, evidence and reports. The browser does
not run arbitrary pytest or shell commands. Source inference and catalog advice are
conservative candidates; the guide documents supported shapes and false-negative limits.
Runtime row volume is not misrepresented as proof of Python-side processing or unused data.

## Reproducible UI examples

- Every shop has a real GET `/patterns` endpoint (Django: `/patterns/`). Before refactor:
  four queries, repeated aggregate scans, deep pagination and full model construction.
  With `SHOP_REFACTOR=1`: two queries and the intended findings disappear.
- Their check suites include keyed `items` and an explicit tolerance for `total`. The
  refactor deliberately reverses the array and changes total by 0.005 so the rules matter.
- POST `/patterns/writes` executes six individual updates and rolls back all of them.
- Analysis → Detector samples records nine clearly labeled synthetic traces for findings
  that are less convenient to provoke. They do not execute SQL or represent measured
  traffic. Replay and real database inspection are disabled for synthetic evidence.
- `scripts/check_demo_patterns.py` runs all four real workflows in-process against
  disposable databases. No server ports are opened. For manual servers use 8011+.

## Verification

| Check | Result |
|---|---|
| Full core suite | 1,183 passed, 5 skipped; 21 setup errors from denied socket binding |
| Core excluding the three server-dependent files | 1,165 passed, 5 skipped |
| Django adapter | 188 passed, 1 skipped |
| FastAPI adapter | 125 passed |
| Flask adapter | 132 passed |
| Litestar adapter | 114 passed |
| Frontend helpers | 31 passed; TypeScript compilation passed |
| Four real demo workflows | Passed: queries 4 → 2, findings fixed, equivalent responses under explicit rules, writes rolled back, source API working |
| Lint / format / whitespace | Passed |
| Strict documentation build | Passed |
| Version consistency | All five packages at 0.3.0a1 |
| Production UI build | Rebuilt through scripts/build_ui.sh; fresh build matches byte-for-byte |
| Wheels | All five built locally; imports, UI/badge assets and source analyzer checked in a fresh local wheel environment using installed framework dependencies |
| Installed pytest plugin | Auto-discovery passed; compliant request succeeds, over-budget and missing-capture cases fail as expected |

A busy-host assertion in the existing FastAPI blocking test was corrected to check the
deliberate `time.sleep` span and its issue, allowing unrelated lag observations.

## Required remaining validation

1. Run the 21 existing server setup tests in an environment that permits local sockets.
   They failed during `bind()` with `PermissionError: Operation not permitted`; they did
   not reach their test bodies. No product assertions were skipped to make the full run pass.
2. Complete desktop/mobile visual and accessibility review. Native browser automation timed
   out before returning a usable page. A local fixture preview was built, but it was not
   visually inspected; no screenshot/accessibility approval is claimed.
3. Run the normal CI matrix before any push/release decision. The old PR's green status
   does not validate these changes. Re-measure request-path overhead before
   making new overhead claims; the published figures remain explicitly for earlier code.

No merge, tag or publication was performed. The author flag and owner approval rules
continue to apply to commit/release work.
