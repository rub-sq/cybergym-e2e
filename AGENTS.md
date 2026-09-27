# Session rules

- Auto-compact the conversation when context reaches ~80% (summarize state, blockers, and next steps; keep working).

## Running the benchmark (context)

- `./run_dual_agents.sh` runs lanes: `openhands` (in-container, KIConnect via proxy), `opencode` (host CLI, KIConnect via proxy), `claude` (host CLI, subscription).
- KIConnect key-rotating proxy: `scripts/kiconnect_proxy.py` on port 8817, pools `KICONNECT_KEY1`/`KICONNECT_KEY2` from `.env`.
- The opencode lane runs ON THE HOST: it must reach the proxy at `http://127.0.0.1:8817/v1` (the opencode binary's bun runtime cannot do outbound remote HTTPS from the host; `host.docker.internal` only exists inside containers).
- The openhands lane runs IN containers: it reaches the proxy at `http://host.docker.internal:8817/v1`.
- Task files use `project/tag` paths (e.g. `duckdb/arvo_56682`), data in `data/projects/<project>/<tag>/` (src.tgz, poc.bin, crash.log), configs in `projects/<project>/<tag>/`.

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
