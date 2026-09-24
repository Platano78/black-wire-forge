# Security

## No authentication

Black Wire Forge has no login and no per-user accounts. It is built for a trusted home
LAN — the machines it talks to (your ComfyUI lanes) are on your network too, and there is
no concept of "your job" vs. "someone else's job": anyone who can reach the app's address
can see every job in History and start new ones.

**Do not expose this app to the internet.** There is no TLS, no auth, and no rate
limiting. Put it behind a VPN (e.g. Tailscale, WireGuard) if you need to reach it away
from home; do not port-forward it.

## What the request guard blocks

The app has no login, so every request has to prove where it came from
(`request_refusal()` in `server.py`). This is **defense in depth against common
browser-based DNS-rebinding and cross-site `POST`s — it is not authentication, and it
must not be relied on to make an internet-exposed deployment safe**:

- **DNS rebinding.** Every request's `Host` header is checked against the app's own
  address — `localhost`, its own IP, its own hostname, and anything you add to
  `"allowed_hosts"` in `config.json`. A request claiming to be for some other host is
  refused with 403, so a malicious page that tricks your browser into resolving a domain
  to `127.0.0.1` still can't get the app to answer as if it were that domain.
- **Cross-site `POST`s.** A `POST` must carry `Content-Type: application/json` (or
  `multipart/form-data` for the upload endpoint — a plain HTML `<form>` CAN send
  multipart with that content type, so this check alone does not stop a form-based
  upload). When an `Origin` header is present and is not the literal string `null`
  (browsers send `null` for some legitimate cross-origin-looking requests, and the
  guard accepts it), only its host and port (`netloc`) are compared against the `Host`
  header — the scheme is not checked. A `Sec-Fetch-Site: cross-site` request is refused,
  but only when the browser actually sends that header; it is optional and older
  browsers omit it.

## What it does NOT protect against

- **Anyone on your LAN can use it.** The request guard proves a request came through the
  app's own address; it does not prove who is sitting at the keyboard. Anything on your
  network that can reach the app's port can drive it exactly as you can.
- **No TLS.** Traffic between your browser and the app, and between the app and its
  ComfyUI lanes, is plain HTTP.
- **No per-user accounts, no audit log of who did what.**
- **No request-size, upload-size, storage-quota, or concurrency limit.** The server
  trusts `Content-Length` and buffers the whole request body in memory before doing
  anything else with it; a process-lane upload is then written to local disk with no
  cap. A reachable client can consume server memory, local disk, lane storage, and
  rendering capacity just by sending large or many requests. If you share this app with
  others, put a reverse proxy with body-size and rate limits in front of it.
- **The optional prompt helper is a third party.** If `"helper"` is configured in
  `config.json`, "Help me write this" and "Describe this picture" send your text (and,
  for the picture case, the image itself) to that endpoint. Rendering itself never
  leaves your configured lanes, but the helper's operator and privacy policy apply to
  whatever you send it.

## Sharing: outputs are stripped of prompt metadata on request

Every file a ComfyUI lane renders can carry the full generation graph embedded in
it — model filenames and your complete prompt, as a PNG text chunk, an MP3 ID3v2 tag, a
FLAC `VORBIS_COMMENT` block, an Ogg Opus comment header, or (for video containers)
`ffmpeg`-level metadata. The **"Download (recipe removed)"** control strips this before
the file leaves the app:

- **PNG/JPEG/WebP** — requires Pillow. Pillow re-encodes the pixel data into a fresh
  file with no text chunks. Lossless for PNG (a byte-for-byte round trip of the pixel
  data); JPEG and WebP are re-encoded, not byte-identical, since Pillow has to decode
  and re-save them to drop their metadata.
- **FLAC/MP3/Opus-in-Ogg only** (`sanitize.strip_audio`) — the container is parsed by
  hand (no third-party audio library): FLAC drops its `VORBIS_COMMENT`/`APPLICATION`/
  `PICTURE`/padding metadata blocks, MP3 drops a leading ID3v2 tag and any trailing
  ID3v1/APEv2 tag, Opus rewrites its comment-header page to an empty tag list. Audio
  frames are copied byte-identical in every case. **WAV and M4A are not implemented** —
  `sanitize.strip_audio` does not understand either container, so a WAV/M4A "clean
  download" is refused rather than served untouched under that label (see below).
- **MP4/WebM/MOV/MKV** — requires `ffmpeg` on `PATH`. `sanitize.strip_video` runs an
  `ffmpeg` stream-copy remux (`-c copy`, no re-encode) that drops container-level
  metadata and chapters.

A strip that cannot fully clean a file (an unsupported sub-format such as WAV/M4A,
a missing dependency such as Pillow or `ffmpeg`, or a parse that doesn't match what the
stripper expects) is **refused with an error** rather than silently served as the
original under the "recipe removed" label — a failed strip must never look like a
successful one. `/api/credits`' `clean_download`/`clean_audio_exts` tell the page which
kinds and audio formats can actually be cleaned right now, so the button is hidden or
explained rather than promising something that will fail. **"Keep the recipe"**
downloads the file exactly as rendered, unaffected by any of the above, for when you
want to hand someone the settings along with the result. Every job also carries a
licence stamp (Credits panel) — check it before you publish anything you made here.

## Reporting a vulnerability

Please report security issues via a [private security advisory](../../security/advisories/new)
on this repository rather than a public issue.
