#!/usr/bin/env python3
"""Batch-run OpenAI GPT Image from image folders.

Default input structure:
  data/01_inputs/<defect_type>/images/<name>.png
  data/01_inputs/<defect_type>/masks/<name>.png
  data/01_inputs/<defect_type>/target_area_masks/<name>_target_area.png

Use --workflow prompt-only-edit for the simplest ChatGPT-like mode: image + prompt only.
Use --workflow target-area-edit only for legacy/optional Target Area mask edits.
Legacy: --workflow repair-and-random-generate still supports source mask + target_area guided relocation.
"""
from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_gpt_image2 import SUPPORTED_GPT_IMAGE_MODELS, project_root, sanitize_defect_type, sanitize_name

SUPPORTED = [".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"]


def list_images(folder: Path) -> list[Path]:
    return sorted([p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED]) if folder.exists() else []


def find_mask(image: Path, masks_dir: Path) -> Path | None:
    candidates = [masks_dir / f"{image.stem}{ext}" for ext in SUPPORTED]
    candidates += [masks_dir / f"{image.stem}_mask{ext}" for ext in SUPPORTED]
    candidates += [masks_dir / f"{image.stem}_source{ext}" for ext in SUPPORTED]
    for p in candidates:
        if p.exists():
            return p
    return None


def find_target_area(image: Path, target_dir: Path) -> Path | None:
    candidates = [target_dir / f"{image.stem}{ext}" for ext in SUPPORTED]
    candidates += [target_dir / f"{image.stem}_target_area{ext}" for ext in SUPPORTED]
    candidates += [target_dir / f"{image.stem}_area{ext}" for ext in SUPPORTED]
    for p in candidates:
        if p.exists():
            return p
    return None


def make_batch_run_name(base_run_name: str | None) -> str:
    """Return the parent batch folder name under runs/<class_name>/."""
    if base_run_name and base_run_name.strip():
        return sanitize_name(base_run_name)
    return "run_" + datetime.now().strftime("%Y%m%d_%H%M%S")


def make_child_run_name(image_stem: str, seed: int) -> str:
    """Create one child folder per output seed inside the batch run folder."""
    return sanitize_name(f"{image_stem}_seed{seed}")


def resolve_size_for_image(size: str, image: Path) -> str:
    # Keep same_as_original as a semantic mode. run_gpt_image2.py will turn it
    # into an API-valid size and resize final outputs back to the source size.
    s = str(size).strip().lower()
    if s in {"same_as_original", "original", "source"}:
        return "same_as_original"
    return size


def original_size_args_if_needed(size: str, image: Path) -> list[str]:
    if str(size).strip().lower() not in {"same_as_original", "original", "source"}:
        return []
    from PIL import Image
    with Image.open(image) as img:
        return ["--final-width", str(img.width), "--final-height", str(img.height)]


def run_and_tee(cmd: list[str], log_path: Path) -> None:
    """Run a subprocess while writing exactly what we print to log.txt."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8", newline="") as log:
        header = f"\n===== RUN START {datetime.now().isoformat(timespec='seconds')} =====\n"
        log.write(header)
        print(header, end="", flush=True)
        run_line = "[RUN] " + " ".join(cmd) + "\n"
        log.write(run_line)
        print(run_line, end="", flush=True)
        log.flush()

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            print(line, end="", flush=True)
            log.write(line)
            log.flush()
        return_code = proc.wait()
        footer = f"===== RUN END {datetime.now().isoformat(timespec='seconds')} return_code={return_code} =====\n"
        log.write(footer)
        print(footer, end="", flush=True)
        if return_code != 0:
            raise subprocess.CalledProcessError(return_code, cmd)


def main() -> None:
    root = project_root()
    p = argparse.ArgumentParser(description="Batch OpenAI GPT Image edits from data/01_inputs/<class_name>/ folders.")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--class-name", help="Class name for folder/config/output naming")
    group.add_argument("--defect-type", help="Backward-compatible alias for --class-name")
    p.add_argument("--images-dir", type=Path, default=None)
    p.add_argument("--masks-dir", type=Path, default=None)
    p.add_argument("--target-area-dir", type=Path, default=None)
    p.add_argument("--selected-stems-file", type=Path, default=None, help="Optional newline-delimited image stems to include in this batch. Used by UI prompt multi-selection.")
    p.add_argument("--workflow", choices=["prompt-only-edit", "target-area-edit", "repair-and-random-generate", "repair-only", "generate-only"], default="prompt-only-edit")
    p.add_argument("--placement-mode", choices=["fixed-mask", "random-in-target"], default="random-in-target")
    p.add_argument("--seed", type=int, default=5000)
    p.add_argument("--num-outputs", type=int, default=1, help="Per-image output count when --total-outputs is not used.")
    p.add_argument("--total-outputs", type=int, default=None, help="Total number of generated images across all input images. When set, jobs are distributed over inputs and stop at this total.")
    p.add_argument("--run-name", default=None, help="Custom output folder name under runs/<class_name>/. Example: --run-name test_001")
    p.add_argument("--output-dir", type=Path, default=None, help="Root folder for run outputs. Default: project root/runs")
    p.add_argument(
        "--model",
        default="gpt-image-2",
        choices=SUPPORTED_GPT_IMAGE_MODELS,
        help="OpenAI GPT Image model. Choices: " + ", ".join(SUPPORTED_GPT_IMAGE_MODELS),
    )
    p.add_argument("--size", default="1280x1280")
    p.add_argument("--quality", default="high", choices=["low", "medium", "high", "auto"])
    p.add_argument("--output-format", default="png", choices=["png", "jpeg", "webp"])
    p.add_argument("--output-compression", type=int, default=None)
    p.add_argument("--background", default="auto", choices=["auto", "opaque"])
    p.add_argument("--prompt", default="", help="Direct Chinese or English prompt. Overrides prompt file.")
    p.add_argument("--prompt-file", type=Path, default=None, help="Optional .txt prompt file. Default: configs/classes/<class_name>/prompt.txt")
    p.add_argument("--prompt-config", type=Path, default=None, help="Deprecated compatibility alias for --prompt-file")
    p.add_argument("--prompt-extra", default="")
    p.add_argument("--mask-dilate", type=int, default=0)
    p.add_argument("--mask-feather", type=int, default=3)
    p.add_argument("--auto-resize-multiple", type=int, default=16)
    p.add_argument("--min-defects", type=int, default=1)
    p.add_argument("--max-defects", type=int, default=3)
    p.add_argument("--random-scale-min", type=float, default=0.85)
    p.add_argument("--random-scale-max", type=float, default=1.15)
    p.add_argument("--placement-attempts", type=int, default=500)
    p.add_argument("--exclude-repair-padding", type=int, default=8)
    p.add_argument("--no-clip-mask-to-target", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if not args.dry_run:
        try:
            from openai import OpenAI as _OpenAI  # noqa: F401
        except Exception as exc:
            raise SystemExit(
                "Missing or incompatible dependency: openai. Install project requirements in the same Python environment that launches the UI:\n"
                f"  {sys.executable} -m pip install -r {root / 'requirements.txt'}\n"
                f"Original import error: {type(exc).__name__}: {exc}"
            )

    defect_type = sanitize_defect_type(args.class_name or args.defect_type)
    images_dir = args.images_dir or root / "data" / "01_inputs" / defect_type / "images"
    masks_dir = args.masks_dir or root / "data" / "01_inputs" / defect_type / "masks"
    target_area_dir = args.target_area_dir or root / "data" / "01_inputs" / defect_type / "target_area_masks"

    if not images_dir.exists():
        raise SystemExit(f"Images folder not found: {images_dir}\nRun: python scripts/init_defect_type.py --class-name {defect_type}")

    images = list_images(images_dir)
    if args.selected_stems_file and args.selected_stems_file.exists():
        selected_stems = {line.strip() for line in args.selected_stems_file.read_text(encoding="utf-8", errors="ignore").splitlines() if line.strip()}
        if selected_stems:
            images = [img for img in images if img.stem in selected_stems]
    if not images:
        raise SystemExit(f"No images found in: {images_dir}")

    needs_mask = args.workflow in {"repair-and-random-generate", "repair-only"} or (args.workflow == "generate-only" and args.placement_mode == "fixed-mask")
    needs_target = args.workflow in {"repair-and-random-generate", "target-area-edit"} or (args.workflow == "generate-only" and args.placement_mode == "random-in-target")

    jobs = []
    missing = []
    missing_target = []
    for img in images:
        mask = find_mask(img, masks_dir) if needs_mask and masks_dir.exists() else None
        target_area = find_target_area(img, target_area_dir) if needs_target and target_area_dir.exists() else None
        if needs_mask and mask is None:
            missing.append(img.name)
        elif needs_target and target_area is None:
            missing_target.append(img.name)
        else:
            jobs.append((img, mask, target_area))

    if missing:
        raise SystemExit(f"Missing matching masks for {len(missing)} image(s) in {masks_dir}:\n" + "\n".join(missing[:20]))
    if missing_target:
        raise SystemExit(f"This workflow requires target-area masks. Missing for {len(missing_target)} image(s) in {target_area_dir}:\n" + "\n".join(missing_target[:20]))

    # Build the execution plan.
    # Legacy behavior: --num-outputs means "per input image".
    # UI behavior: --total-outputs means "total images for the whole batch".
    plan = []
    if args.total_outputs is not None:
        total = max(1, int(args.total_outputs))
        full_rounds, remainder = divmod(total, max(1, len(jobs)))
        for idx, (img, mask, target_area) in enumerate(jobs):
            count = full_rounds + (1 if idx < remainder else 0)
            if count > 0:
                plan.append((img, mask, target_area, count))
    else:
        plan = [(img, mask, target_area, max(1, int(args.num_outputs))) for img, mask, target_area in jobs]

    emitted = 0
    batch_run_name = make_batch_run_name(args.run_name)
    output_root = args.output_dir or root / "runs"
    # Final structure: runs/<class_name>/<run_name>/<image_stem>_seedXXXX/<artifacts>
    batch_output_root = output_root / defect_type / batch_run_name

    for idx, (img, mask, target_area, output_count) in enumerate(plan):
        for local_i in range(max(1, int(output_count))):
            seed = args.seed + emitted
            child_run_name = make_child_run_name(img.stem, seed)
            log_path = batch_output_root / child_run_name / "log.txt"
            size_is_original = str(args.size).strip().lower() in {"same_as_original", "original", "source"}

            cmd = [
                sys.executable, "-u", str(root / "scripts" / "run_gpt_image2.py"),
                "--class-name", defect_type,
                "--image", str(img),
                "--workflow", args.workflow,
                "--placement-mode", args.placement_mode,
                "--seed", str(seed),
                "--num-outputs", "1",
                "--run-name", child_run_name,
                "--batch-run-name", batch_run_name,
                "--output-dir", str(batch_output_root),
                "--model", args.model,
                "--size", resolve_size_for_image(args.size, img),
                "--quality", args.quality,
                "--output-format", args.output_format,
                "--background", args.background,
                "--mask-dilate", str(args.mask_dilate),
                "--mask-feather", str(args.mask_feather),
                "--auto-resize-multiple", "1" if size_is_original else str(args.auto_resize_multiple),
                *original_size_args_if_needed(args.size, img),
                "--min-defects", str(args.min_defects),
                "--max-defects", str(args.max_defects),
                "--random-scale-min", str(args.random_scale_min),
                "--random-scale-max", str(args.random_scale_max),
                "--placement-attempts", str(args.placement_attempts),
                "--exclude-repair-padding", str(args.exclude_repair_padding),
            ]
            if mask is not None:
                cmd += ["--mask", str(mask)]
            if target_area is not None:
                cmd += ["--target-area", str(target_area)]
            if args.output_compression is not None:
                cmd += ["--output-compression", str(args.output_compression)]
            if args.no_clip_mask_to_target:
                cmd.append("--no-clip-mask-to-target")
            if args.prompt.strip():
                cmd += ["--prompt", args.prompt]
            if args.prompt_file:
                cmd += ["--prompt-file", str(args.prompt_file)]
            if args.prompt_config:
                cmd += ["--prompt-config", str(args.prompt_config)]
            if args.prompt_extra.strip():
                cmd += ["--prompt-extra", args.prompt_extra]
            if args.dry_run:
                cmd.append("--dry-run")

            run_and_tee(cmd, log_path)
            emitted += 1


if __name__ == "__main__":
    main()
