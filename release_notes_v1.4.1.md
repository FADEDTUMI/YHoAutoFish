# YHoAutoFish v1.4.1

v1.4.0 的稳定性修复版本，重点修复 Nuitka 打包后 OCR 模块加载失败、自动更新安装异常和强制更新下载链接错误。

## 修复

- **修复 Nuitka 打包后 OCR 模块因 torchvision/torch/scipy 被排除导致加载失败**：patch cnstd/cnocr 关键导入文件，将 PyTorch 生态依赖改为 try/except 安全导入，ONNX 推理路径不受影响。
- **修复自动更新安装后图鉴资源目录丢失**：Nuitka onefile 构建脚本补充 `fish_encyclopedia` 拷贝；更新器在清理旧版 `_internal` 目录前先迁移图鉴资源到根目录。
- **修复强制更新点击下载后报 `UpdateInfo got unexpected keyword argument 'url'`**：构造参数名 `url` 修正为 `download_url`，`download_update` 中 direct 模式读取字段同步修正。
- **修复强制更新下载完成后卡在"正在启动安装"**：改用 `start_external_update()` 解压并安装，不再直接 Popen zip 文件。
- **修复 `py main.py` 报 `No module named 'PySide6'`**：移除入口脚本 `#! python3` shebang，避免 Windows py 启动器选择 Anaconda 等无 PySide6 的 Python 环境。
- **修复 cv2 中文路径读取失败**：图鉴缩略图生成改用 `cv2.imdecode` / `cv2.imencode` 绕过 Windows 中文路径问题。

## 新功能

- 新增极简结算模式（高级设置 > 识别与判定），跳过结算界面识别直接进入下一轮。
- 新增消息中心，标题栏消息按钮，服务端通知收录在下拉面板中。
- 新增游戏分辨率检查，非 1920×1080 分辨率启动时弹窗提醒。

## 优化

- ESC 按键全局防抖，间隔 ≥300ms，自动售鱼两次 ESC 间隔 ≥1s。
- 新增 11 种鱼类/收藏品适配，OCR 混淆字符表扩充繁简转换映射。
- 缺失缩略图的图鉴资源启动时自动生成 160px 缓存。

## 升级说明

- 从 v1.4.0 覆盖升级：直接解压覆盖即可，用户数据保留。
- 从 v1.3.x 及更早版本升级：更新器会自动迁移图鉴资源目录并清理旧版 `_internal/`。

## 发布文件

- `YHoAutoFish-v1.4.1-windows.zip`：完整 Windows 发布包。
- `YHoAutoFish-v1.4.1-windows.zip.001`~`.004`：Gitee 国内源分卷包。
- `latest.json`：自动更新清单，需和发布包一起上传。
