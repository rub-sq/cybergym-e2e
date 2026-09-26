# Session rules

- Auto-compact the conversation when context reaches ~80% (summarize state, blockers, and next steps; keep working).

## Running the benchmark (context)

- `./run_dual_agents.sh` runs lanes: `openhands` (in-container, KIConnect via proxy), `opencode` (host CLI, KIConnect via proxy), `claude` (host CLI, subscription).
- KIConnect key-rotating proxy: `scripts/kiconnect_proxy.py` on port 8817, pools `KICONNECT_KEY1`/`KICONNECT_KEY2` from `.env`.
- The opencode lane runs ON THE HOST: it must reach the proxy at `http://127.0.0.1:8817/v1` (the opencode binary's bun runtime cannot do outbound remote HTTPS from the host; `host.docker.internal` only exists inside containers).
- The openhands lane runs IN containers: it reaches the proxy at `http://host.docker.internal:8817/v1`.
- Task files use `project/tag` paths (e.g. `duckdb/arvo_56682`), data in `data/projects/<project>/<tag>/` (src.tgz, poc.bin, crash.log), configs in `projects/<project>/<tag>/`.
