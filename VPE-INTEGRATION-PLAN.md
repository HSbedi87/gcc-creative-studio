# Veo Pro Experimental (VPE) — integration analysis and implementation plan

> **Pre-GA / experimental.** This describes the Veo Pro Experimental (VPE) program, which is
> allowlist-gated and subject to the Pre-GA Offerings Terms. Capabilities may be rotated or removed
> on 30 days' notice, and nothing here is a commitment that any of it ships.
>
> Do **not** open a PR against `GoogleCloudPlatform/gcc-creative-studio` from this branch until the
> VPE program confirms the material may be published there.

---

## 1. What VPE actually is

"VPE" is **Veo Pro Experimental** — a private-preview program, not a single model. Omni-Cine is one
of eight capabilities in it. Access is via a single new publisher endpoint, `veo-experimental`, in
Gemini Enterprise Agent Platform (formerly Vertex AI), which routes to different checkpoints based
on `parameters.experiments.modelName`.

| Capability | `experiments.modelName` | One-line summary |
|---|---|---|
| Upscaler | `veo3p1_upscale` | 720p/1080p → 1080p or 4K. Standalone post-process. |
| Omni-Cine | `omni-cine` | The Omni model, with EXR/ProRes/DNxHR I/O and 240-frame output. |
| Video transform | `veo-exp-video-transform` | V2V restyle, masked (localised) edits, multi-keyframe I2V. |
| Performance controls | `veo-exp-perf-estimation` → `veo-exp-perf-generation` | Two-step: extract an actor's performance as a "blue mesh", then drive a generated character with it. |
| Dialogue-driven generation | `veo-exp-a2v-generation` | Animate a start frame lip-synced to a **supplied** audio track. No new audio generated. |
| Video textures | `veo-exp-video-textures` | Seamlessly tessellating and/or looping video. |
| Upscaling tessellation/looping | `veo3p1_upscale` + `seamless{}` | 4K upscale that preserves seams for the above. |
| Professional formats | (cross-cutting) | ProRes/DNxHR/16-bit PNG/EXR output config for all of the above. |

---

## 2. The core architectural finding: this is a third API surface

The app currently speaks **two** different video APIs, and VPE is a **third**, unlike either:

| | Current Veo | Current Omni | **VPE** |
|---|---|---|---|
| Call | `client.models.generate_videos` (GenAI SDK) | `client.interactions.create` | raw REST `:predictLongRunning` |
| Endpoint | SDK-managed | SDK-managed | `us-central1-aiplatform.googleapis.com/.../publishers/google/models/veo-experimental` |
| Poll | `client.operations.get` | synchronous | `:fetchPredictOperation` with `{"operationName": ...}` in the **body** |
| Payload | typed SDK config | `steps`/`user_input` | `instances[]` + `parameters{}` with a nested `experiments{}` block |
| I/O | inline or GCS | GCS URI out | **GCS only, both directions** |
| Required header | — | — | `X-Vertex-AI-LLM-Request-Type: shared` |

**Verified:** `google.genai.types.GenerateVideosConfig` has no `experiments` field and no passthrough
for arbitrary parameters (checked by introspecting the installed SDK). So VPE **cannot** be bolted
onto the existing `generate_videos` call — it needs its own REST client. This is the single biggest
driver of the plan's shape.

Secondary but important: `config_service.LOCATION` defaults to `"global"`, while every VPE sample
is `us-central1`. VPE needs its own region setting rather than reusing `LOCATION`.

---

## 3. Findings that change what's worth building

These came out of cross-referencing all nine docs against this codebase and this engagement's
actual usage. They should be settled before committing to scope.

### 3.1 Only the Upscaler can produce 9:16 — and the telenovela work is 9:16

Three tiers, distinguished by what the docs *state* versus what is *inferred*:

| Tier | Capability | Documented wording |
|---|---|---|
| **Portrait supported** ✅ | Upscaler | "1280x720 (720p) or 1920x1080 (1080p). **Both landscape and portrait orientations are supported.**" Its `aspectRatio` parameter accepts `"16:9"` and `"9:16"`. |
| **Explicitly landscape-only** ❌ | Performance controls, Video transform | "1280x720 (720p). **Landscape 16:9 orientation only**; source video is not rescaled." |
| **Unstated — 1280x720 output, no orientation wording** ⚠️ | Dialogue-driven, Video textures, Omni-Cine | Only a resolution is given. Dialogue-driven and Video textures add "Resized and padded internally if not exact." |

The decisive evidence is the parameter tables rather than the prose: **`aspectRatio` is documented as
a parameter for the Upscaler only.** No other capability exposes any way to request a 9:16 output,
and all of them state a 1280x720 output resolution. So for the tier-3 capabilities the practical
answer matches tier 2 even though the docs never say "landscape only" — you cannot ask for a
vertical frame.

Worse, the "resized and padded internally" behaviour on Dialogue-driven and Video textures is a
trap rather than a feature: feed a 720x1280 portrait frame and it is **pillarboxed** into a
1280x720 landscape frame. Cropping the result back to vertical leaves roughly 405x720 of real
subject pixels — a resolution loss that defeats the point of a professional-formats pipeline. It
will not error; it will just quietly produce a bad deliverable.

**The upside:** the single highest-value capability *is* the portrait-capable one. Generate 9:16 at
720p with Omni (what this customer already does) → upscale to 4K vertical is a complete, useful
vertical pipeline available today.

**Still the headline question for TU/ViX:** is their VPE interest vertical or landscape? If vertical,
the Upscaler is essentially the whole addressable suite right now, and Phases 2–4 should wait.

*Confidence note: tiers 1 and 2 are quoted from the docs. Tier 3 is inferred from the absence of an
`aspectRatio` parameter plus a fixed 1280x720 output — worth confirming with the VPE team rather
than treating as settled.*

### 3.2 Omni-Cine is not an upgrade to the Omni already in the app

Different API, and it **loses** capabilities the current integration relies on:
- No audio references, no video references (current Edit Video sends video + images).
- No multi-turn edit conversation — the `interaction.steps` replay this app depends on for
  "edit this clip again" has no equivalent.
- No seed.

What it **gains**: EXR/ProRes/DNxHR, 16-bit output, and 240 frames (10s) vs Omni's 8s.

So it's a parallel professional-finishing path, not a replacement. Presenting it in the UI as just
another model in the existing dropdown would be actively misleading.

### 3.3 Output is a *directory*, not a file

Every VPE job writes a folder: `sample_0.<ext>` (preview + audio), `sample_0/frame_####.png|exr`
(the frame sequence — up to 240 files), `sample_0_audio.wav`, `request.json`, `prompt.txt`.

The gallery's `MediaItem` model assumes one video URI. A frame sequence has no natural
representation there. Plan: ingest the **preview video** as the gallery item, and record the output
directory URI alongside it so the frame sequence and audio stem are retrievable, without trying to
model hundreds of frames as media items.

### 3.4 Input constraints are strict and the error messages are bad

24 fps **exactly**; 4–8s (Upscaler) or ≤192/240 frames; exact resolutions; declared aspect ratio
must match actual pixels. A known issue states the model "doesn't return accurate error messages
(e.g. wrong fps on input video returns high load message instead)" — so a violated constraint can
surface as a *spurious capacity error*.

Therefore **client-side preflight is not optional**; without it, users get misleading failures on a
paid long-running job. `ffmpeg` is already installed in the backend image (Dockerfile:43, added for
the Omni audio-stripping work) so probing and transcoding is available at no new infra cost.

### 3.5 Bootstrap work that has nothing to do with the app code

VPE requires three service agents to hold `roles/storage.admin` on the I/O buckets — and two of
them (`gcp-sa-vertex-bp`, `gcp-sa-vertex-tune`) **do not exist** until a batch-prediction job and a
tuning job have each been started once in the project. The documented procedure is to start and
immediately cancel a throwaway job of each type. Skipping this yields a `Principal does not exist`
error. This belongs in `bootstrap.sh`/Terraform, not in the request path.

Also: input **and** output buckets must live in the allowlisted project. If a customer's media
bucket is in a different project from the allowlisted one, that's a blocking infra problem, not a
code problem.

---

## 4. Recommended scope and sequencing

Ordered by (value to the telenovela use case) ÷ (integration risk).

### Phase 0 — Foundation, no user-visible feature
- `VpeClient`: authenticated REST wrapper for `:predictLongRunning` + `:fetchPredictOperation`
  polling, with the `X-Vertex-AI-LLM-Request-Type: shared` header and its own region config.
- Config: `VPE_ENABLED` (default **off**), `VPE_LOCATION` (default `us-central1`),
  `VPE_ENDPOINT_MODEL` (default `veo-experimental`).
- A `VpeCapability` registry mirroring the frontend's existing `ModelCapability` pattern — one
  declarative entry per capability holding its `modelName`, accepted mime types, fps/resolution/
  duration/orientation constraints, and required vs optional fields.
- `MediaPreflight` (ffmpeg-backed): probe fps/resolution/duration/orientation, and either reject
  with an accurate message or transcode to spec. This is what turns the API's bad errors into
  good ones.
- **Everything gated behind `VPE_ENABLED=false`** so it ships dark and cannot affect existing users.

### ⚠ Phase 1 blocker found by measurement: most real shots are the wrong length

The Upscaler accepts **4–8 seconds**. Omni — the app's default video model — offers
`supportedDurations: [3,4,5,6,7,8,9,10]`. I ffprobed every generated clip in `_review/`:

| Set | Inside 4–8s | Rejected |
|---|---|---|
| Test-harness clips (`_review/videos/`, 5–6s) | 39 / 39 | 0 |
| **Real production shots** (micronovela + cantina) | **4 / 16** | **12 / 16 — 75%** |

All clips are exactly 24 fps and 720x1280, so fps and resolution are fine. It is **duration alone**,
and the constraint is self-inflicted: the producer naturally reached for 9s and 10s for dramatic
beats, and the app's own duration picker offered them.

So "find a video in the library and upscale it" — the exact flow requested — fails on three quarters
of the real work. A user selects 12 shots, 9 grey out, and the feature reads as broken.

**Phase 1 must therefore ship one of these, not just the happy path:**
- **Segment selection** for >8s clips: an in/out selector over the `<video controls>` already in the
  lightbox (it tracks `currentTime`), snapped to 24 fps frame boundaries, submitting a frame range.
- **Split-and-rejoin** as an explicit multi-job chain — note a 10s clip cannot be halved into legal
  jobs (8s + 2s fails the 4s minimum); it needs 0–5s and 5–10s, then a rejoin. The app already owns
  both halves: `/api/videos/concatenate` and `media_utils.concatenate_videos`.
- **Prevention at source**: on a VPE-enabled deployment, annotate the duration picker — "clips
  outside 4–8s can't be upscaled later". One line, and it stops the problem being created.

### Phase 1 — Upscaler (the first vertical slice)
Best first capability, by a distance:
- Supports **portrait**, so it actually fits the micronovela work.
- Semantically a post-process on an existing gallery clip — "Upscale to 4K" as an action on a
  clip the user already has, rather than a new generation mode. Smallest possible UI surface.
- Video in → video out, so it fits `MediaItem` without the frame-sequence problem (unless 16-bit
  PNG output is requested — offer that as an "export frames" option, not the default).
- Exercises the entire Phase 0 stack end-to-end: preflight, submit, poll, ingest.

Known issues to design around, both documented: 4K 16-bit PNG output may not return the operation
ID or output path (so treat that combination as unsupported for now, or poll the bucket); and 1080p
input requires `aspectRatio` to be set explicitly despite being documented as optional (so always
send it).

### Phase 2 — Dialogue-driven generation (`veo-exp-a2v-generation`)
Highest *creative* value for a Spanish-language telenovela: lip-sync a generated character to a
**real recorded voice track**, which is precisely the gap that came up repeatedly in the earlier
content/language work — it sidesteps the model's Spanish dialogue generation entirely by letting
production supply the audio.

Gated on resolving §3.1: it's a 1280x720 landscape frame today.

### Phase 3 — Video transform + masked transform
The most versatile editing capability (restyle, costume/prop swaps via grayscale mask,
multi-keyframe interpolation). Also the most parameters (`videoTransformStrength`,
`numDiffusionSteps`, mask URI, `conditioningFrames[]`), and needs a mask-authoring story the app
has no UI for. Landscape-only.

### Phase 4 — Omni-Cine, Performance controls, Video textures
- **Omni-Cine**: build when there's a confirmed VFX/finishing pipeline that consumes EXR/ProRes.
  Ship as a distinct "professional finishing" path, never as a drop-in Omni replacement (§3.2).
- **Performance controls**: two chained jobs plus a blue-mesh intermediate artifact to store and
  re-use — a genuinely new asset type. High value for consistent character performance, but the
  largest data-model change.
- **Video textures**: least relevant to serialized drama; real value for signage/backgrounds.
  Cheap to add once Phase 0 exists (it's mostly three booleans).

---

## 5. How to build this without being able to test it

The project is not allowlisted, so **no VPE call can be executed**. That constraint should shape
the engineering, not just be a caveat at the end.

1. **Payload-contract tests.** Assert the exact JSON emitted for each capability against fixtures
   transcribed from the doc samples. This is the highest-value test available: every documented
   sample becomes an expected payload. It catches field-name and nesting errors — which, given
   `experiments{}` is nested inside `parameters{}` and differs per capability, is where mistakes
   will actually happen.
2. **Preflight tests run for real.** ffmpeg is local, so fps/resolution/duration validation and
   transcoding can be tested properly with generated fixture clips. No allowlist needed.
3. **Polling/LRO tests against recorded responses.** The docs contain real success and error
   response bodies (including the five distinct error shapes in the user guide's troubleshooting
   section) — replay them against the client.
4. **A dry-run mode.** `VPE_DRY_RUN=true` logs the fully-rendered request JSON and returns a
   synthetic operation, so the whole path can be walked in a browser without an allowlist. This is
   also what someone *with* access uses to sanity-check a payload before spending on a job.
5. **Label it honestly.** Every VPE surface stays behind `VPE_ENABLED`, documented as
   unverified-against-live-API until someone with allowlist access confirms it — consistent with
   how the Firebase rollback path was marked in `UPGRADING.md`.

**The first person with an allowlisted project should run Phase 1 end-to-end before Phase 2 starts.**
Building three capabilities on an unvalidated client just multiplies the rework if the client is
wrong.

---

## 6. Concrete work items

**Backend**
- `backend/src/videos/vpe/client.py` — REST client, LRO polling, error mapping.
- `backend/src/videos/vpe/capabilities.py` — declarative per-capability constraint/schema registry.
- `backend/src/videos/vpe/preflight.py` — ffmpeg probe + transcode-to-spec.
- `backend/src/videos/vpe/payloads.py` — one builder per capability.
- `backend/src/videos/dto/` — a VPE request DTO with per-capability cross-field validation, in the
  style of the existing `CreateVeoDto.validate_cross_fields`.
- Output ingestion: preview video → `MediaItem`; store output directory URI + artifact manifest.
- Config additions (`VPE_ENABLED`, `VPE_LOCATION`, `VPE_DRY_RUN`).

**Frontend**
- Extend `ModelCapability`/`MODEL_CONFIGS` with VPE entries, hidden unless the backend reports VPE
  enabled (needs a small capabilities endpoint or config echo — the UI must not advertise what the
  deployment can't do).
- Phase 1 UI: an "Upscale" action on a gallery clip + a small options dialog (resolution,
  sharpness, codec/quality). Deliberately not a new generation mode.

**Infra / docs**
- `bootstrap.sh` + Terraform: create/verify the three service agents, including the
  start-and-cancel dance for `vertex-bp` and `vertex-tune`, and grant `roles/storage.admin`.
- `UPGRADING.md`: a VPE section covering allowlisting prerequisites, the service-agent bootstrap,
  and the fact that the feature ships off by default.

---

## 6a. How this gets surfaced in the UI

Designed by putting four competing approaches through three adversarial judges (mental model,
implementation cost, constraint survival) plus a completeness critic. All three judges independently
ranked the **asset-action** approach first and the **new-generation-mode** approach last.

### The verdict: post-process actions on assets, never new modes

The instinct to model this like "Edit with Omni" is correct, and the judges were emphatic about why
the alternative fails. Making VPE capabilities into `GenerationMode` entries would:
- Expand a closed union of display strings from 10 to 18 across ~85 comparison sites, 23 of them in
  Angular templates where the compiler cannot catch a typo — on a feature nobody can execute once
  before shipping.
- Leak seven pseudo-models into `MODEL_CONFIGS`, which has 13 non-config importers, two of which
  build the **gallery and admin model-filter dropdowns** — so they would appear for every user in
  every deployment, including the ~all with VPE off.
- Route Upscale through `applyRemixState`, which opens with an unconditional `resetInputs()` —
  clicking "upscale this finished clip" would **destroy the prompt the user was writing**.
- Turn the mode menu into a catalogue of what this customer cannot do (7 of 8 landscape-only).

### Phase 1 surface — concretely

One promoted button, first and primary in the lightbox rail (`media-lightbox.component.html`, in the
`showOmni` slot), reading **"Upscale to 4K"**. Reachable from exactly the two places asked for: the
media library (`media-detail`) and immediately after generation (`video.component`'s result
lightbox) — the only two hosts that already bind lightbox action outputs.

Mechanics, chosen deliberately over the obvious alternatives:
- **Host opt-in `@Input() showUpscaleButton = false`**, not another `return true` getter. The
  existing `showOmni` is hardcoded `true`, which is why Extend/Concatenate render dead in 4 of 6
  lightbox hosts. Do not ship a fifth dead button — least of all one that spends money.
- **Never gate visibility on metadata.** Open the dialog always; it POSTs a **server ffprobe
  preflight on open** and resolves green/amber/red in ~1s. This is load-bearing (see §6b).
- **Never route through `applyRemixState`.** The handler opens a dialog; it does not touch the
  composer.
- Say the customer's format out loud: the target card reads **"4K vertical · 2160×3840"** for a
  portrait source.
- Per-capability server gating (`GET /api/options/capabilities`), not one `vpeEnabled` boolean, so a
  deployment with only the Upscaler collapses to a single button and renders no menu at all.

**Gating rule** (reconciles with commit `ce58363`, which deliberately rejected hiding capabilities):
*deployment cannot do this* → **absent** (don't advertise). *This asset fails preflight* → **visible,
disabled, with the measured reason and the remedy** ("9.0s — trim to 8s"). The earlier fix's point
was that silently removing entries is the wrong remedy; that applies to the per-asset case, not the
per-deployment one.

**Note on SSR:** SSR is enabled (`main.server.ts`, `provideClientHydration`) and there is no
`APP_INITIALIZER` anywhere in the app. A capability flag fed through an initializer into a
synchronous getter will read `false` through hydration. Use an observable + guard, or transfer-state,
and fail closed.

### 6b. Corrections the design pass turned up — all verified against the code

1. **`MediaItem.duration` is a phantom field.** Backend `duration_seconds` serialises as
   `durationSeconds`; `mapUnifiedItem` reads `item.duration`. Neither is ever populated, and
   `duration_seconds` is not in the `unified_gallery_view` whitelist. `resolution` is never written
   for videos at all. Worse, `aspect_ratio` and `duration_seconds` are **copied off the request DTO**,
   not measured — which is exactly the bug class fixed in `2fd1506` (*"stop the settings chips
   disagreeing with what is sent"*). **Any client-side duration/resolution gate is impossible today.**
   Orientation via `aspectRatio` is the only reliable client signal; everything else must be server
   ffprobe.
2. **Audio was missed by every proposal.** Every clip this app produces carries an AAC track
   (verified), and the VPE output directory contains a *separate* `sample_0_audio.wav`. If the
   preview has no audio stream and that stem exists, it must be ffmpeg-muxed before writing
   `gcs_uris[0]` — otherwise the "4K master" of a dialogue-driven telenovela plays **silent**.
3. **`gcs_uris` must only ever hold the H.264 preview.** `MimeTypeEnum` has 8 members with no
   `video/quicktime`, `video/mxf` or EXR, and `BaseRepository` runs `model_validate` on every read —
   a row written with a ProRes mime type becomes **unreadable through the repository**. Frames never
   enter `gcs_uris`; that array means "N sibling clips" end-to-end. The directory goes in a
   `raw_data.vpe` manifest.
4. **Backend job-state gaps that will strand VPE jobs**: status is written exactly once, so "running"
   and "the worker died" are indistinguishable; `operation.name` is only logged, never persisted; and
   the admin reaper is an unconditional `PROCESSING AND created_at < 1h → STOPPED` with no exemption
   — which will mislabel legitimately slow VPE jobs. `STOPPED` is also not terminal in the frontend
   poller, so a reaped job polls forever. Persist `operation_name`, write intermediate `phase`, and
   exempt operation-bearing rows.
5. **Bulk upscale is a money bug as designed.** Gallery multi-select is a `Set<"type:id">` that
   **discards the clip index**, while `numberOfMedia` defaults to 4. Selecting 40 cards is between 40
   and 160 paid 4K jobs, and the selection model cannot express which clip was meant. Either expand
   selections into individual clips at confirmation, or don't ship bulk in v1.
6. **`showOmni` is hardcoded `return true`**, so "Edit with Omni" will render on VPE outputs that
   have no `raw_data.interactions` to replay. Pre-existing latent bug that VPE makes user-facing.

### 6c. Two capabilities have free reuse nobody would guess

- **Dialogue-driven generation**: the audio-reference slot **already exists** in `flow-prompt-box`
  and is already gated on `capabilities.supportsAudioReference` — a flag no current video model sets.
  VPE a2v would be its first real consumer. Nearly free.
- **TTS → lip-sync is a complete in-app chain today.** The app already generates Spanish speech
  (`gemini-2.5-flash-tts`, `chirp_3`, both with voice + language support). TTS → dialogue-driven is
  the strongest demo this integration has, and both halves already exist.

### 6d. Missing primitives worth building regardless of VPE

- **Extract a frame from a clip.** Does not exist anywhere, yet the customer is *already doing it
  manually* (`_review/micronovela-shots/frames/*_t3.2.png`). Three VPE capabilities need it. The
  lightbox already renders `<video controls>` and tracks `currentTime`; grab to canvas → upload via
  the existing asset path is ~40 lines and unlocks dialogue-driven, keyframe conditioning and
  shot continuity.
- **`/workbench` is the app's real timeline editor and no design used it.** Its `/workbench/render`
  returns a **Blob download that never becomes a MediaItem** — so the assembled cut, the one asset
  most worth mastering at 4K, is the one asset that cannot be upscaled. Giving render a
  "save to gallery" path fixes that and makes a "Finishing Room" unnecessary.
- **`ImageCropperDialogComponent` already exists** with 16:9 / 9:16 presets and already fires on
  every image upload. It is the crop-don't-pad answer to the pillarbox trap, and no proposal cited it.

## 7. Open questions to resolve before Phase 1 code

1. **Orientation (§3.1).** Is TU's VPE interest vertical or landscape? This determines whether
   Phases 2–4 are worth building at all in their current form.
2. **Allowlisted project vs. deployment project.** VPE requires input and output buckets in the
   allowlisted project. Is the intended allowlisted project the same one Creative Studio is
   deployed into? If not, the app needs a separate VPE bucket and a copy step.
3. **Who has allowlist access, and when?** Determines when Phase 1 can be validated and therefore
   when Phase 2 can responsibly start.
4. **Is there a real EXR/ProRes finishing pipeline downstream?** If not, Omni-Cine's main advantage
   is unused and it should stay at the back of the queue.
5. **Upstream or fork-only?** This is confidential, pre-GA, allowlist-gated functionality. It
   probably should not go to `GoogleCloudPlatform/gcc-creative-studio` in the open until the
   program says so — which argues for keeping it on a branch in the fork, feature-flagged, until
   authorised.
