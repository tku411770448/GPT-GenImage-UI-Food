#!/usr/bin/env python3
"""Mask editor wrapper for GPT Image API defect editing.

Default input/output:
  input : data/01_inputs/<defect_type>/images/
  output: data/02_mask_editor/<defect_type>/

Use the white Source mask as the original-defect repair mask.
Use the green Target area as the valid region for random new-defect placement.
"""
from advanced_mask_editor import run_editor

if __name__ == "__main__":
    run_editor(pre_crop=False, target_only=False)
