# YHoAutoFish v1.5.0 更新日志

## 🎉 重大新功能：后台钓鱼模式

v1.5.0 新增**后台钓鱼模式**，开启后游戏可以被其他窗口遮挡，用户可以在钓鱼的同时用电脑做其他事情。

### 后台截图
- 使用 **PrintWindow** (PW_CLIENTONLY | PW_RENDERFULLCONTENT) 实现后台窗口截图
- **帧级缓存**：每帧仅执行一次 PrintWindow，所有 ROI 从缓存裁切，性能优异
- GDI 资源智能缓存：窗口尺寸不变时零重建开销
- Buffer 预分配：避免高频内存分配

### 后台输入
- 使用 **PostMessage** + **WM_ACTIVATE** 实现后台按键/鼠标操作
- 每次发送按键前先发送 WM_ACTIVATE(WA_ACTIVE) 唤醒游戏消息循环
- 完整的虚拟键码映射表（44个常用键）
- 正确的 lParam 构造（scan code + transition bits）
- 鼠标点击通过 MAKELONG 坐标编码 + PostMessage 实现

### 悬浮窗增强
- 后台模式下悬浮窗可**自由拖动**（不再固定在游戏左上角）
- 拖动后位置锁定，不会被定时器 snap 回去
- 后台模式下悬浮窗保持可见（即使游戏被遮挡）
- 拖动时鼠标光标变为抓手样式提示

### 设置项
- 新增"后台钓鱼"设置分类
- 包含"后台钓鱼模式"开关 toggle
- 默认关闭，开启后自动禁用用户接管检测

### 前台模式零影响
- 所有后台模式代码通过 `if self._background_mode:` 分支隔离
- 关闭后台模式时，所有代码路径与 v1.4.2 完全一致
- 经过 5 轮全面代码审查确认

## ⚠️ 已知限制
- 后台模式**不支持最小化**游戏窗口（PrintWindow 无法截取最小化窗口）
- PostMessage 输入对部分使用 Raw Input 的游戏可能无效（可随时关闭后台模式回到前台）
- 后台截图延迟约 30-50ms/帧（前台模式约 2-3ms）

## 🔧 技术改进
- 新增 `WM_ACTIVATE` 窗口激活机制
- 新增帧级截图缓存 (`begin_frame` / `_crop_from_cache`)
- 新增 GDI 资源缓存管理 (`_ensure_gdi_resources` / `_release_gdi_resources`)
- 新增 None 输入防御（`key_down` / `key_up` 入口类型检查）
- 新增负坐标防御（`_bg_send_click` 中 `max(0, ...)` 保护）
- 线程安全改进：`_apply_background_mode` 受 `_input_lock` 保护
