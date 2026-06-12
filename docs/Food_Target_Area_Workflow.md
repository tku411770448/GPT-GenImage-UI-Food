# GPT GenImage UI - Food Prompt Workflow

此版本將原本「工業瑕疵 ROI + Target Area」流程轉型為「食物 / 物件 prompt-only 編輯」流程。裁切或使用原圖後會直接進入 Prompt 編輯；後端生成 workflow 使用 `prompt-only-edit`，不再要求 bbox、segment 或 Target Area mask。

```mermaid
flowchart TD
    A[Step 0 Homepage<br/>建立 / 開啟 / 複製專案] --> B[Step 1 資料上傳<br/>上傳炸雞或其他食物圖像]
    B --> C[Step 2 裁切尺寸與圖像裁切<br/>使用原圖或裁切成指定輸出比例]
    C --> D[Step 3 Prompt 編輯<br/>套用食物變化模板；不附加 Target Area 座標]
    D --> E[Step 4 模型參數<br/>設定模型、品質、尺寸、輸出張數與 Run name]
    E --> F[Step 5 Aggregate<br/>確認輸入圖、Prompt 與模型設定]
    F --> G[Step 6 執行生成<br/>prompt-only-edit 使用圖像與 prompt 生成]
    G --> H[Step 7 Export / 輸出<br/>整理生成圖與 YOLO / COCO 標註]
```
