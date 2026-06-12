#!/usr/bin/env python3
"""Create folder structure and a simple Chinese prompt for one class name."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def sanitize_defect_type(name: str) -> str:
    s = str(name).strip().replace(" ", "_")
    s = re.sub(r"[^A-Za-z0-9_.-]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("._-")
    if not s:
        raise ValueError("class_name cannot be empty")
    return s


def default_prompt_text(class_name: str, size: str = "1280x1280") -> str:
    return (
        f"這張圖的 Class Name 為 `{class_name}`\n"
        "請根據 ROI 與 target area 位置資訊進行圖像編輯。\n"
        "請把 ROI 所框選位置中的瑕疵或指定物件去除，接著將 ROI 所代表的視覺特徵，\n"
        "在 target area 範圍內隨機生成 1~4 個相似目標，並重新生成。\n"
        "不要將目標生成到 target area 之外。\n"
        "請不要保留 ROI 框線、target area 框線、標註框、文字、箭頭或任何提示標記，\n"
        "請保持原始背景、材質、光照、相機角度、紋理與整體風格自然一致。"
    )


def default_preprocess(defect_type: str) -> dict:
    return {
        "class_name": defect_type,
        "defect_type": defect_type,
        "enable_roi_crop": True,
        "large_threshold": 1280,
        "medium_threshold": 640,
        "strict_roi_inside_crop": True,
        "raw_images_dir": f"data/00_raw_images/{defect_type}",
        "roi_manifest": f"data/00_roi_selected/{defect_type}/roi_manifest.csv",
        "prepared_images_dir": f"data/01_inputs/{defect_type}/images",
        "note": "ROI crop/source mask/target area flow is retained for optional controlled editing.",
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Initialize one dynamic <class_name> workspace.")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--class-name", help="Example: stain, black_spot, product_class")
    group.add_argument("--defect-type", help="Backward-compatible alias for --class-name")
    p.add_argument("--size", default="1280x1280")
    p.add_argument("--force", action="store_true", help="Overwrite existing prompt files")
    args = p.parse_args()

    root = project_root()
    defect_type = sanitize_defect_type(args.class_name or args.defect_type)

    dirs = [
        root / "data" / "00_raw_images" / defect_type,
        root / "data" / "00_roi_selected" / defect_type,
        root / "data" / "01_inputs" / defect_type / "images",
        root / "data" / "01_inputs" / defect_type / "masks",
        root / "data" / "01_inputs" / defect_type / "target_area_masks",
        root / "data" / "01_inputs" / defect_type / "preprocess",
        root / "data" / "02_mask_editor" / defect_type,
        root / "runs" / defect_type,
        root / "configs" / "classes" / defect_type,
        root / "configs" / "defect_types" / defect_type,
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
        (d / ".gitkeep").write_text("", encoding="utf-8")

    config_dir = root / "configs" / "classes" / defect_type
    legacy_config_dir = root / "configs" / "defect_types" / defect_type
    config_dir.mkdir(parents=True, exist_ok=True)
    legacy_config_dir.mkdir(parents=True, exist_ok=True)
    prompt_txt = config_dir / "prompt.txt"
    prompt_json = config_dir / "prompt.json"
    preprocess_json = config_dir / "preprocess.json"

    prompt = default_prompt_text(defect_type, args.size)
    if not prompt_txt.exists() or args.force:
        prompt_txt.write_text(prompt, encoding="utf-8")
        print(f"[OK] wrote simple prompt: {prompt_txt}")
    else:
        print(f"[SKIP] prompt already exists: {prompt_txt}")

    # JSON is kept only for compatibility with old commands; it contains one simple user_prompt field.
    if not prompt_json.exists() or args.force:
        prompt_json.write_text(json.dumps({"class_name": defect_type,
        "defect_type": defect_type, "user_prompt": prompt}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[OK] wrote compatibility prompt.json: {prompt_json}")
    else:
        print(f"[SKIP] compatibility prompt.json already exists: {prompt_json}")

    if not preprocess_json.exists() or args.force:
        preprocess_json.write_text(json.dumps(default_preprocess(defect_type), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[OK] wrote preprocess config: {preprocess_json}")

    # Mirror configs to legacy configs/defect_types for older helper scripts.
    for src in [prompt_txt, prompt_json, preprocess_json]:
        if src.exists():
            dst = legacy_config_dir / src.name
            if not dst.exists() or args.force:
                dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    print("[OK] created workspace. Step 1 put original full-size images into:")
    print(f"  data/00_raw_images/{defect_type}/")
    print("Then select ROI and prepare API input images:")
    print(f"  python scripts\\select_roi_and_prepare.py --class-name {defect_type}")
    print("Then run GPT image editing:")
    print(f"  python scripts\\batch_from_folders.py --class-name {defect_type} --workflow prompt-only-edit --size {args.size} --quality high --num-outputs 1")


if __name__ == "__main__":
    main()
