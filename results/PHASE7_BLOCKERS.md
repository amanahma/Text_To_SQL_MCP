# Phase 7 Part 1 — blockers and documented deviations

No item was fully skipped. Two items could not be done the *literal* way the prompt
described without violating a hard rule or breaking the existing BIRD workflow; both
were completed in the nearest truthful way, documented here.

## 1. verify_setup: "statement_timeout is 5 s" → role default is 30 s

**Prompt (B4):** assert "statement_timeout is 5 s".

**Reality:** the reused, unchanged `scripts/create_reader.sql` sets the *role* default to
`30s` (`ALTER ROLE ... SET statement_timeout = '30s'`, line 63). The hard 5-second limit
is applied by the executor **per query** via `SET LOCAL statement_timeout = '5s'`
(`src/queryguard/executor.py`: `STATEMENT_TIMEOUT = "5s"`, applied in `_execute`). A bare
`qg_reader` connection — which is what `verify_setup` makes — therefore reports `30s`, not
`5s`. `SHOW statement_timeout` on that connection returns `'30s'`.

**Why not "fix" it:** making `verify_setup` observe `5s` would require editing
`create_reader.sql` to change `30s` → `5s`. B2 says to **reuse** that role/revoke setup and
**not rewrite the security rules**, and the 30s role default is a deliberate project
decision (the executor's 5s is the real cap; the role's 30s is a looser outer bound). I did
not change it.

**What I did instead:** `verify_setup` asserts `statement_timeout` is a **bounded, non-zero
cap**, prints the observed value (`'30s'`), and prints a NOTE that the executor narrows it to
5s per query and that the 5s is not observable on a bare role connection. The check PASSes
truthfully. It does **not** assert `== '5s'`, because that assertion would be false against
the real deployment.

## 2. docker-compose: separate `docker-compose.demo.yml`, not an overwrite of `docker-compose.yml`

**Prompt (B2):** "docker-compose.yml: postgres:16 and the server."

**Reality / risk:** the repo already ships `docker-compose.yml`, which is the owner's BIRD
development database. Its service name (`db`), container name (`queryguard-mcp-db-1`), host
port (`${DB_PORT}`), `pgdata` volume (holding ~955 MB of BIRD data) and `./data` bind mount
are depended on by `scripts/create_reader.py`, `scripts/_dockerutil.py`
(`COMPOSE_SERVICE = "db"`) and `eval/dryrun_attacks.py` (`CONTAINER = "queryguard-mcp-db-1"`).
Overwriting it with a demo-focused compose would break the BIRD setup, the eval harness and
the attack scripts, and risk the owner's data volume.

**What I did instead:** added a new, self-contained `docker-compose.demo.yml` with its own
volume (`demo_pgdata`), its own container/project names (run with `-p queryguard-demo`), its
own non-default host port (`127.0.0.1:55432`), and the `server` service. It reads nothing
from `.env`; all credentials are clearly-labelled dev-only placeholders. The existing
`docker-compose.yml` is left untouched. This satisfies B2's intent (postgres:16 + server,
loopback-only) without the destructive side effects.

## Not blocked (for completeness)

- Docker was healthy (`docker info` → ServerVersion 29.8.1, daemon up), so **Part B and the
  Docker CI job were NOT skipped.**
- No protected file was edited. `src/queryguard/*`, `eval/*`, existing tests,
  `eval/PREREGISTRATION.md` and existing `results/*` were only read. New files only:
  `Dockerfile`, `.dockerignore`, `docker-compose.demo.yml`, `db/demo/*.sql`,
  `scripts/setup_demo.py`, `scripts/verify_setup.py`, `scripts/smoke_mcp.py`,
  `docs/CLIENT_CONFIG.md`, `.github/workflows/ci.yml`, and the named `results/` outputs.
