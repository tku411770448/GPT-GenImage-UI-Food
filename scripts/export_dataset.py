#!/usr/bin/env python3
"""Export generated GPT Image defect outputs to image folder, COCO JSON and YOLO labels.

This exporter is intentionally metadata-first. It reads runs/<defect_type>/**/metadata.json
created by run_gpt_image2.py and uses placement_records[*].bbox_xyxy when available.
For prompt-only-edit runs, bounding boxes are usually unavailable; images are still copied
and COCO/YOLO annotations remain empty for those images.
"""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def unique_name(base: str, used: set[str]) -> str:
    if base not in used:
        used.add(base)
        return base
    stem = Path(base).stem
    suffix = Path(base).suffix
    i = 1
    while True:
        candidate = f"{stem}_{i:03d}{suffix}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        i += 1


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def normalize_xyxy(bbox: list[float] | tuple[float, ...] | None, width: int, height: int) -> tuple[float, float, float, float] | None:
    if not bbox or len(bbox) != 4:
        return None
    x1, y1, x2, y2 = [float(v) for v in bbox]
    x1 = max(0.0, min(x1, width - 1))
    y1 = max(0.0, min(y1, height - 1))
    x2 = max(0.0, min(x2, width))
    y2 = max(0.0, min(y2, height))
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2


def scale_xyxy(
    bbox: list[float] | tuple[float, ...] | None,
    source_width: int | float | None,
    source_height: int | float | None,
    target_width: int,
    target_height: int,
) -> list[float] | tuple[float, ...] | None:
    if not bbox or len(bbox) != 4:
        return bbox
    try:
        sw = float(source_width or 0)
        sh = float(source_height or 0)
    except Exception:
        return bbox
    if sw <= 0 or sh <= 0 or (int(round(sw)) == int(target_width) and int(round(sh)) == int(target_height)):
        return bbox
    sx = float(target_width) / sw
    sy = float(target_height) / sh
    x1, y1, x2, y2 = [float(v) for v in bbox]
    return [x1 * sx, y1 * sy, x2 * sx, y2 * sy]


def main() -> None:
    root = project_root()
    p = argparse.ArgumentParser(description="Export GPT Image class runs to COCO/YOLO datasets.")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--class-name", help="Class name for folder/config/output naming")
    group.add_argument("--defect-type", help="Backward-compatible alias for --class-name")
    p.add_argument("--runs-root", type=Path, default=None)
    p.add_argument("--export-root", type=Path, default=None)
    p.add_argument("--latest-only", action="store_true")
    p.add_argument("--run-name", default="", help="Export only the run/batch with this name. UI child runs are grouped by batch_run_name.")
    p.add_argument("--max-images", type=int, default=None, help="Maximum number of final generated images to export.")
    p.add_argument("--copy-images", action="store_true")
    p.add_argument("--coco", action="store_true")
    p.add_argument("--yolo", action="store_true")
    p.add_argument("--class-id", type=int, default=0)
    args = p.parse_args()

    defect_type = (args.class_name or args.defect_type).strip()
    runs_root = args.runs_root or root / "runs" / defect_type
    if not runs_root.exists():
        raise SystemExit(f"Runs folder not found: {runs_root}")

    all_metas = sorted(runs_root.rglob("metadata.json"), key=lambda x: x.stat().st_mtime, reverse=True)

    def meta_group_key(path: Path) -> str:
        meta = read_json(path)
        batch = str(meta.get("batch_run_name") or "").strip()
        if batch:
            return batch
        parent = path.parent.name
        # Backward-compatible grouping for older UI child runs such as
        # test_001_image_stem_seed5000.
        if "_seed" in parent:
            return parent.split("_seed", 1)[0]
        return parent

    metas = all_metas
    run_name = str(args.run_name or "").strip()
    if run_name:
        metas = []
        for m in all_metas:
            meta = read_json(m)
            batch = str(meta.get("batch_run_name") or "").strip()
            parent = m.parent.name
            if batch == run_name or parent == run_name or parent.startswith(run_name + "_"):
                metas.append(m)
    elif args.latest_only and all_metas:
        latest_group = meta_group_key(all_metas[0])
        metas = [m for m in all_metas if meta_group_key(m) == latest_group]

    if not metas:
        raise SystemExit(f"No metadata.json found under: {runs_root}")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    export_dir = ensure_dir(args.export_root or root / "exports" / defect_type / timestamp)
    images_dir = ensure_dir(export_dir / "images")
    labels_dir = ensure_dir(export_dir / "labels")
    ann_dir = ensure_dir(export_dir / "annotations")

    categories = [{"id": int(args.class_id), "name": defect_type, "supercategory": "class"}]
    coco = {"info": {"description": "GPT GenImage export", "date_created": timestamp}, "images": [], "annotations": [], "categories": categories}
    image_id = 1
    ann_id = 1
    used_names: set[str] = set()
    copied = 0
    annotated = 0

    for meta_path in metas:
        if args.max_images is not None and copied >= max(0, int(args.max_images)):
            break
        meta = read_json(meta_path)
        run_dir = meta_path.parent
        outputs = meta.get("final_outputs") or meta.get("outputs") or []
        placements = meta.get("placement_records") or []
        # Some workflows only have per_seed records. Placement records are mapped by order.
        for idx, out in enumerate(outputs):
            if args.max_images is not None and copied >= max(0, int(args.max_images)):
                break
            out_path = Path(out)
            if not out_path.is_absolute():
                out_path = (run_dir / out_path).resolve() if not (root / out).exists() else (root / out).resolve()
            if not out_path.exists() or out_path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                continue
            try:
                with Image.open(out_path) as img:
                    width, height = img.size
            except Exception:
                continue
            safe_name = unique_name(f"{run_dir.name}_{out_path.name}", used_names)
            dst = images_dir / safe_name
            if args.copy_images:
                shutil.copy2(out_path, dst)
                file_name = f"images/{safe_name}"
            else:
                file_name = str(out_path)
            coco["images"].append({"id": image_id, "file_name": file_name, "width": width, "height": height})

            # Find bbox: prefer same-index placement record, otherwise the first bbox in this run.
            bbox_xyxy = None
            if idx < len(placements):
                bbox_xyxy = placements[idx].get("bbox_xyxy") if isinstance(placements[idx], dict) else None
            if bbox_xyxy is None:
                for rec in placements:
                    if isinstance(rec, dict) and rec.get("bbox_xyxy"):
                        bbox_xyxy = rec.get("bbox_xyxy")
                        break
            bbox_source_w = meta.get("bbox_source_width") or meta.get("preprocess", {}).get("final_width")
            bbox_source_h = meta.get("bbox_source_height") or meta.get("preprocess", {}).get("final_height")
            bbox_xyxy = scale_xyxy(bbox_xyxy, bbox_source_w, bbox_source_h, width, height)
            xyxy = normalize_xyxy(bbox_xyxy, width, height)
            yolo_lines: list[str] = []
            if xyxy is not None:
                x1, y1, x2, y2 = xyxy
                bw, bh = x2 - x1, y2 - y1
                coco["annotations"].append({"id": ann_id, "image_id": image_id, "category_id": int(args.class_id), "bbox": [round(x1, 3), round(y1, 3), round(bw, 3), round(bh, 3)], "area": round(bw * bh, 3), "iscrowd": 0})
                ann_id += 1
                xc = (x1 + x2) / 2 / width
                yc = (y1 + y2) / 2 / height
                yolo_lines.append(f"{int(args.class_id)} {xc:.6f} {yc:.6f} {bw/width:.6f} {bh/height:.6f}")
                annotated += 1
            if args.yolo:
                (labels_dir / f"{Path(safe_name).stem}.txt").write_text("\n".join(yolo_lines) + ("\n" if yolo_lines else ""), encoding="utf-8")
            copied += 1
            image_id += 1

    if args.coco:
        (ann_dir / "coco.json").write_text(json.dumps(coco, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.yolo:
        (export_dir / "data.yaml").write_text(f"path: {export_dir.as_posix()}\ntrain: images\nval: images\nnames:\n  {int(args.class_id)}: {defect_type}\n", encoding="utf-8")
    manifest = {"class_name": defect_type, "defect_type": defect_type, "runs_root": str(runs_root), "metadata_files": [str(m) for m in metas], "export_dir": str(export_dir), "images_exported": copied, "annotations_exported": annotated, "coco": bool(args.coco), "yolo": bool(args.yolo), "copy_images": bool(args.copy_images), "latest_only": bool(args.latest_only), "run_name": run_name, "max_images": args.max_images}
    (export_dir / "export_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[INFO] images_exported={copied}")
    print(f"[INFO] annotations_exported={annotated}")
    print(f"[DONE] export_dir={export_dir}")


if __name__ == "__main__":
    main()
