[English](README.md) | [简体中文](README_CN.md)

# wdnapps_audio_chat

Plugin package for wdnwebui. Once installed, these cards appear on
the host UI:

| Card id | Title | Description |
|---|---|---|
| `tts` | Voice Generation | Qwen3-TTS text-to-speech; with voice selection, offline preview samples, and user voice cloning (upload/record a reference clip and extract the x-vector on the spot) |
| `qwen3-asr` | Streaming Speech Recognition | Qwen3-ASR streaming transcription of audio files / videos with an audio track, plus live recognition from the browser microphone or the on-board ALSA device |
| `text-chat` | Text Chat | Multi-turn text conversation with a local LLM (Qwen3.5-VL 4B / 9B), streaming tokens, tool calls and a context-window counter. **Staged here — see below** |

> ### `text-chat` is staged here, not finally placed ⚠️
>
> `text-chat` is a **text → text** LLM card: it neither produces nor consumes
> audio, and its capability `swcap_text_chat` does not belong to the audio
> domain. This package is the *audio* application plugin package.
>
> It lives here as a **staging step**, not as a final decision:
>
> 1. **Done** — the card moved out of the host repo (`app/apps/text_chat/`) into
>    the plugin system, so the host keeps only core + system pages and no
>    longer carries the source of any individual card.
> 2. **Planned** — the text card moves to a dedicated `wdnapps-chat` package,
>    matching how this ecosystem groups cards **by modality**
>    (`wdnapps_video_analyzer` / `wdnapps_audio_chat` /
>    `wdnapps_media_generator` / `wdnapps_hdmi_processor`).
>
> Do the split when the host's `app/apps/` is empty, when a second text-only
> card shows up, or when this package's semantic boundary starts to hurt.
>
> The split **changes the package, never the contract**: the card id
> `text-chat`, the API prefix `/api/text-chat`, the static path
> `/static/apps/text-chat/*` and the wizard hook
> `window.ImApp_text_chat.applyWizardSnapshot` all stay unchanged — users
> notice nothing.
>
> Read [`docs/TEXT_CHAT_MIGRATION.md`](docs/TEXT_CHAT_MIGRATION.md) before
> touching this card: it has the full migration steps, the extraction steps
> and the regression commands.

## What Form It Takes

**This is a wdnwebui plugin, not a standalone application.** The card backend imports
host services with `from app.core.*` — `ComputeRegistry` (the only source of
`LLMSession`), `app.core.audio.tts_bridge` (TTSRequest / session proxy),
`app.core.audio.capture` / `app.core.audio.endpoint` (PCM capture, resampling, pause
detection), `app.core.uploads` (where user voices land on disk), and so on. These are
the call paths mandated by wdnwebui design principles 4/5; a plugin cannot route around
them and should not try. Therefore this wheel must live in the **same interpreter** as
wdnwebui, with the wdnwebui repo root on `sys.path`.

> ⚠️ The `app.core.audio.*` services above are what the two **audio** cards
> (`tts` / `qwen3-asr`) depend on. The staged `text_chat` card uses a different
> set: `app.core.chat.*` (agent / runner / serial), `app.core.compute.registry`,
> `app.core.tools.registry` and `app.core.thinking` — it does not touch a single
> `app.core.audio.*` module. The three cards share **no code** with each other;
> they merely live in the same package. The dependency direction is always
> **card → `app.core.*`**.

## Installation

The host repo root carries an `update_rkwhl.sh` that drives the four SDKs and the
three card plugin packages together. **Prefer running it** over manual pip:

```bash
cd <wdnwebui>
./update_rkwhl.sh                       # reinstall only what the git HEAD says changed
UPDATE_RKWHL_FORCE_REBUILD=1 ./update_rkwhl.sh   # force a full reinstall
```

What the script does for this package: if HEAD is unchanged it does not touch `.venv`
at all (pip is not even run); only when HEAD changes does it run `purge_install_traces`
+ `pip install -e`. After installing it runs a card registration check
(`scripts/check_plugin_entrypoints.py`) that verifies, card by card: "the entry point
resolves → the manifest `"id"` is character-for-character identical to the entry point
name → the card directory has `static/`". This blocks the silent failure where pip
reports success all the way through, yet the host startup FATAL-skips this card.

Manual installation also works (this is the path the script takes on first clone):

```bash
# lands in wdnwebui's venv, next to wdnai/wdnav/wdndds
cd <wdnwebui>/.venv/src
git clone git@github.com:rockchip-wardner/wdnapps_audio_chat.git
pip install -e wdnapps_audio_chat
```

On host restart it scans the `wdnwebui.cards` entry point group, reads each card's
`manifest.json`, and mounts its routes plus the `/static/apps/<id>` static directory.
No host code or configuration needs to change after installation.

> `pip install` does not silently skip a card for missing dependencies — the host's
> `_discover_from_entry_points()` wraps each card in its own try/except: if one card
> fails to import it only logs `LOG.error` and skips that card, the rest load normally
> and the web UI still starts. For troubleshooting, look straight for
> `[<card_id>] plugin entry point ... not importable` in the host log.

## Adding a New Card

1. Create `src/wdnapps_audio_chat/cards/<new-card>/`, copying the layout of the
   existing card: `__init__.py` (`register_api` / `register_shutdown`), `api.py`,
   `service.py`, `manifest.json`,
   `static/{template.html,app.js,app.css,wizard.json}`.
   Extra backend modules are allowed and are **not** listed in `package-data`
   (only `manifest.json` and `static/*` are data): the `qwen3-asr` card, for
   instance, carries `board_mic.py` (on-board ALSA capture subprocess
   management) next to `api.py` / `service.py`.
2. Always write in-card imports in full as
   `wdnapps_audio_chat.cards.<new-card>.xxx`; **do not** write `app.apps.*`.
3. Register in **two** places in `pyproject.toml`, both required:
   - Add `<card-id> = "…cards.<new-card>"` under
     `[project.entry-points."wdnwebui.cards"]` — the left side must be
     character-for-character identical to the `"id"` in `manifest.json`, otherwise the
     host marks it FATAL and skips it.
   - Add the card's `["manifest.json", "static/*"]` under
     `[tool.setuptools.package-data]` — without it the static assets do not enter the
     wheel, so the card is discoverable but the front end 404s.
     **List sub-directories explicitly** when there are any (this card's preview
     samples live in `static/previews/`, so `static/previews/*` is listed):
     `static/*` only matches direct children, and a missing sub-directory is
     completely invisible under an editable install — it only shows up in a real
     wheel.

## Regenerating the Preview Samples

The preview samples for the built-in voices (`cards/tts/static/previews/*.wav`) are
**offline artefacts**: synthesizing one clip per voice takes over ten seconds (it runs
on the NPU), which is far too slow to compute on the fly when the page loads. The
generation script lives at `scripts/gen_voice_previews.py`:

```bash
# run in this package's repo root; the .venv is the host wdnwebui's — the script synthesizes through the host's HTTP API
cd <wdnwebui>/.venv/src/wdnapps-audio-chat
<wdnwebui>/.venv/bin/python -u scripts/gen_voice_previews.py
```

It calls the host's `POST /api/tts/start` to synthesize (`play_to=browser`, so batch
generation does not sound out of the board's speaker), then converts the SDK's
**32bit float** WAV into **16bit PCM** (half the size, far more compatible with
players) and drops it into the card's `static/previews/`.

After changing the voice table or the preview text, run it again — otherwise the
samples no longer match the actual voices, and it is those files the preview button
plays.

> The other two plugin packages (video-analyzer / hdmi-processor) have no `scripts/`:
> all of their static assets are hand-written, with no "artefacts that need offline
> generation". This package does, so the tool lives next to the artefact it produces
> — whoever changes the voice table can regenerate in place.

## Boundary with the Host

A plugin may **only** `from app.core.*`; it must not import the host's `app.apps.*`
(those are other cards and may not even be installed). The reverse is forbidden too:
core must not depend on plugins — which is why `get_session()` in the host's
`app/core/audio/tts_bridge.py` goes through a **candidate path list** (this package
first, then the old in-tree path). When the plugin is not installed it raises
ImportError, and the caller (the built-in `tts_alert` tool) soft-fails with "tool
unavailable".

> **One exception for the `qwen3-asr` card: the on-board microphone capture script
> stays in the host.** `tools/asr_mic_agent.py` (the standalone process that runs ALSA
> capture) imports `app.core.audio.alsa` — it is a **host** tool, so it stays under the
> host's `tools/` instead of moving into this package with the card. The card locates
> it through `board_mic.py::_host_repo_root()` (which back-derives the host repo root
> from `app.core.__file__`), so the lookup **does not depend on where the plugin is
> installed** — an editable install under `.venv/src/` and a real wheel in
> site-packages both point at the same host `tools/`. When it cannot be found the card
> reports "capture script not found" instead of failing to import.

## License

Apache-2.0. See [LICENSE](LICENSE).
