# Claude Code

This directory contains a complete CyberGym agent adapter, not only a Claude
Code installer. The host-side runner generates a unique CyberGym task, mounts
it read-write at `/workspace`, runs Claude Code non-interactively, and stores
the structured event stream and Claude Code session data with the benchmark
logs.

Claude Code is pinned to `2.1.222` by default for reproducible runs. The image
runs the CLI as a non-root user because unattended permission bypass is rejected
when Claude Code is launched as root.

## Build the image

```bash
bash claudecode/install.sh
```

To build another pinned version or image tag:

```bash
CLAUDE_CODE_VERSION=2.1.222 \
IMAGE_NAME=cybergym/claudecode:2.1.222 \
bash claudecode/install.sh
```

## Run CyberGym

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export CYBERGYM_DATA_DIR=/path/to/cybergym-data
export OUT_DIR=/path/to/output
export MODEL=sonnet
export SERVER_IP=127.0.0.1
export SERVER_PORT=8000
export TASK_ID=...

python3 claudecode/run.py \
    --image_name cybergym/claudecode:latest \
    --model "$MODEL" \
    --log_dir "$OUT_DIR/logs" \
    --tmp_dir "$OUT_DIR/tmp" \
    --data_dir "$CYBERGYM_DATA_DIR" \
    --task_id "$TASK_ID" \
    --server "http://$SERVER_IP:$SERVER_PORT" \
    --timeout 1200 \
    --max_iter 100 \
    --difficulty level1
```

For an Anthropic-compatible gateway, export `ANTHROPIC_AUTH_TOKEN` instead of
`ANTHROPIC_API_KEY` if the gateway uses bearer authentication, and pass its API
endpoint with `--base_url`.

Each run creates `<task-id>-<agent-id>/` under `--log_dir` containing:

- `args.json`: CyberGym task and runner metadata.
- `trajectory.jsonl`: Claude Code's machine-readable event stream.
- `console.log`: CLI diagnostics and errors.
- `claude/`: Claude Code session transcripts and runtime state.
- `home/`: any additional per-run user state written by the CLI or tools.

The generated task directory is removed by default. Pass `--remove_tmp false`
to retain it for debugging.
