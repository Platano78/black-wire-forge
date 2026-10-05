#!/usr/bin/env python3
"""Submit one private Film API job, wait, and download its verified result."""
import argparse
import base64
import mimetypes
import time

if __package__:            # imported by the engine loader (no ENGINE: never a pack)
    from . import _remote_http as rh
else:                      # run as the plan's program: engines/ is sys.path[0]
    import _remote_http as rh


def image_data_uri(path):
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    with open(path, "rb") as handle:
        return "data:%s;base64,%s" % (mime, base64.b64encode(handle.read()).decode("ascii"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--base-setting", default="the Film service address")
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--token-setting", default="the Film token file setting")
    parser.add_argument("--model", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--duration", type=int, required=True)
    parser.add_argument("--deadline-s", type=int, default=21000)
    parser.add_argument("--image")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    deadline = time.monotonic() + args.deadline_s

    token = rh.read_token(args.token_file, args.token_setting, allow_none=True)
    svc = rh.Service(args.base, token, "The Film service", args.base_setting)
    # One short probe first: a service that is down is said in one sentence
    # now, instead of the job failing minutes later.
    svc.alive()

    payload = {
        "model": args.model,
        "prompt": args.prompt,
        "audio": True,
        "mode": "fast",
        "duration_seconds": args.duration,
        "seed": args.seed,
    }
    if args.image:
        payload["image"] = image_data_uri(args.image)

    state = svc.json(svc.url("v1", "video", "generations"), payload, timeout=60)
    job_id = state.get("id") or state.get("job_id")
    if not job_id:
        raise rh.RemoteError("The Film service did not return a job id.")

    def finished(state):
        status = str(state.get("status") or "").lower()
        if status in rh.FAILED:
            raise rh.job_failed(state, "The Film service says the job failed.")
        url = state.get("output_url") or state.get("url")
        if status in rh.DONE and url:
            return url
        return None

    url = svc.poll(svc.url("jobs", job_id), finished, deadline)
    svc.download(url, args.output)
    print("PROGRESS 1/1", flush=True)
    print("Completed %s" % args.model, flush=True)
    return 0


if __name__ == "__main__":
    rh.main_guard(main)
