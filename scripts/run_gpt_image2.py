#!/usr/bin/env python3
"""Run OpenAI GPT Image API for food/object prompt edits.

This script intentionally contains no local diffusion-model loading.  The main
food workflow uses one input image and a natural-language prompt; mask-based
workflows are kept for backward compatibility.

Workflows:
  prompt-only-edit             image + prompt only, closest to ChatGPT UI upload + prompt
  target-area-edit             image + Target Area mask; edit only selected food/object region(s)
  repair-only                  legacy source mask repairs original defect
  generate-only                legacy target/fixed mask generates new defect(s)
  repair-and-random-generate   legacy stage 1 repair source mask, stage 2 random masks inside target_area
"""
from __future__ import annotations

import argparse
import importlib.util
import base64
import json
import os
import random
import re
import shutil
import sys
import time
from datetime import datetime
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


SUPPORTED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_dotenv_key(root: Path) -> None:
    """Load OPENAI_API_KEY from root/.env when the process environment lacks it."""
    if os.environ.get("OPENAI_API_KEY"):
        return
    env_path = root / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.startswith("OPENAI_API_KEY="):
            key = line.split("=", 1)[1].strip()
            if key:
                os.environ["OPENAI_API_KEY"] = key
            return


def sanitize_defect_type(name: str | None) -> str:
    if name is None:
        return "custom"
    s = str(name).strip().replace(" ", "_")
    s = re.sub(r"[^A-Za-z0-9_.-]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("._-")
    if not s:
        raise ValueError("defect_type cannot be empty")
    return s


def sanitize_name(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name).strip())
    return re.sub(r"_+", "_", s).strip("._-") or "run"


def format_elapsed(seconds: float) -> str:
    """Return elapsed seconds as a compact string like 2m05s."""
    total = max(0, int(round(float(seconds))))
    minutes, secs = divmod(total, 60)
    return f"{minutes}m{secs:02d}s"


def default_chinese_prompt(defect_type: str, size: str = "1280x1280") -> str:
    class_name = sanitize_defect_type(defect_type)
    return (
        f"這張圖的 Class Name 為 {class_name}\n"
        f"請根據 {class_name} 的位置進行食物圖像編輯。\n"
        f"可讓 {class_name} 出現自然的翻轉、旋轉、角度變化、擺放位置微調、份量或姿態差異。\n"
        "不要改動以外的背景、餐具、桌面、光照、相機角度與整體風格。\n"
        "輸出需保持食物可辨識、真實自然，避免變形、融化、重複肢解或不合理食材。"
    )

def load_user_prompt(defect_type: str | None, prompt_file: Path | None, inline_prompt: str | None, size: str) -> dict[str, str]:
    """Load the single user-facing prompt.

    This project intentionally uses one simple Chinese prompt by default, matching
    the ChatGPT UI style: input food image + natural-language prompt.
    Older multi-field prompt.json files are still tolerated for compatibility,
    but new projects should use configs/classes/<class_name>/prompt.txt.
    """
    defect_type = sanitize_defect_type(defect_type)
    if inline_prompt and str(inline_prompt).strip():
        text = str(inline_prompt).strip()
        source = "--prompt"
    else:
        if prompt_file is None:
            txt_path = project_root() / "configs" / "classes" / defect_type / "prompt.txt"
            json_path = project_root() / "configs" / "classes" / defect_type / "prompt.json"
            legacy_txt_path = project_root() / "configs" / "defect_types" / defect_type / "prompt.txt"
            legacy_json_path = project_root() / "configs" / "defect_types" / defect_type / "prompt.json"
            if txt_path.exists():
                prompt_file = txt_path
            elif json_path.exists():
                prompt_file = json_path
            elif legacy_txt_path.exists():
                prompt_file = legacy_txt_path
            elif legacy_json_path.exists():
                prompt_file = legacy_json_path
            else:
                prompt_file = None
        if prompt_file is not None and prompt_file.exists():
            if prompt_file.suffix.lower() == ".json":
                data = json.loads(prompt_file.read_text(encoding="utf-8"))
                text = str(
                    data.get("user_prompt")
                    or data.get("prompt")
                    or ""
                ).strip()
                if not text:
                    text = default_chinese_prompt(defect_type, size)
            else:
                text = prompt_file.read_text(encoding="utf-8").strip()
            source = str(prompt_file)
        else:
            text = default_chinese_prompt(defect_type, size)
            source = "built-in default Chinese prompt"
    text = text.replace("{defect_type}", defect_type).replace("{class_name}", defect_type)
    text = text.replace("{size}", size).replace("1280*1280", size).replace("1280x1280", size)
    return {"defect_type": defect_type, "user_prompt": text, "prompt_source": source}


# Backward-compatible function names used by helper scripts.
def load_prompt_config(defect_type: str | None, prompt_config: Path | None) -> dict[str, str]:
    return load_user_prompt(defect_type, prompt_config, None, "1280x1280")


def build_prompt_bundle(cfg: dict[str, str]) -> dict[str, str]:
    class_name = cfg.get("defect_type", "food")
    user_prompt = cfg["user_prompt"].strip()

    base_note = (
        "\n\n補充要求：請把輸入圖像視為待編輯的食物或物件圖。"
        "請維持背景、餐具、桌面、光照、相機角度與整體風格；最終輸出不可保留框線、文字、箭頭或任何標註。"
    )
    prompt_only = user_prompt + base_note

    target_area_edit = (
        user_prompt
        + "\n\nMask-constrained food/object edit instructions:"
        + "\n- The transparent edit mask marks the only editable Target Area region(s)."
        + "\n- Change the target food/object only inside the mask: natural flip, rotation, repositioning, or plausible appearance variation."
        + "\n- Preserve all pixels outside the mask as close as possible to the input image."
        + "\n- Do not add labels, outlines, marker text, non-food artifacts, or changes outside the mask."
        + "\n- Keep the result photorealistic and visually coherent with the original lighting, camera angle, texture, and scene."
    )

    # Legacy prompts kept for old ROI/defect workflows.
    repair_prompt = (
        user_prompt
        + "\n\nLegacy ROI repair stage: repair the provided source mask naturally while preserving the surrounding background."
    )
    generation_prompt = (
        user_prompt
        + f"\n\nLegacy generation stage: generate a visually plausible `{class_name}` target inside the specified mask / target area and preserve other regions."
    )
    single_pass_relocation = (
        user_prompt
        + "\n\nMask-constrained legacy relocation instructions:"
        + "\n- The transparent edit mask contains editable regions for repair/generation."
        + "\n- Keep every opaque/unmasked pixel as close as possible to the input image."
        + "\n- Do not add labels, outlines, marker text, or changes outside the transparent edit mask."
    )
    return {
        "prompt_only_edit_prompt": prompt_only,
        "target_area_edit_prompt": target_area_edit,
        "repair_prompt": repair_prompt,
        "random_generation_prompt": generation_prompt,
        "single_pass_relocation_prompt": single_pass_relocation,
        "full_workflow_prompt": target_area_edit,
        "user_prompt": user_prompt,
    }

def parse_size(size: str) -> tuple[int, int]:
    s = str(size).strip().lower().replace("*", "x")
    if s in {"auto", "same_as_original", "original", "source"}:
        return (0, 0)
    m = re.fullmatch(r"(\d+)x(\d+)", s)
    if not m:
        raise argparse.ArgumentTypeError("size must be like 1280x1280, 1536x1024, same_as_original, or auto")
    return int(m.group(1)), int(m.group(2))


GPT2_MIN_PIXELS = 655_360
GPT2_MAX_PIXELS = 8_294_400
GPT2_MAX_EDGE = 3840
GPT2_MAX_ASPECT = 3.0


def _round_down_to_multiple(value: int, multiple: int = 16) -> int:
    return max(multiple, int(value) // multiple * multiple)


def _round_up_to_multiple(value: int, multiple: int = 16) -> int:
    return max(multiple, ((int(value) + multiple - 1) // multiple) * multiple)


def normalize_gpt2_api_size(width: int, height: int) -> tuple[int, int]:
    """Return a GPT-image-2 API-valid size preserving aspect as much as possible.

    The final project output may later be resized back to the requested original
    dimensions, but the API request itself must satisfy current GPT-image-2
    size constraints: multiples of 16, long edge <= 3840, aspect <= 3:1, and
    total pixels in the documented valid range.
    """
    w, h = max(1, int(width)), max(1, int(height))
    ratio = max(w, h) / max(1, min(w, h))
    if ratio > GPT2_MAX_ASPECT:
        if w >= h:
            h = int(round(w / GPT2_MAX_ASPECT))
        else:
            w = int(round(h / GPT2_MAX_ASPECT))

    scale = 1.0
    if max(w, h) > GPT2_MAX_EDGE:
        scale = min(scale, GPT2_MAX_EDGE / max(w, h))
    if w * h > GPT2_MAX_PIXELS:
        scale = min(scale, (GPT2_MAX_PIXELS / float(w * h)) ** 0.5)
    if w * h < GPT2_MIN_PIXELS:
        scale = max(scale, (GPT2_MIN_PIXELS / float(w * h)) ** 0.5)

    nw = max(16, int(round(w * scale)))
    nh = max(16, int(round(h * scale)))

    if nw * nh > GPT2_MAX_PIXELS or max(nw, nh) > GPT2_MAX_EDGE:
        nw = _round_down_to_multiple(nw, 16)
        nh = _round_down_to_multiple(nh, 16)
        while (nw * nh > GPT2_MAX_PIXELS or max(nw, nh) > GPT2_MAX_EDGE) and nw > 16 and nh > 16:
            if nw >= nh:
                nw -= 16
                nh = _round_down_to_multiple(max(16, int(round(nw * h / w))), 16)
            else:
                nh -= 16
                nw = _round_down_to_multiple(max(16, int(round(nh * w / h))), 16)
    else:
        nw = _round_down_to_multiple(nw, 16)
        nh = _round_down_to_multiple(nh, 16)

    while nw * nh < GPT2_MIN_PIXELS:
        if nw <= nh:
            nw += 16
        else:
            nh += 16
        if max(nw, nh) > GPT2_MAX_EDGE or nw * nh > GPT2_MAX_PIXELS:
            break

    if max(nw, nh) / max(1, min(nw, nh)) > GPT2_MAX_ASPECT:
        if nw >= nh:
            nh = _round_up_to_multiple(nw / GPT2_MAX_ASPECT, 16)
        else:
            nw = _round_up_to_multiple(nh / GPT2_MAX_ASPECT, 16)
    return int(nw), int(nh)


def validate_output_size(size: str) -> None:
    if str(size).strip().lower() in {"auto", "same_as_original", "original", "source"}:
        return
    w, h = parse_size(size)
    if w <= 0 or h <= 0:
        raise ValueError("gpt-image-2 output size width/height must be positive.")
    if w % 16 != 0 or h % 16 != 0:
        raise ValueError(f"Invalid size '{size}': width and height must be multiples of 16 for gpt-image-2.")
    if max(w, h) > GPT2_MAX_EDGE:
        raise ValueError(f"Invalid size '{size}': longest edge must be less than or equal to {GPT2_MAX_EDGE}.")
    if max(w, h) / max(1, min(w, h)) > GPT2_MAX_ASPECT:
        raise ValueError(f"Invalid size '{size}': aspect ratio must be less than or equal to 3:1.")
    if not (GPT2_MIN_PIXELS <= w * h <= GPT2_MAX_PIXELS):
        raise ValueError(f"Invalid size '{size}': total pixels must be between {GPT2_MIN_PIXELS} and {GPT2_MAX_PIXELS}.")


def coerce_gpt2_size_for_api(args: argparse.Namespace) -> None:
    """Make the API request size valid, while preserving final output intent.

    Older UI/batch versions sometimes expanded the semantic mode
    ``same_as_original`` into a literal image size such as 5472x3648 before
    calling this script. GPT-image-2 cannot receive that oversized value
    directly, so when the requested size equals the source image size we treat it
    as the original-size workflow: request the largest valid API size, then
    resize the saved final image back to the source dimensions.

    If the user explicitly requested an invalid custom size that does not match
    the source image, fail with a machine-readable marker so the UI can explain
    that the UI model-parameter step needs correction.
    """
    if str(args.model).strip().lower() != "gpt-image-2":
        return
    requested = str(args.size).strip().lower()
    if requested in {"auto", "same_as_original", "original", "source"}:
        return
    try:
        validate_output_size(args.size)
        return
    except Exception as exc:
        invalid_reason = str(exc)

    try:
        req_w, req_h = parse_size(args.size)
        with Image.open(args.image) as src_img:
            src_w, src_h = int(src_img.width), int(src_img.height)
    except Exception:
        raise SystemExit(
            "[USER_ERROR][SIZE] " + invalid_reason +
            "\n請回到 Step 4 修改輸出尺寸；GPT-image-2 需要使用 API 支援的尺寸。"
        )

    if (req_w, req_h) == (src_w, src_h):
        if args.final_width is None:
            args.final_width = src_w
        if args.final_height is None:
            args.final_height = src_h
        api_w, api_h = normalize_gpt2_api_size(src_w, src_h)
        args.size = f"{api_w}x{api_h}"
        args.width = api_w
        args.height = api_h
        print(
            f"[WARN][SIZE] requested original output {src_w}x{src_h} is not API-valid ({invalid_reason}); "
            f"requesting {api_w}x{api_h} from GPT-image-2 and resizing final saved output back to {args.final_width}x{args.final_height}.",
            flush=True,
        )
        validate_output_size(args.size)
        return

    raise SystemExit(
        "[USER_ERROR][SIZE] " + invalid_reason +
        "\n請回到 Step 4 修改輸出尺寸後重新執行生成。GPT-image-2 尺寸限制包含：寬高需為 16 的倍數、長邊不可超過 3840、長寬比不可超過 3:1，且總像素需落在支援範圍。"
    )


def round_to_multiple(value: int, multiple: int) -> int:
    if multiple <= 1:
        return int(value)
    return max(multiple, int(round(value / multiple) * multiple))


def _load_binary_mask(
    mask_path: Path,
    size: tuple[int, int],
    mask_threshold: int = 127,
    invert: bool = False,
    label: str = "mask",
) -> Image.Image:
    if not mask_path.exists():
        raise FileNotFoundError(f"{label} not found: {mask_path}")
    mask = Image.open(mask_path).convert("L")
    if mask.size != size:
        print(f"[WARN] {label} size {mask.size} != image size {size}; resizing with NEAREST.")
        mask = mask.resize(size, Image.Resampling.NEAREST)
    mask = mask.point(lambda v: 255 if v > mask_threshold else 0)
    if invert:
        mask = Image.eval(mask, lambda v: 255 - v)
    return mask


def prepare_image_and_masks(
    image_path: Path,
    repair_mask_path: Path | None = None,
    target_area_path: Path | None = None,
    prototype_mask_path: Path | None = None,
    invert_mask: bool = False,
    mask_threshold: int = 127,
    mask_dilate: int = 0,
    auto_resize_multiple: int = 16,
    width: int | None = None,
    height: int | None = None,
) -> tuple[Image.Image, Image.Image | None, Image.Image | None, Image.Image | None, dict[str, Any]]:
    if not image_path.exists():
        raise FileNotFoundError(f"Image not found: {image_path}")
    image = Image.open(image_path).convert("RGB")
    repair_mask = None
    prototype_mask = None
    target_area = None

    if repair_mask_path is not None:
        repair_mask = _load_binary_mask(repair_mask_path, image.size, mask_threshold, invert_mask, "Repair/source mask")
        prototype_mask = repair_mask.copy()
    if prototype_mask_path is not None:
        prototype_mask = _load_binary_mask(prototype_mask_path, image.size, mask_threshold, invert_mask, "Prototype mask")
    if target_area_path is not None:
        target_area = _load_binary_mask(target_area_path, image.size, mask_threshold, False, "Target-area mask")

    if mask_dilate > 0:
        k = int(mask_dilate) * 2 + 1
        if repair_mask is not None:
            repair_mask = repair_mask.filter(ImageFilter.MaxFilter(k)).point(lambda v: 255 if v > 0 else 0)
        if prototype_mask is not None:
            prototype_mask = prototype_mask.filter(ImageFilter.MaxFilter(k)).point(lambda v: 255 if v > 0 else 0)

    target_w = int(width) if width else image.width
    target_h = int(height) if height else image.height
    if auto_resize_multiple and auto_resize_multiple > 1:
        target_w = round_to_multiple(target_w, auto_resize_multiple)
        target_h = round_to_multiple(target_h, auto_resize_multiple)

    resized = False
    if (target_w, target_h) != image.size:
        image = image.resize((target_w, target_h), Image.Resampling.LANCZOS)
        for attr_name in []:
            pass
        if repair_mask is not None:
            repair_mask = repair_mask.resize((target_w, target_h), Image.Resampling.NEAREST).point(lambda v: 255 if v > 127 else 0)
        if prototype_mask is not None:
            prototype_mask = prototype_mask.resize((target_w, target_h), Image.Resampling.NEAREST).point(lambda v: 255 if v > 127 else 0)
        if target_area is not None:
            target_area = target_area.resize((target_w, target_h), Image.Resampling.NEAREST).point(lambda v: 255 if v > 127 else 0)
        resized = True

    if repair_mask_path is not None and (repair_mask is None or repair_mask.getbbox() is None):
        raise ValueError("Repair/source mask is empty. White pixels are required as the original defect area.")
    if prototype_mask_path is not None and (prototype_mask is None or prototype_mask.getbbox() is None):
        raise ValueError("Prototype mask is empty. White pixels are required for random new defect placement.")

    meta = {
        "source_image": str(image_path),
        "repair_mask": str(repair_mask_path) if repair_mask_path else None,
        "prototype_mask": str(prototype_mask_path) if prototype_mask_path else (str(repair_mask_path) if repair_mask_path else None),
        "target_area_mask": str(target_area_path) if target_area_path else None,
        "target_area_empty": bool(target_area is not None and target_area.getbbox() is None),
        "final_width": image.width,
        "final_height": image.height,
        "resized": resized,
        "repair_mask_bbox_xyxy": list(map(int, repair_mask.getbbox())) if repair_mask is not None and repair_mask.getbbox() else None,
        "prototype_mask_bbox_xyxy": list(map(int, prototype_mask.getbbox())) if prototype_mask is not None and prototype_mask.getbbox() else None,
        "target_area_bbox_xyxy": list(map(int, target_area.getbbox())) if target_area is not None and target_area.getbbox() else None,
    }
    return image, repair_mask, prototype_mask, target_area, meta


def binarize(mask: Image.Image) -> Image.Image:
    return mask.convert("L").point(lambda v: 255 if v > 127 else 0)


def clip_mask_to_target(mask: Image.Image, target_area: Image.Image | None) -> Image.Image:
    if target_area is None or target_area.getbbox() is None:
        return binarize(mask)
    clipped = ImageChops.multiply(mask.convert("L"), target_area.convert("L")).point(lambda v: 255 if v > 0 else 0)
    if clipped.getbbox() is None:
        raise ValueError("The edit mask becomes empty after clipping to target_area. Enlarge target_area or move the mask inside it.")
    return clipped


def subtract_mask(base: Image.Image | None, remove: Image.Image | None, padding: int = 0) -> Image.Image:
    if base is None or base.getbbox() is None:
        raise ValueError("A non-empty target_area mask is required for random placement.")
    base_bin = binarize(base)
    if remove is None or remove.getbbox() is None:
        return base_bin
    remove_bin = binarize(remove)
    if padding > 0:
        k = int(padding) * 2 + 1
        remove_bin = remove_bin.filter(ImageFilter.MaxFilter(k)).point(lambda v: 255 if v > 0 else 0)
    inv = Image.eval(remove_bin, lambda v: 255 - v)
    out = ImageChops.multiply(base_bin, inv).point(lambda v: 255 if v > 0 else 0)
    if out.getbbox() is None:
        raise ValueError("Allowed target_area became empty after excluding the original repair mask.")
    return out


def _irregular_candidate_from_rect(base_mask: Image.Image, rng: random.Random, defect_type: str = "") -> Image.Image:
    """Turn a solid rectangular ROI prototype into a defect-shaped edit mask.

    Users draw ROI with rectangles, so the raw prototype mask is often a filled
    rectangle.  If that rectangle is reused as the generation mask, the API tends
    to produce rectangular gray patches.  This helper keeps the approximate size
    of the ROI but changes the random-generation mask into a small irregular
    island that is more appropriate for defect relocation.
    """
    m = binarize(base_mask)
    w, h = m.size
    if w <= 2 or h <= 2:
        return m
    arr = np.array(m) > 0
    fill_ratio = float(arr.sum()) / float(max(1, w * h))
    # If the source mask is already irregular/polygon-like, preserve it.
    if fill_ratio < 0.72:
        return m

    kind = str(defect_type or "").lower()
    out = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(out)
    pad_x = max(1, int(w * 0.08))
    pad_y = max(1, int(h * 0.08))

    if "stripe" in kind:
        # Thin line-like island across the ROI bbox.
        y = h // 2 + rng.randint(-max(1, h // 8), max(1, h // 8))
        thickness = max(2, min(h, int(max(2, h * rng.uniform(0.08, 0.18)))))
        pts = [(pad_x, y - thickness // 2), (w - pad_x, y - thickness // 2 + rng.randint(-2, 2)), (w - pad_x, y + thickness // 2), (pad_x, y + thickness // 2 + rng.randint(-2, 2))]
        draw.polygon(pts, fill=255)
    elif "spot" in kind or "black" in kind or "white_spot" in kind:
        # Compact speck with slightly irregular edge.
        cx, cy = w / 2.0, h / 2.0
        rx = max(2.0, (w / 2.0 - pad_x) * rng.uniform(0.65, 0.95))
        ry = max(2.0, (h / 2.0 - pad_y) * rng.uniform(0.65, 0.95))
        pts = []
        n = rng.randint(8, 14)
        for i in range(n):
            a = 2.0 * np.pi * i / n
            rj = rng.uniform(0.72, 1.12)
            pts.append((int(cx + np.cos(a) * rx * rj), int(cy + np.sin(a) * ry * rj)))
        draw.polygon(pts, fill=255)
    else:
        # Default stain/mark: soft cloudy irregular island, not a rectangle.
        cx, cy = w / 2.0, h / 2.0
        rx = max(3.0, (w / 2.0 - pad_x) * rng.uniform(0.70, 1.00))
        ry = max(3.0, (h / 2.0 - pad_y) * rng.uniform(0.55, 0.95))
        pts = []
        n = rng.randint(10, 18)
        for i in range(n):
            a = 2.0 * np.pi * i / n
            rj = rng.uniform(0.55, 1.10)
            pts.append((int(cx + np.cos(a) * rx * rj), int(cy + np.sin(a) * ry * rj)))
        draw.polygon(pts, fill=255)
        if min(w, h) >= 12:
            out = out.filter(ImageFilter.GaussianBlur(radius=max(0.4, min(w, h) * 0.025))).point(lambda v: 255 if v > 50 else 0)

    # Constrain to the original prototype bbox/mask so the size remains comparable.
    out = ImageChops.multiply(out, m).point(lambda v: 255 if v > 0 else 0)
    return out if out.getbbox() is not None else m


def build_random_mask_in_target(
    prototype_mask: Image.Image,
    target_area: Image.Image | None,
    seed: int,
    min_defects: int = 1,
    max_defects: int = 3,
    scale_min: float = 0.85,
    scale_max: float = 1.15,
    placement_attempts: int = 500,
    avoid_overlap: bool = True,
    defect_type: str = "",
) -> tuple[Image.Image, dict[str, Any]]:
    if target_area is None or target_area.getbbox() is None:
        raise ValueError("Random placement requires a non-empty --target-area mask.")
    rng = random.Random(int(seed))
    min_defects = max(1, int(min_defects))
    max_defects = max(min_defects, int(max_defects))
    requested = rng.randint(min_defects, max_defects)

    proto = binarize(prototype_mask)
    bbox = proto.getbbox()
    if bbox is None:
        raise ValueError("Prototype/source mask is empty; cannot create random placements.")
    proto_crop = proto.crop(bbox)
    W, H = target_area.size
    target_np = np.array(target_area.convert("L")) > 0
    out_np = np.zeros((H, W), dtype=np.uint8)
    placements: list[dict[str, Any]] = []

    for defect_idx in range(requested):
        placed = False
        last_reason = ""
        for _ in range(max(1, int(placement_attempts))):
            scale = rng.uniform(float(scale_min), float(scale_max))
            pw, ph = proto_crop.size
            sw = max(1, int(round(pw * scale)))
            sh = max(1, int(round(ph * scale)))
            if sw >= W or sh >= H:
                last_reason = f"scaled prototype {sw}x{sh} is larger than image {W}x{H}"
                continue
            cand = proto_crop.resize((sw, sh), Image.Resampling.NEAREST).point(lambda v: 255 if v > 127 else 0)
            cand = _irregular_candidate_from_rect(cand, rng, defect_type)
            cand_np = np.array(cand) > 0
            if not cand_np.any():
                last_reason = "scaled prototype became empty"
                continue
            x = rng.randint(0, W - sw)
            y = rng.randint(0, H - sh)
            target_crop = target_np[y:y + sh, x:x + sw]
            if not np.all(target_crop[cand_np]):
                last_reason = "candidate mask would exceed target_area"
                continue
            if avoid_overlap:
                existing_crop = out_np[y:y + sh, x:x + sw] > 0
                if np.any(existing_crop[cand_np]):
                    last_reason = "candidate overlaps an existing random defect mask"
                    continue
            out_crop = out_np[y:y + sh, x:x + sw]
            out_crop[cand_np] = 255
            placements.append({"index": defect_idx + 1, "x": int(x), "y": int(y), "width": int(sw), "height": int(sh), "scale": float(round(scale, 4))})
            placed = True
            break
        if not placed:
            raise ValueError(f"Could not place requested defect {defect_idx + 1}/{requested}. Last reason: {last_reason}")

    out_mask = Image.fromarray(out_np, "L")
    meta = {
        "mode": "random-in-target",
        "seed": int(seed),
        "seed_note": "Seed controls only local random mask placement; OpenAI image generation itself does not expose a seed parameter.",
        "requested_defects": requested,
        "placed_defects": len(placements),
        "scale_min": float(scale_min),
        "scale_max": float(scale_max),
        "placements": placements,
        "mask_shape_policy": "solid rectangular ROI prototypes are converted to irregular defect-shaped islands for generation",
        "bbox_xyxy": list(map(int, out_mask.getbbox())) if out_mask.getbbox() else None,
    }
    return out_mask, meta


def make_mask_preview(image: Image.Image, mask: Image.Image, alpha: int = 110, color: tuple[int, int, int] = (255, 0, 0)) -> Image.Image:
    base = image.convert("RGBA")
    overlay = Image.new("RGBA", image.size, (*color, 0))
    overlay.putalpha(mask.point(lambda v: alpha if v > 0 else 0))
    return Image.alpha_composite(base, overlay).convert("RGB")


def composite_keep_unmasked(original: Image.Image, generated: Image.Image, mask: Image.Image, feather: int) -> Image.Image:
    if generated.size != original.size:
        generated = generated.resize(original.size, Image.Resampling.LANCZOS)
    blend_mask = mask.convert("L")
    if feather > 0:
        blend_mask = blend_mask.filter(ImageFilter.GaussianBlur(radius=float(feather)))
    return Image.composite(generated.convert("RGB"), original.convert("RGB"), blend_mask)


def make_openai_edit_mask(binary_mask: Image.Image) -> Image.Image:
    """Convert white-edit binary mask to OpenAI alpha mask.

    OpenAI Image Edit mask convention: fully transparent areas indicate where the
    first input image should be edited. Therefore white pixels in the project
    mask become alpha=0, and all other pixels become alpha=255.
    """
    m = binarize(binary_mask)
    arr = np.array(m)
    alpha = np.where(arr > 0, 0, 255).astype(np.uint8)
    rgba = np.zeros((arr.shape[0], arr.shape[1], 4), dtype=np.uint8)
    rgba[:, :, 0:3] = 255
    rgba[:, :, 3] = alpha
    return Image.fromarray(rgba, "RGBA")


def save_png_for_api(image: Image.Image, path: Path) -> None:
    ensure_dir(path.parent)
    image.save(path, format="PNG")


def open_result_image_over_source(raw: bytes, source_image: Image.Image) -> Image.Image:
    """Decode an API image and prevent transparent pixels from becoming black.

    Some image-edit responses may contain alpha when background=auto. A plain
    ``convert("RGB")`` composites transparent pixels over black, which looks like
    the original ROI became a black patch.  Composite transparent output over the
    current source image instead, so unedited/transparent areas remain visually
    consistent.
    """
    from io import BytesIO
    decoded = Image.open(BytesIO(raw))
    has_alpha = decoded.mode in {"RGBA", "LA"} or (decoded.mode == "P" and "transparency" in decoded.info)
    if has_alpha:
        rgba = decoded.convert("RGBA")
        base = source_image.convert("RGBA")
        if base.size != rgba.size:
            base = base.resize(rgba.size, Image.Resampling.LANCZOS)
        return Image.alpha_composite(base, rgba).convert("RGB")
    return decoded.convert("RGB")


def _union_bbox_from_masks(*masks: Image.Image | None) -> tuple[int, int, int, int] | None:
    boxes = []
    for m in masks:
        if m is None:
            continue
        b = binarize(m).getbbox()
        if b is not None:
            boxes.append(tuple(map(int, b)))
    if not boxes:
        return None
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def _aspect_crop_for_edit_bbox(
    image_size: tuple[int, int],
    bbox: tuple[int, int, int, int] | None,
    out_w: int,
    out_h: int,
    margin_ratio: float = 0.10,
) -> tuple[int, int, int, int]:
    """Return a crop rectangle that preserves output aspect ratio and centers edit areas.

    Large originals such as 5472x3648 edited into 1280x1280 should not be sent as
    the entire source image while the prompt contains high-resolution ROI coords.
    A focused crop around ROI + Target Area preserves texture/detail and makes the
    model see a canvas closer to the final output size.
    """
    W, H = map(int, image_size)
    target_aspect = max(1e-6, float(out_w) / float(out_h))
    if bbox is None:
        # Center crop to requested aspect if no mask information exists.
        if W / H >= target_aspect:
            ch = H
            cw = int(round(ch * target_aspect))
        else:
            cw = W
            ch = int(round(cw / target_aspect))
        left = max(0, (W - cw) // 2)
        top = max(0, (H - ch) // 2)
        return (left, top, min(W, left + cw), min(H, top + ch))

    x1, y1, x2, y2 = map(int, bbox)
    bw = max(1, x2 - x1)
    bh = max(1, y2 - y1)
    mx = int(round(bw * margin_ratio))
    my = int(round(bh * margin_ratio))
    x1, y1, x2, y2 = max(0, x1 - mx), max(0, y1 - my), min(W, x2 + mx), min(H, y2 + my)
    bw = max(1, x2 - x1)
    bh = max(1, y2 - y1)
    cx = (x1 + x2) / 2.0
    cy = (y1 + y2) / 2.0

    crop_w, crop_h = float(bw), float(bh)
    if crop_w / crop_h < target_aspect:
        crop_w = crop_h * target_aspect
    else:
        crop_h = crop_w / target_aspect
    crop_w = min(float(W), max(crop_w, 1.0))
    crop_h = min(float(H), max(crop_h, 1.0))
    # If clamping one dimension broke the aspect, adjust the other dimension.
    if crop_w / crop_h < target_aspect:
        crop_h = min(float(H), crop_w / target_aspect)
    elif crop_w / crop_h > target_aspect:
        crop_w = min(float(W), crop_h * target_aspect)

    left = int(round(cx - crop_w / 2.0))
    top = int(round(cy - crop_h / 2.0))
    left = max(0, min(left, W - int(round(crop_w))))
    top = max(0, min(top, H - int(round(crop_h))))
    right = min(W, left + int(round(crop_w)))
    bottom = min(H, top + int(round(crop_h)))
    return (left, top, right, bottom)


def _transform_coord_prompt_text(text: str, crop_box: tuple[int, int, int, int], out_w: int, out_h: int) -> str:
    import re
    left, top, right, bottom = crop_box
    cw = max(1, right - left)
    ch = max(1, bottom - top)
    sx = float(out_w) / float(cw)
    sy = float(out_h) / float(ch)

    def repl(match: re.Match) -> str:
        key = match.group(1)
        sep = match.group(2)
        value = int(float(match.group(3)))
        if key.lower().startswith('x'):
            new_v = int(round((value - left) * sx))
            new_v = max(0, min(int(out_w), new_v))
        else:
            new_v = int(round((value - top) * sy))
            new_v = max(0, min(int(out_h), new_v))
        return f"{key}{sep}{new_v}"

    return re.sub(r"\b([xy]\d*)\s*([：:])\s*(-?\d+(?:\.\d+)?)", repl, text)


def _prepare_prompt_only_canvas_if_needed(
    image: Image.Image,
    repair_mask: Image.Image | None,
    target_area: Image.Image | None,
    bundle: dict[str, str],
    out_w: int,
    out_h: int,
) -> tuple[Image.Image, dict[str, str], dict[str, Any]]:
    """Create an output-sized prompt-only source canvas and scaled prompt coords.

    This is only for prompt-only-edit.  It keeps the strong GPTUI47 behavior, but
    fixes the large-original -> smaller-output case by making the image canvas and
    coordinate text use the same coordinate system.
    """
    meta: dict[str, Any] = {"enabled": False}
    if out_w <= 0 or out_h <= 0 or image.size == (int(out_w), int(out_h)):
        return image, bundle, meta
    src_w, src_h = image.size
    # Only normalize when the source is substantially larger/different than the
    # requested output.  Small equal-size crops keep their exact pixels.
    if max(src_w / max(1, out_w), src_h / max(1, out_h)) < 1.20 and abs((src_w/src_h) - (out_w/out_h)) < 0.02:
        return image, bundle, meta

    bbox = _union_bbox_from_masks(repair_mask, target_area)
    crop_box = _aspect_crop_for_edit_bbox(image.size, bbox, out_w, out_h)
    api_image = image.crop(crop_box).resize((int(out_w), int(out_h)), Image.Resampling.LANCZOS)
    transformed = dict(bundle)
    for key in [
        "prompt_only_edit_prompt",
        "full_workflow_prompt",
        "user_prompt",
        "repair_prompt",
        "random_generation_prompt",
        "single_pass_relocation_prompt",
    ]:
        if key in transformed and isinstance(transformed[key], str):
            transformed[key] = _transform_coord_prompt_text(transformed[key], crop_box, int(out_w), int(out_h))
    note = (
        f"\n\n[Coordinate normalization] The image has been cropped/resized for API editing from "
        f"source {src_w}x{src_h} to output canvas {out_w}x{out_h}. "
        f"Use the ROI and Target Area coordinates in this normalized {out_w}x{out_h} coordinate system. "
        "Keep all unedited background/black regions and surface texture as close as possible to the input canvas; "
        "only the ROI defect and target-area defect placement should change."
    )
    transformed["prompt_only_edit_prompt"] = transformed.get("prompt_only_edit_prompt", "") + note
    meta = {
        "enabled": True,
        "reason": "large source image normalized to requested prompt-only output canvas so visual detail and coordinate text stay aligned",
        "source_size": [int(src_w), int(src_h)],
        "api_canvas_size": [int(out_w), int(out_h)],
        "crop_box_xyxy": list(map(int, crop_box)),
        "edit_union_bbox_xyxy": list(map(int, bbox)) if bbox else None,
    }
    return api_image, transformed, meta



def _mask_to_prompt_only_canvas(mask: Image.Image | None, canvas_size: tuple[int, int], norm_meta: dict[str, Any]) -> Image.Image | None:
    """Convert an original-size ROI/Target mask into the prompt-only API canvas space."""
    if mask is None:
        return None
    m = binarize(mask)
    if norm_meta.get("enabled") and norm_meta.get("crop_box_xyxy"):
        left, top, right, bottom = [int(v) for v in norm_meta["crop_box_xyxy"]]
        m = m.crop((left, top, right, bottom)).resize(canvas_size, Image.Resampling.NEAREST)
    elif m.size != canvas_size:
        m = m.resize(canvas_size, Image.Resampling.NEAREST)
    return binarize(m)


def _prompt_only_preserve_source_composite(
    source_canvas: Image.Image,
    generated: Image.Image,
    roi_mask_canvas: Image.Image | None,
    target_mask_canvas: Image.Image | None,
    run_dir: Path | None = None,
    seed: int | None = None,
) -> tuple[Image.Image, dict[str, Any]]:
    """Preserve original texture/background for large-image prompt-only edits.

    For large originals normalized into 1280x1280, the edit model may slightly
    re-render the whole surface.  The successful GPTUI47 behavior was much more
    image-preserving.  This post-process keeps the generated pixels only inside
    likely edit zones: the ROI repair area and visually changed pixels inside
    the Target Area.  It prevents global darkening / texture drift while keeping
    newly generated defects.
    """
    src = source_canvas.convert("RGB")
    gen = generated.convert("RGB")
    if gen.size != src.size:
        gen = gen.resize(src.size, Image.Resampling.LANCZOS)

    roi = binarize(roi_mask_canvas) if roi_mask_canvas is not None else None
    tgt = binarize(target_mask_canvas) if target_mask_canvas is not None else None
    if roi is None and tgt is None:
        return gen, {"enabled": False, "reason": "no ROI/Target masks available"}

    # Difference mask catches actual generated changes, avoiding replacing the
    # whole target polygon with a re-rendered surface.
    diff = ImageChops.difference(src, gen).convert("L")
    diff_np = np.array(diff, dtype=np.uint8)
    # Adaptive threshold: low enough for faint stains, high enough to suppress
    # global color/brightness drift.
    nonzero = diff_np[diff_np > 0]
    if nonzero.size:
        adaptive = int(max(5, min(18, np.percentile(nonzero, 70))))
    else:
        adaptive = 8
    change = diff.point(lambda v: 255 if v >= adaptive else 0)
    # Remove isolated single-pixel noise, then expand slightly to cover soft edges.
    change = change.filter(ImageFilter.MedianFilter(size=3)).filter(ImageFilter.MaxFilter(size=5))

    final_mask = Image.new("L", src.size, 0)
    if tgt is not None and tgt.getbbox() is not None:
        target_changes = ImageChops.multiply(change, tgt)
        final_mask = ImageChops.lighter(final_mask, target_changes)
    if roi is not None and roi.getbbox() is not None:
        # Always allow ROI repair to come from generated output, but feather it so
        # the boundary does not look rectangular.
        roi_feather = roi.filter(ImageFilter.GaussianBlur(radius=2.0)).point(lambda v: 255 if v > 24 else 0)
        final_mask = ImageChops.lighter(final_mask, roi_feather)

    if final_mask.getbbox() is None:
        return gen, {
            "enabled": False,
            "reason": "change mask was empty; returning model output",
            "adaptive_diff_threshold": adaptive,
        }

    soft = final_mask.filter(ImageFilter.GaussianBlur(radius=1.5))
    out = Image.composite(gen, src, soft).convert("RGB")
    meta = {
        "enabled": True,
        "policy": "source-preserving composite for normalized large-input prompt-only edits",
        "adaptive_diff_threshold": adaptive,
        "mask_bbox_xyxy": list(map(int, final_mask.getbbox())) if final_mask.getbbox() else None,
    }
    if run_dir is not None and seed is not None:
        try:
            final_mask.save(run_dir / f"prompt_only_preserve_source_mask_seed{seed}.png")
            ImageChops.difference(src, gen).save(run_dir / f"prompt_only_difference_seed{seed}.png")
        except Exception:
            pass
    return out, meta


SUPPORTED_GPT_IMAGE_MODELS = [
    "gpt-image-2",
    "gpt-image-1.5",
    "gpt-image-1",
    "gpt-image-1-mini",
]

# Default public pricing snapshot for GPT Image 2 token cost estimation.
# Keep this editable: the OpenAI pricing page is the source of truth if rates change.
GPT_IMAGE2_PRICING_PER_1M = {
    "text_input_tokens": 5.00,
    "text_cached_input_tokens": 1.25,
    "image_input_tokens": 8.00,
    "image_cached_input_tokens": 2.00,
    "image_output_tokens": 30.00,
}


def to_plain_dict(obj: Any) -> dict[str, Any]:
    """Convert OpenAI SDK response objects into a plain JSON-serializable dict."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    try:
        return json.loads(obj.model_dump_json())
    except Exception:
        pass
    try:
        return dict(obj)
    except Exception:
        return {"repr": repr(obj)}


def scrub_openai_response_for_metadata(response_dict: dict[str, Any]) -> dict[str, Any]:
    """Remove base64 image payloads before writing response metadata."""
    data = response_dict.get("data")
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and "b64_json" in item:
                item["b64_json"] = "<omitted: image base64>"
    return response_dict


def _get_nested(d: dict[str, Any], *keys: str) -> Any:
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def extract_usage_breakdown(response_dict: dict[str, Any]) -> dict[str, Any]:
    """Extract token usage from OpenAI image response if the API returned it.

    The image response usage schema may vary by model/API version, so this
    intentionally accepts multiple possible key names and records both detailed
    values and whether the cost is exact or estimated.
    """
    usage = response_dict.get("usage")
    if not isinstance(usage, dict):
        return {
            "usage_returned": False,
            "note": "The API response did not include usage details. Check OpenAI Platform → Usage/Costs for authoritative billing.",
        }

    input_details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    output_details = usage.get("output_tokens_details") or usage.get("completion_tokens_details") or {}
    if not isinstance(input_details, dict):
        input_details = {}
    if not isinstance(output_details, dict):
        output_details = {}

    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
    total_tokens = usage.get("total_tokens")

    # Known detailed fields. Names differ slightly across SDK/API surfaces.
    text_input = (
        input_details.get("text_tokens")
        or input_details.get("text_input_tokens")
        or input_details.get("prompt_text_tokens")
        or 0
    )
    image_input = (
        input_details.get("image_tokens")
        or input_details.get("image_input_tokens")
        or input_details.get("input_image_tokens")
        or 0
    )
    cached_total = input_details.get("cached_tokens") or usage.get("cached_tokens") or 0
    cached_text = input_details.get("cached_text_tokens") or input_details.get("text_cached_tokens") or 0
    cached_image = input_details.get("cached_image_tokens") or input_details.get("image_cached_tokens") or 0

    image_output = (
        output_details.get("image_tokens")
        or output_details.get("image_output_tokens")
        or output_details.get("output_image_tokens")
        or 0
    )
    text_output = (
        output_details.get("text_tokens")
        or output_details.get("text_output_tokens")
        or 0
    )

    # If the response only reports aggregate output tokens for an image edit,
    # treat output_tokens as image output tokens for a practical estimate.
    if not image_output and output_tokens:
        image_output = output_tokens

    detailed_input_known = bool(text_input or image_input or cached_text or cached_image)
    if not detailed_input_known and input_tokens:
        # GPT Image edits always include image input plus text prompt. Without a
        # detailed split, we cannot know the exact text/image ratio. Use image
        # input pricing as a conservative estimate and mark it as estimated.
        image_input = input_tokens

    if total_tokens is None:
        try:
            total_tokens = int(input_tokens or 0) + int(output_tokens or 0)
        except Exception:
            total_tokens = None

    cost_items = {
        "text_input_tokens": int(text_input or 0),
        "text_cached_input_tokens": int(cached_text or 0),
        "image_input_tokens": int(image_input or 0),
        "image_cached_input_tokens": int(cached_image or 0),
        "image_output_tokens": int(image_output or 0),
    }

    estimated_cost = 0.0
    for k, tokens in cost_items.items():
        estimated_cost += (tokens / 1_000_000.0) * GPT_IMAGE2_PRICING_PER_1M[k]

    estimate_quality = "exact_if_api_token_split_is_exact"
    if not detailed_input_known and input_tokens:
        estimate_quality = "estimated_input_split_not_returned_by_api"
    if cached_total and not (cached_text or cached_image):
        estimate_quality = "partial_estimate_cached_split_not_returned_by_api"

    return {
        "usage_returned": True,
        "raw_usage": usage,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "input_token_details": input_details,
        "output_token_details": output_details,
        "cost_token_items": cost_items,
        "pricing_per_1m_usd": GPT_IMAGE2_PRICING_PER_1M,
        "estimated_cost_usd": round(estimated_cost, 8),
        "estimate_quality": estimate_quality,
        "note": "Cost is calculated from response usage when available. OpenAI Platform Usage/Costs remains the authoritative billing record.",
    }


def print_usage_report(stage_name: str, usage_breakdown: dict[str, Any]) -> None:
    print(f"[USAGE] stage={stage_name}")
    if not usage_breakdown.get("usage_returned"):
        print("[USAGE] token details: not returned by this API response")
        print("[USAGE] cost estimate: unavailable from response; check OpenAI Platform Usage/Costs")
        return

    items = usage_breakdown.get("cost_token_items", {})
    print(f"[USAGE] text_input_tokens={items.get('text_input_tokens', 0)}")
    print(f"[USAGE] text_cached_input_tokens={items.get('text_cached_input_tokens', 0)}")
    print(f"[USAGE] image_input_tokens={items.get('image_input_tokens', 0)}")
    print(f"[USAGE] image_cached_input_tokens={items.get('image_cached_input_tokens', 0)}")
    print(f"[USAGE] image_output_tokens={items.get('image_output_tokens', 0)}")
    print(f"[USAGE] input_tokens={usage_breakdown.get('input_tokens')}")
    print(f"[USAGE] output_tokens={usage_breakdown.get('output_tokens')}")
    print(f"[USAGE] total_tokens={usage_breakdown.get('total_tokens')}")
    print(f"[COST] estimated_cost_usd={usage_breakdown.get('estimated_cost_usd')} ({usage_breakdown.get('estimate_quality')})")


def _call_openai_image_edit(
    client,
    image: Image.Image,
    prompt: str,
    args,
    stage_dir: Path,
    stage_name: str,
    edit_mask: Image.Image | None = None,
    reference_images: list[Image.Image] | None = None,
) -> tuple[Image.Image, dict[str, Any]]:
    image_path = stage_dir / f"_api_input_{stage_name}.png"
    save_png_for_api(image.convert("RGB"), image_path)

    reference_paths: list[Path] = []
    for idx, ref in enumerate(reference_images or [], start=1):
        ref_path = stage_dir / f"_api_reference_{stage_name}_{idx}.png"
        ref_img = ref.convert("RGB")
        if ref_img.size != image.size:
            ref_img = ref_img.resize(image.size, Image.Resampling.LANCZOS)
        save_png_for_api(ref_img, ref_path)
        reference_paths.append(ref_path)

    mask_path = None
    if edit_mask is not None:
        mask_path = stage_dir / f"_api_mask_{stage_name}.png"
        save_png_for_api(make_openai_edit_mask(edit_mask), mask_path)

    call_kwargs: dict[str, Any] = {
        "model": args.model,
        "prompt": prompt,
        "size": args.size,
        "quality": args.quality,
        "n": 1,
        "output_format": args.output_format,
        "background": args.background,
    }
    if args.output_format in {"jpeg", "webp"} and args.output_compression is not None:
        call_kwargs["output_compression"] = int(args.output_compression)

    print(f"[TIME] stage={stage_name} started")
    if reference_paths:
        print(f"[INFO] stage={stage_name} using {len(reference_paths)} reference image(s) for original defect appearance")
    stage_start = time.perf_counter()
    with ExitStack() as stack:
        image_file = stack.enter_context(open(image_path, "rb"))
        image_arg = image_file
        ref_files = []
        for ref_path in reference_paths:
            ref_files.append(stack.enter_context(open(ref_path, "rb")))
        if ref_files:
            # GPT image edit accepts multiple input images; the mask applies to
            # the first image, while later images serve as visual references.
            image_arg = [image_file, *ref_files]
        if mask_path is not None:
            mask_file = stack.enter_context(open(mask_path, "rb"))
            result = client.images.edit(image=image_arg, mask=mask_file, **call_kwargs)
        else:
            result = client.images.edit(image=image_arg, **call_kwargs)
    api_elapsed_seconds = time.perf_counter() - stage_start
    api_elapsed_text = format_elapsed(api_elapsed_seconds)
    print(f"[TIME] stage={stage_name} api_elapsed={api_elapsed_text} ({api_elapsed_seconds:.2f}s)")

    response_dict = scrub_openai_response_for_metadata(to_plain_dict(result))
    response_meta_path = stage_dir / f"openai_response_meta_{stage_name}.json"
    response_meta_path.write_text(json.dumps(response_dict, ensure_ascii=False, indent=2), encoding="utf-8")

    usage_breakdown = extract_usage_breakdown(response_dict)
    usage_path = stage_dir / f"usage_{stage_name}.json"
    usage_path.write_text(json.dumps(usage_breakdown, ensure_ascii=False, indent=2), encoding="utf-8")
    print_usage_report(stage_name, usage_breakdown)
    print(f"[INFO] saved response metadata: {response_meta_path}")
    print(f"[INFO] saved usage metadata: {usage_path}")

    image_base64 = result.data[0].b64_json
    raw = base64.b64decode(image_base64)
    generated = open_result_image_over_source(raw, image)

    meta = {
        "stage": stage_name,
        "model": args.model,
        "size": args.size,
        "quality": args.quality,
        "output_format": args.output_format,
        "background": args.background,
        "api_input_image": str(image_path),
        "api_reference_images": [str(p) for p in reference_paths],
        "api_mask": str(mask_path) if mask_path else None,
        "response_metadata": str(response_meta_path),
        "usage_metadata": str(usage_path),
        "usage": usage_breakdown,
        "estimated_cost_usd": usage_breakdown.get("estimated_cost_usd"),
        "api_elapsed_seconds": round(api_elapsed_seconds, 4),
        "api_elapsed_text": api_elapsed_text,
        "prompt": prompt,
    }
    return generated, meta


def parse_args():
    root = project_root()
    p = argparse.ArgumentParser(description="OpenAI GPT Image food/object prompt edit runner")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--class-name", help="Class name for folder/config/output naming")
    group.add_argument("--defect-type", help="Backward-compatible alias for --class-name")
    p.add_argument("--image", type=Path, required=True, help="Input image path. For food workflow, use the original food/object image.")
    p.add_argument("--mask", type=Path, default=None, help="Legacy optional source/repair mask. White pixels = original area to repair.")
    p.add_argument("--prototype-mask", type=Path, default=None, help="Legacy optional prototype mask for random generation workflows. Defaults to --mask.")
    p.add_argument("--target-area", type=Path, default=None, help="Target Area mask. White pixels = valid editable food/object variation area.")
    p.add_argument("--workflow", choices=["prompt-only-edit", "target-area-edit", "repair-and-random-generate", "repair-only", "generate-only"], default="prompt-only-edit")
    p.add_argument("--placement-mode", choices=["fixed-mask", "random-in-target"], default="random-in-target")
    p.add_argument("--min-defects", type=int, default=1)
    p.add_argument("--max-defects", type=int, default=3)
    p.add_argument("--random-scale-min", type=float, default=0.85)
    p.add_argument("--random-scale-max", type=float, default=1.15)
    p.add_argument("--placement-attempts", type=int, default=500)
    p.add_argument("--exclude-repair-padding", type=int, default=8)
    p.add_argument("--clip-mask-to-target", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--prompt", default="", help="Direct Chinese or English prompt. Overrides prompt file.")
    p.add_argument("--prompt-file", type=Path, default=None, help="Optional .txt prompt file. Default: configs/classes/<class_name>/prompt.txt")
    p.add_argument("--prompt-config", type=Path, default=None, help="Deprecated compatibility alias for --prompt-file")
    p.add_argument("--prompt-extra", default="", help="Extra text appended after the simple prompt")
    p.add_argument("--output-dir", type=Path, default=root / "runs")
    p.add_argument("--run-name", default=None)
    p.add_argument("--batch-run-name", default=None, help="UI batch name used to group child runs for export")
    p.add_argument("--keep-intermediates", action="store_true", help="Keep masks, previews and per-call JSON files. By default they are cleaned after generation.")
    p.add_argument("--seed", type=int, default=5000, help="Controls local random placement only; OpenAI image API does not expose generation seed.")
    p.add_argument("--num-outputs", type=int, default=1)
    p.add_argument(
        "--model",
        default="gpt-image-2",
        choices=SUPPORTED_GPT_IMAGE_MODELS,
        help="OpenAI GPT Image model. Choices: " + ", ".join(SUPPORTED_GPT_IMAGE_MODELS),
    )
    p.add_argument("--size", default="1280x1280", help="Output size, e.g. 1280x1280, 1536x1024, 2048x2048, or auto")
    p.add_argument("--quality", default="high", choices=["low", "medium", "high", "auto"])
    p.add_argument("--output-format", default="png", choices=["png", "jpeg", "webp"])
    p.add_argument("--output-compression", type=int, default=None, help="0-100, only for jpeg/webp")
    p.add_argument("--background", default="opaque", choices=["auto", "opaque"], help="gpt-image-2 does not support transparent output backgrounds")
    p.add_argument("--invert-mask", action="store_true")
    p.add_argument("--mask-threshold", type=int, default=127)
    p.add_argument("--mask-dilate", type=int, default=0)
    p.add_argument("--mask-feather", type=int, default=3)
    p.add_argument("--keep-unmasked", action=argparse.BooleanOptionalAction, default=True, help="Composite original pixels outside mask back to output for mask-based stages")
    p.add_argument("--auto-resize-multiple", type=int, default=16)
    p.add_argument("--width", type=int, default=None)
    p.add_argument("--height", type=int, default=None)
    p.add_argument("--final-width", type=int, default=None, help="Resize the final saved image to this width after the API call.")
    p.add_argument("--final-height", type=int, default=None, help="Resize the final saved image to this height after the API call.")
    p.add_argument("--dry-run", action="store_true", help="Validate inputs/prompts and write masks/previews without calling OpenAI API")
    return p.parse_args()


def main():
    args = parse_args()
    requested_size_mode = str(args.size).strip().lower()
    if requested_size_mode in {"same_as_original", "original", "source"}:
        with Image.open(args.image) as _src_size_img:
            orig_w, orig_h = int(_src_size_img.width), int(_src_size_img.height)
        if args.final_width is None:
            args.final_width = orig_w
        if args.final_height is None:
            args.final_height = orig_h
        api_w, api_h = normalize_gpt2_api_size(orig_w, orig_h)
        args.size = f"{api_w}x{api_h}"
        # Keep API input/mask dimensions aligned with the API output size to
        # avoid oversized uploaded PNG inputs and reduce token/cost overhead.
        args.width = api_w
        args.height = api_h
        if (api_w, api_h) != (orig_w, orig_h):
            print(f"[WARN][SIZE] requested same_as_original={orig_w}x{orig_h}, but GPT-image-2 API request size is capped to {api_w}x{api_h}; final saved image will be resized back to {args.final_width}x{args.final_height}.", flush=True)
    coerce_gpt2_size_for_api(args)
    validate_output_size(args.size)

    # For concrete output sizes, prompt-only-edit keeps the GPTUI47-style
    # image+prompt workflow, but large source images may later be normalized to
    # an output-sized API canvas with ROI/Target coordinates transformed to the
    # same coordinate system. Non prompt-only masked workflows resize image/masks
    # together to avoid final mask-composite dimension mismatches.
    try:
        concrete_w, concrete_h = parse_size(args.size)
    except Exception:
        concrete_w, concrete_h = (0, 0)
    if concrete_w > 0 and concrete_h > 0:
        if args.workflow == "prompt-only-edit":
            if args.final_width is None:
                args.final_width = concrete_w
            if args.final_height is None:
                args.final_height = concrete_h
        else:
            if args.width is None:
                args.width = concrete_w
            if args.height is None:
                args.height = concrete_h
            if args.final_width is None:
                args.final_width = concrete_w
            if args.final_height is None:
                args.final_height = concrete_h

    defect_type = sanitize_defect_type(args.class_name or args.defect_type)
    load_dotenv_key(project_root())

    if not args.dry_run:
        try:
            from openai import OpenAI as _OpenAI  # noqa: F401
        except Exception as exc:
            raise SystemExit(
                "Missing or incompatible dependency: openai. Install project requirements in the same Python environment that launches the UI:\n"
                f"  {sys.executable} -m pip install -r {project_root() / 'requirements.txt'}\n"
                f"Original import error: {type(exc).__name__}: {exc}"
            )
    if not os.environ.get("OPENAI_API_KEY") and not args.dry_run:
        raise SystemExit("OPENAI_API_KEY is not set. Set it first, or create a .env file with OPENAI_API_KEY=...")

    prompt_path = args.prompt_file or args.prompt_config
    cfg = load_user_prompt(defect_type, prompt_path, args.prompt, args.size)
    bundle = build_prompt_bundle(cfg)
    if args.prompt_extra.strip():
        for key in ["prompt_only_edit_prompt", "target_area_edit_prompt", "repair_prompt", "random_generation_prompt", "single_pass_relocation_prompt", "full_workflow_prompt"]:
            bundle[key] += "\n[User extra] " + args.prompt_extra.strip()

    need_source_mask = args.workflow in {"repair-only", "repair-and-random-generate"}
    need_prototype = args.workflow in {"generate-only", "repair-and-random-generate"}
    if need_source_mask and args.mask is None:
        raise ValueError(f"--workflow {args.workflow} requires --mask")
    if args.workflow == "generate-only" and args.placement_mode == "fixed-mask" and args.mask is None and args.prototype_mask is None:
        raise ValueError("generate-only fixed-mask requires --mask or --prototype-mask")
    if args.workflow in {"repair-and-random-generate", "target-area-edit"} and args.target_area is None:
        raise ValueError(f"{args.workflow} requires --target-area")
    if args.workflow == "generate-only" and args.placement_mode == "random-in-target" and args.target_area is None:
        raise ValueError("generate-only random-in-target requires --target-area")

    image, repair_mask, prototype_mask, target_area, prep_meta = prepare_image_and_masks(
        args.image,
        repair_mask_path=args.mask,
        target_area_path=args.target_area,
        prototype_mask_path=args.prototype_mask,
        invert_mask=args.invert_mask,
        mask_threshold=args.mask_threshold,
        mask_dilate=args.mask_dilate,
        auto_resize_multiple=args.auto_resize_multiple,
        width=args.width,
        height=args.height,
    )
    if prototype_mask is None and repair_mask is not None:
        prototype_mask = repair_mask.copy()

    random_allowed_area = None
    if args.workflow in {"generate-only", "repair-and-random-generate"} and target_area is not None and target_area.getbbox() is not None:
        random_allowed_area = subtract_mask(target_area, repair_mask, padding=args.exclude_repair_padding)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    default_run_name = f"{sanitize_name(args.image.stem)}_{args.workflow}_{timestamp}"
    run_name = sanitize_name(args.run_name) if args.run_name else default_run_name
    output_base = args.output_dir
    # Backward compatible output handling:
    #   --output-dir <root>/runs                  -> <root>/runs/<class>/<run_name>
    #   --output-dir <root>/runs/<class>          -> <root>/runs/<class>/<run_name>
    #   --output-dir <root>/runs/<class>/<batch>  -> <root>/runs/<class>/<batch>/<run_name>
    if output_base.name == defect_type or output_base.parent.name == defect_type:
        run_dir = ensure_dir(output_base / run_name)
    else:
        run_dir = ensure_dir(output_base / defect_type / run_name)

    image.save(run_dir / "input.png")
    if repair_mask is not None:
        repair_mask.save(run_dir / "original_repair_mask.png")
        make_mask_preview(image, repair_mask, color=(255, 0, 0)).save(run_dir / "original_repair_mask_preview.png")
        make_openai_edit_mask(repair_mask).save(run_dir / "openai_original_repair_alpha_mask.png")
    if prototype_mask is not None:
        prototype_mask.save(run_dir / "prototype_mask.png")
        make_mask_preview(image, prototype_mask, color=(255, 128, 0)).save(run_dir / "prototype_mask_preview.png")
    if target_area is not None:
        target_area.save(run_dir / "target_area.png")
        make_mask_preview(image, target_area, alpha=80, color=(0, 255, 0)).save(run_dir / "target_area_preview.png")
    if random_allowed_area is not None:
        random_allowed_area.save(run_dir / "random_allowed_area_excluding_original.png")
        make_mask_preview(image, random_allowed_area, alpha=80, color=(0, 180, 255)).save(run_dir / "random_allowed_area_preview.png")

    metadata: dict[str, Any] = {
        "api_provider": "OpenAI",
        "model": args.model,
        "defect_type": defect_type,
        "workflow": args.workflow,
        "run_name": run_name,
        "batch_run_name": sanitize_name(args.batch_run_name) if args.batch_run_name else run_name,
        "prompt": cfg,
        "effective_prompts": bundle,
        "seed_start": args.seed,
        "seed_note": "Seed controls local legacy mask placement only. OpenAI GPT Image API does not expose a deterministic seed parameter.",
        "num_outputs": args.num_outputs,
        "api_parameters": {
            "model": args.model,
            "size": args.size,
            "quality": args.quality,
            "output_format": args.output_format,
            "output_compression": args.output_compression,
            "background": args.background,
            "final_width": args.final_width,
            "final_height": args.final_height,
        },
        "keep_unmasked": args.keep_unmasked,
        "mask_feather": args.mask_feather,
        "placement_mode": args.placement_mode,
        "target_area": str(args.target_area) if args.target_area else None,
        "exclude_repair_padding": args.exclude_repair_padding,
        "min_defects": args.min_defects,
        "max_defects": args.max_defects,
        "random_scale_min": args.random_scale_min,
        "random_scale_max": args.random_scale_max,
        "preprocess": prep_meta,
        "bbox_source_width": image.width,
        "bbox_source_height": image.height,
        "outputs": [],
        "final_outputs": [],
        "placement_records": [],
        "api_calls": [],
        "dry_run": args.dry_run,
    }

    prompt_only_api_image = image
    prompt_only_norm_meta: dict[str, Any] = {"enabled": False}
    if args.workflow == "prompt-only-edit" and concrete_w > 0 and concrete_h > 0:
        prompt_only_api_image, bundle, prompt_only_norm_meta = _prepare_prompt_only_canvas_if_needed(
            image, repair_mask, target_area, bundle, int(concrete_w), int(concrete_h)
        )
        metadata["prompt_only_coordinate_normalization"] = prompt_only_norm_meta
        metadata["effective_prompts"] = bundle
        if prompt_only_norm_meta.get("enabled"):
            ensure_dir(run_dir).mkdir(parents=True, exist_ok=True)
            prompt_only_api_image.save(run_dir / "prompt_only_api_canvas.png")
            (run_dir / "prompt_only_coordinate_normalization.json").write_text(json.dumps(prompt_only_norm_meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[INFO] class_name={defect_type}")
    print(f"[INFO] workflow={args.workflow}")
    print(f"[INFO] model={args.model}")
    print(f"[INFO] image={args.image}")
    print(f"[INFO] mask={args.mask}")
    print(f"[INFO] target_area={args.target_area}")
    print(f"[INFO] output_dir={run_dir}")
    print(f"[INFO] size={args.size}; prepared image={image.width}x{image.height}")
    if args.workflow == "prompt-only-edit" and concrete_w > 0 and concrete_h > 0:
        if prompt_only_norm_meta.get("enabled"):
            print(f"[INFO] prompt-only-edit normalized source for API canvas: {prompt_only_norm_meta}")
        else:
            print("[INFO] prompt-only-edit uses source image as-is; API output size is controlled by --size.")
    if args.final_width and args.final_height:
        print(f"[INFO] final output resize={args.final_width}x{args.final_height}")

    if args.dry_run:
        (run_dir / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        prompt_used_path = run_dir / "prompt_used.txt"
        prompt_used_path.write_text(cfg["user_prompt"], encoding="utf-8")
        print(f"[DONE] dry-run folder: {run_dir}")
        return

    from openai import OpenAI
    client = OpenAI()

    for i in range(int(args.num_outputs)):
        seed = int(args.seed) + i
        output_start = time.perf_counter()
        print(f"[INFO] output {i + 1}/{args.num_outputs}, local placement seed={seed}")
        print(f"[TIME] output {i + 1}/{args.num_outputs} started")
        current_image = prompt_only_api_image if args.workflow == "prompt-only-edit" else image
        repair_output_path = None
        random_output_path = None
        random_mask_path = None
        placement_meta: dict[str, Any] = {"seed": seed}

        if args.workflow == "prompt-only-edit":
            print("[INFO] prompt-only edit: image + prompt, no external mask")
            ref_images = []
            if prompt_only_norm_meta.get("enabled"):
                print("[INFO] normalized large-input prompt-only mode: using source-preserving postprocess after API output")
            generated, api_meta = _call_openai_image_edit(
                client,
                current_image,
                bundle["prompt_only_edit_prompt"],
                args,
                run_dir,
                f"prompt_only_seed{seed}",
                edit_mask=None,
                reference_images=ref_images,
            )
            if prompt_only_norm_meta.get("enabled"):
                roi_canvas = _mask_to_prompt_only_canvas(repair_mask, current_image.size, prompt_only_norm_meta)
                target_canvas = _mask_to_prompt_only_canvas(target_area, current_image.size, prompt_only_norm_meta)
                generated, preserve_meta = _prompt_only_preserve_source_composite(
                    current_image,
                    generated,
                    roi_canvas,
                    target_canvas,
                    run_dir=run_dir,
                    seed=seed,
                )
                api_meta["prompt_only_preserve_source_postprocess"] = preserve_meta
                metadata.setdefault("prompt_only_preserve_source_postprocess", []).append({"seed": seed, **preserve_meta})
                print(f"[INFO] prompt-only source-preserving postprocess: {preserve_meta}")
            if args.final_width and args.final_height and generated.size != (int(args.final_width), int(args.final_height)):
                generated = generated.resize((int(args.final_width), int(args.final_height)), Image.Resampling.LANCZOS)
            out_path = run_dir / f"edited_seed{seed}.{args.output_format}"
            generated.save(out_path)
            output_elapsed_seconds = time.perf_counter() - output_start
            output_elapsed_text = format_elapsed(output_elapsed_seconds)
            metadata["outputs"].append(str(out_path))
            metadata["final_outputs"].append(str(out_path))
            metadata["api_calls"].append(api_meta)
            metadata.setdefault("per_seed", []).append({
                "seed": seed,
                "edited_output": str(out_path),
                "elapsed_seconds": round(output_elapsed_seconds, 4),
                "elapsed_text": output_elapsed_text,
            })
            print(f"[OK] edited: {out_path}")
            print(f"[TIME] output {i + 1}/{args.num_outputs} elapsed={output_elapsed_text} ({output_elapsed_seconds:.2f}s)")
            continue

        if args.workflow == "target-area-edit":
            if target_area is None or target_area.getbbox() is None:
                raise ValueError("target-area-edit requires a non-empty --target-area")
            effective_mask = binarize(target_area)
            bbox = effective_mask.getbbox()
            placement_meta.update({
                "mode": "target-area-edit",
                "workflow": "target-area-edit",
                "bbox_xyxy": list(map(int, bbox)) if bbox else None,
                "note": "Target Area is used directly as the editable mask for food/object variation.",
            })
            random_mask_path = run_dir / f"target_area_effective_mask_seed{seed}.png"
            effective_mask.save(random_mask_path)
            make_mask_preview(current_image, effective_mask, alpha=95, color=(0, 255, 0)).save(run_dir / f"target_area_effective_mask_preview_seed{seed}.png")
            make_openai_edit_mask(effective_mask).save(run_dir / f"openai_target_area_alpha_mask_seed{seed}.png")
            metadata["placement_records"].append(placement_meta)
            print(f"[INFO] target-area edit mask seed={seed}: {placement_meta}")
            print("[INFO] target-area-edit: food/object variation constrained to Target Area via OpenAI Image Edit API")

            generated, api_meta = _call_openai_image_edit(
                client,
                current_image,
                bundle["target_area_edit_prompt"],
                args,
                run_dir,
                f"target_area_seed{seed}",
                edit_mask=effective_mask,
                reference_images=[],
            )
            if args.keep_unmasked:
                generated = composite_keep_unmasked(current_image, generated, effective_mask, args.mask_feather)
            if args.final_width and args.final_height and generated.size != (int(args.final_width), int(args.final_height)):
                generated = generated.resize((int(args.final_width), int(args.final_height)), Image.Resampling.LANCZOS)
            random_output_path = run_dir / f"edited_seed{seed}.{args.output_format}"
            generated.save(random_output_path)
            metadata["outputs"].append(str(random_output_path))
            metadata["final_outputs"].append(str(random_output_path))
            metadata["api_calls"].append(api_meta)
            print(f"[OK] edited: {random_output_path}")

        if args.workflow == "repair-and-random-generate":
            # Single-pass relocation: repair the original ROI and generate the
            # new randomly placed defect islands in one OpenAI edit call.  The
            # edit mask is the union of the repair ROI mask and the random
            # target-area islands; the original unmasked image is supplied as a
            # second reference image so the model can still see the ROI defect
            # appearance even though the first image is masked at the ROI.
            assert repair_mask is not None
            assert prototype_mask is not None
            allowed = random_allowed_area if random_allowed_area is not None else target_area
            effective_mask, placement_meta = build_random_mask_in_target(
                prototype_mask,
                allowed,
                seed=seed,
                min_defects=args.min_defects,
                max_defects=args.max_defects,
                scale_min=args.random_scale_min,
                scale_max=args.random_scale_max,
                placement_attempts=args.placement_attempts,
                defect_type=defect_type,
            )
            placement_meta["excluded_original_repair_area"] = bool(random_allowed_area is not None)
            placement_meta["workflow"] = "single-pass-repair-and-random-generate"

            random_mask_path = run_dir / f"random_effective_mask_seed{seed}.png"
            combined_mask_path = run_dir / f"single_pass_combined_mask_seed{seed}.png"
            effective_mask.save(random_mask_path)
            combined_mask = ImageChops.lighter(binarize(repair_mask), binarize(effective_mask)).point(lambda v: 255 if v > 0 else 0)
            combined_mask.save(combined_mask_path)
            make_mask_preview(current_image, effective_mask, color=(255, 0, 0)).save(run_dir / f"random_effective_mask_preview_seed{seed}.png")
            make_mask_preview(current_image, combined_mask, color=(255, 180, 0)).save(run_dir / f"single_pass_combined_mask_preview_seed{seed}.png")
            make_openai_edit_mask(combined_mask).save(run_dir / f"openai_single_pass_alpha_mask_seed{seed}.png")
            metadata["placement_records"].append(placement_meta)
            print(f"[INFO] single-pass random/generation mask seed={seed}: {placement_meta}")
            print("[INFO] single-pass repair + random generation via one OpenAI Image Edit API call")

            generated, api_meta = _call_openai_image_edit(
                client,
                current_image,
                bundle["single_pass_relocation_prompt"],
                args,
                run_dir,
                f"single_pass_seed{seed}",
                edit_mask=combined_mask,
                reference_images=[image],
            )
            if args.keep_unmasked:
                generated = composite_keep_unmasked(current_image, generated, combined_mask, args.mask_feather)
            if args.final_width and args.final_height and generated.size != (int(args.final_width), int(args.final_height)):
                generated = generated.resize((int(args.final_width), int(args.final_height)), Image.Resampling.LANCZOS)
            random_output_path = run_dir / f"generated_seed{seed}.png"
            generated.save(random_output_path)
            metadata["outputs"].append(str(random_output_path))
            metadata["final_outputs"].append(str(random_output_path))
            metadata["api_calls"].append(api_meta)
            print(f"[OK] generated: {random_output_path}")

        elif args.workflow == "repair-only":
            assert repair_mask is not None
            print("[INFO] repair-only: original defect repair via OpenAI Image Edit API")
            repaired, api_meta = _call_openai_image_edit(
                client, current_image, bundle["repair_prompt"], args, run_dir, f"repair_seed{seed}", edit_mask=repair_mask
            )
            if args.keep_unmasked:
                repaired = composite_keep_unmasked(current_image, repaired, repair_mask, args.mask_feather)
            if args.final_width and args.final_height and repaired.size != (int(args.final_width), int(args.final_height)):
                repaired = repaired.resize((int(args.final_width), int(args.final_height)), Image.Resampling.LANCZOS)
            repair_output_path = run_dir / f"repaired_seed{seed}.png"
            repaired.save(repair_output_path)
            current_image = repaired
            metadata["outputs"].append(str(repair_output_path))
            metadata["final_outputs"].append(str(repair_output_path))
            metadata["api_calls"].append(api_meta)
            print(f"[OK] repaired: {repair_output_path}")

        elif args.workflow == "generate-only":
            if args.placement_mode == "fixed-mask":
                assert prototype_mask is not None
                effective_mask = clip_mask_to_target(prototype_mask, target_area) if args.clip_mask_to_target else prototype_mask
                placement_meta.update({"mode": "fixed-mask", "bbox_xyxy": list(map(int, effective_mask.getbbox())) if effective_mask.getbbox() else None})
            else:
                assert prototype_mask is not None
                allowed = random_allowed_area if random_allowed_area is not None else target_area
                effective_mask, placement_meta = build_random_mask_in_target(
                    prototype_mask,
                    allowed,
                    seed=seed,
                    min_defects=args.min_defects,
                    max_defects=args.max_defects,
                    scale_min=args.random_scale_min,
                    scale_max=args.random_scale_max,
                    placement_attempts=args.placement_attempts,
                    defect_type=defect_type,
                )
                placement_meta["excluded_original_repair_area"] = bool(random_allowed_area is not None)

            random_mask_path = run_dir / f"random_effective_mask_seed{seed}.png"
            effective_mask.save(random_mask_path)
            make_mask_preview(current_image, effective_mask, color=(255, 0, 0)).save(run_dir / f"random_effective_mask_preview_seed{seed}.png")
            make_openai_edit_mask(effective_mask).save(run_dir / f"openai_random_alpha_mask_seed{seed}.png")
            metadata["placement_records"].append(placement_meta)
            print(f"[INFO] random/generation mask seed={seed}: {placement_meta}")

            print("[INFO] generate-only: random new defect generation via OpenAI Image Edit API")
            generated, api_meta = _call_openai_image_edit(
                client,
                current_image,
                bundle["random_generation_prompt"],
                args,
                run_dir,
                f"generate_seed{seed}",
                edit_mask=effective_mask,
                reference_images=[image],
            )
            if args.keep_unmasked:
                generated = composite_keep_unmasked(current_image, generated, effective_mask, args.mask_feather)
            if args.final_width and args.final_height and generated.size != (int(args.final_width), int(args.final_height)):
                generated = generated.resize((int(args.final_width), int(args.final_height)), Image.Resampling.LANCZOS)
            random_output_path = run_dir / f"generated_seed{seed}.png"
            generated.save(random_output_path)
            metadata["outputs"].append(str(random_output_path))
            metadata["final_outputs"].append(str(random_output_path))
            metadata["api_calls"].append(api_meta)
            print(f"[OK] generated: {random_output_path}")

        output_elapsed_seconds = time.perf_counter() - output_start
        output_elapsed_text = format_elapsed(output_elapsed_seconds)
        metadata.setdefault("per_seed", []).append({
            "seed": seed,
            "repair_output": str(repair_output_path) if repair_output_path else None,
            "random_effective_mask": str(random_mask_path) if random_mask_path else None,
            "generated_output": str(random_output_path) if random_output_path else None,
            "elapsed_seconds": round(output_elapsed_seconds, 4),
            "elapsed_text": output_elapsed_text,
        })
        print(f"[TIME] output {i + 1}/{args.num_outputs} elapsed={output_elapsed_text} ({output_elapsed_seconds:.2f}s)")

    (run_dir / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    prompt_used_path = run_dir / "prompt_used.txt"
    prompt_used_path.write_text(cfg["user_prompt"], encoding="utf-8")
    cleanup_run_dir(run_dir, metadata.get("final_outputs") or metadata.get("outputs") or [], args.keep_intermediates)
    print(f"[DONE] outputs: {run_dir}")


def cleanup_run_dir(run_dir: Path, final_outputs: list[str], keep_intermediates: bool = False) -> None:
    """Remove non-final generation artifacts to save disk space.

    Kept files are final generated images plus metadata.json, prompt_used.txt and log.txt.
    This intentionally removes masks, mask previews, target-area previews and per-call API JSON files.
    """
    if keep_intermediates:
        return
    keep: set[Path] = {run_dir / "metadata.json", run_dir / "prompt_used.txt", run_dir / "log.txt"}
    for out in final_outputs:
        p = Path(out)
        if not p.is_absolute():
            p = (run_dir / p).resolve() if not p.exists() else p.resolve()
        keep.add(p.resolve())
    for p in list(run_dir.iterdir()):
        if not p.is_file():
            continue
        try:
            if p.resolve() not in keep:
                p.unlink()
        except Exception as exc:
            print(f"[WARN] cleanup skipped {p.name}: {exc}", flush=True)


if __name__ == "__main__":
    main()
