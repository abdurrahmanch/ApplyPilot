#!/usr/bin/env bash
# Kill hung `claude -p` calls so the pipeline's retry logic can proceed.
#
# llm.py passes timeout=_CLI_TIMEOUT (900s) to subprocess.run, which should
# kill a stuck child. It does not reliably: the CLI spawns its own children
# that keep the stdout pipe open, so the cleanup communicate() blocks and the
# call hangs indefinitely. One stuck call stalls an entire run — observed at 23
# minutes on a single cover letter while the whole batch waited.
#
# This is a stopgap for that bug, not a fix. The fix belongs in llm.py
# (process-group kill on timeout).
#
# Usage:  scripts/llm_watchdog.sh [max_seconds] [check_interval]

MAX_AGE="${1:-360}"      # a normal call is 60-90s; 6 minutes is generous
INTERVAL="${2:-30}"

echo "[watchdog] started $(date +%H:%M:%S) — killing 'claude -p' older than ${MAX_AGE}s"

while true; do
    # etimes is elapsed seconds; pick out our LLM subprocesses only, never the
    # user's interactive `claude` session.
    while read -r pid age cmd; do
        [ -z "$pid" ] && continue
        case "$cmd" in
            *"-p"*"--output-format"*) ;;
            *) continue ;;
        esac
        if [ "$age" -gt "$MAX_AGE" ]; then
            echo "[watchdog] $(date +%H:%M:%S) killing pid $pid (stuck ${age}s)"
            kill -9 "$pid" 2>/dev/null
        fi
    done < <(ps -eo pid=,etimes=,args= | grep 'claude -p' | grep -v grep)

    sleep "$INTERVAL"
done
