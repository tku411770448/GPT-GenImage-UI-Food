#!/usr/bin/env python3
"""Batch-run OpenAI GPT Image 2 from mask_editor manifest output."""
from __future__ import annotations

import argparse
import csv
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_gpt_image2 import SUPPORTED_GPT_IMAGE_MODELS, project_root, sanitize_defect_type, sanitize_name


def make_child_run_name(base_run_name: str | None, image_stem: str, seed: int, total_jobs: int) -> str:
    if base_run_name and base_run_name.strip():
        clean = sanitize_name(base_run_name)
        if total_jobs == 1:
            return clean
        return sanitize_name(f"{clean}_{image_stem}_seed{seed}")
    return sanitize_name(f"{image_stem}_seed{seed}")


def run_and_tee(cmd: list[str], log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8", newline="") as log:
        header = f"\n===== RUN START {datetime.now().isoformat(timespec='seconds')} =====\n"
        print(header, end="")
        log.write(header)
        run_line = "[RUN] " + " ".join(cmd) + "\n"
        print(run_line, end="")
        log.write(run_line)
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
            print(line, end="")
            log.write(line)
            log.flush()
        return_code = proc.wait()
        footer = f"===== RUN END {datetime.now().isoformat(timespec='seconds')} return_code={return_code} =====\n"
        print(footer, end="")
        log.write(footer)
        if return_code != 0:
            raise subprocess.CalledProcessError(return_code, cmd)


def main() -> None:
    root = project_root()
    p = argparse.ArgumentParser(description="Batch OpenAI GPT Image 2 edits from data/02_mask_editor/<defect_type>/logs/mask_editor_manifest.csv")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--class-name", help="Class name for folder/config/output naming")
    group.add_argument("--defect-type", help="Backward-compatible alias for --class-name")
    p.add_argument("--manifest", type=Path, default=None)
    p.add_argument("--workflow", choices=["prompt-only-edit", "repair-and-random-generate", "repair-only", "generate-only"], default="repair-and-random-generate")
    p.add_argument("--placement-mode", choices=["fixed-mask", "random-in-target"], default="random-in-target")
    p.add_argument("--seed", type=int, default=5000)
    p.add_argument("--num-outputs", type=int, default=1)
    p.add_argument("--run-name", default=None, help="Custom output folder name under runs/<defect_type>/. Example: --run-name test_001")
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
    p.add_argument("--prompt-file", type=Path, default=None, help="Optional .txt prompt file. Default: configs/defect_types/<defect_type>/prompt.txt")
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

    defect_type = sanitize_defect_type(args.class_name or args.defect_type)
    manifest = args.manifest or root / "data" / "02_mask_editor" / defect_type / "logs" / "mask_editor_manifest.csv"
    if not manifest.exists():
        raise SystemExit(f"Manifest not found: {manifest}\nRun: python tools/mask_editor.py --class-name {defect_type}")

    rows = list(csv.DictReader(manifest.open("r", encoding="utf-8-sig", newline="")))
    if not rows:
        raise SystemExit(f"Manifest is empty: {manifest}")

    for idx, row in enumerate(rows):
        img = Path(row.get("image_path") or row.get("input_path") or "")
        mask = Path(row.get("mask_path") or row.get("source_mask_path") or "")
        target_area_text = row.get("target_area_path") or row.get("target_mask_path") or ""
        target_area = Path(target_area_text) if target_area_text else None
        seed = args.seed + idx * args.num_outputs
        child_run_name = make_child_run_name(args.run_name, img.stem, seed, len(rows))
        log_path = root / "runs" / defect_type / child_run_name / "log.txt"

        cmd = [
            sys.executable, str(root / "scripts" / "run_gpt_image2.py"),
            "--class-name", defect_type,
            "--image", str(img),
            "--workflow", args.workflow,
            "--placement-mode", args.placement_mode,
            "--seed", str(seed),
            "--num-outputs", str(args.num_outputs),
            "--run-name", child_run_name,
            "--model", args.model,
            "--size", args.size,
            "--quality", args.quality,
            "--output-format", args.output_format,
            "--background", args.background,
            "--mask-dilate", str(args.mask_dilate),
            "--mask-feather", str(args.mask_feather),
            "--auto-resize-multiple", str(args.auto_resize_multiple),
            "--min-defects", str(args.min_defects),
            "--max-defects", str(args.max_defects),
            "--random-scale-min", str(args.random_scale_min),
            "--random-scale-max", str(args.random_scale_max),
            "--placement-attempts", str(args.placement_attempts),
            "--exclude-repair-padding", str(args.exclude_repair_padding),
        ]
        if mask and str(mask) != ".":
            cmd += ["--mask", str(mask)]
        if target_area is not None and str(target_area) != ".":
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


if __name__ == "__main__":
    main()
