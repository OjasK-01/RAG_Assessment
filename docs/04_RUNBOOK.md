# Runbook: running everything by hand

All commands are for **Windows PowerShell**. Open three terminals.

## Prerequisites (once)

- Docker Desktop running
- Python venv already created at `backend\venv` with `requirements.txt` installed:
  ```powershell
  cd D:\RAG\backend
  .\venv\Scripts\python.exe -m pip install -r requirements.txt
  ```
- Node 24 with frontend packages installed:
  ```powershell
  cd D:\RAG\frontend
  $env:NODE_OPTIONS = "--use-system-ca"   # needed on this network: TLS interception breaks npm otherwise
  npm install
  ```
- Both `.env` files exist (they're gitignored, so a fresh checkout only has the `.env.example` templates):
  ```powershell
  cd D:\RAG\backend;  copy .env.example .env   # then fill in LLM_API_KEY, PG_PASSWORD, JWT_SECRET, AUTH_USERS
  cd D:\RAG\frontend; copy .env.example .env   # VITE_BASE_URL=http://127.0.0.1:8000
  ```
  The `.env.example` comments show the one-line commands that generate the secrets and the password hashes.

## Terminal 1: database (pgvector)

```powershell
cd D:\RAG\backend
docker compose up -d                      # starts vectra-pgvector on 127.0.0.1:5433
docker ps                                 # should show: vectra-pgvector ... (healthy)
```

Stop it with `docker compose down`. The data persists in the `backend_pgdata` volume. Wipe it with `docker compose down -v`.

## Terminal 2: backend (FastAPI)

```powershell
cd D:\RAG\backend
.\venv\Scripts\python.exe check_key.py                    # optional: ~10 tokens, checks chat + embeddings
.\venv\Scripts\python.exe -m app.ingest --reset           # optional: rebuild the index, prints the admission report
.\venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

- Swagger: **http://127.0.0.1:8000/docs**. Click **Authorize**, enter a username and password, and leave client id/secret empty.
- Health: http://127.0.0.1:8000/api/health
- Add `--reload` while editing code.
- Add `--workers 4` to run several processes. Rate limits, caches and conversations are shared through Postgres, so this is safe.
- You don't need to run ingest by hand: on startup the API builds the index if it's empty, or if it was built with a different
  embedding provider, and calibrates the relevance gate. The startup log shows it: `ready: … gate=0.562 (grey band 0.04)`.

## Terminal 3: frontend (React + Vite)

```powershell
cd D:\RAG\frontend
npm run dev                               # http://127.0.0.1:5173
```

**How the UI finds the backend** (`frontend/.env`):

| `VITE_BASE_URL` | What the browser does | Needs |
|---|---|---|
| `http://127.0.0.1:8000` (current) | calls the API directly, cross-origin. You'll see an `OPTIONS` preflight before the first call | `CORS_ORIGINS` in `backend/.env` must include `http://127.0.0.1:5173` (it does) |
| empty | calls `/api/...` on its own origin, and the Vite dev server proxies it to `127.0.0.1:8000` | nothing |

`.env` is read when Vite starts, so **restart `npm run dev` after changing it**. Open the UI at `http://127.0.0.1:5173`,
because that is the origin CORS allows. `http://localhost:5173` is allowed too.

Follow-up questions use the conversation, which is held on the server for 120 minutes. **New chat** forgets it immediately.
The open chat survives a page refresh in the same tab.

Production build: `npm run build`, which writes to `frontend\dist`.

## Accounts

Usernames are `admin`, `agent` and `staff`. The passwords are in `backend\.demo_credentials.txt` (gitignored). `.env` holds only
bcrypt hashes. To add or change an account:

```powershell
cd D:\RAG\backend
.\venv\Scripts\python.exe -c "import bcrypt,getpass; print(bcrypt.hashpw(getpass.getpass().encode(), bcrypt.gensalt(12)).decode())"
# then append  ;newuser|staff|<hash>  to AUTH_USERS in .env (keep the single quotes) and restart the API
```

## Useful commands (from `D:\RAG\backend`)

| Task | Command |
|---|---|
| Ask from the terminal | `.\venv\Scripts\python.exe -m app.cli "What is the resolution target for a P2 ticket?" --trace` |
| Multi-turn chat in the terminal | `.\venv\Scripts\python.exe -m app.cli --chat` (empty line quits) |
| Unit + API tests (no LLM calls) | `.\venv\Scripts\python.exe -m pytest tests -q` |
| Full eval (31 cases, about 28 LLM calls when uncached) | `.\venv\Scripts\python.exe -m eval.run_eval` |
| Some eval cases | `.\venv\Scripts\python.exe -m eval.run_eval sample,followup` |
| Show the calibrated relevance gate | `.\venv\Scripts\python.exe -m eval.calibrate` (no LLM calls; embeddings are cached) |
| See stored conversations | `docker exec vectra-pgvector psql -U rag -d vectra_kb -c "SELECT conversation_id, turn_no, account, question_masked, outcome FROM chat_turns ORDER BY created_at DESC LIMIT 10"` |
| Tail the audit log | `Get-Content logs\audit.jsonl -Tail 5 -Wait` |
| SQL shell | `docker exec -it vectra-pgvector psql -U rag -d vectra_kb` |

## Switching to an OpenAI key

1. In `backend\.env`, set `LLM_API_KEY=sk-...`
2. Restart the backend. It detects OpenAI, **rebuilds the index with OpenAI embeddings** and **re-calibrates the relevance gate**
   on startup. Nothing else to set.
3. Recommended once:
   ```powershell
   .\venv\Scripts\python.exe check_key.py         # confirm the key works (chat + embeddings)
   .\venv\Scripts\python.exe -m eval.calibrate    # see the calibrated gate and what it lets through
   .\venv\Scripts\python.exe -m eval.run_eval     # confirm the scores
   ```

To go back to Gemini, put the Gemini key back and restart. The index rebuilds and re-calibrates again, and cached Gemini embeddings make it free.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Backend hangs on start | DB not running (`docker compose up -d`), or `PG_HOST=localhost` resolving to IPv6. Keep `PG_HOST=127.0.0.1` |
| `vector type not found` | The extension wasn't created. Run `python -m app.ingest --reset` once |
| Every answer says "couldn't process your question right now" | Provider quota or outage. Check the API log for `RateLimitError`. Gemini free tier = 20 requests/day/model. Add models to `GEMINI_CHAT_FALLBACKS` or wait |
| `npm install` hangs, log shows `SELF_SIGNED_CERT_IN_CHAIN` | Set `$env:NODE_OPTIONS = "--use-system-ca"` first |
| UI login works but questions return 401 | Token expired (60 min). Sign in again |
| Login shows "Can't reach the server at http://127.0.0.1:8000" | Backend not running, **or** the browser blocked the call by CORS. In DevTools > Console, a CORS error means the page's origin isn't in `CORS_ORIGINS`: add it and restart the backend. Also check `VITE_BASE_URL` has no typo and restart Vite after editing it |
| `npm run dev` exits with "Port 5173 is already in use" | An old Vite is still running (`strictPort` refuses to pick another port). Stop it: `Get-CimInstance Win32_Process -Filter "Name='node.exe'" \| ? CommandLine -like '*vite*' \| % { Stop-Process -Id $_.ProcessId }` |
| 429 "Too many requests" | Rate limit: 10 questions/min per account, 5 logins/min, counted across all workers. To clear while testing: `docker exec vectra-pgvector psql -U rag -d vectra_kb -c "DELETE FROM rate_limits"` |
| A follow-up is answered as if it were a new question | The conversation expired (120 min), you pressed New chat, or the phrasing wasn't recognised as a follow-up. An agent account shows `Follow-up` in "Why this answer" when it was |
| Login page loads but the chat crashes with `destroy is not a function` | Old frontend build in the browser cache. Hard-refresh with Ctrl+Shift+R |
