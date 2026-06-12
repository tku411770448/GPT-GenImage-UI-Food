from __future__ import annotations

import csv
import json
import math
import random
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None

SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def natural_key(path: Path | str):
    name = path.name if isinstance(path, Path) else str(path)
    parts = re.split(r"(\d+)", name.lower())
    return [int(p) if p.isdigit() else p for p in parts]


def list_images(folder: Path) -> list[Path]:
    if not folder.exists():
        return []
    items = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS]
    return sorted(items, key=natural_key)


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(data: dict[str, Any], path: Path) -> None:
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def make_binary_mask(mask: Image.Image, threshold: int = 127) -> Image.Image:
    return mask.convert("L").point(lambda v: 255 if v > threshold else 0)


def fill_mask_holes(mask: Image.Image) -> Image.Image:
    """Fill enclosed holes in a binary mask.

    If the user draws only a closed outline around a defect, this converts that
    outline into a solid source mask so patch extraction and source repair both
    operate on the full defect region, not just the contour stroke. Open contours
    remain unchanged because the flood-fill reaches the image border.
    """
    m = make_binary_mask(mask)
    arr = np.asarray(m, dtype=np.uint8) > 0
    h, w = arr.shape
    if h == 0 or w == 0:
        return m
    outside = np.zeros((h, w), dtype=bool)
    from collections import deque
    q = deque()
    # Start from every border pixel that is not mask.
    for x in range(w):
        if not arr[0, x]:
            outside[0, x] = True; q.append((x, 0))
        if not arr[h-1, x]:
            outside[h-1, x] = True; q.append((x, h-1))
    for y in range(h):
        if not arr[y, 0]:
            outside[y, 0] = True; q.append((0, y))
        if not arr[y, w-1]:
            outside[y, w-1] = True; q.append((w-1, y))
    while q:
        x, y = q.popleft()
        for nx, ny in ((x+1,y),(x-1,y),(x,y+1),(x,y-1)):
            if 0 <= nx < w and 0 <= ny < h and not outside[ny, nx] and not arr[ny, nx]:
                outside[ny, nx] = True
                q.append((nx, ny))
    holes = (~arr) & (~outside)
    if not holes.any():
        return m
    filled = arr | holes
    return Image.fromarray(filled.astype(np.uint8) * 255, "L")


def dilate_mask(mask: Image.Image, pixels: int = 0) -> Image.Image:
    """Return a binary mask expanded by approximately `pixels` pixels.

    Used for source-defect removal. The user-drawn source mask can be very tight;
    if we inpaint only the exact white defect pixels, bright edge pixels often
    remain. Dilation gives OpenCV a slightly larger repair region.
    """
    m = make_binary_mask(mask)
    pixels = int(max(0, pixels))
    if pixels <= 0:
        return m
    # MaxFilter size must be odd. One pass with size 2*pixels+1 is enough.
    return make_binary_mask(m.filter(ImageFilter.MaxFilter(size=pixels * 2 + 1)))



def mask_edge_ring(mask: Image.Image, outer_px: int = 3, inner_px: int = 1) -> Image.Image:
    """Return a thin ring around a binary mask boundary.

    This is useful for seam smoothing: inpainting the entire repair mask can blur
    repetitive textures, while inpainting only the boundary ring can soften clone
    seams without destroying the texture copied into the mask interior.
    """
    m = make_binary_mask(mask)
    outer = dilate_mask(m, max(1, int(outer_px)))
    inner_px = max(0, int(inner_px))
    if inner_px > 0:
        inner = make_binary_mask(m.filter(ImageFilter.MinFilter(size=inner_px * 2 + 1)))
    else:
        inner = m
    arr = np.maximum(np.asarray(outer, dtype=np.int16) - np.asarray(inner, dtype=np.int16), 0)
    return Image.fromarray((arr > 0).astype(np.uint8) * 255, "L")

def combine_binary_masks(masks: list[Image.Image], image_size: tuple[int, int]) -> Image.Image:
    """Union multiple L-mode masks into one binary mask."""
    out = Image.new("L", image_size, 0)
    for m in masks:
        out = Image.fromarray(np.maximum(np.array(out), np.array(make_binary_mask(m.resize(image_size) if m.size != image_size else m))).astype(np.uint8), "L")
    return make_binary_mask(out)


def mask_bbox(mask: Image.Image) -> tuple[int, int, int, int] | None:
    return make_binary_mask(mask).getbbox()


def xyxy_to_xywh(b: tuple[int, int, int, int]) -> list[int]:
    x1, y1, x2, y2 = b
    return [int(x1), int(y1), int(x2 - x1), int(y2 - y1)]


def xyxy_intersects(a: tuple[int, int, int, int], b: tuple[int, int, int, int], margin: int = 0) -> bool:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ax1 -= margin; ay1 -= margin; ax2 += margin; ay2 += margin
    bx1 -= margin; by1 -= margin; bx2 += margin; by2 += margin
    return not (ax2 <= bx1 or bx2 <= ax1 or ay2 <= by1 or by2 <= ay1)


def read_manifest_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"Manifest not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_manifest_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def center_crop_box(width: int, height: int, size: int) -> tuple[int, int, int, int]:
    left = max(0, (width - size) // 2)
    upper = max(0, (height - size) // 2)
    return (left, upper, left + size, upper + size)


def random_crop_box(width: int, height: int, size: int, rng: random.Random) -> tuple[int, int, int, int]:
    max_x = max(0, width - size)
    max_y = max(0, height - size)
    left = rng.randint(0, max_x) if max_x > 0 else 0
    upper = rng.randint(0, max_y) if max_y > 0 else 0
    return (left, upper, left + size, upper + size)



def clamp_crop_box_around_center(width: int, height: int, size: int, center_x: float, center_y: float) -> tuple[int, int, int, int]:
    """Return a square crop box centered as close as possible to (center_x, center_y).

    The crop is clamped so it stays inside the source image. If the requested
    size is larger than either image dimension, the largest possible square is used.
    """
    size = int(min(size, width, height))
    left = int(round(center_x - size / 2))
    upper = int(round(center_y - size / 2))
    left = max(0, min(left, width - size))
    upper = max(0, min(upper, height - size))
    return (left, upper, left + size, upper + size)


def roi_crop_box(width: int, height: int, size: int, roi_xyxy: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """Crop a square around the center of a defect ROI."""
    x1, y1, x2, y2 = roi_xyxy
    cx = (x1 + x2) / 2.0
    cy = (y1 + y2) / 2.0
    return clamp_crop_box_around_center(width, height, size, cx, cy)


def box_contains(inner: tuple[int, int, int, int], outer: tuple[int, int, int, int]) -> bool:
    """Return True if outer fully contains inner."""
    ix1, iy1, ix2, iy2 = inner
    ox1, oy1, ox2, oy2 = outer
    return ox1 <= ix1 and oy1 <= iy1 and ix2 <= ox2 and iy2 <= oy2


def decide_crop_size(width: int, height: int, large_threshold: int = 1280, medium_threshold: int = 640) -> int | None:
    """Return target square crop size.

    Rule used in this project:
    - if the shortest side is >= 1280: crop a 1280x1280 square.
    - elif the shortest side is >= 640: crop a 640x640 square.
    - else: return None, meaning keep the original image because a 640x640 crop is impossible.
    """
    min_side = min(width, height)
    if min_side >= large_threshold:
        return large_threshold
    if min_side >= medium_threshold:
        return medium_threshold
    return None


def copy_or_crop_image(src: Path, dst: Path, crop_mode: str = "center", seed: int = 0,
                       large_threshold: int = 1280, medium_threshold: int = 640) -> dict[str, Any]:
    img = Image.open(src).convert("RGB")
    w, h = img.size
    target = decide_crop_size(w, h, large_threshold, medium_threshold)
    if target is None:
        crop_box = (0, 0, w, h)
        out = img
        crop_status = "kept_original_smaller_than_640"
    else:
        if crop_mode == "random":
            crop_box = random_crop_box(w, h, target, random.Random(seed))
        else:
            crop_box = center_crop_box(w, h, target)
        out = img.crop(crop_box)
        crop_status = f"cropped_{target}x{target}"
    ensure_dir(dst.parent)
    out.save(dst)
    return {
        "source_path": str(src),
        "output_path": str(dst),
        "source_width": w,
        "source_height": h,
        "output_width": out.size[0],
        "output_height": out.size[1],
        "crop_box_xyxy": list(map(int, crop_box)),
        "crop_status": crop_status,
    }


def make_overlay_preview(source: Image.Image, mask: Image.Image, color=(255, 255, 255), alpha=150) -> Image.Image:
    source_rgba = source.convert("RGBA")
    mask_l = make_binary_mask(mask)
    overlay = Image.new("RGBA", source_rgba.size, (0, 0, 0, 0))
    overlay_np = np.array(overlay)
    m = np.array(mask_l) > 0
    overlay_np[m] = [color[0], color[1], color[2], alpha]
    return Image.alpha_composite(source_rgba, Image.fromarray(overlay_np, "RGBA")).convert("RGB")


def paste_with_alpha(base: Image.Image, patch_rgba: Image.Image, x: int, y: int) -> Image.Image:
    out = base.convert("RGBA")
    patch_rgba = patch_rgba.convert("RGBA")
    out.alpha_composite(patch_rgba, (x, y))
    return out.convert("RGB")


def extract_patch(image: Image.Image, mask: Image.Image, pad: int = 8) -> dict[str, Any]:
    mask_l = make_binary_mask(mask)
    bbox = mask_l.getbbox()
    if bbox is None:
        raise ValueError("source defect_mask is empty")
    w, h = image.size
    x1, y1, x2, y2 = bbox
    x1 = max(0, x1 - pad); y1 = max(0, y1 - pad)
    x2 = min(w, x2 + pad); y2 = min(h, y2 + pad)
    crop_box = (x1, y1, x2, y2)
    patch_rgb = image.convert("RGB").crop(crop_box)
    patch_alpha = mask_l.crop(crop_box)
    patch_rgba = patch_rgb.convert("RGBA")
    patch_rgba.putalpha(patch_alpha)
    return {"patch_rgba": patch_rgba, "alpha": patch_alpha, "bbox_xyxy": crop_box}


def cv2_inpaint(image: Image.Image, mask: Image.Image, radius: int = 5, method: str = "telea") -> Image.Image:
    if cv2 is None:
        raise ImportError("opencv-python is required for cv2 inpainting")
    img_np = np.array(image.convert("RGB"))
    mask_np = np.array(make_binary_mask(mask))
    flag = cv2.INPAINT_TELEA if method.lower() == "telea" else cv2.INPAINT_NS
    repaired = cv2.inpaint(img_np, mask_np, radius, flag)
    return Image.fromarray(repaired).convert("RGB")


def expand_box(box: tuple[int, int, int, int], pad: int, image_size: tuple[int, int]) -> tuple[int, int, int, int]:
    """Expand xyxy box while keeping it inside image bounds."""
    x1, y1, x2, y2 = map(int, box)
    w, h = image_size
    return (max(0, x1 - pad), max(0, y1 - pad), min(w, x2 + pad), min(h, y2 + pad))


def _mask_overlap_any(mask_np: np.ndarray, x: int, y: int, w: int, h: int) -> bool:
    crop = mask_np[y:y+h, x:x+w]
    return bool(crop.size and crop.max() > 0)


def local_texture_repair(
    image: Image.Image,
    repair_mask: Image.Image,
    source_bbox: tuple[int, int, int, int] | None = None,
    seed: int = 0,
    context_pad: int = 18,
    search_radius: int = 420,
    num_candidates: int = 350,
    feather_px: int = 5,
    min_shift_px: int = 80,
) -> Image.Image:
    """Repair masked source defect by copying a visually similar clean local texture patch.

    OpenCV inpaint often leaves a smooth blurry blob on repetitive patterns such as LCD/dot grids.
    This function instead searches nearby non-masked regions for a clean patch whose surrounding
    context resembles the defect neighborhood, then composites only the masked pixels with a
    feathered alpha. It is still deterministic through `seed`.
    """
    base = image.convert("RGB")
    m = make_binary_mask(repair_mask)
    bbox = m.getbbox()
    if bbox is None:
        return base

    crop_box = expand_box(bbox, int(context_pad), base.size)
    x1, y1, x2, y2 = crop_box
    pw, ph = x2 - x1, y2 - y1
    if pw <= 2 or ph <= 2:
        return cv2_inpaint(base, m, radius=5, method="telea")

    src_crop = np.asarray(base.crop(crop_box)).astype(np.float32)
    mask_crop = np.asarray(m.crop(crop_box)) > 0
    context = ~mask_crop
    if context.sum() < max(10, mask_crop.sum() // 2):
        # If the crop is almost all mask, fall back to inpaint.
        return cv2_inpaint(base, m, radius=5, method="telea")

    img_np = np.asarray(base).astype(np.float32)
    mask_np = np.asarray(m)
    W, H = base.size
    rng = random.Random(int(seed))

    src_cx = (x1 + x2) / 2.0
    src_cy = (y1 + y2) / 2.0
    max_x = max(0, W - pw)
    max_y = max(0, H - ph)

    # Candidate region centered around the source defect, clamped to image.
    rx1 = max(0, int(src_cx - search_radius - pw / 2))
    ry1 = max(0, int(src_cy - search_radius - ph / 2))
    rx2 = min(max_x, int(src_cx + search_radius - pw / 2))
    ry2 = min(max_y, int(src_cy + search_radius - ph / 2))
    if rx2 < rx1 or ry2 < ry1:
        rx1, ry1, rx2, ry2 = 0, 0, max_x, max_y

    # Include deterministic grid + random candidates.
    candidates: list[tuple[int, int]] = []
    grid_n = max(2, int(math.sqrt(max(16, num_candidates // 4))))
    for gy in np.linspace(ry1, ry2, grid_n).astype(int):
        for gx in np.linspace(rx1, rx2, grid_n).astype(int):
            candidates.append((int(gx), int(gy)))
    for _ in range(max(0, int(num_candidates))):
        candidates.append((rng.randint(rx1, rx2), rng.randint(ry1, ry2)))

    best_score = float("inf")
    best_xy: tuple[int, int] | None = None
    for cx, cy in candidates:
        if abs(cx - x1) < min_shift_px and abs(cy - y1) < min_shift_px:
            continue
        if _mask_overlap_any(mask_np, cx, cy, pw, ph):
            continue
        cand = img_np[cy:cy+ph, cx:cx+pw, :]
        if cand.shape[:2] != (ph, pw):
            continue
        # Compare only the non-masked context around the defect. This preserves local brightness/texture.
        diff = np.abs(cand[context] - src_crop[context]).mean()
        mean_diff = np.abs(cand.mean(axis=(0, 1)) - src_crop.mean(axis=(0, 1))).mean() * 0.15
        score = float(diff + mean_diff)
        if score < best_score:
            best_score = score
            best_xy = (cx, cy)

    if best_xy is None:
        return cv2_inpaint(base, m, radius=5, method="telea")

    bx, by = best_xy
    donor = base.crop((bx, by, bx + pw, by + ph))
    donor_rgba = donor.convert("RGBA")

    alpha = m.crop(crop_box)
    if feather_px > 0:
        # Feather only the outside seam while keeping the true repair region fully opaque.
        # Blurring the raw mask directly can leave the original defect visible through
        # semi-transparent alpha, especially on small bright/black defects.
        seam = dilate_mask(alpha, int(feather_px)).filter(ImageFilter.GaussianBlur(radius=int(feather_px)))
        seam_np = np.array(seam, dtype=np.uint8, copy=True)
        inner_np = np.array(make_binary_mask(alpha), dtype=np.uint8, copy=False)
        seam_np[inner_np > 0] = 255
        alpha = Image.fromarray(seam_np, "L")
    donor_rgba.putalpha(alpha)

    out = base.convert("RGBA")
    out.alpha_composite(donor_rgba, (x1, y1))
    return out.convert("RGB")


def repair_defect_region(
    image: Image.Image,
    repair_mask: Image.Image,
    source_bbox: tuple[int, int, int, int] | None = None,
    mode: str = "local_texture_clone",
    seed: int = 0,
    inpaint_radius: int = 7,
    inpaint_method: str = "telea",
    repair_passes: int = 1,
    context_pad: int = 18,
    search_radius: int = 420,
    num_candidates: int = 350,
    feather_px: int = 5,
) -> Image.Image:
    """Repair source defect with selectable strategy.

    mode:
    - opencv_inpaint: pure OpenCV inpaint.
    - local_texture_clone: copy a similar nearby clean texture into the mask.
    - hybrid: local texture clone followed by light inpaint over the same mask.
    """
    mode = (mode or "local_texture_clone").lower()
    out = image.convert("RGB")
    if mode in {"opencv", "opencv_inpaint", "inpaint", "telea", "ns"}:
        for _ in range(max(1, int(repair_passes))):
            out = cv2_inpaint(out, repair_mask, radius=int(inpaint_radius), method=inpaint_method)
        return out

    out = local_texture_repair(
        out,
        repair_mask,
        source_bbox=source_bbox,
        seed=seed,
        context_pad=context_pad,
        search_radius=search_radius,
        num_candidates=num_candidates,
        feather_px=feather_px,
    )
    if mode == "hybrid":
        # Hybrid no longer inpaints the whole repair mask because that can blur LCD/grid
        # textures. It inpaints only a thin boundary ring to soften clone seams while
        # preserving the locally cloned texture inside the repaired defect region.
        try:
            ring = mask_edge_ring(repair_mask, outer_px=max(1, int(feather_px // 2) + 1), inner_px=max(1, int(feather_px // 3) + 1))
            if ring.getbbox() is not None:
                out = cv2_inpaint(out, ring, radius=max(1, min(3, int(inpaint_radius // 3))), method=inpaint_method)
        except Exception:
            pass
    return out


def _morph_alpha(alpha: Image.Image, morph_px: int) -> Image.Image:
    morph_px = int(morph_px)
    if morph_px == 0:
        return alpha
    size = abs(morph_px) * 2 + 1
    out = alpha.filter(ImageFilter.MaxFilter(size=size)) if morph_px > 0 else alpha.filter(ImageFilter.MinFilter(size=size))
    # Avoid eroding tiny dot defects completely.
    return out if out.getbbox() is not None else alpha


def _apply_gamma_arr(arr: np.ndarray, gamma: float) -> np.ndarray:
    gamma = float(gamma)
    if gamma <= 0 or abs(gamma - 1.0) < 1e-6:
        return arr
    norm = np.clip(arr.astype(np.float32) / 255.0, 0, 1)
    out = np.power(norm, gamma) * 255.0
    return np.clip(out, 0, 255).astype(np.uint8)


def transform_patch(
    patch_rgba: Image.Image,
    scale: float,
    rotation_degree: float,
    brightness_delta: float,
    contrast: float,
    opacity: float,
    edge_blur: int,
    gamma: float = 1.0,
    blur_radius: float = 0.0,
    sharpen_factor: float = 1.0,
    noise_sigma: float = 0.0,
    color_gain: tuple[float, float, float] = (1.0, 1.0, 1.0),
    alpha_morph_px: int = 0,
    noise_seed: int | None = None,
) -> Image.Image:
    """Apply geometry + appearance randomization to a source defect patch.

    The added parameters intentionally avoid a single hard-coded defect look:
    alpha_morph changes size/shape, gamma/brightness/contrast/color_gain change tone,
    blur/sharpen/noise alter sharpness and sensor-like variation.
    """
    patch_rgba = patch_rgba.convert("RGBA")
    rgb = patch_rgba.convert("RGB")
    alpha = patch_rgba.getchannel("A")

    alpha = _morph_alpha(alpha, int(alpha_morph_px))

    arr = np.asarray(rgb).astype(np.float32)
    gains = np.array(color_gain, dtype=np.float32).reshape(1, 1, 3)
    arr = np.clip(arr * gains, 0, 255).astype(np.uint8)
    arr = _apply_gamma_arr(arr, gamma)
    if noise_sigma and float(noise_sigma) > 0:
        # Deterministic enough through caller-generated parameter values; noise is local and subtle.
        rng_seed = int(noise_seed) if noise_seed is not None else int(abs(float(noise_sigma)) * 100000 + arr.shape[0] * 17 + arr.shape[1])
        noise = np.random.default_rng(rng_seed).normal(0, float(noise_sigma), arr.shape)
        arr = np.clip(arr.astype(np.float32) + noise, 0, 255).astype(np.uint8)
    rgb = Image.fromarray(arr, "RGB")

    if brightness_delta != 0:
        factor = max(0.05, 1.0 + float(brightness_delta) / 255.0)
        rgb = ImageEnhance.Brightness(rgb).enhance(factor)
    if contrast != 1.0:
        rgb = ImageEnhance.Contrast(rgb).enhance(float(contrast))
    if sharpen_factor and abs(float(sharpen_factor) - 1.0) > 1e-6:
        rgb = ImageEnhance.Sharpness(rgb).enhance(float(sharpen_factor))
    if blur_radius and float(blur_radius) > 0:
        rgb = rgb.filter(ImageFilter.GaussianBlur(radius=float(blur_radius)))

    alpha = alpha.point(lambda v: int(max(0, min(255, v * opacity))))
    if edge_blur > 0:
        alpha = alpha.filter(ImageFilter.GaussianBlur(radius=int(edge_blur)))
    merged = Image.merge("RGBA", (*rgb.split(), alpha))
    if scale <= 0:
        raise ValueError("scale must be positive")
    new_size = (max(1, int(merged.width * scale)), max(1, int(merged.height * scale)))
    merged = merged.resize(new_size, Image.Resampling.BICUBIC)
    if abs(rotation_degree) > 1e-6:
        merged = merged.rotate(float(rotation_degree), resample=Image.Resampling.BICUBIC, expand=True)
    return merged


def target_mask_from_patch_alpha(image_size: tuple[int, int], patch_rgba: Image.Image, x: int, y: int) -> Image.Image:
    mask = Image.new("L", image_size, 0)
    alpha = patch_rgba.convert("RGBA").getchannel("A")
    mask.paste(alpha, (x, y))
    # Annotation masks must not disappear just because a visual opacity is below 0.5.
    return mask.convert("L").point(lambda v: 255 if v > 5 else 0)


def alpha_blend(base: Image.Image, patch_rgba: Image.Image, x: int, y: int) -> Image.Image:
    return paste_with_alpha(base, patch_rgba, x, y)


def seamless_clone(base: Image.Image, patch_rgba: Image.Image, x: int, y: int, mode: str = "mixed") -> Image.Image:
    if cv2 is None:
        return alpha_blend(base, patch_rgba, x, y)
    base_rgb = base.convert("RGB")
    canvas = Image.new("RGB", base_rgb.size, (0, 0, 0))
    mask = Image.new("L", base_rgb.size, 0)
    patch_rgb = patch_rgba.convert("RGB")
    alpha = patch_rgba.getchannel("A")
    canvas.paste(patch_rgb, (x, y), alpha)
    mask.paste(alpha, (x, y))
    if mask.getbbox() is None:
        return base_rgb
    cx = int(x + patch_rgba.width / 2)
    cy = int(y + patch_rgba.height / 2)
    # Keep center inside image bounds.
    cx = max(1, min(base_rgb.width - 2, cx))
    cy = max(1, min(base_rgb.height - 2, cy))
    try:
        src = cv2.cvtColor(np.array(canvas), cv2.COLOR_RGB2BGR)
        dst = cv2.cvtColor(np.array(base_rgb), cv2.COLOR_RGB2BGR)
        m = np.array(make_binary_mask(mask))
        flag = cv2.MIXED_CLONE if mode == "mixed" else cv2.NORMAL_CLONE
        cloned = cv2.seamlessClone(src, dst, m, (cx, cy), flag)
        return Image.fromarray(cv2.cvtColor(cloned, cv2.COLOR_BGR2RGB)).convert("RGB")
    except Exception:
        return alpha_blend(base_rgb, patch_rgba, x, y)


def mean_abs_diff(a: Image.Image, b: Image.Image, mask: Image.Image | None = None) -> float:
    arr_a = np.array(a.convert("RGB")).astype(np.float32)
    arr_b = np.array(b.convert("RGB")).astype(np.float32)
    diff = np.abs(arr_a - arr_b).mean(axis=2)
    if mask is not None:
        m = np.array(make_binary_mask(mask)) > 0
        if m.sum() == 0:
            return 0.0
        return float(diff[m].mean())
    return float(diff.mean())


def outside_mask(mask: Image.Image) -> Image.Image:
    return ImageOps.invert(make_binary_mask(mask))


def red_pixel_ratio(image: Image.Image) -> float:
    arr = np.array(image.convert("RGB"))
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    red = (r > 180) & (g < 80) & (b < 80) & ((r.astype(np.int16) - g.astype(np.int16)) > 80)
    return float(red.mean())


def yolo_label_from_mask(mask: Image.Image, class_id: int = 0) -> str | None:
    bbox = mask_bbox(mask)
    if bbox is None:
        return None
    w, h = mask.size
    x1, y1, x2, y2 = bbox
    cx = ((x1 + x2) / 2.0) / w
    cy = ((y1 + y2) / 2.0) / h
    bw = (x2 - x1) / w
    bh = (y2 - y1) / h
    return f"{class_id} {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}"


def yolo_label_from_bbox_xyxy(bbox_xyxy: tuple[int, int, int, int], image_size: tuple[int, int], class_id: int = 0) -> str:
    """Create one YOLO detection label from one xyxy bbox."""
    w, h = image_size
    x1, y1, x2, y2 = bbox_xyxy
    x1 = max(0, min(w, int(x1))); x2 = max(0, min(w, int(x2)))
    y1 = max(0, min(h, int(y1))); y2 = max(0, min(h, int(y2)))
    cx = ((x1 + x2) / 2.0) / w
    cy = ((y1 + y2) / 2.0) / h
    bw = max(0, x2 - x1) / w
    bh = max(0, y2 - y1) / h
    return f"{class_id} {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}"


def sanitize_defect_type(value: str | None) -> str:
    """Normalize user-facing defect names to safe folder names.

    Examples: "white mark" -> "white_mark", "Black Spot" -> "black_spot".
    """
    value = (value or "default").strip().lower()
    value = re.sub(r"[^0-9a-zA-Z_\-]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    return value or "default"


def class_path(base: Path, defect_type: str | None) -> Path:
    return base / sanitize_defect_type(defect_type)


def is_binary_mask_nonempty(mask: Image.Image) -> bool:
    return make_binary_mask(mask).getbbox() is not None


def full_white_mask(size: tuple[int, int]) -> Image.Image:
    return Image.new("L", size, 255)


def mask_coverage(candidate_mask: Image.Image, allowed_mask: Image.Image) -> float:
    """Return fraction of candidate-mask nonzero pixels that fall inside allowed-mask."""
    cand = np.array(make_binary_mask(candidate_mask), dtype=np.uint8) > 0
    total = int(cand.sum())
    if total <= 0:
        return 0.0
    allowed = np.array(make_binary_mask(allowed_mask.resize(candidate_mask.size) if allowed_mask.size != candidate_mask.size else allowed_mask), dtype=np.uint8) > 0
    return float((cand & allowed).sum()) / float(total)
