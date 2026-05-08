---
name: gen-images
description: Use when the user asks to generate or edit images with gpt-image-2, including "生成图片", "文生图", "修改图片", "编辑图片", "改图", transparent PNG output, or arbitrary target UI asset sizes through an OpenAI-compatible relay such as sub2api.
argument-hint: <natural language image request>
allowed-tools: [Bash, Read]
---

# gen-images

Use this skill to call `gpt-image-2` through an OpenAI-compatible relay such as sub2api for image generation and image editing.

## Task Types

Generate when the user asks for:

- `生成图片`
- `文生图`
- `画一张图`
- `使用 gpt-image-2`
- a new PNG/JPG/WebP asset

Edit when the user provides an image source and asks to change it:

- `修改图片`
- `编辑图片`
- `改图`
- `把这张图改成...`

If both an image source and edit intent are present, prefer edit mode.

## Required Fields

Generate:

- `prompt`

Edit:

- `prompt`
- `image`

Ask a short follow-up only when a required field is missing.

## Optional Fields

Extract these when present:

- `size`
- `quality`
- `background`
- `output_format`
- `n`
- `moderation`
- `output_compression`
- `partial_images`
- `input_fidelity` for edit only
- `resize_mode`
- `mask`

Do not repeatedly ask for optional fields. Use defaults when unspecified.

## Size Rules

Official stable `gpt-image-2` generation sizes are:

- `1024x1024`
- `1536x1024`
- `1024x1536`
- `auto`

Behavior:

- If the user does not specify `size`, do not pass a size. The API default is `auto`.
- If the user specifies `auto`, pass `auto`.
- If the user specifies a supported size, pass it directly.
- If the user specifies an unsupported target size, choose the closest supported generation size, then locally resize the saved PNG to the target size.

Example:

```text
--size 1920x1080
```

will generate with `1536x1024`, then save a final `1920x1080` image.

## Resize Modes

When local resize is needed:

- `contain` default: preserve aspect ratio, no crop, transparent padding.
- `cover`: preserve aspect ratio, fill target, crop overflow.
- `stretch`: non-uniform scaling to exact target.

Use `contain` for Unity UI overlays, panels, icons, and transparent assets.
Use `cover` for full-screen backgrounds.
Use `stretch` only when the user explicitly accepts distortion.

## Transparent PNG Rules

`gpt-image-2` does not reliably support native `background=transparent` through this chain.

When the user asks for transparent output:

1. Generate a PNG on a flat `#ff00ff` chroma-key background.
2. Locally remove the key color.
3. Save a true alpha PNG.

If the user does not ask for transparency, do not pass `background`; let the API default to `auto`.

## Script

Use:

```bash
python "<skill-dir>/scripts/gen_images.py" --mode generate --prompt "..."
```

or:

```bash
python "<skill-dir>/scripts/gen_images.py" --mode edit --prompt "..." --image "..."
```

Prefer `py` when available; otherwise use `python`.

Common generation example:

```bash
python "<skill-dir>/scripts/gen_images.py" \
  --mode generate \
  --prompt "transparent blue ocean wave Unity UI overlay, no text" \
  --size 1920x1080 \
  --background transparent \
  --output-format png \
  --resize-mode contain
```

## Runtime Config

When installed under `.codex`, the script reads:

- `~/.codex/config.toml`
- `~/.codex/auth.json`

It uses the active `model_provider` from `config.toml`, then reads that provider's `base_url`.
For Codex mode, set `base_url` so `<base_url>/images/generations` and `<base_url>/images/edits` are valid for the relay. If the relay expects standard `/v1/images/...` endpoints, include `/v1` in `base_url`.

When installed under `.claude`, the script reads:

- `~/.claude/settings.json`

## Output

Saved images go to the current working directory:

```text
./gen-images/
```

The script prints JSON:

```json
{"ok": true, "paths": ["..."], "used_params": {"model": "gpt-image-2", "size": "1920x1080", "generation_size": "1536x1024", "resize_mode": "contain", "background": "transparent", "output_format": "png", "n": 1}}
```

On failure:

```json
{"ok": false, "error": "..."}
```

## Notes

- Do not include unsupported OpenAI API fields such as `response_format` or default `stream` in image generation requests.
- Edit mode uses multipart upload with `image[]` and optional `mask`.
- Windows TLS failures fall back to `curl.exe --ssl-no-revoke`.
