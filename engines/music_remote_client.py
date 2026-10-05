#!/usr/bin/env python3
"""Generate one YuE2 song through a private LAN API and save its MP3."""
import argparse
import json
import os
import re
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
        raise RuntimeError("YuE API returned HTTP %d: %s" % (exc.code, detail))


def download(url, token, output):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=300) as response, open(output + ".part", "wb") as handle:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)
    if os.path.getsize(output + ".part") == 0:
        raise RuntimeError("YuE API returned an empty audio file")
    os.replace(output + ".part", output)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--style", required=True)
    parser.add_argument("--lyrics", required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    with open(args.token_file, encoding="utf-8") as handle:
        token = handle.read().strip()
    if not token:
        raise RuntimeError("YuE API token file is empty")

    base = args.base.rstrip("/")
    spec = {"style": args.style, "lyrics": args.lyrics, "seed": args.seed}
    reply = request_json(base + "/v1/chat/completions", token, {
        "model": args.model,
        "messages": [{"role": "user", "content": json.dumps(spec)}],
    }, timeout=7200)
    message = reply["choices"][0]["message"]["content"]
    match = re.search(r"https?://\S+", message)
    if not match:
        raise RuntimeError("YuE API did not return a result URL")
    result_url = match.group(0).rstrip(".,)")

    parsed = urllib.parse.urlparse(result_url)
    if "/watch/" in parsed.path:
        job_id = parsed.path.rsplit("/", 1)[-1]
        failures = 0
        while True:
            try:
                state = request_json(base + "/jobs/" + urllib.parse.quote(job_id), token, timeout=30)
                failures = 0
            except (urllib.error.URLError, TimeoutError, OSError):
                failures += 1
                if failures >= 6:
                    raise
                time.sleep(5)
                continue
            status = str(state.get("status") or "").lower()
            if status in ("failed", "error", "cancelled", "canceled"):
                raise RuntimeError(str(state.get("error") or "YuE generation failed"))
            if status in ("complete", "completed", "done", "succeeded") and state.get("output_url"):
                result_url = state["output_url"]
                break
            time.sleep(5)

    if not urllib.parse.urlparse(result_url).scheme:
        result_url = base + "/" + result_url.lstrip("/")
    download(result_url, token, args.output)
    print("PROGRESS 1/1", flush=True)
    print("Completed %s" % args.model, flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("YuE generation failed: %s" % exc, file=sys.stderr, flush=True)
        sys.exit(1)
