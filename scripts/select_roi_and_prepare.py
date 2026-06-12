#!/usr/bin/env python3
"""Step 1 helper: select rough defect ROI, then prepare cropped API input images.

This script keeps the first ROI-selection step in the workflow:
  raw full-size images -> ROI selector -> ROI-centered crop -> data/01_inputs/<defect_type>/images

It does not call the OpenAI API and does not consume credits.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Select defect ROI first, then prepare cropped images for gpt-image-2 editing.")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--class-name", help="Class name for folder/config/output naming")
    group.add_argument("--defect-type", help="Backward-compatible alias for --class-name")
    p.add_argument("--clear-output", action="store_true", default=True, help="Clear prepared images and old masks before writing. Default: on")
    p.add_argument("--no-clear-output", dest="clear_output", action="store_false", help="Do not clear existing prepared images/masks")
    p.add_argument("--allow-missing-roi-copy", action="store_true", help="Copy images without ROI rows instead of failing")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    root = project_root()
    py = sys.executable

    raw_dir = root / "data" / "00_raw_images" / (args.class_name or args.defect_type)
    if not raw_dir.exists():
        raw_dir.mkdir(parents=True, exist_ok=True)

    print("[STEP 1/2] ROI selection")
    print(f"[INFO] Put original images here first: {raw_dir}")
    subprocess.check_call([
        py,
        str(root / "scripts" / "select_defect_roi.py"),
        "--class-name",
        (args.class_name or args.defect_type),
    ])

    print("[STEP 2/2] Prepare cropped API input images")
    cmd = [
        py,
        str(root / "scripts" / "prepare_inputs.py"),
        "--class-name",
        (args.class_name or args.defect_type),
    ]
    if args.clear_output:
        cmd.append("--clear-output")
    if args.allow_missing_roi_copy:
        cmd.append("--allow-missing-roi-copy")
    subprocess.check_call(cmd)

    print("[DONE] Prepared images are ready at:")
    print(root / "data" / "01_inputs" / (args.class_name or args.defect_type) / "images")
    print("Next run:")
    print(f"  python scripts\\batch_from_folders.py --class-name {(args.class_name or args.defect_type)} --workflow prompt-only-edit --size 1280x1280 --quality high --num-outputs 1")


if __name__ == "__main__":
    main()
