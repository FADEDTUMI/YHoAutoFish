import time
import threading
import queue
import cv2
import numpy as np
import os
import re
import shutil
import traceback
from pathlib import Path
try:
    from importlib import metadata
except ImportError:
    metadata = None
from PIL import Image, ImageDraw, ImageFont

from core.window_manager import WindowManager
from core.screen_capture import ScreenCapture
from core.controller import Controller
from core.vision import VisionCore
from core.pid import PIDController
from core.record_manager import RecordManager
from core.paths import resource_path
from core.user_activity_monitor import UserActivityMonitor
from core._sm_round_state import RoundState
from core._sm_template_res import TemplateResources
from core._sm_auto_sell import AutoSeller
from core._sm_fishing_control import FishingControl
from core._sm_fishing_bar import FishingBarDetector
from core._sm_ocr import SettlementOCR
from core._sm_cast_detector import CastDetector
from core._sm_result_detector import ResultDetector
from core.tracker import EventTracker

class StateMachine:
    STATE_IDLE = 0
    STATE_WAITING = 1
    STATE_FISHING = 2
    STATE_RESULT = 3
    STATE_FAILED = 4
    STATE_PAUSED = 5
    STATE_RECOVERING = 6
    STATE_SELLING_CATCHES = 7
    
    def __init__(self, log_queue=None, debug_queue=None, config=None):
        self.log_queue = log_queue
        self.debug_queue = debug_queue
        
        self.wm = WindowManager()
        self.sc = None 
        self.ctrl = Controller()
        self.user_activity = UserActivityMonitor()
        self._user_takeover_exclude_rects = []
        self._input_lock = threading.RLock()
        self.vis = VisionCore()
        self.record_mgr = RecordManager()
        
        self.is_running = False
        self.current_state = self.STATE_IDLE
        self.fishing_start_time = 0
        self.fishing_timeout = 180 # 3分钟超时防卡死
        self.fish_count = 0
        self._auto_sell_session_catch_count = 0
        self._auto_sell_pending = False
        self._auto_sell_step = ""
        self._auto_sell_step_started = 0
        self._auto_sell_started_at = 0
        self._auto_sell_last_log = 0
        self._auto_sell_ready_wait_started = 0
        self._auto_sell_capture_hidden = False
        self._last_esc_time = 0.0
        self._background_mode = False

        # 实例化真正的 PID 控制器
        # Kp: 比例，影响追赶速度
        # Ki: 积分，消除长期偏差（设为极小）
        # Kd: 微分，物理刹车预测防过冲（异环这种带惯性的游戏，Kd需要比较大）
        self.pid = PIDController(kp=1.2, ki=0.01, kd=0.4, output_limits=(-100, 100))
        self.total_runtime = 0
        self.start_timestamp = 0
        self._stop_requested = False
        
        # 参数配置 (后续可由 GUI 更新)
        self.config = config or {
            "t_hold": 5,        # 安全区内重新触发按键的阈值
            "t_deadzone": 1,    # 追赶触发死区
            "tracking_strength": 180,
            "debug_mode": False,
            "cast_animation_delay": 2,
            "settlement_close_delay": 1,
            "bar_missing_timeout": 3,
            "pre_control_timeout": 14,
            "hook_wait_timeout": 90,
            "recovery_timeout": 8,
            "fishing_result_check_interval": 0.65,
            "fishing_failed_check_interval": 1.25,
            "empty_ready_confirm_delay": 0.45,
            "bar_confidence_threshold": 0.45,
            "feed_forward_gain": 0.18,
            "safe_zone_ratio": 0.08,
            "control_release_cross_ratio": 0.012,
            "control_reengage_ratio": 0.018,
            "control_switch_ratio": 0.08,
            "control_min_hold_time": 0.14,
            "user_takeover_protection": True,
            "user_takeover_mouse_threshold": 12,
            "user_takeover_start_grace": 1.20,
            "auto_sell_catch_threshold": 0,
        }
        self.round = RoundState()
        self.tpl = TemplateResources(log_fn=self._log)
        self.auto_sell = AutoSeller(self)
        self.fish_ctrl = FishingControl(self)
        self.bar_detector = FishingBarDetector(self)
        self.ocr_module = SettlementOCR(self)
        self.cast_det = CastDetector(self)
        self.result_det = ResultDetector(self)
        
    def _log(self, msg):
        """线程安全的日志发送"""
        if self.log_queue is not None:
            self.log_queue.put(msg)
        else:
            print(msg)

    def _should_stop(self):
        return bool(getattr(self, "_stop_requested", False) or not getattr(self, "is_running", False))

    def _esc_safe_gap(self, min_gap=0.30):
        """ESC按键防抖：确保距上次ESC至少min_gap秒，不足则sleep补足。"""
        now = time.time()
        elapsed = now - self._last_esc_time
        if elapsed < min_gap:
            time.sleep(min_gap - elapsed)
            self._last_esc_time = time.time()

    def _tap_key_if_running(self, key, duration=0.01):
        with self._input_lock:
            if self._should_stop():
                return False
            # 后台模式下跳过前台焦点检查和用户接管检查
            if not self._background_mode:
                if self.wm.is_foreground() and self._check_user_takeover():
                    return False
            self._note_program_input((key,), duration=float(duration) + 0.45)
            self.ctrl.key_tap(key, duration=duration)
            if key == 'esc':
                self._last_esc_time = time.time()
            return True

    def _sleep_interruptible(self, seconds, step=0.05):
        deadline = time.time() + max(0.0, float(seconds))
        while time.time() < deadline:
            if getattr(self, "_stop_requested", False):
                return False
            time.sleep(min(step, deadline - time.time()))
        return not getattr(self, "_stop_requested", False)

    def _note_program_input(self, keys=(), duration=0.45):
        if getattr(self, "user_activity", None) is not None:
            self.user_activity.note_program_input(keys, duration=duration)

    def _auto_sell_threshold(self):
        return self.auto_sell.threshold()

    def _reset_auto_sell_runtime(self):
        self.auto_sell.reset()

    def _set_auto_sell_capture_hidden(self, hidden):
        self.auto_sell.set_capture_hidden(hidden)

    def _record_auto_sell_catch(self):
        self.auto_sell.record_catch()

    def _client_point_to_screen(self, rect, roi, loc):
        abs_roi = self.sc.relative_rect(rect, *roi) if self.sc is not None else None
        if abs_roi is None or loc is None:
            return None
        return abs_roi[0] + int(loc[0]), abs_roi[1] + int(loc[1])

    def _click_screen_point_if_running(self, x, y, duration=0.05):
        with self._input_lock:
            if self._should_stop():
                return False
            # 后台模式下跳过前台焦点检查和用户接管检查
            if not self._background_mode:
                if self.wm.is_foreground() and self._check_user_takeover():
                    return False
            self._note_program_input(("mouse_left",), duration=float(duration) + 0.65)
            return self.ctrl.mouse_click(x, y, duration=duration)

    def _record_runtime_for_current_run(self):
        if self.start_timestamp > 0:
            duration = int(time.time() - self.start_timestamp)
            if duration > 0:
                self.total_runtime += duration
                self.record_mgr.add_runtime(duration)
            self.start_timestamp = 0

    def _pause_for_user_takeover(self, reason):
        with self._input_lock:
            if not self.is_running:
                return
            self._stop_requested = True
            self.is_running = False
            self.current_state = self.STATE_PAUSED
            self.ctrl.release_all()
        self._record_runtime_for_current_run()
        detail = reason or "检测到用户输入"
        self._log(f"[安全] {detail}。已暂停自动钓鱼并释放全部按键。需要继续时请重新点击开始，并保持挂机状态不要操作游戏。")
        if self.log_queue:
            self.log_queue.put(f"CMD_USER_TAKEOVER_PAUSED::{detail}")

    def _check_user_takeover(self, game_rect=None):
        # 后台模式下跳过用户接管检测（用户正在使用其他应用）
        if self._background_mode:
            return False
        if self._should_stop() or getattr(self, "user_activity", None) is None:
            return False
        reason = self.user_activity.check(
            getattr(self.ctrl, "pressed_keys", set()),
            game_rect=game_rect,
            excluded_rects=getattr(self, "_user_takeover_exclude_rects", []),
        )
        if not reason:
            return False
        self._pause_for_user_takeover(reason)
        return True

    def start(self):
        """启动状态机"""
        if self.is_running: return
        self._stop_requested = False
        self.is_running = True
        self.current_state = self.STATE_IDLE
        self._reset_round_state()
        self._reset_auto_sell_runtime()
        self.user_activity.reset()
        self.start_timestamp = time.time()
        try:
            EventTracker.get().session_start()
        except Exception as exc:
            import logging; logging.getLogger(__name__).debug("tracker.session_start failed: %s", exc)
        self._log("钓鱼脚本启动中，正在寻找游戏窗口...")
        
        # 在独立线程运行主循环
        t = threading.Thread(target=self._run_loop, daemon=True)
        t.start()

    def stop(self):
        """停止状态机"""
        if not self.is_running: return
        with self._input_lock:
            self._stop_requested = True
            self.is_running = False
            self.ctrl.release_all()
        self._set_auto_sell_capture_hidden(False)
        self._log("[系统] 收到停止指令。")
        
        # 记录本次运行时长
        if self.start_timestamp > 0:
            self._record_runtime_for_current_run()
            try:
                EventTracker.get().session_end("user_stop")
            except Exception as exc:
                import logging; logging.getLogger(__name__).debug("tracker.session_end failed: %s", exc)
            
        self.ctrl.release_all()
        # 释放系统绘图句柄，防止二次启动时抛出 BitBlt 和 SelectObject 异常
        if hasattr(self, 'sc') and self.sc:
            self.sc.close()
        self._log("钓鱼脚本已停止。")
        # 通知 UI 更新
        if self.log_queue:
            self.log_queue.put("CMD_STOP_UPDATE_GUI")

    def update_config(self, key, value):
        self.config[key] = value
        # 对于超时设置，直接同步到实例变量
        if key == "fishing_timeout":
            self.fishing_timeout = value
        elif key == "user_takeover_protection":
            self.user_activity.update_config(enabled=value)
        elif key == "user_takeover_mouse_threshold":
            self.user_activity.update_config(mouse_move_threshold=value)
        elif key == "user_takeover_start_grace":
            self.user_activity.update_config(start_grace=value)
        elif key == "user_takeover_exclude_rects":
            self._user_takeover_exclude_rects = self._normalize_exclude_rects(value)
        elif key == "background_mode":
            with self._input_lock:
                self._apply_background_mode(bool(value))

    def _apply_background_mode(self, enabled):
        """应用后台模式设置，传播到各子系统。"""
        self._background_mode = enabled
        hwnd = self.wm.hwnd if self.wm.is_window_alive() else None
        if self.sc is not None:
            self.sc.set_background_mode(enabled, hwnd)
        self.ctrl.set_background_mode(enabled, hwnd)
        # 后台模式下禁用用户接管检测（用户正在使用其他应用）
        if enabled:
            self.user_activity.update_config(enabled=False)
            self._log("[后台模式] 已启用后台钓鱼。游戏可被遮挡但不能最小化。用户接管检测已禁用。")
        else:
            self.user_activity.update_config(
                enabled=self.config.get("user_takeover_protection", True))
            self._log("[后台模式] 已关闭后台钓鱼，恢复前台模式。")

    def _normalize_exclude_rects(self, rects):
        normalized = []
        for rect in rects or []:
            try:
                left, top, width, height = rect
                width = int(width)
                height = int(height)
                if width > 0 and height > 0:
                    normalized.append((int(left), int(top), width, height))
            except Exception:
                continue
        return normalized

    def _normalize_tracking_strength(self):
        try:
            raw_value = float(self.config.get("tracking_strength", 180))
        except (TypeError, ValueError):
            raw_value = 180.0
        strength = raw_value / 100.0 if raw_value > 5 else raw_value
        return max(0.70, min(strength, 2.40))

    def _normalize_ratio_config(self, key, default, minimum, maximum):
        try:
            value = float(self.config.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(value, maximum))

    def _reset_round_state(self, release_keys=True):
        self.round.reset(
            release_keys_fn=self.ctrl.release_all if release_keys else None,
            pid=self.pid,
        )
        self.fishing_start_time = self.round.fishing_start_time
    def _prepare_fishing_round_state(self, start_time=None):
        self.round.prepare_fishing(pid=self.pid, start_time=start_time)
        self.fishing_start_time = self.round.fishing_start_time
    def _wait_after_cast(self, rect, total_delay):
        return not self._sleep_interruptible(max(0.0, float(total_delay)), step=0.04)

    def _enter_recovering(self, reason, record_empty=False, press_esc=False, allow_second_esc=False):
        self.ctrl.release_all()
        if record_empty:
            self.record_mgr.add_empty_catch()
            self._log("[恢复] 已记录一次空杆/失败尝试。")
        self._reset_round_state()
        self.round.recovery_start_time = time.time()
        self.round.recovery_reason = reason
        self.round.recovery_esc_requested = bool(press_esc)
        self.round.recovery_esc_sent = False
        self.round.recovery_second_esc_sent = False
        self.round.recovery_allow_second_esc = bool(allow_second_esc)
        self.round.recovery_empty_recorded = bool(record_empty)
        self.current_state = self.STATE_RECOVERING
        self._log(f"[恢复] {reason}，开始等待可抛钩界面恢复。")

    def _push_debug_status_frame(self, text):
        """向调试队列推送一帧状态标记画面，避免调试视图冻结无反馈。"""
        if not self.config.get("debug_mode", False) or self.debug_queue is None:
            return
        try:
            import cv2 as _cv2
            frame = _cv2.zeros((60, 300, 3), dtype="uint8")
            _cv2.putText(frame, text, (10, 40), _cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 200, 255), 2)
            if self.debug_queue.qsize() < 2:
                self.debug_queue.put(frame)
        except Exception:
            pass

    def _enter_result_from_fishing_anomaly(self, reason):
        self._log(f"[溜鱼] {reason}，进入结果判定...")
        self._push_debug_status_frame(reason[:30])
        self.ctrl.release_all()
        self.round.fish_control_direction = 0
        self.round.fish_control_min_hold_until = 0
        self.round.result_quick_check_last = 0
        self.round.result_full_check_last = 0
        self.result_det.clear_result_ready_candidate()
        self.current_state = self.STATE_RESULT

    def get_ocr_init_failure_message(self):
        return self.ocr_module.get_init_failure_message()

    def prepare_recognition_modules(self, progress_fn=None):
        """预热所有识别模块：OCR、视觉模板、HSV 色彩轮廓、形态学管线。

        Parameters
        ----------
        progress_fn : callable, optional
            进度回调，签名 progress_fn(message: str)。
        """
        def _report(msg):
            if progress_fn is not None:
                try:
                    progress_fn(msg)
                except Exception:
                    pass
            self._log(f"[预热] {msg}")

        # ── 第 1 步：OCR 运行时路径准备 ──
        t0 = time.perf_counter()
        _report("步骤 1/6: 准备 OCR 运行时路径...")
        try:
            self.ocr_module.prepare_ocr_runtime_roots()
        except Exception as exc:
            self._log(f"[预热] OCR 路径准备失败（非致命）: {exc}")
        _report(f"步骤 1/6 完成 ({time.perf_counter() - t0:.2f}s)")

        # ── 第 2 步：OCR 模型加载 ──
        t0 = time.perf_counter()
        _report("步骤 2/6: 加载 OCR 识别模型（名称/重量/通用）...")
        name_ocr = self.ocr_module.ensure_ocr("name")
        weight_ocr = self.ocr_module.ensure_ocr("weight")
        general_ocr = self.ocr_module.ensure_ocr("general")
        _report(f"步骤 2/6 完成 ({time.perf_counter() - t0:.2f}s)")

        # ── 第 3 步：图像兜底匹配参考图 ──
        t0 = time.perf_counter()
        _report("步骤 3/6: 加载图像兜底匹配参考图...")
        try:
            self.ocr_module.load_fish_matcher_refs()
        except Exception as exc:
            self._log(f"[预热] 图像匹配参考加载失败（非致命）: {exc}")
        _report(f"步骤 3/6 完成 ({time.perf_counter() - t0:.2f}s)")

        # ── 第 4 步：模板 PNG 预加载 ──
        t0 = time.perf_counter()
        _report("步骤 4/6: 预加载全部模板 PNG...")
        preheat_warn = False
        try:
            self._preload_all_templates()
        except Exception as exc:
            self._log(f"[预热] ⚠️ 模板预加载失败: {exc}")
            _report(f"⚠️ 步骤 4/6 模板预加载失败: {exc}")
            preheat_warn = True
        _report(f"步骤 4/6 完成 ({time.perf_counter() - t0:.2f}s)")

        # ── 第 5 步：HSV 色彩轮廓预计算 ──
        t0 = time.perf_counter()
        _report("步骤 5/6: 预计算 HSV 色彩轮廓...")
        try:
            self._precompute_color_profiles()
        except Exception as exc:
            self._log(f"[预热] ⚠️ 色彩轮廓预计算失败: {exc}")
            _report(f"⚠️ 步骤 5/6 色彩轮廓预计算失败: {exc}")
            preheat_warn = True
        _report(f"步骤 5/6 完成 ({time.perf_counter() - t0:.2f}s)")

        # ── 第 6 步：analyze_fishing_bar 哑调用 ──
        t0 = time.perf_counter()
        _report("步骤 6/6: 预热耐力条分析管线（dummy call）...")
        try:
            self._preheat_analyze_fishing_bar()
        except Exception as exc:
            self._log(f"[预热] ⚠️ 耐力条管线预热失败: {exc}")
            _report(f"⚠️ 步骤 6/6 耐力条管线预热失败: {exc}")
            preheat_warn = True
        _report(f"步骤 6/6 完成 ({time.perf_counter() - t0:.2f}s)")

        if preheat_warn:
            _report("⚠️ 部分预热步骤失败，首帧处理可能较慢")
        return name_ocr is not None and weight_ocr is not None and general_ocr is not None

    def _preload_all_templates(self):
        """预加载 TemplateResources 中所有模板 PNG 到 VisionCore 的缓存。"""
        accessor_methods = [
            self.tpl.f_button_templates,
            self.tpl.initial_q_button_templates,
            self.tpl.initial_e_button_templates,
            self.tpl.initial_r_button_templates,
            self.tpl.ready_start_button_templates,
            self.tpl.ready_panel_templates,
            self.tpl.hook_text_templates,
            self.tpl.failed_text_templates,
            self.tpl.weight_unit_templates,
            self.tpl.success_close_prompt_templates,
            self.tpl.success_exp_templates,
            self.tpl.cursor_templates,
            self.tpl.target_bar_templates,
            self.tpl.auto_sell_fish_cabin_templates,
            self.tpl.auto_sell_one_click_templates,
            self.tpl.auto_sell_confirm_templates,
        ]
        total = 0
        for accessor in accessor_methods:
            for p in accessor():
                self.vis._read_template(p)
                total += 1
        self._log(f"[预热] 已预加载 {total} 个模板文件。")

    def _precompute_color_profiles(self):
        """预计算耐力条分析所需的 HSV 色彩轮廓。"""
        target_paths = self.tpl.target_bar_templates()
        if target_paths:
            self.vis._target_color_profile(target_paths)
        cursor_paths = self.tpl.cursor_templates()
        if cursor_paths:
            self.vis._cursor_color_profile(cursor_paths)
        self._log("[预热] HSV 色彩轮廓已缓存。")

    def _preheat_analyze_fishing_bar(self):
        """用合成小图像调用一次 analyze_fishing_bar，预热所有内部处理管线。"""
        import numpy as _np
        dummy_img = _np.zeros((20, 200, 3), dtype=_np.uint8)
        cursor_paths = self.tpl.cursor_templates()
        target_paths = self.tpl.target_bar_templates()
        self.vis.analyze_fishing_bar(
            dummy_img,
            cursor_template_paths=cursor_paths,
            cursor_color_reference_paths=cursor_paths,
            target_color_reference_paths=target_paths,
            cursor_scale_range=(0.5, 2.0),
            cursor_scale_steps=5,
            draw_debug=False,
        )
        self._log("[预热] analyze_fishing_bar 管线已预热。")

    def _run_loop(self):
        # 确保在当前线程中实例化 ScreenCapture
        self.sc = ScreenCapture()
        # 如果已配置后台模式，立即应用到截图实例
        if self._background_mode and self.wm.is_window_alive():
            self.sc.set_background_mode(True, self.wm.hwnd)
            self.ctrl.set_background_mode(True, self.wm.hwnd)

        # 初始化与绑定窗口
        if not self.wm.find_window():
            self._log("错误: 未找到游戏进程 HTGame.exe。请确保游戏正在运行。")
            self.stop()
            return
            
        initial_rect = self.wm.get_client_rect()
        dpi_scale = self.wm.get_dpi_scale()
        if initial_rect:
            self._log(f"成功绑定游戏窗口。客户区: {initial_rect[2]}x{initial_rect[3]}，DPI倍率: {dpi_scale:.2f}")
        else:
            self._log(f"成功绑定游戏窗口。DPI倍率: {dpi_scale:.2f}")
        # find_window 成功后，补一次后台模式传播（确保 hwnd 正确）
        if self._background_mode:
            self.sc.set_background_mode(True, self.wm.hwnd)
            self.ctrl.set_background_mode(True, self.wm.hwnd)
        # 后台模式下不强制置顶游戏窗口
        if not self._background_mode:
            self.wm.set_foreground()
        if not self._sleep_interruptible(1): # 等待窗口置顶完成
            self.sc.close()
            return
        self.user_activity.reset()
        
        # ROI 定义 (相对于客户区宽高)
        # 缩小寻找 F 键的范围，只截取屏幕真正的右下角边缘，避免把中间的发光背景截进去
        ROI_F_BTN = (0.75, 0.75, 0.25, 0.25)

        # 恢复合理的高度范围，根据用户提供的精确比例进行定位：
        # 横向占比是30%到70% (X: 0.3, Width: 0.4)
        # 竖向占比是从6.21%到7.68% (Y: 0.0621, Height: 0.0147)
        ROI_FISHING_BAR = (0.3, 0.0621, 0.4, 0.0147)
        
        ROI_CENTER_TEXT = (0.2, 0.2, 0.6, 0.5)
        
        # DEBUG 计数器，防止写爆硬盘
        debug_save_count = 0

        while self.is_running:
            # 1. 焦点保护机制（后台模式下跳过）
            if not self._background_mode:
                if not self.wm.is_foreground():
                    # 检查当前焦点是否是被我们自己的 Debug 窗口抢走了
                    import win32gui
                    fg_hwnd = win32gui.GetForegroundWindow()
                    if win32gui.GetWindowText(fg_hwnd) == "Fishing Bar Tracker (Debug)":
                        # 如果是被 Debug 窗口抢走的，不要暂停按键，尝试切回去
                        self.wm.set_foreground()
                    else:
                        self._log("警告: 游戏窗口失去焦点，暂停按键发送。")
                        self.ctrl.release_all()
                        if not self._sleep_interruptible(1):
                            break
                        continue

            # 2. 获取实时窗口坐标 (防止窗口被拖动)
            rect = self.wm.get_client_rect()
            if not rect:
                if self._background_mode:
                    self._log("获取窗口坐标失败，游戏窗口可能已最小化。后台模式不支持最小化。")
                else:
                    self._log("获取窗口坐标失败，请不要最小化游戏。")
                if not self._sleep_interruptible(1):
                    break
                continue

            # 后台模式下每帧更新坐标原点并截取一次完整画面缓存
            if self._background_mode and self.sc and rect:
                self.sc.set_capture_origin(rect[0], rect[1])
                self.ctrl.set_click_origin(rect[0], rect[1])
                self.sc.begin_frame()  # 帧级缓存：一次 PrintWindow，所有 ROI 共享

            if self._check_user_takeover(game_rect=rect):
                break
                
            # 3. 状态分发
            prev_state = self.current_state
            state_start = time.time()
            if self.current_state == self.STATE_IDLE:
                self._handle_idle(rect, ROI_F_BTN)
            elif self.current_state == self.STATE_WAITING:
                self._handle_waiting(rect, ROI_CENTER_TEXT)
            elif self.current_state == self.STATE_FISHING:
                self._handle_fishing(rect, ROI_FISHING_BAR)
            elif self.current_state == self.STATE_RESULT:
                self._handle_result(rect)
            elif self.current_state == self.STATE_FAILED:
                self._handle_failed()
            elif self.current_state == self.STATE_RECOVERING:
                self._handle_recovering(rect)
            elif self.current_state == self.STATE_SELLING_CATCHES:
                self._handle_auto_sell(rect)
            if self.current_state != prev_state:
                try:
                    EventTracker.get().state_transition(prev_state, self.current_state, (time.time() - state_start) * 1000)
                except Exception as exc:
                    import logging; logging.getLogger(__name__).debug("tracker.state_transition failed: %s", exc)
                
            # 控制基础循环帧率
            if not self._sleep_interruptible(0.01, step=0.01):
                break
            
        self._set_auto_sell_capture_hidden(False)
        self.sc.close()
        try:
            EventTracker.get().session_end("loop_exit")
        except Exception as exc:
            import logging; logging.getLogger(__name__).debug("tracker.session_end(loop_exit) failed: %s", exc)

    def _handle_idle(self, rect, roi):
        if self._should_stop():
            return
        self._log("[待机] 正在检测右下角抛竿图标...")
            
        # DEBUG 计数器
        if not hasattr(self, '_debug_count'): self._debug_count = 0
        self._debug_count += 1

        ready_info = self.cast_det.detect_ready_to_cast(rect, allow_heavy=(self._debug_count % 6 == 0))
        if self._should_stop():
            return

        if ready_info and ready_info.get("blocking_result"):
            if self.cast_det.handle_ready_blocking_result(rect, ready_info, "待机"):
                return
        
        if ready_info and ready_info.get("location"):
            if getattr(self, "_auto_sell_pending", False) and self._auto_sell_threshold() > 0:
                strict_ready = self.cast_det.detect_ready_to_cast(rect, allow_heavy=False, require_initial_controls=True)
                if strict_ready and strict_ready.get("blocking_result"):
                    if self.cast_det.handle_ready_blocking_result(rect, strict_ready, "待机"):
                        return
                if strict_ready and strict_ready.get("location"):
                    if self.auto_sell.start_flow(rect, strict_ready):
                        return
                else:
                    now = time.time()
                    if not getattr(self, "_auto_sell_ready_wait_started", 0):
                        self._auto_sell_ready_wait_started = now
                        self._log("[售鱼] 已达到自动售鱼阈值，但尚未确认钓鱼初始界面组合控件，暂缓抛竿并继续确认。")
                    if now - self._auto_sell_ready_wait_started < 4.0:
                        self._sleep_interruptible(0.18)
                        return
                    self._log("[售鱼] 暂未确认可安全进入售鱼界面，本轮先继续钓鱼，后续回到初始界面再尝试。")
                    self._auto_sell_ready_wait_started = 0
            if not self.fish_ctrl.send_cast_input(ready_info, "待机"):
                return
            if self._should_stop():
                return
            cast_delay = max(1, min(int(self.config.get("cast_animation_delay", 2)), 5))
            self._log(f"[待机] > 发送完成，等待 {cast_delay} 秒抛竿动画...")
            self.current_state = self.STATE_WAITING
            self._wait_after_cast(rect, cast_delay)
            return
        else:
            now = time.time()
            if now - getattr(self, "_idle_result_check_last", 0) >= 1.20:
                self._idle_result_check_last = now
                success_info = self.result_det.detect_fast_success_result(rect, fast_only=True)
                if success_info and success_info.get("location"):
                    self._log("[待机] 检测到成功结算界面仍未关闭，优先处理结算。")
                    self.result_det.finish_fast_success_result(rect, success_info, source_label="待机")
                    return
                failed_info = self.result_det.detect_fast_failed_result(rect)
                if self.result_det.maybe_finish_failed_result(rect, failed_info, source_label="待机"):
                    self._log("[待机] 检测到失败提示仍未恢复，进入失败恢复流程。")
                    return
            if self._debug_count % 10 == 0 and self._debug_count <= 30:
                btn_img = self.sc.capture_relative(rect, *roi)
                if btn_img is not None:
                    cv2.imwrite("debug_f_btn_roi.png", btn_img)
                conf = ready_info.get("confidence") if ready_info else 0.0
                self._log(f"[排错] 抛竿图标匹配失败，最高置信度: {conf:.2f}。已保存当前截图至根目录 debug_f_btn_roi.png")
            self._sleep_interruptible(0.18)

    def _handle_auto_sell(self, rect):
        self.auto_sell.handle(rect)

    def _handle_waiting(self, rect, roi):
        # 每隔一小段时间检测一次即可，不需要过高频率
        if not self._sleep_interruptible(0.1):
            return
        if self._should_stop():
            return
        if getattr(self.round, 'waiting_start_time', 0) == 0:
            self.round.waiting_start_time = time.time()
        if getattr(self.round, 'last_cast_time', 0) == 0:
            self.round.last_cast_time = self.round.waiting_start_time

        now = time.time()
        text_img = self.sc.capture_relative(rect, *roi)
        if text_img is None: return

        loc, conf, matched_path = self.vis.find_best_template(
            text_img,
            self.tpl.hook_text_templates(),
            threshold=0.68,
            use_edge=False,
            use_binary=False,
            scale_range=self.tpl.scale_range(rect, 0.62, 1.55),
            scale_steps=11,
        )
        
        if loc:
            matched_name = Path(matched_path).name if matched_path else "未知模板"
            self._log(f"[等待] 识别到上钩提示 (置信度: {conf:.2f}，模板: {matched_name})，迅速按F！")
            if not self._tap_key_if_running('F'):
                return
            self._prepare_fishing_round_state(time.time())
            self.round.waiting_start_time = 0
            self.round.last_cast_time = 0
            self.round.waiting_recast_count = 0
            self.round.waiting_ready_recheck_last = 0
            self.current_state = self.STATE_FISHING
            # 移除了硬编码的 1.5 秒 sleep，改为在 _handle_fishing 中动态等待耐力条出现，
            # 这样对于出现极快的稀有鱼可以做到零延迟响应。
            return

        wait_timeout = max(20, min(int(self.config.get("hook_wait_timeout", 90)), 300))
        if now - self.round.waiting_start_time > wait_timeout:
            self._log(f"[等待] 超过 {wait_timeout} 秒未识别到上钩提示，释放按键并回到待机重新检测。")
            self._enter_recovering("抛竿后长时间未识别到上钩提示", record_empty=True, press_esc=False)
            return

        cast_retry_delay = max(6.0, min(float(self.config.get("cast_retry_delay", 8)), 30.0))
        if now - self.round.last_cast_time >= cast_retry_delay and now - getattr(self.round, 'waiting_ready_recheck_last', 0) >= 1.0:
            self.round.waiting_ready_recheck_last = now
            ready_info = self.cast_det.detect_ready_to_cast(
                rect,
                allow_heavy=(now - self.round.last_cast_time >= cast_retry_delay + 4.0),
                require_initial_controls=False,
                include_f=False,
            )
            if ready_info and ready_info.get("blocking_result"):
                if self.cast_det.handle_ready_blocking_result(rect, ready_info, "等待"):
                    return
            if ready_info and ready_info.get("location"):
                retry_count = int(getattr(self.round, 'waiting_recast_count', 0))
                max_retries = 2
                if retry_count < max_retries:
                    self.round.waiting_recast_count = retry_count + 1
                    self._log(f"[等待] 抛竿后仍检测到{ready_info.get('kind') or '初始钓鱼界面'}，判定可能未进入等待上钩流程，重试抛竿 ({self.round.waiting_recast_count}/{max_retries})。")
                    if not self.fish_ctrl.send_cast_input(ready_info, "等待"):
                        return
                    self._wait_after_cast(rect, 1.40)
                    return
                self._log("[等待] 多次重试后仍停留在初始钓鱼界面，进入恢复流程。")
                self._enter_recovering("多次重发 F 后仍未进入抛竿流程", record_empty=False, press_esc=False)
                return


    def _handle_fishing(self, rect, roi):
        if self._should_stop():
            return
        # 记录进入溜鱼状态的时间，用于防卡死
        if getattr(self.round, 'fishing_start_time', 0) == 0:
            self._prepare_fishing_round_state(time.time())
            
        elapsed = time.time() - self.round.fishing_start_time
        if elapsed > self.fishing_timeout:
            self._log("[防卡死] 溜鱼超时，强制结束当前回合。")
            self._push_debug_status_frame("fishing timeout -> RESULT")
            self.round.fishing_start_time = 0
            self.current_state = self.STATE_RESULT
            return

        recent_bar_seen = getattr(self.round, 'last_bar_seen_time', 0) and (time.time() - getattr(self.round, 'last_bar_seen_time', 0) <= 0.35)
        if elapsed >= 1.0 and not getattr(self.round, 'confirmed_fishing_bar', False) and not recent_bar_seen and self.result_det.check_terminal_result_before_bar(rect, elapsed):
            return

        det_result = self.bar_detector.select_fishing_bar_detection(rect, roi)
        target_x, cursor_x, target_w, debug_img, bar_confidence = det_result[:5]
        is_stale = det_result[5] if len(det_result) > 5 else False
        debug_meta = det_result[6] if len(det_result) > 6 else None
        # detection_confidence removed — fired at ~100Hz, flooding the analytics queue
        # with 5000+ events per round and burying important events like fishing_success.
        # Bar confidence is already captured by PerfSampler via perf_snapshot.

        # 性能优化：限制 Debug 图像的发送频率（一秒最多 10 帧），防止撑爆队列导致主线程阻塞
        if self.config.get("debug_mode", False) and debug_img is not None:
            now = time.time()
            if getattr(self, '_last_debug_time', 0) == 0 or (now - self._last_debug_time) >= 0.10:
                if self.debug_queue is not None and self.debug_queue.qsize() < 2:
                    self.debug_queue.put(debug_img)
                self._last_debug_time = now
                self._last_debug_meta = debug_meta

        # 判断是否结束 (无论是成功还是鱼儿溜走，耐力条都会消失)
        if target_x is None or cursor_x is None:
            if getattr(self.round, "last_bar_capture_failed", False):
                if getattr(self.round, "capture_missing_start_time", 0) == 0:
                    self.round.capture_missing_start_time = time.time()
                capture_missing_elapsed = time.time() - self.round.capture_missing_start_time
                if capture_missing_elapsed <= 0.55:
                    if not self.bar_detector.hold_recent_fishing_control_on_gap():
                        self.ctrl.release_all()
                        self.round.fish_control_direction = 0
                        self.round.fish_control_min_hold_until = 0
                    return
                self.round.capture_missing_start_time = 0

            if self.bar_detector.hold_recent_fishing_control_on_gap():
                # Even during gap-hold, enforce fishing timeout to prevent permanent freeze
                elapsed = time.time() - self.round.fishing_start_time
                if elapsed > self.fishing_timeout:
                    self._enter_result_from_fishing_anomaly("溜鱼超时（保持控制期间）")
                return

            # 安全保护：如果丢失目标，立刻释放所有按键，防止游标因为惯性飞出界
            self.ctrl.release_all()
            self.round.fish_control_direction = 0
            self.round.fish_control_min_hold_until = 0
            
            if not getattr(self.round, 'fishing_control_started', False):
                last_seen_time = getattr(self.round, 'last_bar_seen_time', 0)
                if last_seen_time and time.time() - last_seen_time > 0.55:
                    self.round.bar_seen_streak = 0
                    self.round.seen_fishing_bar = False
                    self.round.confirmed_fishing_bar = False
                    self.round.fishing_bar_confirmed_time = 0

                transition_elapsed = time.time() - self.round.fishing_start_time
                if transition_elapsed >= 1.0 and self.result_det.check_terminal_result_before_bar(rect, transition_elapsed):
                    return
                pre_control_timeout = max(10.0, min(float(self.config.get("pre_control_timeout", 14)), 30.0))
                if transition_elapsed > pre_control_timeout:
                    self._enter_result_from_fishing_anomaly(f"上钩后 {pre_control_timeout:.0f} 秒仍未进入有效溜鱼控制")
                return

            if not getattr(self.round, 'confirmed_fishing_bar', False):
                last_seen_time = getattr(self.round, 'last_bar_seen_time', 0)
                if last_seen_time and time.time() - last_seen_time > 0.55:
                    self.round.bar_seen_streak = 0
                    self.round.seen_fishing_bar = False
                # 还没看到过耐力条，说明还在播放上钩的过渡动画
                # 增加一个初始等待超时，比如 5 秒
                transition_elapsed = time.time() - self.round.fishing_start_time
                if transition_elapsed >= 1.0 and self.result_det.check_terminal_result_before_bar(rect, transition_elapsed):
                    return
                if transition_elapsed > 5.0:
                    self._enter_result_from_fishing_anomaly("上钩后长时间未出现耐力条")
                return

            # 引入容错：偶尔一帧没识别到不算结束，连续丢失超过用户设定才算结束
            if getattr(self.round, 'missing_start_time', 0) == 0:
                self.round.missing_start_time = time.time()
                self.round.result_quick_check_last = 0
                self.round.result_full_check_last = self.round.missing_start_time

            missing_elapsed = time.time() - self.round.missing_start_time
            # 最小溜鱼时间守卫：溜鱼开始后 3 秒内不退出 FISHING 状态
            # 防止特效/粒子短暂遮挡耐力条导致误判"鱼溜走了"
            fishing_elapsed = time.time() - getattr(self.round, 'fishing_start_time', 0)
            if fishing_elapsed < 3.0:
                return
            if missing_elapsed >= 0.50 and self.result_det.check_result_signals_after_bar_missing(rect, missing_elapsed):
                return
            if self.bar_detector.should_enter_result_after_confirmed_bar_missing(missing_elapsed):
                self.bar_detector.enter_result_after_bar_missing()
                return

            missing_timeout = max(0.8, min(float(self.config.get("bar_missing_timeout", 2)), 5.0))
            if missing_elapsed > missing_timeout:
                self.bar_detector.enter_result_after_bar_missing()
            return
        
        # 识别到了，重置丢失计时器，并标记已经看到过耐力条
        self.round.missing_start_time = 0
        self.round.capture_missing_start_time = 0
        self.round.result_quick_check_last = 0
        self.round.result_full_check_last = 0
        self.result_det.clear_result_ready_candidate()

        now = time.time()
        last_seen_time = getattr(self.round, 'last_bar_seen_time', 0)
        if not is_stale:
            if last_seen_time and now - last_seen_time <= 0.55:
                self.round.bar_seen_streak = int(getattr(self.round, 'bar_seen_streak', 0)) + 1
            else:
                self.round.bar_seen_streak = 1
                self.round.bar_first_seen_time = now
            self.round.last_bar_seen_time = now
        self.round.seen_fishing_bar = True
        if not getattr(self.round, 'confirmed_fishing_bar', False) and self.round.bar_seen_streak >= 2:
            self.round.confirmed_fishing_bar = True
            self.round.fishing_bar_confirmed_time = now
            if not getattr(self.round, "fishing_control_started", False):
                self.round.fishing_control_started = True
                self.round.fishing_control_started_time = now

        # === 核心追踪算法：直接误差 + 滞回保持 ===
        # A/D 是离散按键，不是连续舵量；真实游戏里视觉速度噪声较大，
        # 因此控制方向只使用当前可靠位置，避免速度预测把方向带偏。
        error = target_x - cursor_x
        abs_error = abs(error)
        self.round.last_control_error = error
        self.round.last_control_target_w = target_w

        now = time.time()
        if getattr(self.round, 'last_target_time', 0) == 0:
            self.round.last_target_x = target_x
            self.round.last_target_time = now
            target_velocity = 0
        else:
            dt = now - self.round.last_target_time
            if dt > 0.001:
                raw_velocity = (target_x - self.round.last_target_x) / dt
                old_velocity = getattr(self.round, 'target_velocity', 0)
                target_velocity = old_velocity * 0.70 + raw_velocity * 0.30
            else:
                target_velocity = getattr(self.round, 'target_velocity', 0)
            self.round.last_target_x = target_x
            self.round.last_target_time = now
            self.round.target_velocity = target_velocity
            
        # 动态安全区：低级鱼竿容错更小，默认更积极追赶。
        safe_zone_ratio = self._normalize_ratio_config("safe_zone_ratio", 0.08, 0.04, 0.28)
        safe_zone = target_w * safe_zone_ratio if target_w else 8
        
        # PID 控制器计算基础偏差修正力
        tracking_strength = self._normalize_tracking_strength()
        control_signal = self.pid.update(error, measurement=target_x) * tracking_strength
        if not hasattr(self, '_pid_sample_counter'):
            self._pid_sample_counter = 0
        self._pid_sample_counter += 1
        if self._pid_sample_counter >= 10:
            self._pid_sample_counter = 0
            # pid_metrics event removed — was flooding buffer at 10Hz

        ff_gain = self._normalize_ratio_config("feed_forward_gain", 0.18, 0.0, 0.45) * tracking_strength
        total_signal = control_signal + target_velocity * ff_gain

        # --- 纯非阻塞高频按键控制 ---
        # 动态阈值：
        # 如果游标在安全区内且目标没有高速移动，我们提高触发阈值，释放按键让游标自然滑动，避免左右鬼畜抽搐
        # 如果游标偏离或者目标正在高速逃离，我们降低阈值，要求立即按键追赶
        is_safe = (abs_error <= safe_zone) and (abs(target_velocity) < 90)
        hold_threshold = max(2, min(int(self.config.get("t_hold", 5)), 60))
        deadzone_threshold = max(1, min(int(self.config.get("t_deadzone", 1)), 30))
        threshold = hold_threshold if is_safe else deadzone_threshold

        direction = self.fish_ctrl.choose_direction(
            error,
            target_w,
            target_velocity,
            total_signal,
            threshold,
        )
        if direction:
            self.round.fishing_control_frame_count = int(getattr(self.round, "fishing_control_frame_count", 0)) + 1
            if not getattr(self.round, "fishing_control_started", False):
                self.round.fishing_control_started = True
                self.round.fishing_control_started_time = time.time()
            self.round.round_had_fishing_bar = True
        self.fish_ctrl.apply_direction(direction)

    def _handle_result(self, rect):
        if self._should_stop():
            return
        self._log("[结算] 正在检测钓鱼结果...")

        # 极简模式：跳过结算识别，直接等待后进入下一轮
        if self.config.get("minimal_settlement_mode", False):
            self.ctrl.release_all()
            wait_time = max(1.0, min(float(self.config.get("minimal_mode_wait", 2.5)), 5.0))
            self._log(f"[结算] 极简模式：跳过结算识别，等待 {wait_time:.1f} 秒后继续...")
            if not self._sleep_interruptible(wait_time):
                return
            self.record_mgr.add_catch_count_only()
            self.fish_count += 1
            self._record_auto_sell_catch()
            started = getattr(self.round, "fishing_control_started_time", 0)
            round_duration = (time.time() - started) if started > 0 else 0
            try:
                EventTracker.get().fishing_success("极简模式", 0, "", round_duration)
            except Exception:
                pass
            self._esc_safe_gap(0.30)
            if not self._tap_key_if_running('esc', duration=0.15):
                return
            close_delay = max(0.4, min(float(self.config.get("settlement_close_delay", 1)), 5.0))
            if not self._sleep_interruptible(close_delay):
                return
            self._log(f"[结算] 极简模式：当前累计钓获: {self.fish_count} 条。")
            self._reset_round_state()
            self.current_state = self.STATE_IDLE
            return

        max_attempts = 10 # 增加循环次数，但缩短每次的等待时间，实现更敏捷的响应
        if getattr(self.round, "success_recorded_pending_close", False):
            ready_info = self.cast_det.detect_ready_to_cast(rect, allow_heavy=False, require_initial_controls=True)
            if ready_info and ready_info.get("location"):
                self.result_det.finish_empty_ready_result(ready_info)
                return

            now = time.time()
            close_delay = max(0.4, min(float(self.config.get("settlement_close_delay", 1)), 5.0))
            retry_count = int(getattr(self.round, "success_close_retry_count", 0))
            if now - getattr(self.round, "success_close_last_esc", 0) >= max(0.75, close_delay, 0.30):
                success_info = self.result_det.detect_success_settlement_still_visible(rect)
                if success_info and retry_count < max_attempts:
                    self.result_det.finish_success_result(
                        rect,
                        success_info,
                        attempt=retry_count + 1,
                        max_attempts=max_attempts,
                    )
                    return
                if success_info:
                    self._log("[结算] 成功结算界面多次 ESC 后仍未确认关闭，进入恢复流程继续处理。")
                    self._enter_recovering("成功结算界面关闭未确认", record_empty=False, press_esc=False)
                    return
                self._log("[结算] 已发送 ESC 关闭结算，且未再确认结算界面仍存在；为避免重复 ESC 退出钓鱼界面，返回待机继续扫描。")
                self._reset_round_state()
                self.current_state = self.STATE_IDLE
                return

            self._sleep_interruptible(0.15)
            return

        result_start = time.time()
        result_timeout = max(6.0, min(float(self.config.get("result_detect_timeout", 9.0)), 18.0))
        full_interval = 0.70
        for attempt in range(max_attempts):
            success_info = self.result_det.detect_fast_success_result(rect, fast_only=False)
            if success_info and success_info.get("location"):
                self.result_det.finish_success_result(rect, success_info, attempt=attempt + 1, max_attempts=max_attempts)
                return

            failed_info = self.result_det.detect_fast_failed_result(rect)
            if self.result_det.maybe_finish_failed_result(rect, failed_info):
                return

            now = time.time()
            if attempt == 0 or now - getattr(self.round, "result_full_check_last", 0) >= full_interval:
                full_checked_at = time.time()
                success_info = self.result_det.detect_success_result(rect)
                if success_info and success_info.get("location"):
                    self.result_det.finish_success_result(rect, success_info, attempt=attempt + 1, max_attempts=max_attempts)
                    return
                failed_info = self.result_det.detect_failed_result(rect)
                if self.result_det.maybe_finish_failed_result(rect, failed_info):
                    return
                self.round.result_full_check_last = full_checked_at

            if getattr(self.round, "round_had_fishing_bar", False) and (attempt >= 1 or time.time() - result_start >= 0.75):
                if self.result_det.try_finish_success_by_settlement_probe(rect, source_label="结算"):
                    return

            ready_info = self.cast_det.detect_ready_to_cast(rect, allow_heavy=False, require_initial_controls=True)
            if ready_info and ready_info.get("location"):
                if self.result_det.confirm_empty_ready_result(rect, ready_info):
                    return
            else:
                self.result_det.clear_result_ready_candidate()
                    
            # 如果既没有 F 键，也没有底部文字，说明可能还在播放动画，稍微等一下继续循环
            if not self._sleep_interruptible(0.25):
                return
            if time.time() - result_start >= result_timeout:
                break

        # 如果试了多次还是不行，就强行重置，避免脚本卡死在这个状态
        self._log("[警告] 结算超时，强制返回待机状态。")
        self._enter_recovering("结算判定超时", record_empty=False, press_esc=False)

    def _handle_recovering(self, rect):
        if self._should_stop():
            return
        if getattr(self.round, "recovery_start_time", 0) == 0:
            self.round.recovery_start_time = time.time()

        now = time.time()
        elapsed = now - self.round.recovery_start_time

        ready_info = self.cast_det.detect_ready_to_cast(rect, allow_heavy=(elapsed >= 2.0), require_initial_controls=True)
        if ready_info and ready_info.get("blocking_result"):
            if self.cast_det.handle_ready_blocking_result(rect, ready_info, "恢复"):
                return

        if ready_info and ready_info.get("location"):
            self._log(f"[恢复] 已检测到{ready_info.get('kind') or '可抛钩提示'}，恢复到待机流程。")
            self._reset_round_state()
            self.current_state = self.STATE_IDLE
            return

        if getattr(self.round, "recovery_esc_requested", False) and not getattr(self.round, "recovery_esc_sent", False):
            self.ctrl.release_all()
            self._esc_safe_gap(0.30)
            if not self._tap_key_if_running('esc', duration=0.15):
                return
            self.round.recovery_esc_sent = True
            self.round.recovery_first_esc_time = time.time()
            self._sleep_interruptible(0.35)
            return

        first_esc_elapsed = now - getattr(self.round, "recovery_first_esc_time", 0) if getattr(self.round, "recovery_esc_sent", False) else 999
        if (
            getattr(self.round, "recovery_esc_requested", False)
            and getattr(self.round, "recovery_allow_second_esc", True)
            and elapsed >= 3.0
            and first_esc_elapsed >= 1.0
            and not getattr(self.round, "recovery_second_esc_sent", False)
        ):
            self._log("[恢复] 暂未看到可抛钩提示，执行一次轻量 ESC 复位。")
            self.ctrl.release_all()
            self._esc_safe_gap(0.30)
            if not self._tap_key_if_running('esc', duration=0.12):
                return
            self.round.recovery_second_esc_sent = True
            self._sleep_interruptible(0.35)
            return

        recovery_timeout = max(4, min(int(self.config.get("recovery_timeout", 8)), 20))
        if elapsed > recovery_timeout:
            reason = getattr(self.round, "recovery_reason", "未知异常")
            self._log(f"[恢复] {reason} 后 {recovery_timeout} 秒仍未确认可抛钩界面，退回待机继续扫描；如画面已被用户接管，请手动停止脚本。")
            self._reset_round_state()
            self.current_state = self.STATE_IDLE
            return

        self._sleep_interruptible(0.2)

    def _handle_failed(self):
        # 注意: 这里的"溜走了"如果用户提供了图片，建议也走 find_template
        # 目前暂时作为占位或使用超时跳出
        self._log("[失败/结束] 释放按键，等待复位。")
        self._enter_recovering("进入失败兜底状态", record_empty=True, press_esc=False)
