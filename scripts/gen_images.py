#!/usr/bin/env python3
import argparse
import base64
import http.client
import json
import mimetypes
import os
import re
import shutil
import subprocess
import struct
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib
from datetime import datetime
from pathlib import Path


TRANSPARENT_POSTPROCESS_KEY = "_transparent_postprocess"
IMAGE_SOURCES_KEY = "_image_sources"
MASK_SOURCE_KEY = "_mask_source"
RESIZE_TARGET_KEY = "_resize_target"
RESIZE_MODE_KEY = "_resize_mode"
RESTORE_SOURCE_SIZE_KEY = "_restore_source_size"
CHROMA_KEY_HEX = "#ff00ff"
CHROMA_KEY_RGB = (255, 0, 255)
SUPPORTED_GPT_IMAGE_2_SIZES = {
    "1024x1024": (1024, 1024),
    "1536x1024": (1536, 1024),
    "1024x1536": (1024, 1536),
}


class TransientImageApiError(RuntimeError):
    pass


def fail(message: str, status_code: int = 1):
    print(json.dumps({"ok": False, "error": message}, ensure_ascii=False))
    sys.exit(status_code)


def load_json(path: Path, label: str):
    if not path.exists():
        raise RuntimeError(f"未找到配置文件: {path}")

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"读取 {label} 失败: {exc}") from exc


def load_toml(path: Path, label: str):
    if not path.exists():
        raise RuntimeError(f"未找到配置文件: {path}")

    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"读取 {label} 失败: {exc}") from exc


def load_claude_settings():
    settings_path = Path.home() / ".claude" / "settings.json"
    data = load_json(settings_path, "Claude settings.json")

    env = data.get("env") or {}
    base_url = env.get("ANTHROPIC_BASE_URL")
    token = env.get("ANTHROPIC_AUTH_TOKEN")

    if not base_url:
        raise RuntimeError("settings.json 中缺少 env.ANTHROPIC_BASE_URL")
    if not token:
        raise RuntimeError("settings.json 中缺少 env.ANTHROPIC_AUTH_TOKEN")

    return base_url.rstrip("/"), token


def load_codex_settings():
    config_path = Path.home() / ".codex" / "config.toml"
    auth_path = Path.home() / ".codex" / "auth.json"

    config = load_toml(config_path, "Codex config.toml")
    auth = load_json(auth_path, "Codex auth.json")

    model_providers = config.get("model_providers") or {}
    provider_name = config.get("model_provider") or "OpenAI"
    provider = model_providers.get(provider_name) or model_providers.get("OpenAI") or {}
    base_url = provider.get("base_url")
    token = auth.get("OPENAI_API_KEY")

    if not base_url:
        raise RuntimeError("config.toml 中缺少 model_providers.<active-provider>.base_url")
    if not token:
        raise RuntimeError("auth.json 中缺少 OPENAI_API_KEY")

    return str(base_url).rstrip("/"), str(token)


def detect_caller_from_script_dir():
    current = Path(__file__).resolve().parent
    directories = (current, *current.parents)

    for directory in directories:
        if directory.name == ".codex":
            return "codex"

    for directory in directories:
        if directory.name == ".claude":
            return "claude"

    return None


def load_runtime_settings():
    caller = detect_caller_from_script_dir()
    if caller == "claude":
        base_url, token = load_claude_settings()
        return caller, base_url, token
    if caller == "codex":
        base_url, token = load_codex_settings()
        return caller, base_url, token

    errors = []
    for name, loader in (("claude", load_claude_settings), ("codex", load_codex_settings)):
        try:
            base_url, token = loader()
            return name, base_url, token
        except RuntimeError as exc:
            errors.append(f"{name}: {exc}")

    raise RuntimeError("; ".join(errors))


def build_api_url(caller: str, base_url: str, mode: str):
    endpoint = "/images/generations" if mode == "generate" else "/images/edits"
    if caller == "claude":
        return f"{base_url}/v1{endpoint}"
    return f"{base_url}{endpoint}"


def guess_mime(path: Path):
    mime, _ = mimetypes.guess_type(str(path))
    return mime or "application/octet-stream"


def file_to_data_url(path_str: str):
    path = Path(path_str)
    if not path.exists():
        fail(f"图片文件不存在: {path_str}")
    data = path.read_bytes()
    mime = guess_mime(path)
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


DATA_URL_RE = re.compile(r"^data:(?P<mime>[^;]+);base64,(?P<data>.+)$", re.IGNORECASE)


def parse_data_url(data_url: str):
    match = DATA_URL_RE.match(data_url)
    if not match:
        fail("返回的 data URL 格式无效")
    mime = match.group("mime")
    raw = match.group("data")
    try:
        data = base64.b64decode(raw)
    except Exception as exc:
        fail(f"解析返回图片失败: {exc}")
    return mime, data


def choose_extension(output_format: str | None, mime: str | None = None):
    if output_format:
        normalized = output_format.lower()
        if normalized == "jpeg":
            return "jpg"
        return normalized
    if mime:
        ext = mimetypes.guess_extension(mime)
        if ext:
            return ext.lstrip(".").replace("jpe", "jpg")
    return "png"


def is_gpt_image_2(model: str | None):
    return (model or "gpt-image-2") == "gpt-image-2"


def should_chroma_key_transparency(payload: dict):
    return bool(payload.pop(TRANSPARENT_POSTPROCESS_KEY, False))


def strip_internal_payload_fields(payload: dict):
    internal_keys = {
        TRANSPARENT_POSTPROCESS_KEY,
        IMAGE_SOURCES_KEY,
        MASK_SOURCE_KEY,
            RESIZE_TARGET_KEY,
            RESIZE_MODE_KEY,
            RESTORE_SOURCE_SIZE_KEY,
        }
    return {
        key: value
        for key, value in payload.items()
        if key not in internal_keys
    }


def parse_size(value: str | None):
    if not value:
        return None
    match = re.fullmatch(r"([1-9]\d*)x([1-9]\d*)", value)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def best_supported_generation_size(target_width: int, target_height: int):
    target_ratio = target_width / target_height

    def score(item):
        _, (width, height) = item
        ratio = width / height
        ratio_delta = abs(ratio - target_ratio)
        area_delta = abs((width * height) - (target_width * target_height)) / max(target_width * target_height, 1)
        return ratio_delta * 10 + area_delta

    return min(SUPPORTED_GPT_IMAGE_2_SIZES.items(), key=score)


def normalize_generation_size(payload: dict):
    size = payload.get("size")
    parsed = parse_size(size)
    if not parsed or not is_gpt_image_2(payload.get("model")):
        return
    if size in SUPPORTED_GPT_IMAGE_2_SIZES:
        return
    width, height = parsed
    supported_size, _ = best_supported_generation_size(width, height)
    payload[RESIZE_TARGET_KEY] = (width, height)
    payload["size"] = supported_size


def normalize_resize_mode(payload: dict, resize_mode: str | None):
    if not resize_mode:
        return
    mode = resize_mode.lower()
    if mode not in {"contain", "cover", "stretch"}:
        fail("resize_mode must be one of contain, cover, or stretch")
    payload[RESIZE_MODE_KEY] = mode


def apply_transparent_generation_fallback(payload: dict):
    payload[TRANSPARENT_POSTPROCESS_KEY] = True
    payload["background"] = "opaque"
    payload["output_format"] = "png"
    payload["prompt"] = (
        f"Create the requested image on a perfectly flat solid {CHROMA_KEY_HEX} "
        "chroma-key background for background removal. The background must be one "
        "uniform color with no shadows, gradients, texture, reflections, floor plane, "
        f"or lighting variation. Do not use {CHROMA_KEY_HEX} anywhere in the subject. "
        "No cast shadow, no contact shadow, no reflection. Original request: "
        f"{payload['prompt']}"
    )


def ensure_output_dir():
    output_dir = Path.cwd() / "gen-images"
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def retry_transient(operation, attempts: int = 3, delays: tuple[int, ...] = (5, 10)):
    last_error = None
    for attempt in range(attempts):
        try:
            return operation()
        except TransientImageApiError as exc:
            last_error = exc
            if attempt >= attempts - 1:
                fail(f"临时上游错误，重试后仍失败: {exc}")
            delay = delays[min(attempt, len(delays) - 1)]
            time.sleep(delay)
    raise last_error


def save_images(image_entries, output_format: str | None):
    output_dir = ensure_output_dir()
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    paths = []

    for index, item in enumerate(image_entries, start=1):
        ext = choose_extension(output_format)
        if item.get("b64_json"):
            try:
                binary = base64.b64decode(item["b64_json"])
            except Exception as exc:
                fail(f"解码返回图片失败: {exc}")
        elif item.get("url", "").startswith("data:"):
            mime, binary = parse_data_url(item["url"])
            ext = choose_extension(output_format, mime)
        else:
            fail("接口返回中未找到可保存的图片数据")

        file_path = output_dir / f"{timestamp}-{index:02d}.{ext}"
        file_path.write_bytes(binary)
        paths.append(str(file_path))

    return paths


def paeth_predictor(a: int, b: int, c: int):
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def parse_png_rgba(data: bytes):
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG image")

    offset = 8
    width = height = bit_depth = color_type = interlace = None
    chunks = []

    while offset < len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        chunk_type = data[offset + 4 : offset + 8]
        chunk_data = data[offset + 8 : offset + 8 + length]
        offset += 12 + length

        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type, _, _, interlace = struct.unpack(
                ">IIBBBBB", chunk_data
            )
        elif chunk_type == b"IDAT":
            chunks.append(chunk_data)
        elif chunk_type == b"IEND":
            break

    if bit_depth != 8 or color_type not in (2, 6) or interlace != 0:
        raise ValueError("unsupported PNG format for chroma-key removal")

    bytes_per_pixel = 4 if color_type == 6 else 3
    stride = width * bytes_per_pixel
    raw = zlib.decompress(b"".join(chunks))
    pixels = bytearray(width * height * bytes_per_pixel)
    raw_offset = 0
    out_offset = 0
    previous = bytearray(stride)

    for _ in range(height):
        filter_type = raw[raw_offset]
        raw_offset += 1
        row = bytearray(raw[raw_offset : raw_offset + stride])
        raw_offset += stride

        for x in range(stride):
            left = row[x - bytes_per_pixel] if x >= bytes_per_pixel else 0
            up = previous[x]
            up_left = previous[x - bytes_per_pixel] if x >= bytes_per_pixel else 0
            if filter_type == 0:
                value = row[x]
            elif filter_type == 1:
                value = row[x] + left
            elif filter_type == 2:
                value = row[x] + up
            elif filter_type == 3:
                value = row[x] + ((left + up) // 2)
            elif filter_type == 4:
                value = row[x] + paeth_predictor(left, up, up_left)
            else:
                raise ValueError(f"unsupported PNG filter: {filter_type}")
            row[x] = value & 0xFF

        pixels[out_offset : out_offset + stride] = row
        out_offset += stride
        previous = row

    rgba = bytearray(width * height * 4)
    for src, dst in zip(range(0, len(pixels), bytes_per_pixel), range(0, len(rgba), 4)):
        rgba[dst] = pixels[src]
        rgba[dst + 1] = pixels[src + 1]
        rgba[dst + 2] = pixels[src + 2]
        rgba[dst + 3] = pixels[src + 3] if color_type == 6 else 255

    return width, height, rgba


def png_chunk(chunk_type: bytes, data: bytes = b""):
    return (
        struct.pack(">I", len(data))
        + chunk_type
        + data
        + struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
    )


def encode_png_rgba(width: int, height: int, rgba: bytearray):
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    stride = width * 4
    raw = bytearray((stride + 1) * height)
    for y in range(height):
        row_start = y * (stride + 1)
        raw[row_start] = 0
        raw[row_start + 1 : row_start + 1 + stride] = rgba[y * stride : (y + 1) * stride]

    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", ihdr)
        + png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + png_chunk(b"IEND")
    )


def clamp_byte(value: float):
    return max(0, min(255, int(round(value))))


def remove_chroma_key_from_png(data: bytes):
    width, height, rgba = parse_png_rgba(data)
    key_r, key_g, key_b = CHROMA_KEY_RGB

    for index in range(0, len(rgba), 4):
        r, g, b = rgba[index], rgba[index + 1], rgba[index + 2]
        distance = ((r - key_r) ** 2 + (g - key_g) ** 2 + (b - key_b) ** 2) ** 0.5
        alpha = 255
        if distance <= 36:
            alpha = 0
        elif distance < 120:
            alpha = clamp_byte(((distance - 36) / 84) * 255)

        if 0 < alpha < 255:
            alpha_ratio = alpha / 255
            rgba[index] = clamp_byte((r - (1 - alpha_ratio) * key_r) / alpha_ratio)
            rgba[index + 1] = clamp_byte((g - (1 - alpha_ratio) * key_g) / alpha_ratio)
            rgba[index + 2] = clamp_byte((b - (1 - alpha_ratio) * key_b) / alpha_ratio)
        rgba[index + 3] = min(rgba[index + 3], alpha)

    for y in range(height):
        for x in range(width):
            if 80 < x < width - 81 and 80 < y < height - 81:
                continue
            alpha_index = (y * width + x) * 4 + 3
            if rgba[alpha_index] < 64:
                rgba[alpha_index] = 0

    return encode_png_rgba(width, height, rgba)


def sample_rgba_bilinear(width: int, height: int, rgba: bytearray, x: float, y: float, channel: int):
    x0 = max(0, min(width - 1, int(x)))
    y0 = max(0, min(height - 1, int(y)))
    x1 = max(0, min(width - 1, x0 + 1))
    y1 = max(0, min(height - 1, y0 + 1))
    tx = x - x0
    ty = y - y0
    i00 = (y0 * width + x0) * 4 + channel
    i10 = (y0 * width + x1) * 4 + channel
    i01 = (y1 * width + x0) * 4 + channel
    i11 = (y1 * width + x1) * 4 + channel
    top = rgba[i00] * (1 - tx) + rgba[i10] * tx
    bottom = rgba[i01] * (1 - tx) + rgba[i11] * tx
    return clamp_byte(top * (1 - ty) + bottom * ty)


def resize_png_contain(data: bytes, target_width: int, target_height: int):
    width, height, rgba = parse_png_rgba(data)
    return resize_rgba(width, height, rgba, target_width, target_height, "contain")


def resize_png(data: bytes, target_width: int, target_height: int, mode: str = "contain"):
    width, height, rgba = parse_png_rgba(data)
    return resize_rgba(width, height, rgba, target_width, target_height, mode)


def resize_rgba(
    width: int,
    height: int,
    rgba: bytearray,
    target_width: int,
    target_height: int,
    mode: str,
):
    if mode == "contain":
        scale_x = scale_y = min(target_width / width, target_height / height)
    elif mode == "cover":
        scale_x = scale_y = max(target_width / width, target_height / height)
    elif mode == "stretch":
        scale_x = target_width / width
        scale_y = target_height / height
    else:
        fail("resize_mode must be one of contain, cover, or stretch")

    resized_width = max(1, round(width * scale_x))
    resized_height = max(1, round(height * scale_y))
    resized = bytearray(resized_width * resized_height * 4)

    for y in range(resized_height):
        source_y = (y + 0.5) / scale_y - 0.5
        for x in range(resized_width):
            source_x = (x + 0.5) / scale_x - 0.5
            dst = (y * resized_width + x) * 4
            for channel in range(4):
                resized[dst + channel] = sample_rgba_bilinear(
                    width, height, rgba, source_x, source_y, channel
                )

    canvas = bytearray(target_width * target_height * 4)
    left = (target_width - resized_width) // 2
    top = (target_height - resized_height) // 2
    for y in range(resized_height):
        dst_y = top + y
        if dst_y < 0 or dst_y >= target_height:
            continue
        src_x0 = max(0, -left)
        dst_x0 = max(0, left)
        copy_width = min(resized_width - src_x0, target_width - dst_x0)
        if copy_width <= 0:
            continue
        src_start = (y * resized_width + src_x0) * 4
        src_end = src_start + copy_width * 4
        dst_start = (dst_y * target_width + dst_x0) * 4
        canvas[dst_start : dst_start + copy_width * 4] = resized[src_start:src_end]

    return encode_png_rgba(target_width, target_height, canvas)


def png_has_transparency(data: bytes):
    _, _, rgba = parse_png_rgba(data)
    return any(rgba[index + 3] < 255 for index in range(0, len(rgba), 4))


def restore_alpha_from_source_png(edited_data: bytes, source_data: bytes):
    source_width, source_height, source_rgba = parse_png_rgba(source_data)
    edited_width, edited_height, edited_rgba = parse_png_rgba(edited_data)

    if source_width != edited_width or source_height != edited_height:
        source_png = encode_png_rgba(source_width, source_height, source_rgba)
        source_width, source_height, source_rgba = parse_png_rgba(
            resize_png(source_png, edited_width, edited_height, "stretch")
        )

    for index in range(0, len(edited_rgba), 4):
        edited_rgba[index + 3] = source_rgba[index + 3]

    return encode_png_rgba(edited_width, edited_height, edited_rgba)


def read_local_image_bytes(source: str | None):
    if not source or source.startswith(("http://", "https://", "data:")):
        return None
    path = Path(source)
    if not path.exists():
        return None
    return path.read_bytes()


def read_local_png_size(source: str | None):
    data = read_local_image_bytes(source)
    if not data:
        return None
    try:
        width, height, _ = parse_png_rgba(data)
        return width, height
    except Exception:
        return None


def save_image_bytes(
    image_entries,
    output_format: str | None,
    transparent_postprocess: bool = False,
    resize_target: tuple[int, int] | None = None,
    resize_mode: str = "contain",
    restore_alpha_source: bytes | None = None,
):
    output_dir = ensure_output_dir()
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    paths = []

    for index, item in enumerate(image_entries, start=1):
        ext = choose_extension(output_format)
        if item.get("b64_json"):
            try:
                binary = base64.b64decode(item["b64_json"])
            except Exception as exc:
                fail(f"解码返回图片失败: {exc}")
        elif item.get("url", "").startswith("data:"):
            mime, binary = parse_data_url(item["url"])
            ext = choose_extension(output_format, mime)
        else:
            fail("接口返回中未找到可保存的图片数据")

        if transparent_postprocess:
            try:
                binary = remove_chroma_key_from_png(binary)
                ext = "png"
            except Exception as exc:
                fail(f"透明背景后处理失败: {exc}")

        if resize_target:
            try:
                binary = resize_png(binary, resize_target[0], resize_target[1], resize_mode)
                ext = "png"
            except Exception as exc:
                fail(f"尺寸后处理失败: {exc}")

        if restore_alpha_source:
            try:
                binary = restore_alpha_from_source_png(binary, restore_alpha_source)
                ext = "png"
            except Exception as exc:
                fail(f"透明通道恢复失败: {exc}")

        file_path = output_dir / f"{timestamp}-{index:02d}.{ext}"
        file_path.write_bytes(binary)
        paths.append(str(file_path))

    return paths


def post_json(url: str, token: str, payload: dict):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (502, 504):
            raise TransientImageApiError(f"HTTP {exc.code}: {exc.reason}")
        try:
            raw = exc.read().decode("utf-8")
            data = json.loads(raw)
            message = data.get("error", {}).get("message") or data.get("message") or raw
        except Exception:
            message = exc.reason or f"HTTP {exc.code}"
        fail(f"接口调用失败: {message}")
    except urllib.error.URLError as exc:
        if should_retry_with_curl(exc):
            return post_json_with_curl(url, token, payload)
        fail(f"网络请求失败: {exc.reason}")
    except http.client.RemoteDisconnected:
        raise TransientImageApiError("RemoteDisconnected")


def should_retry_with_curl(exc: urllib.error.URLError):
    return os.name == "nt" and shutil.which("curl.exe") and "SSL" in str(exc.reason)


def post_json_with_curl(url: str, token: str, payload: dict):
    output_dir = ensure_output_dir()
    request_path = output_dir / f"request-{uuid.uuid4().hex}.json"
    response_path = output_dir / f"response-{uuid.uuid4().hex}.json"
    request_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    command = [
        "curl.exe",
        "--fail-with-body",
        "--silent",
        "--show-error",
        "--ssl-no-revoke",
        "--request",
        "POST",
        "--url",
        url,
        "--header",
        "Content-Type: application/json",
        "--header",
        f"Authorization: Bearer {token}",
        "--data-binary",
        f"@{request_path}",
        "--output",
        str(response_path),
    ]
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=900)
        raw = response_path.read_text(encoding="utf-8") if response_path.exists() else ""
        if result.returncode != 0:
            if "502" in raw or "504" in raw or "Gateway Time-out" in raw or "Gateway Timeout" in raw:
                raise TransientImageApiError(raw or result.stderr or "transient gateway failure")
            try:
                data = json.loads(raw)
                message = data.get("error", {}).get("message") or data.get("message") or raw
            except Exception:
                message = raw or result.stderr or f"curl exited {result.returncode}"
            fail(f"接口调用失败: {message}")
        return json.loads(raw)
    finally:
        try:
            request_path.unlink(missing_ok=True)
            response_path.unlink(missing_ok=True)
        except Exception:
            pass


def parse_curl_response(response_path: Path, result: subprocess.CompletedProcess):
    raw = response_path.read_text(encoding="utf-8") if response_path.exists() else ""
    if result.returncode != 0:
        if "502" in raw or "504" in raw or "Gateway Time-out" in raw or "Gateway Timeout" in raw:
            raise TransientImageApiError(raw or result.stderr or "transient gateway failure")
        try:
            data = json.loads(raw)
            message = data.get("error", {}).get("message") or data.get("message") or raw
        except Exception:
            message = raw or result.stderr or f"curl exited {result.returncode}"
        fail(f"接口调用失败: {message}")
    return json.loads(raw)


def post_multipart_with_curl(
    url: str,
    token: str,
    payload: dict,
    image_sources: list[str],
    mask_source: str | None,
):
    output_dir = ensure_output_dir()
    response_path = output_dir / f"response-{uuid.uuid4().hex}.json"
    command = [
        "curl.exe",
        "--fail-with-body",
        "--silent",
        "--show-error",
        "--ssl-no-revoke",
        "--request",
        "POST",
        "--url",
        url,
        "--header",
        f"Authorization: Bearer {token}",
    ]

    for key, value in payload.items():
        if value is not None:
            command.extend(["--form", f"{key}={value}"])

    for source in image_sources:
        if source.startswith("data:"):
            fail("curl fallback for edit mode does not support data URL images yet")
        path = Path(source)
        if not path.exists():
            fail(f"图片文件不存在: {source}")
        command.extend(["--form", f"image[]=@{path}"])

    if mask_source:
        if mask_source.startswith("data:"):
            fail("curl fallback for edit mode does not support data URL masks yet")
        mask_path = Path(mask_source)
        if not mask_path.exists():
            fail(f"mask 文件不存在: {mask_source}")
        command.extend(["--form", f"mask=@{mask_path}"])

    command.extend(["--output", str(response_path)])
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=900)
        return parse_curl_response(response_path, result)
    finally:
        try:
            response_path.unlink(missing_ok=True)
        except Exception:
            pass


def post_multipart(url: str, token: str, payload: dict, image_sources: list[str], mask_source: str | None):
    boundary = f"----genImages{uuid.uuid4().hex}"
    parts = bytearray()

    def add_field(name: str, value):
        parts.extend(f"--{boundary}\r\n".encode("utf-8"))
        parts.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        parts.extend(str(value).encode("utf-8"))
        parts.extend(b"\r\n")

    def add_file(field_name: str, source: str):
        if source.startswith("data:"):
            mime, binary = parse_data_url(source)
            filename = f"{field_name}.png"
        else:
            path = Path(source)
            if not path.exists():
                fail(f"图片文件不存在: {source}")
            mime = guess_mime(path)
            filename = path.name
            binary = path.read_bytes()

        parts.extend(f"--{boundary}\r\n".encode("utf-8"))
        parts.extend(
            (
                f'Content-Disposition: form-data; name="{field_name}"; '
                f'filename="{filename}"\r\n'
            ).encode("utf-8")
        )
        parts.extend(f"Content-Type: {mime}\r\n\r\n".encode("utf-8"))
        parts.extend(binary)
        parts.extend(b"\r\n")

    for key, value in payload.items():
        if value is not None:
            add_field(key, value)

    for source in image_sources:
        add_file("image[]", source)

    if mask_source:
        add_file("mask", mask_source)

    parts.extend(f"--{boundary}--\r\n".encode("utf-8"))
    req = urllib.request.Request(
        url,
        data=bytes(parts),
        method="POST",
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (502, 504):
            raise TransientImageApiError(f"HTTP {exc.code}: {exc.reason}")
        try:
            raw = exc.read().decode("utf-8")
            data = json.loads(raw)
            message = data.get("error", {}).get("message") or data.get("message") or raw
        except Exception:
            message = exc.reason or f"HTTP {exc.code}"
        fail(f"接口调用失败: {message}")
    except urllib.error.URLError as exc:
        if should_retry_with_curl(exc):
            return post_multipart_with_curl(url, token, payload, image_sources, mask_source)
        fail(f"网络请求失败: {exc.reason}")
    except http.client.RemoteDisconnected:
        return post_multipart_with_curl(url, token, payload, image_sources, mask_source)


def build_generation_payload(args):
    if not args.prompt:
        fail("缺少 prompt")

    payload = {
        "model": args.model or "gpt-image-2",
        "prompt": args.prompt,
        "n": args.n or 1,
    }

    optional_fields = [
        "size",
        "quality",
        "background",
        "output_format",
        "output_compression",
        "partial_images",
        "moderation",
    ]
    for field in optional_fields:
        value = getattr(args, field, None)
        if value is not None:
            payload[field] = value
    normalize_resize_mode(payload, getattr(args, "resize_mode", None))
    normalize_generation_size(payload)
    if payload.get("background") == "transparent" and is_gpt_image_2(payload.get("model")):
        apply_transparent_generation_fallback(payload)
    return payload


def build_edit_payload(args):
    if not args.prompt:
        fail("缺少 prompt")
    if not args.image:
        fail("缺少要编辑的图片来源")

    payload = {
        "model": args.model or "gpt-image-2",
        "prompt": args.prompt,
        "n": args.n or 1,
        IMAGE_SOURCES_KEY: [args.image],
        RESTORE_SOURCE_SIZE_KEY: True,
    }

    if args.mask:
        payload[MASK_SOURCE_KEY] = args.mask

    optional_fields = [
        "size",
        "quality",
        "background",
        "output_format",
        "output_compression",
        "partial_images",
        "moderation",
        "input_fidelity",
    ]
    for field in optional_fields:
        value = getattr(args, field, None)
        if field == "input_fidelity" and is_gpt_image_2(payload.get("model")):
            continue
        if value is not None:
            payload[field] = value
    normalize_resize_mode(payload, getattr(args, "resize_mode", None))
    normalize_generation_size(payload)
    if payload.get("background") == "transparent" and is_gpt_image_2(payload.get("model")):
        apply_transparent_generation_fallback(payload)
    return payload


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["generate", "edit"], required=True)
    parser.add_argument("--prompt")
    parser.add_argument("--model")
    parser.add_argument("--image")
    parser.add_argument("--mask")
    parser.add_argument("--size")
    parser.add_argument("--quality")
    parser.add_argument("--background")
    parser.add_argument("--output-format", dest="output_format")
    parser.add_argument("--output-compression", dest="output_compression", type=int)
    parser.add_argument("--partial-images", dest="partial_images", type=int)
    parser.add_argument("--n", type=int)
    parser.add_argument("--moderation")
    parser.add_argument("--input-fidelity", dest="input_fidelity")
    parser.add_argument("--resize-mode", dest="resize_mode", choices=["contain", "cover", "stretch"])
    return parser.parse_args()


def main():
    args = parse_args()
    try:
        caller, base_url, token = load_runtime_settings()
    except RuntimeError as exc:
        fail(str(exc))

    if args.mode == "generate":
        payload = build_generation_payload(args)
    else:
        payload = build_edit_payload(args)

    url = build_api_url(caller, base_url, args.mode)

    transparent_postprocess = should_chroma_key_transparency(payload)
    resize_target = payload.pop(RESIZE_TARGET_KEY, None)
    resize_mode = payload.pop(RESIZE_MODE_KEY, "contain")
    restore_source_size = payload.pop(RESTORE_SOURCE_SIZE_KEY, False)
    image_sources = payload.pop(IMAGE_SOURCES_KEY, None)
    mask_source = payload.pop(MASK_SOURCE_KEY, None)
    restore_alpha_source = None
    if args.mode == "edit" and image_sources:
        if restore_source_size and not resize_target:
            resize_target = read_local_png_size(image_sources[0])
            resize_mode = "stretch"
        source_bytes = read_local_image_bytes(image_sources[0])
        if source_bytes and png_has_transparency(source_bytes):
            restore_alpha_source = source_bytes
    api_payload = strip_internal_payload_fields(payload)

    if args.mode == "edit":
        response = retry_transient(
            lambda: post_multipart(url, token, api_payload, image_sources or [], mask_source)
        )
    else:
        response = retry_transient(lambda: post_json(url, token, api_payload))
    data = response.get("data")
    if not isinstance(data, list) or not data:
        fail("接口返回中缺少 data")

    used_params = {
        "model": api_payload.get("model", "gpt-image-2"),
        "size": f"{resize_target[0]}x{resize_target[1]}" if resize_target else api_payload.get("size"),
        "generation_size": api_payload.get("size") if resize_target else None,
        "resize_mode": resize_mode if resize_target else None,
        "quality": api_payload.get("quality"),
        "background": "transparent" if transparent_postprocess else api_payload.get("background"),
        "output_format": api_payload.get("output_format") or "png",
        "n": api_payload.get("n", 1),
    }
    if args.mode == "edit" and api_payload.get("input_fidelity") is not None:
        used_params["input_fidelity"] = api_payload.get("input_fidelity")

    paths = save_image_bytes(
        data,
        api_payload.get("output_format"),
        transparent_postprocess,
        resize_target,
        resize_mode,
        restore_alpha_source,
    )
    print(json.dumps({"ok": True, "paths": paths, "used_params": used_params}, ensure_ascii=False))


if __name__ == "__main__":
    main()
