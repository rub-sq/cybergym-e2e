# Session rules

- Auto-compact the conversation when context reaches ~80% (summarize state, blockers, and next steps; keep working).

## Running the benchmark (context)

- `./run_dual_agents.sh` runs lanes: `openhands`, `opencode`, `claude` — **every agent CLI runs INSIDE its task container** (`scripts/container_agent_runner.py`, driven by `docker exec`). There is no host-CLI mode anymore; this closes the GT-patch leak (host agents once cd'ed into the repo and read `patch.diff`).
- KIConnect key-rotating proxy: `scripts/kiconnect_proxy.py` on port 8817, pools `KICONNECT_KEY1`/`KICONNECT_KEY2` from `.env`.
- All lanes reach the proxy through the SAME container-side URL: `http://host.docker.internal:8817/v1` by default, **but on the tower (10.150.0.212) container→host-gateway traffic to the proxy port is REFUSED** — the only container-reachable address is the host LAN IP. Always launch on the tower with `OPENHANDS_PROXY_URL=http://10.150.0.212:8817/v1` (it is passed to every KIConnect lane, opencode included).
- claude lane auth, first match wins: `CLAUDE_CODE_OAUTH_TOKEN` (run `claude setup-token` once on the host; a long-lived subscription token, nothing to refresh), else the host's own login, i.e. whatever `claude login` on the host saved: `~/.claude/.credentials.json` on Linux (the tower), the `Claude Code-credentials` item of the login Keychain on macOS. Only that login is piped in (through stdin, never a host file); the rest of `~/.claude` is the host user's prompt history, plans and memory. Else `ANTHROPIC_API_KEY`. If none exists the lane dies with `CLAUDE_AUTH_ERROR`.
- Agent releases are pinned in `run_agent.py` (`CLAUDE_CODE_VERSION`, `OPENCODE_VERSION`); OpenHands stays at 1.0.0 (`install_openhands.sh`).
- Before running the full bench on a new machine: `python3 scripts/sandbox.py --check --allow <proxy URL as containers see it> --allow https://api.anthropic.com` (no model call), then one real task per lane, e.g. `python3 scripts/run_agent.py arduinojson/arvo_24633 --agent opencode --mode patch-only --prompt-style no-test --max-attempts 1`. `run_dual_agents.sh` runs the check itself and refuses to start if it fails.
- Task files use `project/tag` paths (e.g. `duckdb/arvo_56682`), data in `data/projects/<project>/<tag>/` (src.tgz, poc.bin, crash.log), configs in `projects/<project>/<tag>/`.

## Agent sandbox (anti-cheating)

- Agents have searched for the maintainers' fix instead of writing one: on GitHub and in patch files next to their workspace. Turning off an agent's web tools does not stop `curl` or `git clone` from its shell, so every agent's container is locked after the agent is installed and before it starts (`scripts/sandbox.py`, called from `run_agent.run_agent` for every lane):
  - egress: only TCP to the lane's model endpoint is accepted (the KIConnect proxy for openhands and opencode; `api.anthropic.com` and its login host `platform.claude.com`, the same Anthropic address, for claude). Everything else is rejected, IPv6 included; the allowed hosts are pinned in `/etc/hosts` and `resolv.conf` is emptied, so no other name resolves.
  - the firewall is written by a throwaway helper container (`cybergym-netlock:1`, alpine + iptables, built on first use) that joins only the network namespace and holds NET_ADMIN. The task container never has NET_ADMIN, so nothing inside, root included, can change the rules. OpenHands runs as root, which is why dropping privileges inside the container (the approach of the SecRepoBench harness this was ported from) would not have been enough.
  - after locking, `sandbox.verify` tries, as root and as the agent user, GitHub, raw.githubusercontent.com, GitLab, PyPI, npm, bare IPs, GitHub by address on 443/22/9418, DNS over UDP and IPv6, and requires each to fail and the model endpoint to answer. If the lock does not hold, no agent starts: the task ends as `sandbox_error` (not counted as done, rerun later) and the orchestrator stops all lanes on the `SANDBOX_ERROR` marker.
  - the agent container runs with `no-new-privileges` and without NET_RAW; the agent user owns only the repository it patches, `/output`, `/work` and `/out`, everything else (build scripts, crash log, PoC, config, validate.py) is root's and read-only, and sudo cannot lift that. git is told to trust every repository (`safe.directory '*'`, system-wide): git refuses a repository another user owns, and OpenHands runs as root, so its `git diff > /output/fix.patch` came out empty (exit 129) once the repository belonged to the agent user, and every OpenHands task ended as `no_patch`. `sandbox.verify` checks that git works in the repository as root and as the agent.
  - `summary.json` records per attempt what the container could reach (`network`).
- The proxy is the one route out of a KIConnect lane's container, so it forwards model calls only (`/chat/completions`, `/completions`, `/responses`, `GET /models`) and strips request fields and tool types that make the gateway or the model search the web or fetch files on their own (`features`, `files`, `tool_ids`, `web_search_options`, non-function tools...). It used to forward any path, and gateways of this kind can offer endpoints that fetch a URL. Refused paths and stripped fields are logged in `proxy.log`.
- Containers get no real KIConnect key: the proxy sets `Authorization` itself, and clients carry a placeholder shaped like a key (`proxy-managed:not-a-real-key`), which opencode 2 requires.
- What the lock cannot cover: tools that run on the model provider's side. Claude Code's WebSearch is executed by Anthropic, so it is `--disallowedTools` that keeps it out, not the network.
- Builds that download dependencies fail inside a locked container. The orchestrator's lanes use the `no-test` prompt (the agent does not build), and the final validation runs in fresh, unlocked containers.

## Tower (10.150.0.212) gotchas

- SHARED machine: other users run their own KIConnect proxies (e.g. `phil`, CWE-inject, port 8818). Never trust `proxy.log` without checking the process `lstart` matches the bench launch time, and `ps aux | grep kiconnect_proxy` for impostors.
- Verification gate after (re)launch: `POST /v1/chat/completions` lines must appear in `parallel_logs_dual/proxy.log` within ~5 minutes (openhands reaches it within ~2 min of a task start). Zero POST lines = agents never reached the LLM = results are meaningless `no_patch` (waste of hours).
- 2026-09-28 incident: openhands spent the whole run hitting the refused host-gateway address (0 LLM calls, all `no_patch`); opencode lane produced real results but the same proxy logged 0 requests for its first ~5 tasks (root cause unconfirmed — v2 `opencode.db` state suspected; a from-scratch probe run with identical wiring routed through the proxy correctly). Clean relaunch + verification gate is the mitigation.

## Paper protocol (arXiv:2606.04460) — patch-only mode

- 120 minutes TOTAL per task, shared by both attempts (`run_agent.py --timeout 7200`, the default everywhere). The budget counts every attempt's agent run minus its waits for a model key, plus every attempt's validation (prepare + compile + tests + PoC, both stages); starting the container, preparing the workspace and installing the agent are not counted. A validation that starts in time always finishes, so the last one can overshoot. This is the rule of the TeamRocket loop (agentic-redblue): the study asks how long an agent takes to resolve the issue, builds included. The paper itself sets 90 minutes and $10 per run, with the agent's own builds inside it (its patch-only prompt has the agent run validate.py); the 120-minute shared budget is this study's choice for both systems. $10 cost cap is not meterable on KIConnect (quota locks are the analog).
- Key waits: `kiconnect_proxy.py` adds up, per run, the time a run's requests waited for a key (all keys busy or quota-locked, or a concurrency back-off); a request carries its run in the URL (`/run/<id>/v1`) and `GET /_waits/<id>` returns the total. For lanes through the proxy (opencode; openhands with `--model-provider kiconnect`), `key_wait_clock.AgentClock` lets the agent run until elapsed minus waits reaches its allowance, then times it out like its own `timeout` wrapper (SIGALRM to the oldest `timeout` in the container); the wrapper's outer limit is the allowance plus one quota lock (`MAX_KEY_WAIT_S`). The claude lanes do not use the proxy: retries inside the Claude CLI count as agent time, and a quota stop re-runs the task from scratch (the orchestrator's quota sleep), so it costs no budget. `summary.json` records `key_wait_seconds`, `validation_seconds` and the running `budget_charged_seconds` per attempt, and the total at the top.
- At most 2 attempts per task, second attempt gets cross-run feedback (trajectory summary + why validation failed) — `--max-attempts 2`, already the orchestrator default.
- Success = S3 (project tests pass with patch) AND S4 (ground-truth PoC no longer crashes).

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

Resume note: a task counts as done on SUCCESS, or on a failure whose latest run lasted ≥60s; safeguard-flagged runs and `sandbox_error` runs always retry. Wipe `agent_output_<lane>` to force a full re-run of a lane (e.g. after an infra bug made the failures meaningless).
