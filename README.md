# gen-images

`gen-images` is a Codex/Claude skill for generating and editing images through a `gpt-image-2` backend exposed by an OpenAI-compatible relay, such as a sub2api-style proxy.

## Features

- Text-to-image generation
- Image editing
- Transparent PNG output through local chroma-key removal
- Arbitrary target output sizes through local resize
- Codex and Claude runtime config detection
- JSON result output for agent consumption

## Requirements

- Python 3.11+
- An OpenAI-compatible image backend or relay that supports `gpt-image-2` image endpoints.
  - Codex mode calls `<base_url>/images/generations` and `<base_url>/images/edits`.
  - If your relay expects standard OpenAI `/v1/images/...` paths, set Codex `base_url` to include `/v1`.
  - Claude mode calls `<base_url>/v1/images/generations` and `<base_url>/v1/images/edits`.
- `curl.exe` on Windows is optional but recommended as a TLS fallback

## Install For Codex

Copy the whole skill directory to:

```text
~/.codex/skills/gen-images/
```

Windows example:

```powershell
$src = "D:\path\to\codex-gpt-image-2-skill"
$dest = "$env:USERPROFILE\.codex\skills\gen-images"
Copy-Item -LiteralPath $src -Destination $dest -Recurse -Force
```

Restart Codex after installing or updating a skill.

You can also install directly from GitHub by cloning this repository into
`~/.codex/skills/gen-images`.

## Config

Codex mode reads:

```text
~/.codex/config.toml
~/.codex/auth.json
```

The script uses `model_provider` from `config.toml` and then reads:

```toml
[model_providers.<active-provider>]
base_url = "https://your-proxy.example"
```

For a sub2api or OpenAI-compatible relay, configure `base_url` so the final URL produced by the script is accepted by your relay. In Codex mode, this usually means:

```toml
base_url = "https://your-relay.example/v1"
```

Any relay can work as long as it is compatible with the image generation/edit API shape used by OpenAI-compatible `gpt-image-2` providers. A relay that only supports chat completions will not work.

The token is read from:

```json
{"OPENAI_API_KEY":"..."}
```

## Usage

Generate:

```powershell
python C:\Users\123\.codex\skills\gen-images\scripts\gen_images.py `
  --mode generate `
  --prompt "blue ocean wave Unity UI overlay, no text" `
  --size 1920x1080 `
  --background transparent `
  --output-format png `
  --resize-mode contain `
  --output-dir .\assets\ui `
  --request-timeout 900
```

Edit:

```powershell
python C:\Users\123\.codex\skills\gen-images\scripts\gen_images.py `
  --mode edit `
  --image .\input.png `
  --prompt "make the image watercolor style, preserve the subject" `
  --output-format png
```

## Size Behavior

Stable native generation sizes:

- `1024x1024`
- `1536x1024`
- `1024x1536`
- `auto`

If no `--size` is provided, the script does not pass a size and the API default is `auto`.

If an unsupported target size is provided, the script:

1. Chooses the closest supported native generation size.
2. Generates the image.
3. Resizes locally to the requested target size.

Example:

```text
target size: 1920x1080
generation size: 1536x1024
final saved PNG: 1920x1080
```

## Resize Modes

- `contain`: preserve aspect ratio, transparent padding. Best for UI overlays and icons.
- `cover`: preserve aspect ratio, crop overflow. Best for full-screen backgrounds.
- `stretch`: exact target size with distortion. Use only when explicitly desired.

Default:

```text
contain
```

## Transparent PNG

When `--background transparent` is used with `gpt-image-2`, the script:

1. Rewrites the prompt to request a flat `#ff00ff` chroma-key background.
2. Sends `background=opaque` to the backend.
3. Removes the key color locally.
4. Saves a true alpha PNG.

If no transparent background is requested, the script leaves `background` unset so the backend can use its default `auto` behavior.

## Request Timeout

The default request timeout is 600 seconds. Use a larger value for slow relays, edit mode, transparent assets, or large final output sizes:

```powershell
python C:\Users\123\.codex\skills\gen-images\scripts\gen_images.py `
  --mode generate `
  --prompt "large beach island Unity UI background, no text" `
  --size 1920x1080 `
  --output-format png `
  --request-timeout 900
```

HTTP 502, HTTP 504, gateway timeout, `RemoteDisconnected`, and local request timeout are treated as transient failures and retried before the script returns a JSON error. Repeated timeout errors usually mean the relay or upstream image provider is overloaded or its gateway timeout is too short.

## Output

Images are saved under:

```text
./gen-images/
```

You can choose the output folder:

```powershell
python C:\Users\123\.codex\skills\gen-images\scripts\gen_images.py `
  --mode generate `
  --prompt "blue ocean wave game UI panel, no text" `
  --output-dir .\assets\ui `
  --output-format png
```

This saves a prompt-derived file name such as:

```text
.\assets\ui\blue-ocean-wave-game-ui-panel-no-text.png
```

You can also set an explicit base name:

```powershell
python C:\Users\123\.codex\skills\gen-images\scripts\gen_images.py `
  --mode generate `
  --prompt "blue ocean wave game UI panel, no text" `
  --output-dir .\assets\ui `
  --output-name beach-panel `
  --output-format png
```

If `--output-dir` is omitted, the legacy default remains `./gen-images/` with timestamped names like `20260508-113439-01.png`. Multiple images add `-01`, `-02`, etc. Existing files are not overwritten; a numeric suffix is added.

Success result:

```json
{
  "ok": true,
  "paths": ["C:\\path\\to\\workspace\\gen-images\\20260508-113439-01.png"],
  "used_params": {
    "model": "gpt-image-2",
    "size": "1920x1080",
    "generation_size": "1536x1024",
    "resize_mode": "contain",
    "quality": "low",
    "background": "transparent",
    "output_format": "png",
    "output_dir": ".\\assets\\ui",
    "output_name": null,
    "request_timeout": 900,
    "n": 1
  }
}
```

Failure result:

```json
{"ok": false, "error": "short reason"}
```
