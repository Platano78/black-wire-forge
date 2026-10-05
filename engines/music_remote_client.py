#!/usr/bin/env python3
"""Generate one YuE2 song through a private LAN API and save its MP3."""
import argparse
import json
import re
import time
import urllib.parse

if __package__:            # imported by the engine loader (no ENGINE: never a pack)
    from . import _remote_http as rh
else:                      # run as the plan's program: engines/ is sys.path[0]
    import _remote_http as rh


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--base-setting", default="the YuE service address")
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--token-setting", default="the YuE token file setting")
    parser.add_argument("--model", required=True)
    parser.add_argument("--style", required=True)
    parser.add_argument("--lyrics", required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--deadline-s", type=int, default=7000)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    deadline = time.monotonic() + args.deadline_s

    token = rh.read_token(args.token_file, args.token_setting)
    svc = rh.Service(args.base, token, "The YuE service", args.base_setting)

    spec = {"style": args.style, "lyrics": args.lyrics, "seed": args.seed}
    # The service may hold this request open while it writes the song, so it
    # may take the whole budget -- but never more than what is left of it.
    reply = svc.json(svc.url("v1", "chat", "completions"), {
        "model": args.model,
        "messages": [{"role": "user", "content": json.dumps(spec)}],
    }, timeout=max(1, deadline - time.monotonic()))
    try:
        message = str(reply["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError):
        raise rh.RemoteError("The YuE service sent a reply without a message.")
    match = re.search(r"https?://\S+", message)
    if not match:
        raise rh.RemoteError("The YuE service did not return a result address.")
    # resolve() refuses an address on any host but the configured one.
    result_url = svc.resolve(match.group(0).rstrip(".,)"))

    path = urllib.parse.urlsplit(result_url).path
    if "/watch/" in path:
        job_id = urllib.parse.unquote(path.rstrip("/").rsplit("/", 1)[-1])

        def finished(state):
            status = str(state.get("status") or "").lower()
            if status in rh.FAILED:
                raise rh.job_failed(state, "The YuE service says the song failed.")
            if status in rh.DONE and state.get("output_url"):
                return state["output_url"]
            return None

        result_url = svc.poll(svc.url("jobs", job_id), finished, deadline)

    svc.download(result_url, args.output)
    print("PROGRESS 1/1", flush=True)
    print("Completed %s" % args.model, flush=True)
    return 0


if __name__ == "__main__":
    rh.main_guard(main)
