# Live Google Flow Acceptance — 2026-09-06

## Scope

This acceptance verifies the real Google Flow production path used by Flow Story Studio on the migrated `flow.google.com` frontend. No mock provider was used for the two generation runs below.

Validated transport:

- Application route: `FlowCLIIntegration.generate()`
- Video transport: `gflow-cli`
- Upstream source pin: `ffroliva/gflow-cli@438f5cf68256947e92c3ed506e5d3046a4b66c14`
- Reported package version: `0.68.0`
- Browser channel: system Google Chrome
- Flow host: `flow.google.com`
- Flow project: `5fa6b591-8d1c-4a59-9106-2b930a4661f2`
- Model: Veo 3.1 Lite [Lower Priority]
- Aspect: 16:9
- Output count: 1
- Explicit Veo duration: omitted because the control is cohort-dependent

No cookies, auth tokens, signed media URLs, API keys, or account identifiers are recorded in this document.

## Compatibility fixes validated

1. Migrated Flow can render more than one `.settings-trigger-button` simultaneously:
   - one responsive copy may carry `hidden`
   - another copy is visible
   - selecting the first copy can time out even though the editor is ready
   - the application compatibility layer narrows the selector to `.settings-trigger-button:not([hidden])`

2. Veo duration is not forced by the application transport because migrated Flow cohorts may omit the duration control.

3. The gflow profile is configured for the system Chrome channel, avoiding the profile-version mismatch seen when the same profile was opened with an older bundled Chromium.

## T2V live run

Submission:

- Mode: T2V
- Submit RPC observed: `YhhmEf`
- Media ID: `bcc50291-c084-4522-acb8-66c9153d56f2`
- Workflow ID: `79170c9d-0951-4fef-9af1-1edc924ce832`
- Result path: `data/renders/73593b19e3ed/SCENE_001/bcc50291-c084-4522-acb8-66c9153d56f2.mp4`
- Extracted last frame: `data/references/73593b19e3ed/SCENE_001-last-frame.jpg`

Media QC:

- Container: MP4, valid `ftyp` signature
- Video codec: H.264
- Resolution: 1280x720
- Frame rate: 24 fps
- Duration: 8.000 s
- Audio: AAC, 48 kHz, stereo
- File size: 2,123,073 bytes

Result: PASS.

## I2V continuity live run

The exact extracted last frame from the T2V run above was passed back to the application as the next scene's local `reference_image`.

Start-frame attach:

- Local start frame: `data/references/73593b19e3ed/SCENE_001-last-frame.jpg`
- Flow upload media ID: `29d59973-d4b2-422f-aea9-5b6c60f02d5c`
- Upload observed: PASS
- Frame bound in Flow UI: PASS

Submission:

- Mode: I2V
- Submit RPC observed: `eb1hJf`
- Media ID: `12b23eee-9df4-48f5-bb32-750c36aca422`
- Workflow ID: `23f95fc6-de4b-46cf-a355-55b384d3a65d`
- Result path: `data/renders/259541f53447/SCENE_001/12b23eee-9df4-48f5-bb32-750c36aca422.mp4`
- Extracted last frame: `data/references/259541f53447/SCENE_001-last-frame.jpg`

Media QC:

- Container: MP4
- Video codec: H.264
- Resolution: 1280x720
- Frame rate: 24 fps
- Duration: 8.000 s
- Audio: AAC, 48 kHz, stereo
- File size: 2,604,339 bytes

Continuity boundary QC:

- Compared: T2V last frame vs I2V first decoded frame
- SSIM All: `0.978602`
- PSNR average: `40.311044 dB`

This is strong evidence that the I2V clip starts from the supplied boundary frame rather than silently degrading to T2V.

Result: PASS.

## Authentication/status acceptance

`FlowCLIIntegration.status(verify=True)` was run after the live generations on the same saved profile:

- `configured=True`
- `authenticated=True`
- `transport=gflow+flow.google.com`
- `interactive_login_required=False`

Result: PASS.

## Regression gates

- `tests/test_gflow_transport.py`: 10 passed
- Flow/gflow regression: 131 passed, 207 deselected
- Full test suite: 339 passed
- `ruff check src tests`: PASS
- `pip check`: PASS
- `gflow doctor`: Overall: ok
- Editable install dry-run with `requirements.lock.txt`: PASS
- Secret-like patterns in Git diff: 0
- `git diff --check`: PASS

One unrelated Starlette/AnyIO deprecation warning remains in the test environment.

## Acceptance conclusion

The real migrated Google Flow path is validated for:

- T2V generation
- MP4 download
- media/workflow identity capture
- last-frame extraction
- local-frame I2V upload and binding
- I2V generation on `flow.google.com`
- strong scene-boundary continuity
- application connection/status verification

Reference-image generation through the migrated image surface was not part of this live acceptance and remains a separate gate.
