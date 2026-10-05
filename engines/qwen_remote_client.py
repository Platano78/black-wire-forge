#!/usr/bin/env python3
"""Submit one Qwen Image job, poll it, and download its PNG."""
import argparse
import os
import time

if __package__:            # imported by the engine loader (no ENGINE: never a pack)
    from . import _remote_http as rh
else:                      # run as the plan's program: engines/ is sys.path[0]
    import _remote_http as rh

PNG = b"\x89PNG\r\n\x1a\n"
POLLS = 720


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--base-setting", default="the image service address")
    ap.add_argument("--token-file", required=True)
    ap.add_argument("--token-setting", default="the image token file setting")
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--width", type=int, default=1024)
    ap.add_argument("--height", type=int, default=1024)
    ap.add_argument("--steps", type=int, default=25)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--deadline-s", type=int, default=3540)
    ap.add_argument("--output", required=True)
    args = ap.parse_args()
    deadline = time.monotonic() + args.deadline_s

    token = rh.read_token(args.token_file, args.token_setting)
    svc = rh.Service(args.base, token, "The image service", args.base_setting)
    submitted = svc.json(svc.url("v1", "images", "generations"), {
        "prompt": args.prompt,
        "width": args.width,
        "height": args.height,
        "steps": args.steps,
        "seed": args.seed,
        "response_format": "url",
    }, timeout=45)
    job_id = submitted.get("id") or submitted.get("job_id")
    data = submitted.get("data")
    if not job_id and isinstance(data, list) and data and isinstance(data[0], dict):
        watch_url = str(data[0].get("url") or "")
        job_id = watch_url.rstrip("/").rsplit("/", 1)[-1]
    if not job_id:
        raise rh.RemoteError("The image service did not return a job id.")

    def tick(n):
        print("PROGRESS %d/%d" % (min(n, POLLS), POLLS), flush=True)

    def finished(state):
        status = str(state.get("status", "")).lower()
        if status in rh.FAILED:
            raise rh.job_failed(state, "The image service says the picture failed.")
        if status in rh.DONE:
            output_url = state.get("output_url")
            data = state.get("data")
            if not output_url and isinstance(data, list) and data and isinstance(data[0], dict):
                output_url = data[0].get("url")
            return output_url or svc.url("files", "%s.png" % job_id)
        return None

    file_url = svc.poll(svc.url("jobs", job_id), finished, deadline, on_tick=tick)
    out = os.path.abspath(args.output)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    # A picture is small: cap it far below the video/song limit.
    svc.download(file_url, out, timeout=120, limit=512 * 1024 * 1024)
    with open(out, "rb") as handle:
        head = handle.read(len(PNG))
    if head != PNG:
        os.remove(out)
        raise rh.RemoteError("The image service sent a file that is not a PNG picture.")
    print("WROTE %s (%d bytes)" % (out, os.path.getsize(out)), flush=True)
    return 0


if __name__ == "__main__":
    rh.main_guard(main)
