<div align="center">
  <img src="logo.jpg" alt="YHo AutoFish" width="180">

# 异环自动钓鱼 YHo AutoFish

面向《异环》的 Windows 桌面自动钓鱼工具：自动抛竿、自动上钩、自动溜鱼、自动结算、自动记录战绩。

  <p>
    <img alt="Windows" src="https://img.shields.io/badge/Windows-10%20%2F%2011-0078D6?style=for-the-badge&logo=windows&logoColor=white">
    <img alt="Python" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white">
    <img alt="GUI" src="https://img.shields.io/badge/GUI-PySide6-41CD52?style=for-the-badge&logo=qt&logoColor=white">
    <img alt="Vision" src="https://img.shields.io/badge/Vision-OpenCV-5C3EE8?style=for-the-badge&logo=opencv&logoColor=white">
  </p>

  <p>
    <img alt="No Injection" src="https://img.shields.io/badge/no%20injection-screen%20vision-1DD0D6?style=flat-square">
    <img alt="Admin" src="https://img.shields.io/badge/admin-required-FF667E?style=flat-square">
    <img alt="Local Data" src="https://img.shields.io/badge/data-local%20only-6FE39A?style=flat-square">
    <img alt="Version" src="https://img.shields.io/badge/version-1.4.1-63E4E4?style=flat-square">
  </p>
</div>

## 重要声明

YHo AutoFish 仅用于图像识别、桌面自动化流程学习与个人技术研究。程序通过屏幕截图、模板识别、OCR 和普通键盘输入工作，不读取游戏内存，不注入 DLL，不修改游戏资源文件。

使用自动化工具仍可能违反游戏或平台规则，并可能带来账号、收益、设备环境或其他风险。请只在你充分理解并能自行承担后果的前提下使用。禁止商业代练、批量传播、二次售卖、卡密售卖、冒充官方工具或任何侵权用途。

本程序以源码可见（Source-Available）方式免费发布，禁止二次分发、再分发及商业使用。若你从付费渠道获得本程序，请立即停止付款，并优先从项目 Release 页面获取发布包。

## 1.4.1 版本重点

1.4.1 是 v1.4.0 的稳定性修复版本：全新 v2 授权认证系统、全局埋点追踪、战绩分享、强制更新机制，同时切换至 Nuitka 编译打包以增强代码安全保护。

- 全新 v2 JWT 认证系统，支持 token 刷新与多主机故障自动切换。
- 新增 ECDSA-P256 离线 License 验证，断网场景下仍可正常运行。
- 新增全局埋点追踪系统（EventTracker），支持离线 SQLite 队列与 WebSocket 实时上报。
- 新增战绩分享功能，支持截图保存与一键复制。
- 新增强制更新系统，支持 GitHub/Gitee 多源下载、分卷校验与 SHA256 验证。
- 新增累计钓获自动售鱼功能，达到阈值后自动出售。
- 新增极简结算模式，跳过结算界面识别直接进入下一轮，适合结算识别频繁出错的用户。
- 新增消息中心，服务端通知不再弹窗阻断，收录在标题栏消息按钮中。
- 新增游戏分辨率检查，非 1920×1080 分辨率启动时弹窗提醒效果无法保障。
- 切换打包方案至 Nuitka standalone，代码经 C 编译保护，反逆向能力显著增强。
- ESC 按键安全强化，全局防抖确保间隔 ≥300ms，自动售鱼两次 ESC 间隔 ≥1s。
- 新增 11 种鱼类/收藏品适配（星斑鱼、海星、海中栗、金梭子、红/蓝/绿鳞旗、泡水的异象市民证、灯塔、黑夜蝠鲼、条纹椰），OCR 混淆字符表扩充。

完整发布说明见 [release_notes_v1.4.1.md](release_notes_v1.4.1.md)。

## 下载与运行

推荐使用 GitHub Release 中的压缩包，不需要自己配置 Python 环境。

1. 下载 `YHoAutoFish-v1.4.1-windows.zip`。
2. 解压到一个固定目录，不要直接在压缩包内运行。
3. 打开《异环》，进入可以钓鱼的位置。
4. 运行 `YHoAutoFish.exe`。
5. Windows 弹出管理员权限确认时选择“是”。
6. 首次启动阅读用户协议和反侵权提示。
7. 点击“初始化模块”，完成后点击“开始钓鱼”。

从 1.2 起，程序会强制请求管理员权限。若拒绝 UAC 权限，程序不会继续运行。

## 使用前准备

- 系统：Windows 10 或 Windows 11。
- 游戏：保持《异环》窗口可见，不要最小化。
- 权限：游戏和工具都建议以管理员权限运行；1.2 会自动请求管理员权限。
- 显示：推荐 1920×1080 分辨率，非推荐分辨率下识别效果无法保障；保持游戏 UI 不被遮挡。
- 钓鱼点：角色站到钓鱼点，右下角能看到钓鱼交互提示。
- 运行中：不要遮挡顶部溜鱼 HUD、右下角交互 UI、上钩提示和结算界面。

## 核心功能

| 功能 | 说明 |
| --- | --- |
| 自动抛竿 | 识别右下角钓鱼交互状态后自动按 F |
| 自动上钩 | 识别上钩文字提示后迅速按 F |
| 自动溜鱼 | 根据绿色耐力条和黄色游标自动控制 A/D |
| 自动结算 | 识别成功结算或失败提示，自动记录并进入下一轮 |
| 捕获记录 | 保存鱼名、重量、时间、稀有度和统计信息 |
| 图鉴系统 | 显示鱼类资源、解锁状态和稀有度筛选 |
| 阶段总结 | 按新增记录生成阶段统计，不受历史记录数量影响 |
| 悬浮窗 | 支持展开/收起，显示状态和日志 |
| 高级设置 | 按分类调整溜鱼、流程、识别、安全接管等参数 |
| 在线更新 | 标题栏版本入口检查更新并一键安装 |
| 极简结算模式 | 跳过结算识别直接进入下一轮，适合结算识别频繁出错的用户 |
| 消息中心 | 标题栏消息按钮，服务端通知收录在下拉面板中，不再弹窗阻断 |

## 界面说明

- 钓鱼记录：查看捕获历史、筛选鱼类、统计重量和数量。
- 图鉴记录：按稀有度浏览鱼类，查看已解锁与未解锁状态。
- 运行日志：查看自动钓鱼状态、错误、恢复流程和调试信息。
- 高级设置：调整溜鱼控制、流程超时、识别判定和安全接管。
- 悬浮窗：游戏旁快速开始/停止，支持收起为横条。
- 关于：查看版本号、作者、项目地址、用户协议和反侵权协议。

## 默认参数建议

1.2.5 的默认参数已按当前游戏 A/D 移动速度调校。多数用户不需要修改。

| 情况 | 建议 |
| --- | --- |
| 游标跟不上耐力条 | 适当提高“跟鱼力度” |
| 反应不够及时 | 降低“跟鱼死区”或“中心安全区宽度” |
| 左右抖动过猛 | 略微提高“跟鱼死区”或“最短按键保持” |
| 白天或树林环境识别不稳 | 开启调试溜鱼视图，反馈截图 |
| 失败后长时间不继续 | 检查恢复超时、上钩等待超时设置 |
| 结算识别慢 | 降低成功结算检测间隔，但会增加截图匹配频率 |

## 在线更新

程序会在启动后后台自动检查一次更新，运行期间默认每 30 分钟轮询一次静态 `latest.json`。轮询只读取轻量清单，不调用 GitHub Release API；手动点击标题栏版本按钮会立即重新检查，不受自动轮询间隔影响。发现新版本后，标题栏版本按钮会变醒目，后台轮询会停止，避免反复提醒。

更新流程：

1. 发现新版本后，标题栏版本按钮变醒目。
2. 点击版本按钮，先确认用户协议和反侵权协议。
3. 查看更新说明，选择一键全自动更新。
4. 选择下载源：默认 GitHub 官方源，也可以切换到 Gitee 国内源。
5. 程序下载更新包并校验 SHA256。
6. 主程序退出，独立 `YHoUpdater.exe` 显示安装进度并覆盖程序文件。
7. 安装完成后停留在成功页面，由用户选择“启动新版”或“完成退出”。

更新工作目录为程序目录下的 `.updates/`，下载包、解压目录和更新器运行副本都会放在软件所在盘符，不再默认占用 C 盘 `%TEMP%`。更新完成后会清理下载包和解压目录，运行副本会在后续更新前按过期规则清理。

受保护的数据包括 `config.json`、`records.json`、`records.db`、`logs/`、`screenshots/`、`captures/`、`.updates/` 等。

国内网络访问 GitHub 不稳定时，可以在 `config.json` 中增加备用更新源，不需要改代码：

```json
{
  "update_manifest_urls": [
    "https://你的国内静态站/latest.json"
  ],
  "gitee_repository_url": "https://gitee.com/fadedtumi/YHoAutoFish",
  "update_gitee_manifest_urls": [
    "https://你的 Gitee latest.json 直链"
  ],
  "update_gitee_download_urls": [
    "https://gitee.com/fadedtumi/YHoAutoFish/releases/download/{tag}/{asset_name}"
  ],
  "update_download_urls": [
    "https://你的国内静态站/{asset_name}"
  ],
  "update_mirror_prefixes": [
    "https://你的-github-代理/"
  ],
  "update_startup_jitter_seconds": 20,
  "update_check_interval_minutes": 30
}
```

自动检查和手动检查更新都会优先读取 GitHub 官方 `latest.json`；如果 GitHub 不可用或 GitHub 当前没有比本地更新的版本，会继续尝试 Gitee。Gitee 默认会先请求 `https://gitee.com/api/v5/repos/fadedtumi/YHoAutoFish/releases/latest` 获取最新发行版标签，再读取 `https://gitee.com/fadedtumi/YHoAutoFish/releases/download/{tag}/latest.json`。如果直链 `latest.json` 偶发返回 502，程序会继续尝试 Gitee Release 附件下载接口。`update_check_interval_minutes` 控制后台轮询间隔，建议保持 30 分钟或更长；手动检查始终立即执行。`update_download_urls`、`update_gitee_download_urls` 支持 `{version}`、`{tag}`、`{asset_name}` 占位符。即使使用备用下载源，程序仍会按 `latest.json` 中的 SHA256 校验更新包，校验失败会拒绝安装。

Gitee 国内源使用连续分卷附件，例如 `YHoAutoFish-v1.4.1-windows.zip.001`、`.002`、`.003`。程序会自动下载所有分卷、校验 SHA256、合并为完整更新包并安装，普通用户不需要手动合并分卷。若下载过程中点击取消，程序会清理未完成的分卷、合并包和临时目录。

## 数据保存

用户数据保存在程序目录：

- `records.json`：捕获记录、图鉴解锁、统计数据。
- `config.json`：高级设置与运行偏好。
- `messages.json`：消息中心持久化存储。
- `logs/`：更新器日志等运行日志。

发布包不会内置作者测试记录。你可以备份 `records.json` 和 `config.json` 来保留自己的数据。

## 常见问题

| 问题 | 解决办法 |
| --- | --- |
| 程序启动要求管理员 | 这是 1.2 及后续版本的强制要求，点击 UAC 窗口“是” |
| 找不到游戏窗口 | 先启动游戏，确认窗口可见且进程为 `HTGame.exe` |
| 按键没有反应 | 确认管理员权限，避免游戏窗口失焦或被遮挡 |
| 不自动抛竿 | 确认角色在钓鱼点，右下角交互 UI 可见 |
| 溜鱼失败率高 | 先使用默认参数；必要时微调跟鱼力度、死区和安全区 |
| 鱼儿溜走后没有继续 | 查看运行日志中的恢复提示，确认回到可抛钩界面 |
| 鱼名或重量识别不准 | 保持结算界面无遮挡，必要时反馈调试截图 |
| 检查更新失败 | 可稍后手动检查；国内网络可配置 `update_manifest_urls` / `update_download_urls` 作为备用源 |
| 自动更新后数据还在吗 | 在受保护名单内的数据不会被覆盖 |
| `py main.py` 提示缺少 `PySide6` | 当前 `py` 默认 Python 可能缺少依赖，可尝试 `py -3.12 main.py` 或在对应 Python 环境中安装 PySide6 |

## 发行文件说明

普通用户只需要下载 `YHoAutoFish-v1.4.1-windows.zip`，解压后运行 `YHoAutoFish.exe`。

GitHub Release 提供完整压缩包；Gitee Release 可能因附件体积限制提供 `.zip.001`、`.zip.002` 等分卷。分卷主要用于程序自动更新，手动下载安装时建议优先使用 GitHub 完整压缩包；如果只能使用 Gitee 分卷，请下载所有连续分卷后按文件名顺序合并为完整 zip 再解压。

程序内的一键更新会自动处理 GitHub 完整包或 Gitee 分卷包，并在安装前校验 SHA256。校验失败时会拒绝安装，避免使用损坏或不匹配的更新包。

## 许可

本项目采用源码可见（Source-Available）自定义限制性许可证：[LICENSE](LICENSE)。

允许个人学习、研究、查看源码和本地非商业使用。本项目不是开源软件，源码公开仅为透明与审查目的。未经作者书面许可，禁止商用、二次分发、再分发、二次修改分发、打包转卖、转载镜像、改名发布、去除署名或制作衍生收费版本。

## 项目信息

- 作者：`FADEDTUMI`
- 项目地址：`https://github.com/FADEDTUMI/YHoAutoFish`
- 当前版本：`1.4.1`
