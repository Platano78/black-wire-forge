# Remote generation backends

Three optional process packs (`engines/film_remote.py`, `engines/music_remote.py`,
`engines/qwen_remote.py`) send a job to a Film video, YuE2 song or Qwen picture
service that you already run yourself, wait for it, and save the result. They do not
install the services or download model weights.

## Turning them on

On a stock install these packs do not exist: no modes, no rooms, nothing in Setup.
A pack appears only when one of its base address variables is set in the
environment of the process that starts Forge. Then create a process lane in Setup for
the matching capability (video, audio or image).

| Mode | Base address variable | Token file variable | Default token file |
| --- | --- | --- | --- |
| Film `film5080` | `FILM5080_BASE` | `FILM5080_TOKEN_FILE` | `/run/secrets/film5080-token` |
| Film `film9700` | `FILM9700_BASE` | `FILM9700_TOKEN_FILE` | `/run/secrets/film9700-token` |
| YuE2 `yue2.yue2-bf16-5080` | `YUE2_5080_BASE` | `YUE2_5080_TOKEN_FILE` | `/run/secrets/yue2-5080-token` |
| YuE2 `yue.int8-r9700` | `YUE_R9700_BASE` | `YUE_R9700_TOKEN_FILE` | `/run/secrets/yue-r9700-token` |
| Qwen `qwen.image21-r9700`, `qwen.texture-r9700` | `QWEN_IMAGE_BASE` | `QWEN_IMAGE_TOKEN_FILE` | `/run/secrets/qwen-image21-r9700-token` |

Film and YuE2 each have two modes. Setting one of a pack's base variables loads the
pack; a mode whose own base variable is still unset is shown as unavailable, with the
variable to set.

A base address is an `https://` address, or `http://` to this machine (`localhost`,
`127.0.0.1`, `::1`). Plain `http://` to another machine sends the token unencrypted,
so it is refused unless `BWF_REMOTE_ALLOW_HTTP=1` is also set; set that only for a
network you trust (or use an encrypted tunnel).

## The token

Each request carries `Authorization: Bearer <token>`, read from the token file. Keep
the files outside the repository and readable only by the account that runs Forge.
The token is never sent to the browser or stored with a job.

- The token is sent **only to the configured base address** (same scheme, host and
  port). If a service's reply points anywhere else — a result URL on another host, or
  a redirect — the job stops with a sentence saying so, and nothing is sent there.
- Redirects are never followed, and proxy settings from the environment are not used.
- A missing, unreadable or empty token file stops the job with a sentence naming the
  file and its variable.
- A Film service that takes no token: set its token file variable to the word `none`.
  YuE2 and Qwen always need a token file.

## Limits

- A JSON reply over 8 MiB, a downloaded video or song over 4 GiB, or a picture over
  512 MiB is refused.
- A job is waited on until a little before its step timeout (Film 6 h, YuE2 2 h, Qwen
  1 h); six failed polls in a row also end it.
- A failed job shows the service's own `error` text, cut to 300 characters.

## Service contracts

- Film: `GET /health` (checked once when a job starts), `POST /v1/video/generations`,
  then `GET /jobs/{id}`. A completed job returns `output_url` or `url`, relative or on
  the same address. The client saves an MP4. The service must accept the model
  identifiers declared in `film_remote.py`. `film5080` seeds are reduced to the signed
  32-bit range.
- YuE2: `POST /v1/chat/completions` with a JSON song specification in the user
  message. The assistant reply contains a result URL, or a `/watch/{id}` URL on the
  same address; watched jobs are polled at `GET /jobs/{id}` and return `output_url`.
  The client saves an MP3.
- Qwen Image: `POST /v1/images/generations`, then `GET /jobs/{id}`. A completed job
  returns `output_url` (or `data[0].url`); without one, `/files/{id}.png` is fetched.
  The downloaded file must have a PNG signature.

Remote model licences remain backend-specific; the packs do not claim verified
commercial rights.
