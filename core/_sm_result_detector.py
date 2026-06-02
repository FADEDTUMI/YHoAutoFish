import time
from pathlib import Path

import cv2

from core.tracker import EventTracker


class ResultDetector:
    """从 StateMachine 提取的结果检测相关方法集合。"""

    def __init__(self, sm):
        self._sm = sm

    # ------------------------------------------------------------------ #
    #  信号匹配基础
    # ------------------------------------------------------------------ #

    def detect_failed_result(self, rect):
        failed_templates = self._sm.tpl.failed_text_templates()
        if not failed_templates:
            return None

        rois = (
            (0.18, 0.38, 0.64, 0.22),
            (0.20, 0.45, 0.60, 0.12),
            (0.12, 0.32, 0.76, 0.34),
        )
        strategies = (
            {"name": "failed-edge", "threshold": 0.60, "use_edge": True},
            {"name": "failed-plain", "threshold": 0.66},
        )
        best = None
        for roi in rois:
            image = self._sm.sc.capture_relative(rect, *roi)
            if image is None:
                continue
            loc, conf, matched_path, strategy = self._sm.vis.find_best_template_multi_strategy(
                image,
                failed_templates,
                strategies,
                threshold=0.64,
                scale_range=self._sm.tpl.scale_range(rect, 0.68, 1.42),
                scale_steps=7,
            )
            if best is None or conf > best["confidence"]:
                best = {"location": loc, "confidence": conf, "template": matched_path, "strategy": strategy, "roi": roi}
            if loc is not None:
                return best
        return None

    def match_result_signal(self, rect, kind, templates, rois, strategies, threshold, low_factor, high_factor, scale_steps):
        if not templates:
            return None

        best = None
        for roi in rois:
            image = self._sm.sc.capture_relative(rect, *roi)
            if image is None:
                continue
            loc, conf, matched_path, strategy = self._sm.vis.find_best_template_multi_strategy(
                image,
                templates,
                strategies,
                threshold=threshold,
                scale_range=self._sm.tpl.scale_range(rect, low_factor, high_factor),
                scale_steps=scale_steps,
            )
            if best is None or conf > best["confidence"]:
                best = {
                    "kind": kind,
                    "location": loc,
                    "confidence": conf,
                    "template": matched_path,
                    "strategy": strategy,
                    "roi": roi,
                }
            if loc is not None:
                return best

        return None

    def build_success_result_info(self, success_signals):
        best = max(success_signals, key=lambda item: item["confidence"])
        return {
            "location": best.get("location"),
            "confidence": min(0.99, sum(item["confidence"] for item in success_signals) / len(success_signals)),
            "template": best.get("template"),
            "strategy": best.get("strategy"),
            "signals": success_signals,
        }

    # ------------------------------------------------------------------ #
    #  成功 / 失败检测（按速度分层）
    # ------------------------------------------------------------------ #

    def detect_ultrafast_success_result(self, rect):
        close_info = self.match_result_signal(
            rect,
            "click close prompt",
            self._sm.tpl.success_close_prompt_templates(),
            (
                (0.22, 0.76, 0.56, 0.20),
            ),
            (
                {"name": "close-ultra-edge", "threshold": 0.70, "use_edge": True, "early_accept": 0.92},
            ),
            threshold=0.70,
            low_factor=0.82,
            high_factor=1.24,
            scale_steps=5,
        )
        if close_info and close_info.get("location") and close_info.get("confidence", 0.0) >= 0.84:
            return self.build_success_result_info([close_info])

        exp_info = self.match_result_signal(
            rect,
            "fishing exp prompt",
            self._sm.tpl.success_exp_templates(),
            (
                (0.24, 0.48, 0.52, 0.25),
            ),
            (
                {"name": "exp-ultra-edge", "threshold": 0.64, "use_edge": True, "early_accept": 0.90},
            ),
            threshold=0.64,
            low_factor=0.82,
            high_factor=1.24,
            scale_steps=5,
        )
        if exp_info and exp_info.get("location") and exp_info.get("confidence", 0.0) >= 0.92:
            weight_info = self.match_result_signal(
                rect,
                "重量单位 g",
                self._sm.tpl.weight_unit_templates(),
                (
                    (0.33, 0.58, 0.34, 0.18),
                ),
                (
                    {"name": "g-ultra-plain", "threshold": 0.70},
                ),
                threshold=0.70,
                low_factor=0.82,
                high_factor=1.24,
                scale_steps=5,
            )
            if weight_info and weight_info.get("location"):
                return self.build_success_result_info([exp_info, weight_info])

        return None

    def detect_fast_success_result(self, rect, fast_only=False):
        if self._sm.cast_det.detect_initial_f_prompt_quick(rect, threshold=0.88):
            return None
        ultra_info = self.detect_ultrafast_success_result(rect)
        if ultra_info and ultra_info.get("location"):
            return ultra_info
        if fast_only:
            return None

        close_info = self.match_result_signal(
            rect,
            "点击关闭提示",
            self._sm.tpl.success_close_prompt_templates(),
            (
                (0.22, 0.76, 0.56, 0.20),
                (0.18, 0.74, 0.64, 0.24),
            ),
            (
                {"name": "close-fast-edge", "threshold": 0.66, "use_edge": True},
                {"name": "close-fast-plain", "threshold": 0.74},
            ),
            threshold=0.66,
            low_factor=0.62,
            high_factor=1.50,
            scale_steps=11,
        )
        if not close_info or not close_info.get("location"):
            return None

        success_signals = [close_info]
        if close_info.get("confidence", 0.0) >= 0.92:
            return self.build_success_result_info(success_signals)

        weight_info = self.match_result_signal(
            rect,
            "重量单位 g",
            self._sm.tpl.weight_unit_templates(),
            (
                (0.33, 0.58, 0.34, 0.18),
                (0.30, 0.56, 0.42, 0.22),
            ),
            (
                {"name": "g-fast-plain", "threshold": 0.64},
            ),
            threshold=0.64,
            low_factor=0.70,
            high_factor=1.35,
            scale_steps=9,
        )
        if weight_info and weight_info.get("location"):
            success_signals.append(weight_info)
            return self.build_success_result_info(success_signals)

        exp_info = self.match_result_signal(
            rect,
            "获得钓鱼经验",
            self._sm.tpl.success_exp_templates(),
            (
                (0.24, 0.48, 0.52, 0.25),
                (0.18, 0.42, 0.64, 0.32),
            ),
            (
                {"name": "exp-fast-edge", "threshold": 0.58, "use_edge": True},
            ),
            threshold=0.58,
            low_factor=0.70,
            high_factor=1.35,
            scale_steps=9,
        )
        if exp_info and exp_info.get("location"):
            success_signals.append(exp_info)
            return self.build_success_result_info(success_signals)

        return None

    def detect_fast_failed_result(self, rect):
        return self.match_result_signal(
            rect,
            "鱼儿溜走了",
            self._sm.tpl.failed_text_templates(),
            (
                (0.18, 0.38, 0.64, 0.22),
            ),
            (
                {"name": "failed-fast-edge", "threshold": 0.64, "use_edge": True},
                {"name": "failed-fast-plain", "threshold": 0.70},
            ),
            threshold=0.68,
            low_factor=0.78,
            high_factor=1.26,
            scale_steps=5,
        )

    def detect_success_result(self, rect):
        if self._sm.cast_det.detect_initial_f_prompt_quick(rect, threshold=0.88):
            return None

        success_signals = []

        weight_info = self.match_result_signal(
            rect,
            "重量单位 g",
            self._sm.tpl.weight_unit_templates(),
            (
                (0.30, 0.58, 0.42, 0.22),
                (0.33, 0.60, 0.34, 0.18),
                (0.36, 0.62, 0.28, 0.16),
                (0.25, 0.54, 0.50, 0.30),
            ),
            (
                {"name": "g-edge", "threshold": 0.50, "use_edge": True},
                {"name": "g-plain", "threshold": 0.62},
            ),
            threshold=0.58,
            low_factor=0.45,
            high_factor=1.95,
            scale_steps=17,
        )
        if weight_info and weight_info.get("location"):
            success_signals.append(weight_info)

        close_info = self.match_result_signal(
            rect,
            "点击关闭提示",
            self._sm.tpl.success_close_prompt_templates(),
            (
                (0.24, 0.76, 0.52, 0.20),
                (0.20, 0.80, 0.60, 0.16),
                (0.30, 0.82, 0.40, 0.14),
            ),
            (
                {"name": "close-edge", "threshold": 0.50, "use_edge": True},
                {"name": "close-plain", "threshold": 0.62},
            ),
            threshold=0.60,
            low_factor=0.52,
            high_factor=1.80,
            scale_steps=17,
        )
        if close_info and close_info.get("location"):
            success_signals.append(close_info)
            if len(success_signals) >= 2:
                return self.build_success_result_info(success_signals)

        exp_info = self.match_result_signal(
            rect,
            "获得钓鱼经验",
            self._sm.tpl.success_exp_templates(),
            (
                (0.24, 0.48, 0.52, 0.30),
                (0.30, 0.52, 0.40, 0.24),
                (0.18, 0.42, 0.64, 0.38),
            ),
            (
                {"name": "exp-edge", "threshold": 0.50, "use_edge": True},
                {"name": "exp-plain", "threshold": 0.60},
            ),
            threshold=0.58,
            low_factor=0.52,
            high_factor=1.80,
            scale_steps=17,
        )
        if exp_info and exp_info.get("location"):
            success_signals.append(exp_info)

        if len(success_signals) < 2:
            return None

        return self.build_success_result_info(success_signals)

    def format_success_signals(self, success_info):
        parts = []
        for item in (success_info or {}).get("signals", []):
            matched_path = item.get("template")
            matched_name = Path(matched_path).name if matched_path else "未知模板"
            parts.append(f"{item.get('kind') or '成功特征'}:{item.get('confidence', 0):.2f}/{matched_name}/{item.get('strategy') or '默认'}")
        return "；".join(parts) if parts else "无"

    # ------------------------------------------------------------------ #
    #  处理方法
    # ------------------------------------------------------------------ #

    def record_empty_result_once(self, reason):
        if getattr(self._sm.round, "result_empty_recorded", False):
            return
        self._sm.record_mgr.add_empty_catch()
        self._sm.round.result_empty_recorded = True
        self._sm._log(f"[结算] {reason}，已记录一次失败/空杆尝试。")

    def finish_fast_success_result(self, rect, success_info, source_label="溜鱼"):
        self.clear_failed_result_candidate()
        self._sm.ctrl.release_all()
        self.finish_success_result(rect, success_info, attempt=1, max_attempts=1, source_label=source_label)

    def clear_failed_result_candidate(self):
        self._sm.round.failed_result_candidate_seen_time = 0
        self._sm.round.failed_result_candidate_count = 0
        self._sm.round.failed_result_candidate_signature = ""

    def is_strong_failed_result(self, failed_info):
        confidence = float((failed_info or {}).get("confidence") or 0.0)
        strategy = ((failed_info or {}).get("strategy") or "").lower()
        if "edge" in strategy:
            return confidence >= 0.70
        return confidence >= 0.76

    def maybe_finish_failed_result(self, rect, failed_info, source_label="结算"):
        if not failed_info or not failed_info.get("location"):
            return False

        confidence = float((failed_info or {}).get("confidence") or 0.0)
        strategy = ((failed_info or {}).get("strategy") or "").lower()

        # 失败横幅只有一张文字模板。低置信度 plain 匹配容易在成功结算/过渡动画中误报，
        # 因此只允许高置信度立即判失败；其余候选必须连续出现并在确认前再次排除成功结算。
        if self.is_strong_failed_result(failed_info):
            self.clear_failed_result_candidate()
            self.finish_failed_result(failed_info, source_label=source_label)
            return True

        min_candidate = 0.64 if "edge" in strategy else 0.70
        if confidence < min_candidate:
            self.clear_failed_result_candidate()
            return False

        now = time.time()
        matched_path = failed_info.get("template") or ""
        roi = failed_info.get("roi") or ()
        signature = f"{matched_path}|{strategy}|{roi}"
        if signature != getattr(self._sm.round, "failed_result_candidate_signature", ""):
            self._sm.round.failed_result_candidate_signature = signature
            self._sm.round.failed_result_candidate_seen_time = now
            self._sm.round.failed_result_candidate_count = 1
            return False

        self._sm.round.failed_result_candidate_count = int(getattr(self._sm.round, "failed_result_candidate_count", 0)) + 1
        seen_time = float(getattr(self._sm.round, "failed_result_candidate_seen_time", 0) or now)
        if now - seen_time < 0.35 or self._sm.round.failed_result_candidate_count < 2:
            return False

        success_info = self.detect_fast_success_result(rect, fast_only=False)
        if success_info and success_info.get("location"):
            self.clear_failed_result_candidate()
            self.finish_fast_success_result(rect, success_info, source_label=source_label)
            return True

        self.clear_failed_result_candidate()
        self.finish_failed_result(failed_info, source_label=source_label)
        return True

    # ------------------------------------------------------------------ #
    #  信号检查
    # ------------------------------------------------------------------ #

    def check_result_signals_during_fishing(self, rect, elapsed):
        now = time.time()
        interval = self._sm._normalize_ratio_config("fishing_result_check_interval", 0.65, 0.35, 1.50)
        if now - getattr(self._sm.round, "fishing_result_check_last", 0) < interval:
            return False
        self._sm.round.fishing_result_check_last = now

        success_info = self.detect_fast_success_result(rect, fast_only=True)
        if success_info and success_info.get("location"):
            self.finish_fast_success_result(rect, success_info, source_label="溜鱼")
            return True

        failed_interval = self._sm._normalize_ratio_config("fishing_failed_check_interval", 1.25, 0.70, 3.00)
        if elapsed >= 1.5 and now - getattr(self._sm.round, "fishing_failed_check_last", 0) >= failed_interval:
            self._sm.round.fishing_failed_check_last = now
            failed_info = self.detect_fast_failed_result(rect)
            if self.maybe_finish_failed_result(rect, failed_info, source_label="溜鱼"):
                return True

        return False

    def check_terminal_result_before_bar(self, rect, elapsed):
        now = time.time()
        interval = 0.25 if elapsed < 3.0 else 0.20
        if now - getattr(self._sm.round, "result_quick_check_last", 0) < interval:
            return False
        self._sm.round.result_quick_check_last = now

        success_info = self.detect_fast_success_result(rect, fast_only=True)
        if success_info and success_info.get("location"):
            self.finish_fast_success_result(rect, success_info, source_label="溜鱼")
            return True

        if elapsed >= 2.0:
            failed_info = self.detect_fast_failed_result(rect)
            if self.maybe_finish_failed_result(rect, failed_info, source_label="溜鱼"):
                return True

        return False

    # ------------------------------------------------------------------ #
    #  结算相关
    # ------------------------------------------------------------------ #

    def finish_failed_result(self, failed_info, source_label="结算"):
        matched_path = failed_info.get("template") if failed_info else None
        matched_name = Path(matched_path).name if matched_path else "未知模板"
        confidence = float((failed_info or {}).get("confidence") or 0.0)
        strategy = (failed_info or {}).get("strategy") or "默认"
        self._sm._log(f"[{source_label}] 识别到\u201c鱼儿溜走了\u201d横幅 (置信度: {confidence:.2f}，模板: {matched_name}，策略: {strategy})！判定为钓鱼失败。")
        self._sm.ctrl.release_all()
        round_duration = self.round_fishing_elapsed()
        try:
            EventTracker.get().fishing_failed("鱼儿溜走了", round_duration)
        except Exception as exc:
            import logging; logging.getLogger(__name__).debug("tracker.fishing_failed failed: %s", exc)
        self._sm._enter_recovering("识别到鱼儿溜走失败提示", record_empty=True, press_esc=False)

    def finish_empty_ready_result(self, ready_info, source_label="结算"):
        kind = (ready_info or {}).get("kind") or "可抛钩界面"
        if getattr(self._sm.round, "success_recorded_pending_close", False):
            self._sm._log(f"[{source_label}] 已检测到{kind}，确认成功结算界面已关闭。当前累计钓获: {self._sm.fish_count} 条。等待抛竿...")
            self._sm._reset_round_state()
            self._sm.current_state = self._sm.STATE_IDLE
            return
        if getattr(self._sm.round, "round_had_fishing_bar", False):
            self.record_empty_result_once(f"未检测到成功结算或失败横幅，但已回到{kind}，判定本轮失败/空杆")
        self._sm._log(f"[{source_label}] 已回到{kind}，直接进入待机。")
        self._sm._reset_round_state()
        self._sm.current_state = self._sm.STATE_IDLE

    def save_empty_ready_debug(self, rect, ready_info, source_label):
        if getattr(self._sm.round, "result_ready_debug_saved", False):
            return
        self._sm.round.result_ready_debug_saved = True
        if not self._sm.config.get("debug_mode", False) or self._sm.sc is None:
            return
        image = self._sm.sc.capture_relative(rect, 0, 0, 1, 1)
        if image is None or image.size <= 0:
            return
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        path = f"debug_result_empty_ready_{timestamp}.png"
        cv2.imwrite(path, image)
        kind = (ready_info or {}).get("kind") or "可抛钩界面"
        self._sm._log(f"[排错] {source_label} 未识别到成功/失败但准备判定为空杆，已保存画面: {path}，检测到: {kind}")

    def clear_result_ready_candidate(self):
        self._sm.round.result_ready_seen_time = 0
        self._sm.round.result_ready_confirm_count = 0
        self._sm.round.result_ready_last_kind = ""

    def round_fishing_elapsed(self):
        r = self._sm.round
        start_time = (
            r.fishing_control_started_time
            or r.fishing_bar_confirmed_time
            or self._sm.fishing_start_time
            or r.fishing_start_time
        )
        if not start_time:
            return 0.0
        return max(0.0, time.time() - start_time)

    def is_known_settlement_name(self, fish_name):
        fish_name = (fish_name or "").strip()
        if not fish_name or fish_name in {"未知鱼类", "未识别鱼类"}:
            return False
        return fish_name in self._sm.record_mgr.get_encyclopedia()

    def try_finish_success_by_settlement_probe(self, rect, source_label="结算"):
        if getattr(self._sm.round, "result_text_probe_done", False):
            return False
        self._sm.round.result_text_probe_done = True
        fish_name, weight_g = self._sm.ocr_module.read_settlement_info(rect, save_unknown_debug=False)
        if not self.is_known_settlement_name(fish_name):
            return False
        success_info = {
            "confidence": 0.62,
            "signals": [
                {
                    "kind": "结算文字",
                    "confidence": 0.62,
                    "template": None,
                    "strategy": "settlement-text",
                }
            ],
        }
        self.finish_success_result(
            rect,
            success_info,
            attempt=1,
            max_attempts=1,
            source_label=source_label,
            settlement_info=(fish_name, weight_g),
        )
        return True

    def confirm_empty_ready_result(self, rect, ready_info, source_label="结算"):
        if not ready_info or not ready_info.get("location"):
            return False
        if getattr(self._sm.round, "success_recorded_pending_close", False):
            self.finish_empty_ready_result(ready_info, source_label=source_label)
            return True

        success_info = self.detect_fast_success_result(rect, fast_only=True)
        if success_info and success_info.get("location"):
            self.finish_fast_success_result(rect, success_info, source_label=source_label)
            return True

        failed_info = self.detect_fast_failed_result(rect)
        if self.maybe_finish_failed_result(rect, failed_info, source_label=source_label):
            return True

        if not getattr(self._sm.round, "round_had_fishing_bar", False):
            self.finish_empty_ready_result(ready_info, source_label=source_label)
            return True

        now = time.time()
        kind = (ready_info or {}).get("kind") or "可抛钩界面"
        if self._sm.round.result_ready_last_kind != kind or getattr(self._sm.round, "result_ready_seen_time", 0) == 0:
            self._sm.round.result_ready_seen_time = now
            self._sm.round.result_ready_confirm_count = 1
            self._sm.round.result_ready_last_kind = kind
            elapsed = self.round_fishing_elapsed()
            self._sm._log(f"[{source_label}] 本轮溜鱼耗时 {elapsed:.1f}s，已检测到{kind}；继续短暂确认成功/失败结算，避免误记空杆。")
            return False

        self._sm.round.result_ready_confirm_count += 1
        confirm_delay = self._sm._normalize_ratio_config("empty_ready_confirm_delay", 0.45, 0.25, 3.0)
        if getattr(self._sm.round, "round_had_fishing_bar", False):
            confirm_delay = max(confirm_delay, 3.0)
        min_confirm_count = 4 if getattr(self._sm.round, "round_had_fishing_bar", False) else 2
        if now - self._sm.round.result_ready_seen_time < confirm_delay or self._sm.round.result_ready_confirm_count < min_confirm_count:
            return False

        success_info = self.detect_success_result(rect)
        if success_info and success_info.get("location"):
            self.finish_fast_success_result(rect, success_info, source_label=source_label)
            return True

        failed_info = self.detect_failed_result(rect)
        if self.maybe_finish_failed_result(rect, failed_info, source_label=source_label):
            return True

        if getattr(self._sm.round, "round_had_fishing_bar", False):
            if self.try_finish_success_by_settlement_probe(rect, source_label=source_label):
                return True

        if getattr(self._sm.round, "round_had_fishing_bar", False):
            last_full_check = getattr(self._sm.round, "result_full_check_last", 0)
            if not last_full_check or now - last_full_check > 0.75:
                return False

        self.save_empty_ready_debug(rect, ready_info, source_label)
        self.finish_empty_ready_result(ready_info, source_label=source_label)
        return True

    def wait_after_settlement_close(self, rect, max_delay):
        if getattr(self._sm, "_stop_requested", False):
            return False
        deadline = time.time() + max_delay
        if not self._sm._sleep_interruptible(min(0.18, max_delay)):
            return False
        while time.time() < deadline:
            if getattr(self._sm, "_stop_requested", False):
                return False
            current_rect = self._sm.wm.get_client_rect() or rect
            ready_info = self._sm.cast_det.detect_cast_prompt_after_settlement(current_rect)
            if not (ready_info and ready_info.get("location")):
                ready_info = self._sm.cast_det.detect_ready_to_cast(current_rect, allow_heavy=False, require_initial_controls=True)
            if ready_info and ready_info.get("location"):
                self._sm._log(f"[结算] 已检测到{ready_info.get('kind') or '可抛钩界面'}，提前进入下一轮。")
                return True
            if not self._sm._sleep_interruptible(0.10):
                return False
        return False

    def detect_success_settlement_still_visible(self, rect):
        success_info = self.detect_fast_success_result(rect, fast_only=True)
        if success_info and success_info.get("location"):
            return success_info
        success_info = self.detect_success_result(rect)
        if success_info and success_info.get("location"):
            return success_info
        return None

    def finish_success_result(self, rect, success_info, attempt=1, max_attempts=1, source_label="结算", settlement_info=None):
        if getattr(self._sm, "_stop_requested", False):
            return
        self.clear_failed_result_candidate()

        # 极简模式：跳过OCR识别，仅计数
        if self._sm.config.get("minimal_settlement_mode", False):
            if not getattr(self._sm.round, "success_recorded_pending_close", False):
                self._sm.ctrl.release_all()
                self._sm.record_mgr.add_catch_count_only()
                self._sm.fish_count += 1
                self._sm._record_auto_sell_catch()
                self._sm.round.success_recorded_pending_close = True
                round_duration = self.round_fishing_elapsed()
                try:
                    EventTracker.get().fishing_success("极简模式", 0, "", round_duration)
                except Exception:
                    pass
                self._sm._log(f"[{source_label}] 极简模式：跳过结算识别。当前累计钓获: {self._sm.fish_count} 条。")
            wait_time = max(1.0, min(float(self._sm.config.get("minimal_mode_wait", 2.5)), 5.0))
            if not self._sm._sleep_interruptible(wait_time):
                return
            self._sm._esc_safe_gap(0.30)
            if not self._sm._tap_key_if_running('esc', duration=0.15):
                return
            close_delay = max(0.4, min(float(self._sm.config.get("settlement_close_delay", 1)), 5.0))
            if not self._sm._sleep_interruptible(close_delay):
                return
            self._sm._reset_round_state()
            self._sm.current_state = self._sm.STATE_IDLE
            return

        if not getattr(self._sm.round, "success_recorded_pending_close", False):
            self._sm._log(f"[{source_label}] 识别到成功结算组合特征 (综合置信度: {success_info['confidence']:.2f}，{self.format_success_signals(success_info)})，开始识别鱼类信息...")

            if settlement_info is None:
                fish_name, weight_g = self._sm.ocr_module.read_settlement_info(rect)
            else:
                fish_name, weight_g = settlement_info
            if getattr(self._sm, "_stop_requested", False):
                return
            self._sm.record_mgr.add_catch(fish_name, weight_g)
            self._sm.fish_count += 1
            self._sm._record_auto_sell_catch()
            self._sm.round.success_recorded_pending_close = True
            round_duration = self.round_fishing_elapsed()
            try:
                rarity = ""
                enc_entry = self._sm.record_mgr.get_encyclopedia().get(fish_name, {})
                if isinstance(enc_entry, dict):
                    rarity = enc_entry.get("rarity", "")
                EventTracker.get().fishing_success(fish_name, weight_g, rarity, round_duration)
            except Exception as exc:
                import logging; logging.getLogger(__name__).debug("tracker.fishing_success failed: %s", exc)
            if getattr(self._sm, "_stop_requested", False):
                return

            self._sm._log(f"[结算] 捕获: {fish_name}, 重量: {weight_g}g。尝试 ESC 关闭结算界面 (尝试 {attempt}/{max_attempts})...")
        else:
            self._sm._log(f"[结算] 本次成功结算已记录，继续尝试 ESC 关闭结算界面 (尝试 {attempt}/{max_attempts})...")

        self._sm._esc_safe_gap(0.30)
        if not self._sm._tap_key_if_running('esc', duration=0.15):
            return
        self._sm.round.success_close_retry_count = max(int(getattr(self._sm.round, "success_close_retry_count", 0)), int(attempt))
        self._sm.round.success_close_last_esc = time.time()
        if getattr(self._sm, "_stop_requested", False):
            return

        close_delay = max(0.4, min(float(self._sm.config.get("settlement_close_delay", 1)), 5.0))
        closed = self.wait_after_settlement_close(rect, close_delay)
        if getattr(self._sm, "_stop_requested", False):
            return
        if closed:
            self._sm._log(f"[结算] 成功关闭结算界面。当前累计钓获: {self._sm.fish_count} 条。等待抛竿...")
            self._sm._reset_round_state()
            self._sm.current_state = self._sm.STATE_IDLE
            return

        self._sm._log("[结算] 已记录本次钓获，但尚未确认结算界面关闭，继续停留在结算状态确认关闭。")
        self._sm.current_state = self._sm.STATE_RESULT

    def check_result_signals_after_bar_missing(self, rect, missing_elapsed):
        now = time.time()
        interval = 0.12 if missing_elapsed < 1.5 else 0.22
        if now - getattr(self._sm.round, "result_quick_check_last", 0) < interval:
            return False
        self._sm.round.result_quick_check_last = now

        success_info = self.detect_fast_success_result(rect, fast_only=True)
        if success_info and success_info.get("location"):
            self.finish_fast_success_result(rect, success_info, source_label="溜鱼")
            return True

        failed_info = self.detect_fast_failed_result(rect)
        if self.maybe_finish_failed_result(rect, failed_info, source_label="溜鱼"):
            return True

        full_interval = 0.35 if missing_elapsed < 2.0 else 0.55
        if now - getattr(self._sm.round, "result_full_check_last", 0) >= full_interval:
            self._sm.round.result_full_check_last = now
            success_info = self.detect_success_result(rect)
            if success_info and success_info.get("location"):
                self.finish_fast_success_result(rect, success_info, source_label="溜鱼")
                return True

            failed_info = self.detect_failed_result(rect)
            if self.maybe_finish_failed_result(rect, failed_info, source_label="溜鱼"):
                return True

        # STATE_FISHING must not use F/Q/E/R ready UI as a terminal signal.
        # Those translucent templates can false-positive on the fishing HUD/background
        # and stop reel control before settlement is actually reached.
        return False
