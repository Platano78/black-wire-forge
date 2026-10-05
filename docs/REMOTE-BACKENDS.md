# Remote generation backends

These optional process packs connect Forge to existing Film, YuE2 and Qwen
HTTP services. They do not install the services or download model weights.
Create a process lane in Forge's setup for the relevant capability.

Configure environment variables in the process that starts Forge:

| Backend | Base URL variable | Default | Token-file variable |
| --- | --- | --- | --- |
| Film RTX 5080 | `FILM5080_BASE` | `http://127.0.0.1:8094` | `FILM5080_TOKEN_FILE` |
| Film R9700 | `FILM9700_BASE` | `http://127.0.0.1:8093` | `FILM9700_TOKEN_FILE` |
| YuE2 RTX 5080 | `YUE2_5080_BASE` | `http://127.0.0.1:8096` | `YUE2_5080_TOKEN_FILE` |
| YuE2 R9700 | `YUE_R9700_BASE` | `http://127.0.0.1:8097` | `YUE_R9700_TOKEN_FILE` |
| Qwen Image R9700 | `QWEN_IMAGE_BASE` | `http://127.0.0.1:8098` | `QWEN_IMAGE_TOKEN_FILE` |

Token paths default to `/run/secrets/film5080-token`,
`/run/secrets/film9700-token`, `/run/secrets/yue2-5080-token`,
`/run/secrets/yue-r9700-token` and `/run/secrets/qwen-image21-r9700-token`,
respectively. Keep these files outside the repository and restrict their
permissions to the service account. Music and image clients require the file;
Film permits a missing file for a trusted backend that accepts no token.
Only use trusted backends and result URLs; clients authenticate requests with
the configured token. Use HTTPS or a private encrypted tunnel across networks.

## Service contracts

- Film: `GET /health`, `POST /v1/video/generations`, then `GET /jobs/{id}`.
  Completed jobs return `output_url` or `url`. The client saves an MP4.
  The service must accept the model identifiers declared in `film_remote.py`.
- YuE2: `POST /v1/chat/completions` with a JSON song specification in the user
  message. The assistant response contains a result URL or `/watch/{id}` URL;
  watched jobs are polled at `GET /jobs/{id}` and return `output_url`.
  The client saves an MP3.
- Qwen Image: `POST /v1/images/generations`, then `GET /jobs/{id}`.
  The downloaded artifact must have a PNG signature.

The Video room shows a 5080/R9700 shortcut when both Film modes are present.
It uses the existing engine radio's change handler. A failed 5080 health probe
refuses submission rather than silently switching GPUs. Only Film5080 seeds
are normalized to the backend's signed 32-bit range.

Remote model licences remain backend-specific; the packs do not claim verified
commercial rights. No private deployment configuration or backend implementation
is included here.
