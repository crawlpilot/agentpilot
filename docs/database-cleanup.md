# Clearing run history from Postgres

How to reclaim a database full of builds, replays and agent runs — and what
each level of clearing actually destroys.

Everything here is irreversible. The first section is not optional.

## Back up first

```bash
export PGPASSWORD=agentpilot
pg_dump -h 127.0.0.1 -U agentpilot -d agentpilot > agentpilot-$(date +%Y%m%d-%H%M%S).sql
```

A full dump of a development database is a couple of megabytes and takes under a
second. Check it actually captured something before you delete anything — a
dump that failed silently looks exactly like a dump that succeeded:

```bash
grep '^COPY public' agentpilot-*.sql | sed 's/ (.*//'   # one line per table
```

Restore with `psql -h 127.0.0.1 -U agentpilot -d agentpilot -f <file>.sql`.

## What is connected to what

The FKs decide what a delete takes with it. `ON DELETE CASCADE` throughout, so
removing a parent is never a partial operation:

```
recipes ─┬─> recipe_versions
         ├─> recipe_jobs ──> recipe_runs ──> recipe_run_artifacts
         └─> recipe_runs ──> recipe_run_artifacts

agent_runs ──> agent_steps

jobs ─┬─> crawl_tasks ──> documents   (documents.task_id is SET NULL)
      └─> documents
```

Read it as: **deleting `recipes` deletes everything about recipes**, including
every run and every saved version. That is rarely what "clean up the runs" means.

Confirm the current shape rather than trusting this diagram, which will drift:

```sql
SELECT tc.table_name AS child, ccu.table_name AS parent, rc.delete_rule
FROM information_schema.table_constraints tc
JOIN information_schema.constraint_column_usage ccu USING (constraint_name)
JOIN information_schema.referential_constraints rc USING (constraint_name)
WHERE tc.constraint_type = 'FOREIGN KEY' ORDER BY 2, 1;
```

## Look before deleting

```sql
SELECT kind, status, count(*), min(created_at)::date AS oldest
FROM recipe_runs GROUP BY 1, 2 ORDER BY 1, 2;
```

**The statuses that matter are `running`, `queued` and `needs_input`.** A
`needs_input` run is parked with a live browser session, one of eight warm
identities and a proxy pin held open for a person who has not answered yet;
`running` means a worker is mid-build. Deleting those rows does not crash
anything — `park_refused` is a case `RecipeWorkerLoop` already handles, and the
worker logs it and releases — but the build is lost and the session is abandoned
until its lease expires.

Age is the cheap way to tell live from stuck:

```sql
SELECT count(*) FROM recipe_runs
WHERE status IN ('running', 'queued', 'needs_input')
  AND created_at < now() - interval '1 hour';
```

Anything in those states and older than an hour is almost certainly abandoned:
`assist_timeout_s` defaults to 1800s, and no build runs for an hour.

## The four levels

Pick the narrowest one that does what you want. Each assumes you have a dump.

### 1. Job runs only

A "job" is one submission of N URLs against a recipe. This clears the
submissions and their runs and nothing else:

```sql
TRUNCATE recipe_jobs, recipe_runs, recipe_run_artifacts RESTART IDENTITY;
```

`recipe_runs` has to be named even though the cascade would reach it, because
`TRUNCATE` refuses to truncate a table that is referenced unless every
referencing table is in the same statement.

To keep the non-job runs, delete instead of truncating:

```sql
DELETE FROM recipe_jobs;   -- cascades to its runs and their artifacts
```

### 2. All recipe run history, keeping what is in flight

The common one. Clears the record of what ran without touching the recipes
themselves or anything still working:

```sql
DELETE FROM recipe_runs
WHERE status IN ('completed', 'failed', 'cancelled');
DELETE FROM recipe_jobs
WHERE NOT EXISTS (SELECT 1 FROM recipe_runs r WHERE r.job_id = recipe_jobs.job_id);
```

Artifacts go with their runs. The recipes still work — you have only lost the
history of having used them.

### 3. All run history, app-wide

Adds the agent loop and the crawl side:

```sql
TRUNCATE
    recipe_jobs, recipe_runs, recipe_run_artifacts,
    agent_runs, agent_steps,
    jobs, crawl_tasks, documents
RESTART IDENTITY;
```

Recipes, their versions and `api_keys` survive.

### 4. Full reset

Everything except how you authenticate and what the schema version is:

```sql
BEGIN;
TRUNCATE
    recipes, recipe_versions, recipe_jobs, recipe_runs, recipe_run_artifacts,
    agent_runs, agent_steps, jobs, crawl_tasks, documents
RESTART IDENTITY;
COMMIT;
```

**Name every table rather than using `TRUNCATE ... CASCADE`.** Naming them means
nothing is removed that was not listed, which is the whole difference between a
reset and an accident — `CASCADE` on `recipes` would silently reach four more
tables, and the day the schema grows a fifth it would reach that too without
anyone deciding.

Two tables are deliberately absent:

- `api_keys` — how you get back in. Truncating it locks you out of your own
  service.
- `alembic_version` — what tells alembic the schema is already migrated. Empty
  it and the next `alembic upgrade head` tries to create tables that exist.

## Afterwards

Check the workers survived. They should not have restarted:

```bash
docker ps --format '{{.Names}}' | grep worker | while read -r c; do
  docker inspect -f '{{.Name}} {{.State.Status}} restarts={{.RestartCount}}' "$c"
done
```

A worker that was holding a parked run logs `park_refused` and carries on. A
non-zero restart count means something else went wrong and is worth reading the
logs for.

Browser sessions opened for runs you deleted are not cleaned up by this — they
are held by the session registry, not the database, and are released when their
lease expires. `GET /v1/sessions` lists what is still open if you want to close
them sooner.

## Not covered here

Production. Everything above assumes a development database you are willing to
lose. On anything shared, a `DELETE ... WHERE created_at < ...` on the history
tables is the operation you want, and the live-run statuses are not yours to
remove — someone is waiting on them.
