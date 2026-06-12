# GPT GenImage UI - Food Target Area Workflow

此版本將原本「工業瑕疵 ROI + Target Area」流程轉型為「食物 / 物件 Target Area 限定變化」流程。Step 3 僅保留 Target Area 框選，Step 4 改為食物變化 prompt；後端生成 workflow 使用 `target-area-edit`，把 Target Area mask 作為唯一允許編輯的範圍。

```mermaid
flowchart TD
    A[Step 0 Homepage<br/>建立 / 開啟 / 複製專案] --> B[Step 1 資料上傳<br/>上傳炸雞或其他食物圖像]
    B --> C[Step 2 裁切尺寸與圖像裁切<br/>使用原圖或裁切成指定輸出比例]
    C --> D[Step 3 Target Area 框選<br/>只保留 Target Area；可框選多個矩形或多邊形]
    D --> E[Step 4 Prompt 編輯<br/>套用食物變化模板並附加 Target Area 座標]
    E --> F[Step 5 模型參數<br/>設定模型、品質、尺寸、輸出張數與 Run name]
    F --> G[Step 6 Aggregate<br/>確認輸入圖、Target Area、Prompt 與模型設定]
    G --> H[Step 7 執行生成<br/>target-area-edit 使用 Target Area mask 限定編輯區]
    H --> I[Step 8 Export / 輸出<br/>整理生成圖與 YOLO / COCO 標註]
```
