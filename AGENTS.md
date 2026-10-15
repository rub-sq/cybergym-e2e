# Session rules

- Auto-compact the conversation when context reaches ~80% (summarize state, blockers, and next steps; keep working).

## Running the benchmark (context)

- `./run_dual_agents.sh` runs lanes: `openhands`, `opencode`, `claude` — **every agent CLI runs INSIDE its task container** (`scripts/container_agent_runner.py`, driven by `docker exec`). There is no host-CLI mode anymore; this closes the GT-patch leak (host agents once cd'ed into the repo and read `patch.diff`).
- KIConnect key-rotating proxy: `scripts/kiconnect_proxy.py` on port 8817, pools `KICONNECT_KEY1`/`KICONNECT_KEY2` from `.env`.
- All lanes reach the proxy through the SAME container-side URL: `http://host.docker.internal:8817/v1` by default, **but on the tower (10.150.0.212) container→host-gateway traffic to the proxy port is REFUSED** — the only container-reachable address is the host LAN IP. Always launch on the tower with `OPENHANDS_PROXY_URL=http://10.150.0.212:8817/v1` (it is passed to every KIConnect lane, opencode included).
- claude lane auth: the host's `~/.claude` subscription login is streamed into each task container per run (bulk session history excluded); `ANTHROPIC_API_KEY` in the environment is the fallback. If neither exists the lane dies with `CLAUDE_AUTH_ERROR`.
- Before running the full bench on a new machine: `bash scripts/probe_opencode.sh` (host-side proxy round-trip) **and** `bash scripts/probe_opencode.sh --in-container` (the exact in-container path: install script, `su agent`, env file, container→proxy hop).
- Task files use `project/tag` paths (e.g. `duckdb/arvo_56682`), data in `data/projects/<project>/<tag>/` (src.tgz, poc.bin, crash.log), configs in `projects/<project>/<tag>/`.

## Tower (10.150.0.212) gotchas

- SHARED machine: other users run their own KIConnect proxies (e.g. `phil`, CWE-inject, port 8818). Never trust `proxy.log` without checking the process `lstart` matches the bench launch time, and `ps aux | grep kiconnect_proxy` for impostors.
- Verification gate after (re)launch: `POST /v1/chat/completions` lines must appear in `parallel_logs_dual/proxy.log` within ~5 minutes (openhands reaches it within ~2 min of a task start). Zero POST lines = agents never reached the LLM = results are meaningless `no_patch` (waste of hours).
- 2026-09-28 incident: openhands spent the whole run hitting the refused host-gateway address (0 LLM calls, all `no_patch`); opencode lane produced real results but the same proxy logged 0 requests for its first ~5 tasks (root cause unconfirmed — v2 `opencode.db` state suspected; a from-scratch probe run with identical wiring routed through the proxy correctly). Clean relaunch + verification gate is the mitigation.

## Paper protocol (arXiv:2606.04460) — patch-only mode

- 90 minutes TOTAL per task, shared across attempts (`run_agent.py --timeout 5400`); $10 cost cap is not meterable on KIConnect (quota locks are the analog).
- At most 2 attempts per task, second attempt gets cross-run feedback (trajectory summary + why validation failed) — `--max-attempts 2`, already the orchestrator default.
- Success = S3 (project tests pass with patch) AND S4 (ground-truth PoC no longer crashes).
- Before running the full bench on a new machine: `bash scripts/probe_opencode.sh` (validates the exact opencode v2 invocation end-to-end in ~2-4 min).

## Stopping a run (thorough — do all of these)

```bash
tmux kill-session -t bench
pkill -u "$USER" -f run_dual_agents
pkill -u "$USER" -f dual_agent_orchestrator
pkill -u "$USER" -f kiconnect_proxy.py
pkill -u "$USER" -f run_agent.py
docker rm -f $(docker ps -aq --filter name=opencode-) 2>/dev/null
docker rm -f $(docker ps -aq --filter name=openhands-) 2>/dev/null
```

Resume note: a task counts as done on SUCCESS, or on a failure whose latest run lasted ≥60s; safeguard-flagged runs always retry. Wipe `agent_output_<lane>` to force a full re-run of a lane (e.g. after an infra bug made the failures meaningless).
