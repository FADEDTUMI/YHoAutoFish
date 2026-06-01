import mss
import mss.exception
import numpy as np
import time

class ScreenCapture:
    """屏幕截图工具类，使用 mss 实现高频低延迟截图"""
    
    def __init__(self):
        # 放弃全局单例，改为每个线程拥有自己独立的 mss 实例
        # 这样在线程销毁时，可以安全地释放对应的系统 GDI 句柄
        self.sct = None
        self._failure_count = 0
        self._last_error_log_time = 0
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
        """显式释放 mss 占用的系统 GDI 句柄资源"""
        sct = getattr(self, "sct", None)
        if sct is not None:
            try:
                sct.close()
            except Exception:
                pass
        self.sct = None
            
    def capture_roi(self, left, top, width, height):
        """
        截取屏幕上指定 ROI 区域，并返回 numpy (OpenCV BGR格式)
        参数为屏幕绝对坐标
        """
        # 防止因窗口极度缩小导致计算出的 width/height <= 0
        # 增加更严格的尺寸校验：如果窗口被缩得太小（例如最小化时变成极小的图标），也拒绝截图
        if width <= 10 or height <= 10:
            return None

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
                # SelectObject/BitBlt 失败通常意味着当前 mss/GDI 句柄已不稳定，立即重建后端并重试一次。
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
