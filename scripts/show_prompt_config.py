#!/usr/bin/env python3
"""Print the simple prompt used by the GPT Image 2 workflow."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_gpt_image2 import load_user_prompt, build_prompt_bundle, sanitize_defect_type


def main() -> None:
    p = argparse.ArgumentParser()
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--class-name", help="Class name for folder/config/output naming")
    group.add_argument("--defect-type", help="Backward-compatible alias for --class-name")
    p.add_argument("--size", default="1280x1280")
    p.add_argument("--prompt", default="")
    p.add_argument("--prompt-file", type=Path, default=None)
    args = p.parse_args()
    defect_type = sanitize_defect_type(args.class_name or args.defect_type)
    cfg = load_user_prompt(defect_type, args.prompt_file, args.prompt, args.size)
    bundle = build_prompt_bundle(cfg)
    print("[PROMPT SOURCE]", cfg["prompt_source"])
    print("\n========== USER PROMPT ==========")
    print(cfg["user_prompt"])
    print("\n========== EFFECTIVE API PROMPT ==========")
    print(bundle["prompt_only_edit_prompt"])


if __name__ == "__main__":
    main()
