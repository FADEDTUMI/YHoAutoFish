import mss
import mss.exception
import numpy as np
import time
import ctypes
import ctypes.wintypes


# 模块级定义，避免每次函数调用重复创建类
class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.wintypes.DWORD),
        ("biWidth", ctypes.wintypes.LONG),
        ("biHeight", ctypes.wintypes.LONG),
        ("biPlanes", ctypes.wintypes.WORD),
        ("biBitCount", ctypes.wintypes.WORD),
        ("biCompression", ctypes.wintypes.DWORD),
        ("biSizeImage", ctypes.wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.wintypes.LONG),
        ("biYPelsPerMeter", ctypes.wintypes.LONG),
        ("biClrUsed", ctypes.wintypes.DWORD),
        ("biClrImportant", ctypes.wintypes.DWORD),
    ]


class ScreenCapture:
    """屏幕截图工具类，使用 mss 实现高频低延迟截图（前台模式）
    或 PrintWindow + 帧缓存实现后台窗口截图（后台模式）"""

    # PrintWindow 标志: PW_CLIENTONLY(1) | PW_RENDERFULLCONTENT(2)
    PW_CLIENTONLY = 0x00000001
    PW_RENDERFULLCONTENT = 0x00000002
    PW_FLAGS = PW_CLIENTONLY | PW_RENDERFULLCONTENT  # = 3

    def __init__(self):
        self.sct = None
        self._failure_count = 0
        self._last_error_log_time = 0
        # --- 后台模式 ---
        self._background_mode = False
        self._target_hwnd = None
        self._origin_x = 0
        self._origin_y = 0
        self._pw_warned = False
        # --- 帧级缓存 ---
        self._frame_cache = None       # 当前帧的完整客户区 BGR numpy 数组
        self._frame_cache_origin = (0, 0)
        # --- GDI 资源缓存 ---
        self._gdi_hwnd = None
        self._gdi_hdc_window = None
        self._gdi_hdc_mem = None
        self._gdi_hbmp = None
        self._gdi_old_bmp = None
        self._gdi_w = 0
        self._gdi_h = 0
        # --- 预分配 buffer ---
        self._pw_buf = None
        self._pw_buf_size = 0
        self._recreate_sct()

    def _new_mss(self):
        return mss.mss()

    def _recreate_sct(self):
        old_sct = getattr(self, "sct", None)
        if old_sct is not None:
            try:
                old_sct.close()
            except Exception:
                pass
        self.sct = None
        try:
            self.sct = self._new_mss()
            return True
        except Exception as exc:
            self._log_capture_error("初始化 mss 截图后端失败", exc)
            return False

    def _log_capture_error(self, prefix, exc):
        self._failure_count += 1
        now = time.time()
        should_log = self._failure_count <= 3 or now - self._last_error_log_time >= 2.0
        if should_log:
            print(f"[ScreenCapture] {prefix}: {exc} (连续失败 {self._failure_count} 次，正在重建截图句柄)")
            self._last_error_log_time = now

    def close(self):
        """释放所有截图资源"""
        sct = getattr(self, "sct", None)
        if sct is not None:
            try:
                sct.close()
            except Exception:
                pass
        self.sct = None
        self._release_gdi_resources()

    # ------------------------------------------------------------------
    #  后台模式：GDI 资源管理
    # ------------------------------------------------------------------

    def _release_gdi_resources(self):
        """释放缓存的 GDI 资源"""
        try:
            if self._gdi_old_bmp is not None and self._gdi_hdc_mem:
                ctypes.windll.gdi32.SelectObject(self._gdi_hdc_mem, self._gdi_old_bmp)
            if self._gdi_hbmp:
                ctypes.windll.gdi32.DeleteObject(self._gdi_hbmp)
            if self._gdi_hdc_mem:
                ctypes.windll.gdi32.DeleteDC(self._gdi_hdc_mem)
            if self._gdi_hdc_window and self._gdi_hwnd:
                ctypes.windll.user32.ReleaseDC(self._gdi_hwnd, self._gdi_hdc_window)
        except Exception:
            pass
        self._gdi_hwnd = None
        self._gdi_hdc_window = None
        self._gdi_hdc_mem = None
        self._gdi_hbmp = None
        self._gdi_old_bmp = None
        self._gdi_w = 0
        self._gdi_h = 0

    def _ensure_gdi_resources(self, hwnd, w, h):
        """确保 GDI 资源就绪，窗口尺寸变化时重建。返回 True 表示成功。"""
        if (self._gdi_hwnd == hwnd and self._gdi_w == w and self._gdi_h == h
                and self._gdi_hdc_mem):
            return True

        self._release_gdi_resources()
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32

        hdc_window = user32.GetDC(hwnd)
        if not hdc_window:
            return False
        hdc_mem = gdi32.CreateCompatibleDC(hdc_window)
        if not hdc_mem:
            user32.ReleaseDC(hwnd, hdc_window)
            return False
        hbmp = gdi32.CreateCompatibleBitmap(hdc_window, w, h)
        if not hbmp:
            gdi32.DeleteDC(hdc_mem)
            user32.ReleaseDC(hwnd, hdc_window)
            return False
        old_bmp = gdi32.SelectObject(hdc_mem, hbmp)

        self._gdi_hwnd = hwnd
        self._gdi_hdc_window = hdc_window
        self._gdi_hdc_mem = hdc_mem
        self._gdi_hbmp = hbmp
        self._gdi_old_bmp = old_bmp
        self._gdi_w = w
        self._gdi_h = h
        return True

    # ------------------------------------------------------------------
    #  后台模式：帧级缓存
    # ------------------------------------------------------------------

    def set_background_mode(self, enabled, hwnd=None):
        """设置后台截图模式。"""
        self._background_mode = bool(enabled)
        self._target_hwnd = hwnd if enabled else None
        self._pw_warned = False
        self._frame_cache = None
        if not enabled:
            self._release_gdi_resources()

    def set_capture_origin(self, client_left, client_top):
        """设置客户区原点屏幕坐标，状态机每帧调用一次。"""
        self._origin_x = int(client_left)
        self._origin_y = int(client_top)

    def begin_frame(self):
        """每帧开始时调用一次，执行一次 PrintWindow 截取完整客户区。
        后续所有 capture_roi 调用从缓存裁切，零额外 PrintWindow 开销。"""
        self._frame_cache = None
        if not self._background_mode or not self._target_hwnd:
            return

        hwnd = self._target_hwnd
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32

        # 获取客户区尺寸
        cr = ctypes.wintypes.RECT()
        if not user32.GetClientRect(hwnd, ctypes.byref(cr)):
            return
        w, h = cr.right, cr.bottom
        if w <= 0 or h <= 0:
            return

        # 确保 GDI 资源就绪（尺寸不变时不重建）
        if not self._ensure_gdi_resources(hwnd, w, h):
            return

        # PrintWindow
        result = user32.PrintWindow(hwnd, self._gdi_hdc_mem, self.PW_FLAGS)
        if not result:
            if not self._pw_warned:
                self._pw_warned = True
                print("[ScreenCapture] 警告: PrintWindow 失败。请确保游戏为窗口化/无边框模式。")
            return

        # 预分配 buffer
        buf_size = w * h * 4
        if self._pw_buf_size < buf_size:
            self._pw_buf = ctypes.create_string_buffer(buf_size)
            self._pw_buf_size = buf_size

        # BITMAPINFO
        bmi = _BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.biWidth = w
        bmi.biHeight = -h  # 自上而下
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = 0

        copied = gdi32.GetDIBits(self._gdi_hdc_mem, self._gdi_hbmp, 0, h,
                                  self._pw_buf, ctypes.byref(bmi), 0)
        if copied == 0:
            return

        # BGRA → BGR（只裁切一次，整帧共享）
        full_img = np.frombuffer(self._pw_buf, dtype=np.uint8).reshape((h, w, 4))
        self._frame_cache = np.ascontiguousarray(full_img[:, :, :3])
        self._frame_cache_origin = (self._origin_x, self._origin_y)

    def _crop_from_cache(self, left, top, width, height):
        """从帧缓存中裁切 ROI（屏幕绝对坐标）"""
        cache = self._frame_cache
        if cache is None:
            return None
        ox, oy = self._frame_cache_origin
        h, w = cache.shape[:2]

        x1 = max(0, min(int(left) - ox, w))
        y1 = max(0, min(int(top) - oy, h))
        x2 = max(x1, min(x1 + int(width), w))
        y2 = max(y1, min(y1 + int(height), h))

        roi = cache[y1:y2, x1:x2]
        if roi.size == 0:
            return None
        return roi

    # ------------------------------------------------------------------
    #  原有方法（前台模式完全不变）
    # ------------------------------------------------------------------

    def capture_roi(self, left, top, width, height):
        """
        截取屏幕上指定 ROI 区域，并返回 numpy (OpenCV BGR格式)
        参数为屏幕绝对坐标
        """
        if width <= 10 or height <= 10:
            return None

        # === 后台模式: 从帧缓存裁切 ===
        if self._background_mode and self._target_hwnd:
            return self._crop_from_cache(left, top, width, height)

        # === 以下为原有前台模式逻辑，完全不变 ===

        monitor = {
            "top": int(top),
            "left": int(left),
            "width": int(width),
            "height": int(height)
        }

        for attempt in range(2):
            if self.sct is None and not self._recreate_sct():
                time.sleep(0.03)
                return None

            try:
                sct_img = self.sct.grab(monitor)
                # mss 返回的是 BGRA，转换为 BGR
                img = np.array(sct_img)[:, :, :3]
                # mss grab 返回的 np.array 默认是只读的，如果要用 cv2 处理建议 copy
                self._failure_count = 0
                result = np.copy(img)
                try:
                    from core.tracker import EventTracker
                    tracker = EventTracker.get()
                    cnt = getattr(tracker, "_fps_sample_counter", 0) + 1
                    tracker._fps_sample_counter = cnt
                    if cnt % 50 == 0:
                        now_ts = time.monotonic()
                        last_ts = getattr(tracker, "_fps_last_sample_ts", 0.0)
                        if last_ts > 0:
                            dt = now_ts - last_ts
                            if dt > 0:
                                tracker.perf_sample_fps(50.0 / dt)
                        tracker._fps_last_sample_ts = now_ts
                except Exception:
                    pass
                return result
            except mss.exception.ScreenShotError as e:
                self._log_capture_error("mss 截图异常 (系统绘图失败)", e)
                self._recreate_sct()
            except Exception as e:
                self._log_capture_error("未知截图异常", e)
                self._recreate_sct()

            if attempt == 0:
                time.sleep(0.01)

        return None

    def capture_relative(self, window_rect, rx, ry, rw, rh):
        """
        基于客户区窗口截取相对区域。
        例如 rx=0.5, ry=0.1, rw=0.2, rh=0.1 表示截取中心偏上的一块区域。
        window_rect: (left, top, width, height)
        """
        absolute = self.relative_rect(window_rect, rx, ry, rw, rh)
        if absolute is None:
            return None
        return self.capture_roi(*absolute)

    def relative_rect(self, window_rect, rx, ry, rw, rh):
        """把客户区比例 ROI 转换成屏幕绝对像素 ROI。"""
        if not window_rect:
            return None

        w_left, w_top, w_width, w_height = window_rect
        if w_width <= 0 or w_height <= 0:
            return None

        abs_left = w_left + int(w_width * rx)
        abs_top = w_top + int(w_height * ry)
        abs_width = max(1, int(w_width * rw))
        abs_height = max(1, int(w_height * rh))

        return abs_left, abs_top, abs_width, abs_height
