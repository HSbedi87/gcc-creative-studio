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

## 6e. Live results - measured, not inferred

First run against an allowlisted project. Each line below replaces a guess with a fact, and two of
them overturn what this document previously assumed.

| Case | Result | What it settles |
|---|---|---|
| `upscale` | SUCCESS, 315s -> **3840x2160** | The Phase 1 capability works. |
| `upscale_portrait` | SUCCESS, 314s -> **2160x3840** | **Genuine vertical 4K**, not a rotated or padded landscape frame. |
| `omni_cine` | SUCCESS, 193s | The output really is a directory: 192 PNG frames, a separate WAV, an MP4 preview. |
| `omni_cine_portrait` | **REJECTED in 10s** | `Input video must be exactly 1280x720, but got 720x1280`. |
| `video_transform` | **SUCCESS, 194s** on real footage | The capability works; the earlier filter was the fixture - see below. |
| `video_transform_masked` | RAI filtered, 193s | `Recitation check failed` on the same fixture, not yet retried. |

**Vertical 4K is real, so the Phase 1 pipeline works end to end.** Generate 9:16 at 720p with Omni,
upscale to 2160x3840. The whole vertical case rested on that assumption and it now has a measured
answer rather than a documented one.

**Omni-Cine is landscape only, and the service says so itself.** §3.1 filed it as tier three,
"unstated", and warned the failure would be silent pillarboxing on a paid job. It is not: the
request is rejected outright in ten seconds, naming the exact expected dimensions. That is the good
failure mode, and preflight can now state the rule rather than hedge. This does **not** settle the
same question for dialogue-driven or video textures - those take a still rather than a video and are
documented as "resized and padded internally", so the silent-pillarbox hazard may still apply there
and needs its own probe.

**The audio stem is real, so the silent-master risk in §6b is not theoretical.** `omni_cine`
returned a WAV beside the preview. Ingesting only the preview, for a capability whose output carries
a separate audio track, yields a 4K master that plays silent.

**The RAI filtering was our fixture, not the capability - retried and confirmed.** Both transform
variants first filtered on `Recitation check failed` while every other case using the same generated
media passed. The hypothesis was that video transform is a structure-preserving restyle and the
fixture was an ffmpeg `testsrc` card - canonical colour bars plus a seven-segment counter. Asked to
restyle that while holding structure, the output is a near-verbatim reproduction of one of the most
reproduced images there is, which is what a recitation check exists to catch.

Re-running `video_transform` against a real 720p clip returned **SUCCESS in 193.9s with
`raiMediaFilteredCount: 0`**. So the capability is viable and the capability matrix changes: video
transform moves from "blocked by RAI" to "works, subject to the usual content policy". Two things
follow. **The smoke fixture is misleading for any structure-preserving capability** and should use
real footage rather than a test card. And **`video_transform_masked` has not been retried** - it
filtered on the same card, so the same explanation almost certainly covers it, but that is an
inference and it is written here as one.

**Timing sets a floor for the UX.** Roughly 315s for a 4K upscale, 193s for Omni-Cine. Phase 1
cannot be a dialog with a spinner; it needs a real background job with progress, which in turn needs
the intermediate phase writes described in §6b.

## 7. Open questions to resolve before Phase 1 code

1. ~~**Orientation (§3.1).**~~ **Answered for the two that matter.** The upscaler produces genuine
   2160x3840 vertical; Omni-Cine rejects portrait outright. Still open for dialogue-driven and video
   textures, which take stills and may pad silently. What remains is the commercial question: is
   TU's slate vertical or landscape, and therefore is anything past Phase 1 worth building?
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

## 6f. Doc review of `preflight.py` and `client.py`

These two modules were the only ones never checked line-by-line against the documentation. The
earlier review pass had a reviewer-matching bug that handed all three reviewers the same
`payloads.py` report, so 2,700 lines went unread. This closes that gap. Both findings below were
reproduced by running the code, not inferred from reading it.

### `client.py` — no defects found

Checked against the user guide's API contract: the shared publisher endpoint, `predictLongRunning`
/ `fetchPredictOperation`, the required `X-Vertex-AI-LLM-Request-Type: shared` header, the
`done: true` + `error` envelope, and the eight documented failure bodies. All present and correct.

Two things worth recording as verified rather than assumed:

* Every documented error is represented in `errors.py`, and the classifier matches on message text
  before status code — correct, because duration, frame rate, aspect ratio and frame size
  rejections all arrive as a bare `code: 3` and only the text distinguishes them.
* The 1800s default timeout clears the slowest observed live job (315s) by a wide margin.

One judgement call is undocumented and should be: a `done: true` failure raises out of the polling
loop rather than being retried, including `VpeServiceOverloadedError`, which is marked `retryable`.
That is right — a finished-and-failed operation cannot be re-polled into success, so retrying means
submitting a new job — but nothing says so, and `retryable` on an error the poller never retries
invites the opposite reading.

### `preflight.py` — one real gap

**A still sent to omni-cine is under-checked.** `validate` infers the slot from the file's kind,
and justifies it in its own docstring by claiming the still slots of a capability share one set of
constraints. That is false for omni-cine, which takes stills in two roles: reference images, at
**any resolution**, and frames of a PNG or EXR input sequence, which the input table requires to be
**1280x720**. Both probe as `IMAGE` and both are checked against the reference-image constraints.

Confirmed by running it — a 3000x2000 PNG returns zero findings against `omni_cine`:

```
--- offspec_frame.png vs omni_cine: blocking=0
      (no findings at all)
```

The byte cap is 30 MiB in both roles, so only resolution goes unchecked. The fix is to let a caller
say which slot it means instead of guessing from the file; that changes `validate`'s signature, so
it belongs with the omni-cine work rather than as a drive-by. The docstring no longer claims
otherwise. **No impact on Phase 1**, which is the upscaler and sends no reference images.

### Not a code defect: the upscaler's mime type list contradicts itself

The Upscaler page states the input may be *"ProRes, DNxHR, H264 (video/mp4), or PNG frames"*, then
states of the `mimeType` field: *"The following mime type is accepted: video/mp4."* Those cannot
both be true — a ProRes `.mov` has no legal value to declare. The registry accepts
`video/quicktime` and `application/mxf` as well, and a ProRes file passes preflight clean.

The registry is probably right: the Professional formats page says the pipeline detects the format
*"based on the input container metadata or the specified mimeType"* and lists ProRes and DNxHR as
accepted upscaler input, which reads as the more specific and more current statement. Left as is
deliberately — warning on every ProRes job on the strength of one abbreviated sentence would be
noise. **Worth raising with the VPE team**, along with the same page's `resolution` parameter, whose
documented default `720p` is not among its own accepted values (`1080p`, `4k`).

Note the codebase already handles this exact shape for audio: `_check_audio` warns that AAC is named
as accepted while no AAC mime type is listed as declarable. Video gets no equivalent warning. If the
VPE team confirms the upscaler page is stale, that asymmetry is fine; if they confirm `video/mp4` is
genuinely the only accepted value, the registry is wrong and preflight should block.

## 6g. The Phase 1 scope question — settled: split-and-rejoin works

The upscaler accepts **4 to 8 seconds**. Of the customer's 16 measured shots, **4 fit**. A plain
"Upscale to 4K" button would therefore be greyed out on 12 of 16 assets, which is not a feature
anyone would call shipped.

There are two ways out and only one of them is useful:

- **Segment selection** — let the user pick an 8 second window of a 10 second shot. Trivial to
  build, and it hands the customer 8 seconds of a shot they need 10 seconds of. Useless for
  delivery.
- **Split and rejoin** — cut 240 frames into 2x120, upscale each as its own job, concatenate. This
  is what they actually need, and it has one real risk: the two halves are **independent jobs**, and
  nothing documented promises deterministic grain, sharpening or face texture between two of them.
  If they do not match, the join is a visible pop and split-and-rejoin is not shippable.

That risk is measurable, so it is being measured rather than argued about.

**`backend/scripts/vpe_seam_check.py`** cuts a source into balanced upscalable segments, upscales
each through `build_payload` and `VpeClient` — the shipped path, not a bespoke one — concatenates by
stream copy so no re-encode can mask or invent a seam, and then measures the join. The measurement
is a ratio, not an absolute: `tblend=all_mode=difference,signalstats` gives a per-transition average
luma delta, frames inside a segment establish what a normal step looks like for this footage, and
the step across the seam is read against that distribution. Under 1.5x the p95 is invisible; over 4x
is a pop.

The detector was validated against synthetic joins before being trusted with a paid job — a
deliberately mismatched pair (brightness +0.10) reads **VISIBLE at 9.08x p95**, a mild 0.02 shift
reads **INVISIBLE at 1.05x**, and two identical halves read **INVISIBLE at 0.72x**. Worth recording
why: the first version of `_analyse` reported an identical clean verdict for the mismatched pair and
the control, which is what exposed the bug. `tblend` emits one value per *transition*, so a 240
frame clip yields 239 deltas and the seam at frame 120 sits at index **119**. Reading index 120 read
an ordinary step inside the second segment, and would have returned "no visible seam" for any
footage whatsoever — a green result that meant nothing, on the strength of which Phase 1's scope
would have been decided.

    python -m scripts.vpe_seam_check --project P --bucket gs://B \
        --source shot.mp4 --out seam_report

Two upscale jobs, roughly 5 minutes in parallel. `--dry-run` exercises the split and the analysis
wiring without calling VPE and needs no allowlist. Exit code is 0 only on INVISIBLE.

**What the answer decides.** INVISIBLE and Phase 1 covers all 16 shots with a split-and-rejoin path
behind the same button. VISIBLE and the honest answer to the customer is that the upscaler is for
4-8 second shots, Phase 1 covers 4 of 16, and the gap goes to the VPE programme as a product
request rather than being papered over in the UI.

### The answer: INVISIBLE, at 0.34x the p95

Run against a real 10 second 16:9 shot, split into two 120 frame halves and upscaled as two
independent jobs. Results are checked in under `backend/seam_report/`.

| Measure | Value |
|---|---|
| Verdict | **INVISIBLE** |
| Seam delta at frame 120 | 7.4825 |
| Baseline median | 8.122 |
| Baseline p95 | 21.7286 (238 transitions) |
| Seam as a multiple of p95 | **0.34x** |

The number to notice is not the ratio but the comparison with the median: **the step across the join
is smaller than a typical frame-to-frame step in this footage.** The seam is not merely inside the
normal spread, it is below the middle of it. There is nothing marginal about this result.

Confirmed visually as well as numerically, because the two are not the same claim. The detector
measures average luma difference, which is sensitive to an exposure or brightness pop and much less
sensitive to a change in grain or sharpening that leaves average brightness alone. Inspecting the
two 3840x2160 frames either side of the join at native resolution — a static horizon and water
region, where a texture mismatch would be obvious and motion would not disguise it — the detail
level, edge sharpness and noise character are consistent across the join. Motion advances; the
rendering does not change.

**So Phase 1 covers all 16 shots, not 4.** The upscale button splits anything over 8 seconds,
upscales the pieces and rejoins them, and the customer gets a full-length 4K master rather than a
truncated one. The output was also confirmed as genuine 3840x2160.

### The 9:16 case, confirmed the same way

Run again against a real 10 second **9:16** shot (720x1280 to 2160x3840), split into two 120 frame
halves and upscaled as two independent jobs, same as the 16:9 run above.

| Measure | Value |
|---|---|
| Verdict | **INVISIBLE** |
| Seam delta at frame 120 | 12.4561 |
| Baseline median | 12.011 |
| Baseline p95 | 19.9789 (238 transitions) |
| Seam as a multiple of p95 | **0.62x** |

Output confirmed as genuine 2160x3840 at exactly 240 frames, 24 fps. This is the orientation this
customer's slate actually needs, so split-and-rejoin is no longer only proven for 16:9 — the
vertical case now has the same measured answer.

**What still has not been established.** A shot long enough to need three or more segments and
therefore two or more seams, and footage whose baseline variation is much lower than either run
above — a locked-off dialogue two-shot has far less frame-to-frame motion, so the same absolute
mismatch would read as a much larger multiple of a much smaller p95. **A near-static shot is the
adversarial case and it has not been run**: a repeat attempt measured the same source clip as the
original 16:9 run above rather than genuinely low-motion footage, so this gap is still open. The
script exists and is cheap to re-run against the right source, so this is a gap to close rather than
a risk to carry silently.

### Consequence: the job count doubles, and the executor cannot take it

Splitting means **two VPE jobs per shot**, so the customer's 16 shots become **32 jobs of ~315s
each**. That runs headlong into `main.py`, which creates a single `ThreadPoolExecutor(max_workers=4)`
shared by every background job in the application, and into the existing long-job pattern in
`veo_service.py`, which polls `while not operation.done` and therefore **pins one of those four
threads for the entire job**.

Four upscales is enough to consume the app's whole background capacity. Thirty-two of them is
roughly **42 minutes during which no Veo generation, no concatenation and no workflow can start.**
This is a pre-existing ceiling rather than something Phase 1 introduces, but Phase 1 is the first
feature that gives a user an obvious reason to queue sixteen five-minute jobs at once.

**Minimum fix for Phase 1: a separate executor for VPE.** A second pool of 2-3 workers, so VPE
saturation degrades VPE and leaves the rest of the app responsive. The correct fix is a durable job
queue with resumable polling, which is real work and does not belong in Phase 1.
