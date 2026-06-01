"""耐力条检测相关方法 —— 从 StateMachine 中提取。"""

import time


class FishingBarDetector:
    """封装耐力条（fishing bar）检测与过滤逻辑。"""

    def __init__(self, sm):
        self._sm = sm

    # ------------------------------------------------------------------
    # 耐力条检测结果过滤
    # ------------------------------------------------------------------
    def filter_bar_detection(self, target_x, cursor_x, target_w, confidence, roi_width):
        now = time.time()
        if target_x is None or cursor_x is None or target_w is None:
            previous_time = getattr(self._sm.round, "last_valid_bar_time", 0)
            if previous_time and now - previous_time <= 0.70:
                fallback_target = self._sm.round.last_valid_target_x if target_x is None else target_x
                fallback_cursor = self._sm.round.last_valid_cursor_x if cursor_x is None else cursor_x
                fallback_width = self._sm.round.last_valid_target_w if target_w is None else target_w
                if fallback_target is not None and fallback_cursor is not None and fallback_width is not None:
                    return fallback_target, fallback_cursor, fallback_width, max(float(confidence or 0.0), 0.30)
            return None, cursor_x, target_w, confidence

        min_confidence = self._sm._normalize_ratio_config("bar_confidence_threshold", 0.45, 0.25, 0.85)
        if confidence < min_confidence:
            previous_time = getattr(self._sm.round, "last_valid_bar_time", 0)
            if previous_time and now - previous_time <= 0.70:
                fallback_cursor = self._sm.round.last_valid_cursor_x if cursor_x is None else cursor_x
                return self._sm.round.last_valid_target_x, fallback_cursor, self._sm.round.last_valid_target_w, confidence
            return None, cursor_x, target_w, confidence

        previous_cursor_x = getattr(self._sm.round, "last_valid_cursor_x", None)
        previous_cursor_time = getattr(self._sm.round, "last_valid_cursor_time", 0)
        if previous_cursor_x is not None and previous_cursor_time and now - previous_cursor_time <= 0.75:
            cursor_jump = abs(cursor_x - previous_cursor_x)
            cursor_jump_limit = max(72, int(roi_width * 0.24))
            if cursor_jump > cursor_jump_limit and confidence < 0.86:
                self._sm.round.bar_cursor_jump_reject_count = int(getattr(self._sm.round, "bar_cursor_jump_reject_count", 0)) + 1
                if self._sm.round.bar_cursor_jump_reject_count <= 4:
                    cursor_x = previous_cursor_x
                    confidence = max(0.0, confidence * 0.75)
                else:
                    return None, cursor_x, target_w, confidence

        previous_x = getattr(self._sm.round, "last_valid_target_x", None)
        previous_w = getattr(self._sm.round, "last_valid_target_w", None) or target_w
        previous_time = getattr(self._sm.round, "last_valid_bar_time", 0)
        if previous_x is not None and previous_time and now - previous_time <= 0.9:
            width_jump_limit = max(int(roi_width * 0.42), int(previous_w * 2.25), 120)
            if target_w > width_jump_limit:
                self._sm.round.bar_jump_reject_count = int(getattr(self._sm.round, "bar_jump_reject_count", 0)) + 1
                if now - previous_time <= 0.75:
                    return previous_x, cursor_x, previous_w, max(0.0, confidence * 0.55)
                return None, cursor_x, target_w, confidence
            jump = abs(target_x - previous_x)
            jump_limit = max(56, int(roi_width * 0.18), int(max(previous_w, target_w) * 1.55))
            if jump > jump_limit and confidence < 0.82:
                self._sm.round.bar_jump_reject_count = int(getattr(self._sm.round, "bar_jump_reject_count", 0)) + 1
                if self._sm.round.bar_jump_reject_count <= 3 and now - previous_time <= 0.65:
                    return previous_x, cursor_x, previous_w, max(0.0, confidence * 0.7)
                return None, cursor_x, target_w, confidence

        self._sm.round.last_valid_target_x = int(target_x)
        self._sm.round.last_valid_target_w = int(target_w)
        self._sm.round.last_valid_bar_time = now
        self._sm.round.last_valid_cursor_x = int(cursor_x)
        self._sm.round.last_valid_cursor_time = now
        self._sm.round.bar_jump_reject_count = 0
        self._sm.round.bar_cursor_jump_reject_count = 0
        return target_x, cursor_x, target_w, confidence

    # ------------------------------------------------------------------
    # 坐标转换
    # ------------------------------------------------------------------
    def bar_local_to_client_x(self, rect, roi, target_x, cursor_x):
        """把不同溜鱼 ROI 内的局部 x 坐标统一到客户区 x 坐标。"""
        if not rect or not roi:
            return target_x, cursor_x
        roi_left = int(rect[2] * roi[0])
        converted_target = None if target_x is None else int(round(roi_left + float(target_x)))
        converted_cursor = None if cursor_x is None else int(round(roi_left + float(cursor_x)))
        return converted_target, converted_cursor

    # ------------------------------------------------------------------
    # 调试帧检查
    # ------------------------------------------------------------------
    def should_draw_fishing_debug_frame(self):
        if not self._sm.config.get("debug_mode", False):
            return False
        if self._sm.debug_queue is None or self._sm.debug_queue.qsize() >= 2:
            return False
        now = time.time()
        return getattr(self._sm, "_last_debug_time", 0) == 0 or (now - self._sm._last_debug_time) >= 0.25

    # ------------------------------------------------------------------
    # 游标模板选择
    # ------------------------------------------------------------------
    def cursor_templates_for_current_frame(self):
        now = time.time()
        if getattr(self._sm.round, "fishing_control_started", False) or getattr(self._sm.round, "confirmed_fishing_bar", False):
            return None
        recent_cursor_time = getattr(self._sm.round, "last_valid_cursor_time", 0)
        if recent_cursor_time and now - recent_cursor_time <= 1.20:
            return None
        last_template_time = getattr(self._sm.round, "last_cursor_template_time", 0)
        if last_template_time and now - last_template_time < 0.80:
            return None
        self._sm.round.last_cursor_template_time = now
        return self._sm.tpl.cursor_templates()

    # ------------------------------------------------------------------
    # ROI 分析
    # ------------------------------------------------------------------
    def analyze_fishing_bar_roi(self, rect, roi, draw_debug=False):
        bar_img = self._sm.sc.capture_relative(rect, *roi)
        if bar_img is None:
            return {
                "target_x": None,
                "cursor_x": None,
                "target_w": None,
                "debug_img": None,
                "confidence": 0.0,
                "width": int(rect[2] * roi[2]) if rect else 0,
                "roi": roi,
                "capture_failed": True,
            }

        target_x, cursor_x, target_w, debug_img, confidence = self._sm.vis.analyze_fishing_bar(
            bar_img,
            cursor_template_paths=self.cursor_templates_for_current_frame(),
            cursor_color_reference_paths=self._sm.tpl.cursor_templates(),
            target_color_reference_paths=self._sm.tpl.target_bar_templates(),
            cursor_scale_range=self._sm.tpl.scale_range(rect, 0.70, 1.55),
            cursor_scale_steps=5,
            draw_debug=draw_debug,
        )
        target_x, cursor_x = self.bar_local_to_client_x(rect, roi, target_x, cursor_x)
        return {
            "target_x": target_x,
            "cursor_x": cursor_x,
            "target_w": target_w,
            "debug_img": debug_img,
            "confidence": float(confidence or 0.0),
            "width": bar_img.shape[1],
            "roi": roi,
            "capture_failed": False,
        }

    # ------------------------------------------------------------------
    # 检测选择
    # ------------------------------------------------------------------
    def select_fishing_bar_detection(self, rect, primary_roi):
        self._sm.round.last_bar_capture_failed = False
        primary = self.analyze_fishing_bar_roi(
            rect,
            primary_roi,
            draw_debug=self.should_draw_fishing_debug_frame(),
        )
        if primary is None:
            return None, None, None, None, 0.0
        if primary.get("capture_failed"):
            self._sm.round.last_bar_capture_failed = True
            return None, None, None, None, 0.0

        target_x, cursor_x, target_w, confidence = self.filter_bar_detection(
            primary.get("target_x"),
            primary.get("cursor_x"),
            primary.get("target_w"),
            primary.get("confidence"),
            primary.get("width") or int(rect[2] * primary_roi[2]),
        )
        return target_x, cursor_x, target_w, primary.get("debug_img"), confidence

    # ------------------------------------------------------------------
    # 断帧保持
    # ------------------------------------------------------------------
    def hold_recent_fishing_control_on_gap(self):
        """短时截图/识别断帧时保持当前 A/D，避免白天高亮环境下游标停住。"""
        if not getattr(self._sm.round, "fishing_control_started", False):
            return False

        now = time.time()
        last_valid_time = getattr(self._sm.round, "last_valid_bar_time", 0)
        if not last_valid_time or now - last_valid_time > 0.55:
            self._sm.round._gap_hold_streak = 0
            return False

        # Limit consecutive gap-hold frames: dusk/warm backgrounds cause false
        # cursor detections that refresh last_valid_bar_time indefinitely.
        # After ~0.3s of continuous gap-hold, force release so missing_timeout can trigger.
        streak = getattr(self._sm.round, "_gap_hold_streak", 0) + 1
        self._sm.round._gap_hold_streak = streak
        if streak > 30:  # ~0.3s at 100Hz loop rate
            return False

        current_direction = int(getattr(self._sm.round, "fish_control_direction", 0) or 0)
        if current_direction == 0:
            last_error = float(getattr(self._sm.round, "last_control_error", 0) or 0)
            target_w = getattr(self._sm.round, "last_control_target_w", None) or getattr(self._sm.round, "last_valid_target_w", None) or 80
            if abs(last_error) >= max(0.8, min(float(target_w) * 0.012, 4.0)):
                current_direction = 1 if last_error > 0 else -1

        if current_direction == 0:
            return False

        self._sm.fish_ctrl.apply_direction(current_direction)
        return True

    # ------------------------------------------------------------------
    # 丢失后判定
    # ------------------------------------------------------------------
    def should_enter_result_after_confirmed_bar_missing(self, missing_elapsed):
        if not getattr(self._sm.round, "round_had_fishing_bar", False):
            return False
        if not getattr(self._sm.round, "confirmed_fishing_bar", False):
            return False
        if not getattr(self._sm.round, "fishing_control_started", False):
            return False
        if int(getattr(self._sm.round, "fishing_control_frame_count", 0) or 0) < 3:
            return False

        missing_start = float(getattr(self._sm.round, "missing_start_time", 0) or 0)
        last_valid_time = float(getattr(self._sm.round, "last_valid_bar_time", 0) or 0)
        if missing_start <= 0 or last_valid_time <= 0:
            return False

        now = time.time()
        if now - last_valid_time < 1.25:
            return False

        fast_delay = self._sm._normalize_ratio_config("bar_missing_fast_result_delay", 0.95, 0.70, 1.80)
        configured_timeout = max(0.8, min(float(self._sm.config.get("bar_missing_timeout", 2)), 5.0))
        if missing_elapsed < min(fast_delay, configured_timeout):
            return False

        quick_probe_done = float(getattr(self._sm.round, "result_quick_check_last", 0) or 0) > missing_start
        full_probe_done = float(getattr(self._sm.round, "result_full_check_last", 0) or 0) > missing_start
        return quick_probe_done and full_probe_done

    # ------------------------------------------------------------------
    # 进入结果状态
    # ------------------------------------------------------------------
    def enter_result_after_bar_missing(self):
        self._sm._log("[溜鱼] 耐力条消失，停止溜鱼，进入结果判定...")
        self._sm.ctrl.release_all()
        self._sm.round.fishing_start_time = 0
        self._sm.round.missing_start_time = 0
        self._sm.round.result_quick_check_last = 0
        self._sm.round.last_cursor_x = None
        self._sm.round.seen_fishing_bar = False
        self._sm.round.last_target_time = 0
        self._sm.round.target_velocity = 0
        self._sm.current_state = self._sm.STATE_RESULT
