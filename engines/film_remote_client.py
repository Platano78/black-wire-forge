#!/usr/bin/env python3
"""Submit one private Film API job, wait, and download its verified result."""
import argparse
import base64
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def request_json(url, token, payload=None, timeout=60):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": "Bearer " + token,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:2000]
        raise RuntimeError("Film API returned HTTP %d: %s" % (exc.code, detail))


def image_data_uri(path):
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    with open(path, "rb") as handle:
        return "data:%s;base64,%s" % (mime, base64.b64encode(handle.read()).decode("ascii"))


def download(url, token, output):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=300) as response, open(output + ".part", "wb") as handle:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)
    if os.path.getsize(output + ".part") == 0:
        raise RuntimeError("Film API returned an empty video")
    os.replace(output + ".part", output)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--duration", type=int, required=True)
    parser.add_argument("--image")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    token = ""
    if args.token_file and os.path.isfile(args.token_file):
        with open(args.token_file, encoding="utf-8") as handle:
            token = handle.read().strip()

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

    base = args.base.rstrip("/")
    state = request_json(base + "/v1/video/generations", token, payload, timeout=60)
    job_id = state.get("id") or state.get("job_id")
    if not job_id:
        raise RuntimeError("Film API did not return a job id: %s" % json.dumps(state)[:1000])

    failures = 0
    while True:
        try:
            state = request_json(base + "/jobs/" + urllib.parse.quote(str(job_id)), token, timeout=30)
            failures = 0
        except (urllib.error.URLError, TimeoutError, OSError):
            failures += 1
            if failures >= 6:
                raise
            time.sleep(5)
            continue
        status = str(state.get("status") or "").lower()
        if status in ("failed", "error", "cancelled", "canceled"):
            raise RuntimeError(str(state.get("error") or "Film generation failed"))
        url = state.get("output_url") or state.get("url")
        if status in ("complete", "completed", "done", "succeeded") and url:
            if not urllib.parse.urlparse(url).scheme:
                url = base + "/" + str(url).lstrip("/")
            download(url, token, args.output)
            print("PROGRESS 1/1", flush=True)
            print("Completed %s" % args.model, flush=True)
            return 0
        time.sleep(5)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("Film generation failed: %s" % exc, file=sys.stderr, flush=True)
        sys.exit(1)
