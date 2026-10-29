# 本地验证码模型

拟发布版将本目录中的两个 ONNX 模型和字符表随仓库一起提供，使用者无需单独下载。

| 放置的文件名 | 对应上游文件 | 来源 |
| --- | --- | --- |
| `yolo_detect.onnx` | `best_v3.onnx` | [Text_select_captcha / model](https://github.com/MgArcher/Text_select_captcha/tree/master/model) |
| `ppocr.onnx` | `[Dddd]-OCR.onnx` | [AntiCAP / AntiCAP-Models](https://github.com/81NewArk/AntiCAP/tree/main/AntiCAP/AntiCAP-Models) |
| `charsets.txt` | `[Dddd]-CharSets.txt` | 同上 |

第一列是仓库内文件名，第二列用于追溯上游来源。OCR 模型与字符表配套提供，不要混用其他版本。文件校验信息见仓库根目录 `THIRD_PARTY.md`。

第三方文件保留原作者权利，本项目的代码许可证不改变其许可。维护者仍需在公开发布前完成权重分发条件核对；本地准备包含这些文件不代表已取得全部授权。

无需验证码的场馆不会加载模型；需要验证码而缺少文件时，程序会在开始预约前停止并提示。
