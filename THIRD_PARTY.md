# 第三方模型来源与校验

这三个文件按随仓库提供的方案准备。权利归原作者；项目代码的许可证不自动适用于模型。

## 来源

| 仓库内文件 | 原文件 | 上游 |
| --- | --- | --- |
| `models/yolo_detect.onnx` | `best_v3.onnx` | [Text_select_captcha](https://github.com/MgArcher/Text_select_captcha/tree/master/model) |
| `models/ppocr.onnx` | `[Dddd]-OCR.onnx` | [AntiCAP](https://github.com/81NewArk/AntiCAP/tree/main/AntiCAP/AntiCAP-Models) |
| `models/charsets.txt` | `[Dddd]-CharSets.txt` | 同上 |

AntiCAP 的仓库根目录提供 [MIT LICENSE](https://github.com/81NewArk/AntiCAP/blob/main/LICENSE)。对权重的来源链和检测权重的再分发许可，目前仍未全部核实，不宣称本项目拥有或已获得全部模型授权。公开分发前应补齐适用的许可证和版权声明，不能仅用来源链接代替许可条件。

## 本地版本 SHA-256

以下用于核对本次随项目准备的文件，不表示已核对上游当前分支的二进制文件。

| 文件 | 字节数 | SHA-256 |
| --- | ---: | --- |
| `yolo_detect.onnx` | 9806720 | `a9804980ad250f236104c2befac980321de29799410847c52f5b855ac9de347d` |
| `ppocr.onnx` | 54088400 | `33b5cd351ee94e73a6bf8fa18c415ed8b819b3ffd342e267c30d8ad8334e34e8` |
| `charsets.txt` | 57250 | `1e891ea64ed699413c5716393d2ef1226c5be81b6b359b38470e263eac0ac160` |

更新权重时，应同时确认 OCR 字符表兼容性，并更新来源与校验信息。
