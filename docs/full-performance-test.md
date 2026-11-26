# A full performance test of a multi-user app

How the pieces fit together, in the order you would use them. Each step links to the page that
has the detail; every key is optional, so start with step 1 and add the rest as you need them.

## 1. Make sure the run lands where the app can see it

A load run only fills the bottleneck table when the app tags its traces with the run, which
needs the run to exist in the app's store.

- Same machine: `redline load scenario.toml --store path/to/redline.db`.
- Generator on another host or app in a container: write to the shared PostgreSQL store
  (`--store-url postgresql://...`) or register over HTTP (`--register-url https://app/_redline`,
  with `accept_remote_runs` on the app). See
  [Generator and application on different machines](load-testing.md#generator-and-application-on-different-machines).
- Add `--require-recording` to stop early, with the likely causes, when the app does not record
  the run.
- Make the heaviest SQL visible: statements sent through a raw driver cursor are recorded too
  ([Raw DB-API cursors](integrations.md#raw-db-api-cursors)).

## 2. Offer realistic load

| Problem with a flat scenario | Fix |
|---|---|
| One user repeated thousands of times keeps caches warm | [Identities](load-testing.md#identities) |
| Random ids miss real rows and hit every row equally | [Data feeds](load-testing.md#data-feeds) with `pick = "zipf"` |
| Tokens expire mid-run and the knee is just 401s | [Token refresh](load-testing.md#token-refresh) |
| Detail requests use ids that do not exist | [Flows](load-testing.md#flows) with `extract` and `{var:name}` |
| A `200` with an error body counts as a success | [Response checks](load-testing.md#response-checks) |
| Real users pause between actions | [Sessions and think time](load-testing.md#sessions-and-think-time) |
| Live updates, streams, WebSockets | [Streams and WebSockets](load-testing.md#streams-and-websockets) |

`redline load init --from-traces --data-dir data/ --identities-file users.csv` writes a starting
scenario with several of these.

## 3. Choose the shape

`--steps` finds the knee; `--stages 10:1m,200:30s,10:3m` tests a spike and reports how long p95
took to recover; a long single stage (`--stages 40:30m`) is a soak and reports drift (a leak shows
as `degrading`). If the generator saturates a core, steps are marked `generator_limited`: add
[agents](load-testing.md#several-generator-hosts-agents) (`redline load agent`) before believing
the knee.

## 4. Read the answer

1. The step table and the knee: where it stopped keeping up.
2. **Limiting resource**: pool, CPU, GC, or unknown, from samples the app takes of itself
   ([Resources](load-testing.md#resources-which-one-ran-out)); add a `metric_sources` function
   for the database or host.
3. The **bottleneck table**: which query or route grew, per step; flows add a row per step.
4. Flow and step results, recovery, drift, identities, first-byte timing.

## 5. Turn it into a fix

- N+1 in raw SQL gets a diff that batches the loop
  ([N+1 in raw SQL](n-plus-one.md#n1-in-raw-sql-text-and-driver-sql)); apply it, then verify
  with `redline check verify`.
- SQL Server: [permissions and capabilities](database.md#sql-server-permissions-and-capabilities)
  say which `GRANT` is missing; paste a plan when no login may read plans.
- An agent can do all of this through [the MCP tools](agents.md).
