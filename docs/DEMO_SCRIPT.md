# QueryGuard — 2-minute demo script

> **On-screen note (keep visible):** *This demo drives the server directly with an MCP
> client (e.g. MCP Inspector), not through an AI assistant, and the client is not the model
> used in the evaluation.* Calling the tools directly is deliberate: it shows the **server**
> blocking a query, not an assistant declining it on its own. The evaluation used Groq
> `openai/gpt-oss-120b`.

Target length ~2:00. Timings are cues, not hard cuts. The demo uses the **synthetic**
bookstore database `queryguard_demo` (a fictional bookstore with fake `@example.com`
emails and `555-01xx` phones) — no BIRD data.

---

### (a) What it is + the result — 0:00–0:15

> "QueryGuard is a read-only text-to-SQL MCP server for PostgreSQL: an assistant can
> explore and query a database, but writes, dangerous functions and runaway queries are
> blocked, and common PII columns are masked (best-effort)."

> "In our preregistered evaluation, the guarded server scored 75/150 versus the baseline's
> 81/150 — the difference is inconclusive at this sample size, so we make no accuracy
> claim."

### (b) Start the demo and answer a normal question — 0:15–0:45

Bring the stack up (already warm for the recording):

```powershell
docker compose -p queryguard-demo -f docker-compose.demo.yml up -d
$env:DEMO_READER_PASSWORD = "qg_reader_demo_only"
uv run python -m scripts.setup_demo
```

In the MCP client (configured per `docs/CLIENT_CONFIG.md`), answer *"How many books are
in each genre?"* with three tool calls:

1. `list_tables(database="queryguard_demo")` → `books`, `customers`, `orders`.
2. `describe_table(database="queryguard_demo", table="books")` → columns `book_id, title,
   author, genre, price`.
3. `execute_query(database="queryguard_demo", sql="SELECT genre, COUNT(*) FROM books GROUP
   BY genre ORDER BY genre")` → a small table of genres and counts.

> "Normal read-only work just flows through."

### (c) Two blocked attacks, showing the rule id — 0:45–1:20

Parts (c) and (d) **call the tools directly** — not through an AI assistant, which might
decline a `DROP` itself and so demonstrate the model rather than the server. Use MCP
Inspector in CLI mode, which launches the server over the demo stack's stdio transport
(`docker exec` into the running `queryguard-demo-server-1` container). The commands below
were run and produced the output shown.

Destructive statement (blocked by rule **R4**):

```powershell
npx @modelcontextprotocol/inspector --cli docker exec -i queryguard-demo-server-1 python -m queryguard.server --method tools/call --tool-name execute_query --tool-arg database=queryguard_demo --tool-arg "sql=DROP TABLE books"
```

Output: `isError: true`, text `blocked: R4: only read queries are allowed; this is a DROP statement. Rewrite it as a SELECT.`

Server-file read (blocked by rule **R6**):

```powershell
npx @modelcontextprotocol/inspector --cli docker exec -i queryguard-demo-server-1 python -m queryguard.server --method tools/call --tool-name execute_query --tool-arg database=queryguard_demo --tool-arg "sql=SELECT pg_ls_dir('.')"
```

Output: `isError: true`, text `blocked: R6: function pg_ls_dir is not on the list of allowed read-only functions. Rewrite the query without it.`

> "The write is stopped by rule R4 and the filesystem function by rule R6 — before anything
> reaches the database."

### (d) The masked PII column — 1:20–1:40

Same direct MCP Inspector CLI call, this time a plain SELECT of the fake-email column:

```powershell
npx @modelcontextprotocol/inspector --cli docker exec -i queryguard-demo-server-1 python -m queryguard.server --method tools/call --tool-name execute_query --tool-arg database=queryguard_demo --tool-arg "sql=SELECT email FROM customers ORDER BY customer_id LIMIT 5"
```

Output: `isError: false`, every `email` cell returned as `***` (5 rows masked).

> "Even a plain, legitimate SELECT of an email column is masked on the way out."

### (e) The results table and the limits — 1:40–2:00

Show the accuracy + exploratory tables (README "Results").

> "One line on the caveats: this is one model, one sample of 150, one run per question;
> masking was only exercised in scripted runs and has known bypasses; personal-name columns
> (e.g. `last_name`) are not masked; and the server does not re-check the model's final SQL
> text."

> "And it is not free: the guarded server costs about **2.7× more** and is about **4× slower**
> than the baseline (derived from the README table: $0.4273 / $0.1581 ≈ 2.7×;
> 134.1 s / 32.6 s ≈ 4.1×)."

Stop the demo:

```powershell
docker compose -p queryguard-demo -f docker-compose.demo.yml down -v
```
