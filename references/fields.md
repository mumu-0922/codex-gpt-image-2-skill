# gen-images Fields

## Modes

`generate`:

- Required: `prompt`
- Endpoint: `/images/generations`

`edit`:

- Required: `prompt`, `image`
- Optional: `mask`
- Endpoint: `/images/edits`

## Common Fields

- `model`: default `gpt-image-2`
- `prompt`
- `size`
- `quality`
- `background`
- `output_format`
- `n`: default `1`
- `moderation`
- `output_compression`
- `partial_images`

Edit-only:

- `image`
- `mask`
- `input_fidelity`

Local-only:

- `resize_mode`: `contain`, `cover`, or `stretch`
- `output_dir`: destination folder; default is `./gen-images`
- `output_name`: destination file base name; extension is derived from `output_format`

## Natural Language Mapping

Size:

- `1:1` -> `1024x1024`
- `4:3` -> `1536x1024`
- `3:4` -> `1024x1536`
- `16:9` -> target size such as `1920x1080`, then local resize
- `9:16` -> target size such as `1080x1920`, then local resize
- explicit `WIDTHxHEIGHT` -> pass through target size handling
- `auto` -> `size=auto`

Quality:

- `高清`, `高质量`, `high quality` -> `quality=high`
- `中等质量`, `medium` -> `quality=medium`
- `快速`, `草稿`, `low` -> `quality=low`

Background:

- `透明背景`, `transparent PNG`, `alpha PNG` -> `background=transparent`
- unspecified -> do not pass `background`

Format:

- `png` -> `output_format=png`
- `jpg`, `jpeg` -> `output_format=jpeg`
- `webp` -> `output_format=webp`

Count:

- `生成3张`, `3 variants`, `n=3` -> `n=3`

Resize:

- `UI overlay`, `Unity UI`, `icon`, `panel`, `transparent layer` -> `resize_mode=contain`
- `full screen background`, `cover`, `铺满` -> `resize_mode=cover`
- `stretch`, `拉伸` -> `resize_mode=stretch`

Output:

- `保存到 assets/ui`, `输出到 ./images`, `save to ./out` -> `output_dir=<path>`
- `命名为 beach-panel`, `filename beach-panel`, `output name beach-panel` -> `output_name=beach-panel`
- output folder specified but output name omitted -> save with a prompt-derived slug
- neither output folder nor output name specified -> save under `./gen-images/` with timestamp naming

## Size Handling

Native stable generation sizes:

- `1024x1024`
- `1536x1024`
- `1024x1536`
- `auto`

Rules:

1. Missing `size`: omit the field and let the API default to `auto`.
2. `size=auto`: pass `auto`.
3. Supported explicit size: pass directly.
4. Unsupported explicit size: choose closest native generation size and resize locally to the target.

The script reports both:

- `size`: final requested output size
- `generation_size`: backend generation size, only when local resize was needed

## Transparent Handling

For `gpt-image-2`, transparent output is produced by local post-processing:

1. Prompt is rewritten to request a flat `#ff00ff` chroma-key background.
2. API receives `background=opaque`.
3. Saved PNG is processed locally into alpha.

This avoids upstream failures when `background=transparent` is unsupported by the proxy chain.

## Timeout Guidance

Use 10 minutes for normal jobs:

```text
timeout=600000
```

Use 15 minutes for very large or complex jobs:

```text
timeout=900000
```

Target sizes above 8 million pixels should use the longer timeout even if native generation uses a smaller supported size.
