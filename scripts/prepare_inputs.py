#!/usr/bin/env python3
"""Prepare images before mask editing.

Two modes are supported per defect type:

1) ROI crop enabled
   data/00_raw_images/<defect_type>/ -> select_defect_roi.py -> prepare_inputs.py
   The image is cropped around the selected ROI center using this square rule:
     shortest side >= 1280 -> 1280x1280 crop
     shortest side >= 640  -> 640x640 crop
     shortest side < 640   -> keep original

2) ROI crop disabled
   data/00_raw_images/<defect_type>/ -> prepare_inputs.py
   Images are copied/conformed into data/01_inputs/<defect_type>/images without ROI selection.

The output folder data/01_inputs/<defect_type>/images is the folder used by tools/mask_editor.py.
"""
from __future__ import annotations

import argparse
import ast
import csv
import json
import shutil
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from PIL import Image
from utils import (
    box_contains,
    decide_crop_size,
    ensure_dir,
    list_images,
    project_root,
    read_manifest_csv,
    roi_crop_box,
    sanitize_defect_type,
    write_manifest_csv,
)


def parse_bool_text(value: str | bool | None) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    s = str(value).strip().lower()
    if s in {"yes", "y", "true", "1", "on", "enable", "enabled"}:
        return True
    if s in {"no", "n", "false", "0", "off", "disable", "disabled"}:
        return False
    if s in {"auto", "config", ""}:
        return None
    raise argparse.ArgumentTypeError("Use yes/no/auto for --enable-roi-crop")


def load_preprocess_config(defect_type: str, config_path: Path | None) -> dict[str, Any]:
    root = project_root()
    path = config_path or root / "configs" / "defect_types" / defect_type / "preprocess.json"
    if not path.exists():
        # Safe defaults if user upgraded from an older package.
        return {
            "enable_roi_crop": True,
            "large_threshold": 1280,
            "medium_threshold": 640,
            "strict_roi_inside_crop": True,
            "output_format": "png",
        }
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_project_path(root: Path, value: str | Path | None, fallback: Path) -> Path:
    if value is None or str(value).strip() == "":
        return fallback
    path = Path(str(value))
    if path.is_absolute():
        return path
    return root / path


def parse_box(value: str) -> tuple[int, int, int, int]:
    try:
        parsed = ast.literal_eval(str(value))
    except Exception as exc:
        raise ValueError(f"Cannot parse ROI box value: {value}") from exc
    if isinstance(parsed, (list, tuple)) and len(parsed) == 4:
        return tuple(int(round(float(v))) for v in parsed)  # type: ignore[return-value]
    raise ValueError(f"ROI box must contain 4 values: {value}")


def clear_folder_images(folder: Path) -> None:
    ensure_dir(folder)
    for p in folder.iterdir():
        if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}:
            p.unlink()


def clear_dependent_masks(root: Path, defect_type: str) -> None:
    """Remove masks/editor outputs that would no longer align after image preparation."""
    candidates = [
        root / "data" / "01_inputs" / defect_type / "masks",
        root / "data" / "01_inputs" / defect_type / "target_area_masks",
        root / "data" / "02_mask_editor" / defect_type / "inputs",
        root / "data" / "02_mask_editor" / defect_type / "masks",
        root / "data" / "02_mask_editor" / defect_type / "target_area_masks",
        root / "data" / "02_mask_editor" / defect_type / "previews",
    ]
    for folder in candidates:
        if folder.exists():
            for p in folder.glob("*.png"):
                p.unlink()
    manifest = root / "data" / "02_mask_editor" / defect_type / "logs" / "mask_editor_manifest.csv"
    if manifest.exists():
        manifest.unlink()


def save_rgb_as_png(src: Path, dst: Path) -> tuple[int, int]:
    img = Image.open(src).convert("RGB")
    ensure_dir(dst.parent)
    img.save(dst)
    return img.size


def crop_with_roi(src: Path, dst: Path, roi_xyxy: tuple[int, int, int, int], large_threshold: int, medium_threshold: int, strict_roi_inside: bool) -> dict[str, Any]:
    img = Image.open(src).convert("RGB")
    w, h = img.size
    target = decide_crop_size(w, h, large_threshold, medium_threshold)
    if target is None:
        crop_box = (0, 0, w, h)
        out = img
        crop_status = "kept_original_smaller_than_640"
    else:
        crop_box = roi_crop_box(w, h, target, roi_xyxy)
        roi_inside = box_contains(roi_xyxy, crop_box)
        if strict_roi_inside and not roi_inside:
            raise ValueError(
                f"ROI is not fully inside the computed {target}x{target} crop for {src.name}.\n"
                f"ROI={list(roi_xyxy)}, crop_box={list(crop_box)}.\n"
                "Please draw a tighter ROI around the real defect or disable strict_roi_inside_crop."
            )
        out = img.crop(crop_box)
        crop_status = f"roi_center_cropped_{target}x{target}"

    ensure_dir(dst.parent)
    out.save(dst)
    roi_inside = box_contains(roi_xyxy, crop_box)
    return {
        "source_path": str(src),
        "output_path": str(dst),
        "source_width": w,
        "source_height": h,
        "output_width": out.size[0],
        "output_height": out.size[1],
        "roi_xyxy": list(roi_xyxy),
        "crop_box_xyxy": list(map(int, crop_box)),
        "roi_fully_inside_crop": int(bool(roi_inside)),
        "crop_status": crop_status,
    }


def copy_without_crop(src: Path, dst: Path) -> dict[str, Any]:
    w, h = save_rgb_as_png(src, dst)
    return {
        "source_path": str(src),
        "output_path": str(dst),
        "source_width": w,
        "source_height": h,
        "output_width": w,
        "output_height": h,
        "roi_xyxy": "",
        "crop_box_xyxy": [0, 0, w, h],
        "roi_fully_inside_crop": "",
        "crop_status": "copied_without_crop",
    }


def parse_args() -> argparse.Namespace:
    root = project_root()
    p = argparse.ArgumentParser(description="Prepare images for mask_editor with optional ROI-centered crop.")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--class-name", help="Class name for folder/config/output naming")
    group.add_argument("--defect-type", help="Backward-compatible alias for --class-name")
    p.add_argument("--preprocess-config", type=Path, default=None, help="Default: configs/defect_types/<defect_type>/preprocess.json")
    p.add_argument("--enable-roi-crop", type=parse_bool_text, default=None, help="yes/no/auto. auto uses preprocess.json")
    p.add_argument("--raw-dir", type=Path, default=None, help="Default: data/00_raw_images/<defect_type>/")
    p.add_argument("--roi-manifest", type=Path, default=None, help="Default: data/00_roi_selected/<defect_type>/roi_manifest.csv")
    p.add_argument("--output-dir", type=Path, default=None, help="Default: data/01_inputs/<defect_type>/images/")
    p.add_argument("--manifest", type=Path, default=None, help="Default: data/01_inputs/<defect_type>/preprocess_manifest.csv")
    p.add_argument("--large-threshold", type=int, default=None)
    p.add_argument("--medium-threshold", type=int, default=None)
    p.add_argument("--strict-roi-inside-crop", action=argparse.BooleanOptionalAction, default=None)
    p.add_argument("--clear-output", action="store_true", help="Clear prepared images and dependent masks/editor outputs before writing.")
    p.add_argument("--allow-missing-roi-copy", action="store_true", help="If ROI crop is enabled but some raw images have no ROI row, copy them without crop instead of failing.")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    root = project_root()
    defect_type = sanitize_defect_type(args.class_name or args.defect_type)
    cfg = load_preprocess_config(defect_type, args.preprocess_config)

    enable_roi_crop = args.enable_roi_crop
    if enable_roi_crop is None:
        enable_roi_crop = bool(cfg.get("enable_roi_crop", True))

    raw_dir = args.raw_dir or resolve_project_path(root, cfg.get("raw_images_dir"), root / "data" / "00_raw_images" / defect_type)
    roi_manifest = args.roi_manifest or resolve_project_path(root, cfg.get("roi_manifest"), root / "data" / "00_roi_selected" / defect_type / "roi_manifest.csv")
    output_dir = args.output_dir or resolve_project_path(root, cfg.get("prepared_images_dir"), root / "data" / "01_inputs" / defect_type / "images")
    manifest = args.manifest or root / "data" / "01_inputs" / defect_type / "preprocess_manifest.csv"
    large_threshold = int(args.large_threshold if args.large_threshold is not None else cfg.get("large_threshold", 1280))
    medium_threshold = int(args.medium_threshold if args.medium_threshold is not None else cfg.get("medium_threshold", 640))
    strict_roi_inside = bool(args.strict_roi_inside_crop if args.strict_roi_inside_crop is not None else cfg.get("strict_roi_inside_crop", True))

    ensure_dir(raw_dir)
    ensure_dir(output_dir)

    if args.clear_output:
        clear_folder_images(output_dir)
        clear_dependent_masks(root, defect_type)

    raw_images = list_images(raw_dir)
    if not raw_images:
        raise SystemExit(f"[ERROR] No raw images found: {raw_dir}\nPut original images here first.")

    rows: list[dict[str, Any]] = []
    if enable_roi_crop:
        if not roi_manifest.exists():
            raise SystemExit(
                "[ERROR] ROI crop is enabled, but ROI manifest was not found.\n"
                f"Expected: {roi_manifest}\n\n"
                f"Run first:\n  python scripts/select_defect_roi.py --class-name {defect_type}\n\n"
                "Or disable crop:\n  python scripts/prepare_inputs.py --class-name " + defect_type + " --enable-roi-crop no --clear-output"
            )
        roi_rows = read_manifest_csv(roi_manifest)
        roi_by_path: dict[str, dict[str, str]] = {}
        for r in roi_rows:
            if str(r.get("has_roi", "1")).lower() in {"0", "false", "no"}:
                continue
            raw_path = Path(r.get("raw_path") or r.get("source_path") or r.get("image_path") or "")
            if not raw_path.is_absolute():
                raw_path = (root / raw_path).resolve()
            roi_by_path[str(raw_path.resolve())] = r

        for idx, src in enumerate(raw_images, start=1):
            key = str(src.resolve())
            dst = output_dir / f"{idx:04d}.png"
            if key not in roi_by_path:
                if not args.allow_missing_roi_copy:
                    raise SystemExit(
                        f"[ERROR] Missing ROI row for raw image: {src}\n"
                        "Open ROI selector and save one ROI for every image, or pass --allow-missing-roi-copy."
                    )
                row = copy_without_crop(src, dst)
                row["crop_status"] = "copied_without_crop_missing_roi"
            else:
                roi_xyxy = parse_box(roi_by_path[key].get("roi_xyxy") or "")
                row = crop_with_roi(src, dst, roi_xyxy, large_threshold, medium_threshold, strict_roi_inside)
            row["index"] = idx
            row["defect_type"] = defect_type
            row["raw_filename"] = src.name
            row["enable_roi_crop"] = int(True)
            rows.append(row)
            print(f"[PREP] {src.name} -> {dst.name} | {row['crop_status']} | output={row['output_width']}x{row['output_height']}")
    else:
        for idx, src in enumerate(raw_images, start=1):
            dst = output_dir / f"{idx:04d}.png"
            row = copy_without_crop(src, dst)
            row["index"] = idx
            row["defect_type"] = defect_type
            row["raw_filename"] = src.name
            row["enable_roi_crop"] = int(False)
            rows.append(row)
            print(f"[COPY] {src.name} -> {dst.name} | no crop | output={row['output_width']}x{row['output_height']}")

    fieldnames = [
        "index", "defect_type", "raw_filename", "enable_roi_crop", "source_path", "output_path",
        "source_width", "source_height", "output_width", "output_height", "roi_xyxy", "crop_box_xyxy",
        "roi_fully_inside_crop", "crop_status",
    ]
    write_manifest_csv(manifest, rows, fieldnames)
    print(f"[DONE] Prepared {len(rows)} image(s).")
    print(f"[MANIFEST] {manifest}")
    print(f"[NEXT] python tools/mask_editor.py --class-name {defect_type}")


if __name__ == "__main__":
    main()
