#!/usr/bin/env python3
"""Preview random new-defect placement masks before calling OpenAI GPT Image API.

This creates effective masks constrained inside target_area and excludes the original repair mask area.
Use it before API generation to verify that random defects will not appear outside the user-drawn target area.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from run_gpt_image2 import (
    project_root,
    sanitize_defect_type,
    sanitize_name,
    ensure_dir,
    prepare_image_and_masks,
    build_random_mask_in_target,
    subtract_mask,
    make_mask_preview,
)


def parse_args():
    root = project_root()
    p = argparse.ArgumentParser(description="Preview random-in-target masks without calling the OpenAI API.")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--class-name", help="Class name for folder/config/output naming")
    group.add_argument("--defect-type", help="Backward-compatible alias for --class-name")
    p.add_argument("--image", type=Path, required=True)
    p.add_argument("--mask", type=Path, required=True, help="Original-defect repair/prototype mask")
    p.add_argument("--prototype-mask", type=Path, default=None, help="Optional prototype mask for new random defects; defaults to --mask")
    p.add_argument("--target-area", type=Path, required=True, help="Allowed area mask")
    p.add_argument("--seed", type=int, default=5000)
    p.add_argument("--num-outputs", type=int, default=4)
    p.add_argument("--min-defects", type=int, default=1)
    p.add_argument("--max-defects", type=int, default=3)
    p.add_argument("--random-scale-min", type=float, default=0.85)
    p.add_argument("--random-scale-max", type=float, default=1.15)
    p.add_argument("--placement-attempts", type=int, default=500)
    p.add_argument("--exclude-repair-padding", type=int, default=8)
    p.add_argument("--output-dir", type=Path, default=root / "runs")
    p.add_argument("--run-name", default=None)
    p.add_argument("--mask-threshold", type=int, default=127)
    p.add_argument("--auto-resize-multiple", type=int, default=16)
    p.add_argument("--width", type=int, default=None)
    p.add_argument("--height", type=int, default=None)
    return p.parse_args()


def main():
    args = parse_args()
    defect_type = sanitize_defect_type(args.class_name or args.defect_type)
    image, repair_mask, prototype_mask, target_area, prep_meta = prepare_image_and_masks(
        args.image,
        args.mask,
        target_area_path=args.target_area,
        prototype_mask_path=args.prototype_mask,
        mask_threshold=args.mask_threshold,
        auto_resize_multiple=args.auto_resize_multiple,
        width=args.width,
        height=args.height,
    )
    allowed = subtract_mask(target_area, repair_mask, padding=args.exclude_repair_padding)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_name = sanitize_name(args.run_name) if args.run_name else f"placement_preview_{timestamp}"
    run_dir = ensure_dir(args.output_dir / defect_type / run_name)
    image.save(run_dir / "input.png")
    repair_mask.save(run_dir / "original_repair_mask.png")
    prototype_mask.save(run_dir / "prototype_mask.png")
    target_area.save(run_dir / "target_area.png")
    allowed.save(run_dir / "random_allowed_area_excluding_original.png")
    make_mask_preview(image, repair_mask, color=(255, 0, 0)).save(run_dir / "original_repair_mask_preview.png")
    make_mask_preview(image, target_area, alpha=80, color=(0, 255, 0)).save(run_dir / "target_area_preview.png")
    make_mask_preview(image, allowed, alpha=80, color=(0, 180, 255)).save(run_dir / "random_allowed_area_preview.png")

    records = []
    for i in range(args.num_outputs):
        seed = args.seed + i
        eff, meta = build_random_mask_in_target(
            prototype_mask,
            allowed,
            seed=seed,
            min_defects=args.min_defects,
            max_defects=args.max_defects,
            scale_min=args.random_scale_min,
            scale_max=args.random_scale_max,
            placement_attempts=args.placement_attempts,
        )
        eff.save(run_dir / f"random_effective_mask_seed{seed}.png")
        make_mask_preview(image, eff).save(run_dir / f"random_effective_mask_preview_seed{seed}.png")
        records.append(meta)
        print(f"[OK] seed={seed}, placed={meta['placed_defects']}, bbox={meta['bbox_xyxy']}")

    (run_dir / "placement_preview_metadata.json").write_text(json.dumps({
        "defect_type": defect_type,
        "preprocess": prep_meta,
        "exclude_repair_padding": args.exclude_repair_padding,
        "records": records,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[DONE] preview folder: {run_dir}")


if __name__ == "__main__":
    main()
