# Connecting an MCP client to QueryGuard

QueryGuard's server speaks MCP over **stdio only**. `src/queryguard/server.py`
calls `mcp.run(show_banner=False)`, whose default transport is stdio, and the
module notes that "the MCP protocol owns stdout." There is **no HTTP or SSE
endpoint** — so every example below is a stdio launch command, i.e. the client
spawns the server as a child process and talks to it over stdin/stdout.

The server reads its configuration from the environment (see `.env.example`):
`BIRD_DBS` (the databases it will expose), `DB_HOST`, `DB_PORT`, `READER_USER`,
`READER_PASSWORD`. For the demo those are the synthetic `queryguard_demo` database
and the `qg_reader` role.

## 1. Local (no Docker), via uv

Runs the server from a checkout, reading `.env` at the project root.

```json
{
  "mcpServers": {
    "queryguard": {
      "command": "uv",
      "args": ["run", "python", "-m", "queryguard.server"],
      "cwd": "C:\\queryguard-mcp"
    }
  }
}
```

(`db.py` loads `.env` relative to the package, so `BIRD_DBS`/`DB_*`/`READER_*` are
picked up from there. Point the client's working directory at the checkout.)

## 2. Docker, via the demo compose stack

Build and start the demo stack once (see the repository README / Phase 7 packaging
notes):

```
docker compose -p queryguard-demo -f docker-compose.demo.yml up -d
```

Then configure the client to launch the server over stdio inside a one-shot
container on the same compose network (so it can reach the `db` service). The
`-T` flag disables pseudo-TTY allocation, which is required for a clean stdio
byte stream:

```json
{
  "mcpServers": {
    "queryguard": {
      "command": "docker",
      "args": [
        "compose",
        "-p", "queryguard-demo",
        "-f", "docker-compose.demo.yml",
        "run", "--rm", "-T",
        "server",
        "python", "-m", "queryguard.server"
      ],
      "cwd": "C:\\queryguard-mcp"
    }
  }
}
```

The `server` service already carries the connection environment
(`DB_HOST=db`, `DB_PORT=5432`, `BIRD_DBS=queryguard_demo`, `READER_USER`,
`READER_PASSWORD`) from `docker-compose.demo.yml`, so no extra `-e` flags are
needed. All demo credentials in that file are **dev-only placeholders**.

## Notes

- **stdio only.** Do not configure an HTTP/SSE URL for this server; it does not
  serve one. A client that only supports URL-based transports cannot connect
  without an external stdio-to-HTTP bridge, which this project does not ship.
- **One database per call.** After connecting, call `list_databases` first, then
  pass a returned name as the `database` argument to every other tool (the server's
  own instructions say so).
- **Nothing is written to stdout but MCP.** Server logs go to stderr; keep client
  logging separate from the protocol stream.
