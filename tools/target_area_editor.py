#!/usr/bin/env python3
"""Target-area-only editor for constrained random defect placement.

Default input : data/01_inputs/<defect_type>/images/
Default output: data/01_inputs/<defect_type>/target_area_masks/

Use this when you already have source/prototype masks in data/01_inputs/<defect_type>/masks/
and only need to draw the allowed green area where random defects may be generated.
"""
from advanced_mask_editor import run_editor

if __name__ == "__main__":
    run_editor(pre_crop=False, target_only=True)
