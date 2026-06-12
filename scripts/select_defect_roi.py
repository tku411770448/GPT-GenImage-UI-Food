#!/usr/bin/env python3
"""Select a rough defect ROI on original full-size images before optional cropping.

This step is used only when ROI-crop preprocessing is enabled.
It does not create an API edit mask. It only records a rectangle around the original
visible defect so that the next preprocessing step can crop a 1280x1280 or
640x640 square without losing the defect region.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

# Allow importing project tools when this file is executed from scripts/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from PIL import Image, ImageDraw, ImageTk
from utils import ensure_dir, list_images, project_root, sanitize_defect_type


def clamp_box(x1: int, y1: int, x2: int, y2: int, width: int, height: int) -> tuple[int, int, int, int] | None:
    left = max(0, min(x1, x2))
    upper = max(0, min(y1, y2))
    right = min(width, max(x1, x2))
    lower = min(height, max(y1, y2))
    if right - left < 2 or lower - upper < 2:
        return None
    return int(left), int(upper), int(right), int(lower)


class ROISelector:
    def __init__(self, root: tk.Tk, source_paths: list[Path], defect_type: str, output_root: Path, manifest_path: Path):
        self.root = root
        self.source_paths = source_paths
        self.defect_type = defect_type
        self.output_root = output_root
        self.preview_dir = ensure_dir(output_root / "previews")
        self.manifest_path = manifest_path
        self.current_index = 0
        self.states: dict[str, tuple[int, int, int, int]] = {}
        self.source_path: Path | None = None
        self.source: Image.Image | None = None
        self.tk_image = None
        self.width = 0
        self.height = 0
        self.scale = 1.0
        self.display_w = 0
        self.display_h = 0
        self.start_display: tuple[int, int] | None = None
        self.current_rect_id = None
        self.saved_after_last_edit = False

        self.root.title(f"Step 00 - Rough Defect ROI Selector [{defect_type}]")
        self.build_ui()
        self.bind_events()
        self.load_image(0)

    def build_ui(self) -> None:
        self.info_label = ttk.Label(self.root, text="")
        self.info_label.pack(anchor="w", padx=10, pady=(8, 4))

        toolbar = ttk.Frame(self.root)
        toolbar.pack(fill="x", padx=10, pady=4)
        ttk.Button(toolbar, text="← Prev", command=self.prev_image).pack(side="left", padx=(0, 4))
        ttk.Button(toolbar, text="Next →", command=self.next_image).pack(side="left", padx=4)
        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(toolbar, text="Clear Current ROI (C)", command=self.clear_current).pack(side="left", padx=4)
        ttk.Button(toolbar, text="Save Selected ROIs (Ctrl+S)", command=self.save_all).pack(side="left", padx=4)
        ttk.Button(toolbar, text="Exit (Enter)", command=self.close_from_enter).pack(side="left", padx=4)

        self.canvas = tk.Canvas(self.root, width=900, height=650, bg="black", cursor="crosshair", highlightthickness=1, highlightbackground="#666")
        self.canvas.pack(padx=10, pady=8)

        self.status_label = ttk.Label(self.root, text="", justify="left")
        self.status_label.pack(anchor="w", padx=10, pady=(0, 8))

    def bind_events(self) -> None:
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.root.bind_all("<Control-s>", lambda e: self.save_all())
        self.root.bind_all("<Control-S>", lambda e: self.save_all())
        self.root.bind_all("<Left>", lambda e: self.prev_image())
        self.root.bind_all("<Right>", lambda e: self.next_image())
        self.root.bind_all("c", lambda e: self.clear_current())
        self.root.bind_all("C", lambda e: self.clear_current())
        self.root.bind_all("<Return>", lambda e: self.close_from_enter())
        self.root.bind_all("<KP_Enter>", lambda e: self.close_from_enter())

    def current_key(self) -> str:
        assert self.source_path is not None
        return str(self.source_path.resolve())

    def load_image(self, index: int) -> None:
        index = max(0, min(len(self.source_paths) - 1, index))
        self.current_index = index
        self.source_path = self.source_paths[index]
        self.source = Image.open(self.source_path).convert("RGB")
        self.width, self.height = self.source.size
        max_display_w, max_display_h = 1240, 780
        self.scale = min(1.0, max_display_w / self.width, max_display_h / self.height)
        self.display_w = max(1, int(round(self.width * self.scale)))
        self.display_h = max(1, int(round(self.height * self.scale)))
        display = self.source.resize((self.display_w, self.display_h), Image.Resampling.LANCZOS)
        self.tk_image = ImageTk.PhotoImage(display)
        self.canvas.config(width=self.display_w, height=self.display_h)
        self.root.geometry(f"{min(self.display_w + 50, 1320)}x{min(self.display_h + 155, 980)}")
        self.redraw()
        self.update_labels()

    def update_labels(self) -> None:
        selected_count = len(self.states)
        current = self.states.get(self.current_key()) if self.source_path else None
        current_text = f"ROI: {current}" if current else "ROI: not selected"
        self.info_label.config(
            text=f"[{self.current_index + 1}/{len(self.source_paths)}] defect_type={self.defect_type} | source={self.source_path.name if self.source_path else ''} | size={self.width}x{self.height} | selected={selected_count} image(s)"
        )
        self.status_label.config(
            text="操作：在原始大圖上用滑鼠拖曳一個矩形，粗略框住瑕疵位置。這裡只框 ROI，不是精細 mask。\n"
                 "←/→ 切換圖片；C 清除目前 ROI；Ctrl+S 儲存；Enter 關閉。下一步會依 ROI 中心裁切 1280x1280 或 640x640。\n"
                 f"{current_text}"
        )

    def redraw(self) -> None:
        self.canvas.delete("all")
        if self.tk_image is not None:
            self.canvas.create_image(0, 0, anchor="nw", image=self.tk_image)
        box = self.states.get(self.current_key()) if self.source_path else None
        if box:
            x1, y1, x2, y2 = box
            self.canvas.create_rectangle(x1 * self.scale, y1 * self.scale, x2 * self.scale, y2 * self.scale,
                                         outline="#00ff66", width=3)

    def display_to_original(self, x: int, y: int) -> tuple[int, int]:
        ox = max(0, min(self.width - 1, int(round(x / max(self.scale, 1e-9)))))
        oy = max(0, min(self.height - 1, int(round(y / max(self.scale, 1e-9)))))
        return ox, oy

    def on_press(self, event) -> None:
        self.saved_after_last_edit = False
        self.start_display = (event.x, event.y)
        if self.current_rect_id is not None:
            self.canvas.delete(self.current_rect_id)
            self.current_rect_id = None

    def on_drag(self, event) -> None:
        if self.start_display is None:
            return
        if self.current_rect_id is not None:
            self.canvas.delete(self.current_rect_id)
        x1, y1 = self.start_display
        self.current_rect_id = self.canvas.create_rectangle(x1, y1, event.x, event.y, outline="#00ff66", width=3)

    def on_release(self, event) -> None:
        if self.start_display is None:
            return
        p1 = self.display_to_original(*self.start_display)
        p2 = self.display_to_original(event.x, event.y)
        box = clamp_box(p1[0], p1[1], p2[0], p2[1], self.width, self.height)
        if box is not None:
            self.states[self.current_key()] = box
        self.start_display = None
        self.current_rect_id = None
        self.redraw()
        self.update_labels()

    def prev_image(self) -> None:
        if self.current_index > 0:
            self.load_image(self.current_index - 1)

    def next_image(self) -> None:
        if self.current_index < len(self.source_paths) - 1:
            self.load_image(self.current_index + 1)

    def clear_current(self) -> None:
        if self.source_path is None:
            return
        self.states.pop(self.current_key(), None)
        self.saved_after_last_edit = False
        self.redraw()
        self.update_labels()

    def save_preview(self, source_path: Path, roi_xyxy: tuple[int, int, int, int], out_path: Path) -> None:
        img = Image.open(source_path).convert("RGB")
        draw = ImageDraw.Draw(img)
        draw.rectangle(roi_xyxy, outline=(0, 255, 100), width=max(4, img.width // 300))
        ensure_dir(out_path.parent)
        img.save(out_path)

    def save_all(self) -> None:
        selected_items = []
        for path in self.source_paths:
            key = str(path.resolve())
            if key in self.states:
                selected_items.append((path, self.states[key]))
        if not selected_items:
            messagebox.showwarning("No ROI", "尚未框選任何 ROI，請先框住至少一個瑕疵位置。")
            return

        ensure_dir(self.manifest_path.parent)
        ensure_dir(self.preview_dir)
        rows = []
        with self.manifest_path.open("w", newline="", encoding="utf-8-sig") as f:
            fieldnames = [
                "index", "defect_type", "raw_path", "raw_filename", "width", "height",
                "roi_xyxy", "roi_xywh", "roi_center_xy", "has_roi", "preview_path"
            ]
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for idx, (path, box) in enumerate(selected_items, start=1):
                with Image.open(path) as im:
                    w, h = im.size
                x1, y1, x2, y2 = box
                roi_xywh = [x1, y1, x2 - x1, y2 - y1]
                roi_center = [int(round((x1 + x2) / 2)), int(round((y1 + y2) / 2))]
                preview = self.preview_dir / f"{idx:04d}_roi_preview.png"
                self.save_preview(path, box, preview)
                row = {
                    "index": idx,
                    "defect_type": self.defect_type,
                    "raw_path": str(path.resolve()),
                    "raw_filename": path.name,
                    "width": w,
                    "height": h,
                    "roi_xyxy": list(box),
                    "roi_xywh": roi_xywh,
                    "roi_center_xy": roi_center,
                    "has_roi": 1,
                    "preview_path": str(preview),
                }
                writer.writerow(row)
                rows.append(row)
        self.saved_after_last_edit = True
        self.update_labels()
        messagebox.showinfo("Saved", f"已儲存 {len(rows)} 筆 ROI。\nmanifest: {self.manifest_path}\n\n下一步：python scripts/prepare_inputs.py --class-name {self.defect_type} --clear-output")
        print(f"[SAVED] {len(rows)} ROI row(s) -> {self.manifest_path}")

    def close_from_enter(self) -> None:
        if self.states and not self.saved_after_last_edit:
            answer = messagebox.askyesnocancel("Exit", "目前有 ROI 尚未儲存，是否先儲存再關閉？")
            if answer is None:
                return
            if answer is True:
                self.save_all()
                if not self.saved_after_last_edit:
                    return
        self.root.destroy()


def parse_args() -> argparse.Namespace:
    root = project_root()
    p = argparse.ArgumentParser(description="Select rough ROI on original full-size images before crop.")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--class-name", help="Class name for folder/config/output naming")
    group.add_argument("--defect-type", help="Backward-compatible alias for --class-name")
    p.add_argument("--input-dir", type=Path, default=None, help="Default: data/00_raw_images/<defect_type>/")
    p.add_argument("--output-root", type=Path, default=None, help="Default: data/00_roi_selected/<defect_type>/")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    rootp = project_root()
    cls = sanitize_defect_type(args.class_name or args.defect_type)
    input_dir = args.input_dir or rootp / "data" / "00_raw_images" / cls
    output_root = args.output_root or rootp / "data" / "00_roi_selected" / cls
    manifest_path = output_root / "roi_manifest.csv"
    ensure_dir(input_dir)
    ensure_dir(output_root)
    sources = list_images(input_dir)
    if not sources:
        msg = f"找不到原始圖片。請將圖片放到：{input_dir}"
        print(f"[ERROR] {msg}")
        try:
            messagebox.showerror("No images", msg)
        except Exception:
            pass
        sys.exit(1)
    print(f"[INFO] input_dir={input_dir}")
    print(f"[INFO] output_root={output_root}")
    print(f"[INFO] found {len(sources)} image(s)")
    tkroot = tk.Tk()
    ROISelector(tkroot, sources, cls, output_root, manifest_path)
    tkroot.mainloop()


if __name__ == "__main__":
    main()
