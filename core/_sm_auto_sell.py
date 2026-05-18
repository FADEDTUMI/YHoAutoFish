"""
自动售鱼子状态机。

从 state_machine 提取售鱼相关状态变量和流程逻辑。
AutoSeller 通过 _sm 引用访问 StateMachine 的方法和属性。
"""

import time


class AutoSeller:
    """自动售鱼子状态机。"""

    CONFIRM_BUTTON_ROIS = (
        (0.43, 0.58, 0.54, 0.40),
        (0.48, 0.62, 0.48, 0.34),
    )

    def __init__(self, sm):
        self._sm = sm

    # ------------------------------------------------------------------
    #  状态代理（读写 StateMachine 的 _auto_sell_xxx 变量）
    # ------------------------------------------------------------------

    @property
    def session_catch_count(self):
        return getattr(self._sm, '_auto_sell_session_catch_count', 0)

    @session_catch_count.setter
    def session_catch_count(self, v):
        self._sm._auto_sell_session_catch_count = v

    @property
    def pending(self):
        return getattr(self._sm, '_auto_sell_pending', False)

    @pending.setter
    def pending(self, v):
        self._sm._auto_sell_pending = v

    @property
    def step(self):
        return getattr(self._sm, '_auto_sell_step', '')

    @step.setter
    def step(self, v):
        self._sm._auto_sell_step = v

    @property
    def step_started(self):
        return getattr(self._sm, '_auto_sell_step_started', 0)

    @step_started.setter
    def step_started(self, v):
        self._sm._auto_sell_step_started = v

    @property
    def started_at(self):
        return getattr(self._sm, '_auto_sell_started_at', 0)

    @started_at.setter
    def started_at(self, v):
        self._sm._auto_sell_started_at = v

    @property
    def last_log(self):
        return getattr(self._sm, '_auto_sell_last_log', 0)

    @last_log.setter
    def last_log(self, v):
        self._sm._auto_sell_last_log = v

    @property
    def ready_wait_started(self):
        return getattr(self._sm, '_auto_sell_ready_wait_started', 0)

    @ready_wait_started.setter
    def ready_wait_started(self, v):
        self._sm._auto_sell_ready_wait_started = v

    @property
    def capture_hidden(self):
        return getattr(self._sm, '_auto_sell_capture_hidden', False)

    @capture_hidden.setter
    def capture_hidden(self, v):
        self._sm._auto_sell_capture_hidden = v

    # ------------------------------------------------------------------
    #  阈值与记录
    # ------------------------------------------------------------------

    def threshold(self):
        try:
            threshold = int(float(self._sm.config.get("auto_sell_catch_threshold", 0)))
        except (TypeError, ValueError):
            threshold = 0
        return max(0, min(threshold, 999))

    def record_catch(self):
        self.session_catch_count += 1
        threshold = self.threshold()
        if threshold <= 0:
            return
        if self.session_catch_count >= threshold and not self.pending:
            self.pending = True
            self.ready_wait_started = 0
            self._sm._log(
                f"[售鱼] 本次运行已累计钓获 {self.session_catch_count} 条，"
                "达到自动售鱼阈值，等待回到可抛竿界面后出售鱼获。"
            )

    def reset(self):
        self.session_catch_count = 0
        self.pending = False
        self.step = ""
        self.step_started = 0
        self.started_at = 0
        self.last_log = 0
        self.ready_wait_started = 0

    # ------------------------------------------------------------------
    #  悬浮窗隐藏/恢复
    # ------------------------------------------------------------------

    def set_capture_hidden(self, hidden):
        hidden = bool(hidden)
        if self.capture_hidden == hidden:
            return
        self.capture_hidden = hidden
        if self._sm.log_queue:
            self._sm.log_queue.put(
                "CMD_FLOATING_HIDE_FOR_CAPTURE" if hidden else "CMD_FLOATING_RESTORE_AFTER_CAPTURE"
            )

    # ------------------------------------------------------------------
    #  步骤管理
    # ------------------------------------------------------------------

    def _set_step(self, step):
        self.step = step
        self.step_started = time.time()

    # ------------------------------------------------------------------
    #  流程控制
    # ------------------------------------------------------------------

    def start_flow(self, rect, ready_info):
        """在 IDLE 状态中检测到可售鱼时调用。返回 True 表示已进入售鱼。"""
        if not self.pending:
            return False
        self.set_capture_hidden(True)
        self._sm._log("[售鱼] 已回到钓鱼初始界面，正在按 Q 进入鱼获出售界面。")
        if not self._sm._tap_key_if_running("q", duration=0.12):
            self.set_capture_hidden(False)
            return False
        self._sm.current_state = self._sm.STATE_SELLING_CATCHES
        self.started_at = time.time()
        self._set_step("fish_cabin")
        return True

    def finish(self):
        self.session_catch_count = 0
        self.pending = False
        self.step = ""
        self.ready_wait_started = 0
        self.set_capture_hidden(False)
        self._sm._log("[售鱼] 已完成一键出售鱼获，继续自动钓鱼。")
        self._sm.current_state = self._sm.STATE_IDLE

    def fail(self, reason, press_esc=True, rect=None):
        self._sm._log(f"[售鱼] {reason}，本次自动售鱼停止，避免继续误操作。")
        if press_esc and not self._sm._should_stop():
            ready_info = self._sm.cast_det.detect_ready_to_cast(
                rect, allow_heavy=False, require_initial_controls=True
            ) if rect else None
            if not (ready_info and ready_info.get("location")):
                self._sm._tap_key_if_running("esc", duration=0.12)
        self.session_catch_count = 0
        self.pending = False
        self.step = ""
        self.ready_wait_started = 0
        self.set_capture_hidden(False)
        self._sm.current_state = self._sm.STATE_IDLE

    # ------------------------------------------------------------------
    #  模板匹配
    # ------------------------------------------------------------------

    def _match_template(self, rect, templates, rois):
        sm = self._sm
        if sm.sc is None or not rect or not templates:
            return None
        best = None
        strategies = (
            {"name": "sell-gray-mask", "threshold": 0.70, "use_mask": True, "mask_threshold": 6, "early_accept": 0.92},
            {"name": "sell-edge", "threshold": 0.58, "use_edge": True, "early_accept": 0.86},
            {"name": "sell-plain", "threshold": 0.72, "early_accept": 0.93},
        )
        for roi in rois:
            image = sm.sc.capture_relative(rect, *roi)
            if image is None:
                continue
            loc, conf, matched_path, strategy = sm.vis.find_best_template_multi_strategy(
                image,
                templates,
                strategies,
                threshold=0.68,
                scale_range=sm.tpl.scale_range(rect, 0.50, 1.85),
                scale_steps=13,
            )
            if best is None or float(conf or 0.0) > best.get("confidence", 0.0):
                best = {
                    "location": loc,
                    "confidence": float(conf or 0.0),
                    "template": matched_path,
                    "strategy": strategy,
                    "roi": roi,
                }
            if loc and conf >= 0.92:
                break
        if best and best.get("location"):
            point = sm._client_point_to_screen(rect, best["roi"], best["location"])
            if point:
                best["screen_point"] = point
                return best
        return best

    # ------------------------------------------------------------------
    #  主处理方法（原 _handle_auto_sell）
    # ------------------------------------------------------------------

    def handle(self, rect):
        sm = self._sm
        if sm._should_stop():
            return

        now = time.time()
        total_elapsed = now - float(self.started_at or now)
        if total_elapsed > 55.0:
            self.fail("自动售鱼流程超时", rect=rect)
            return

        step = self.step
        step_elapsed = now - float(self.step_started or now)

        if step == "fish_cabin":
            if step_elapsed < 0.7:
                return
            info = self._match_template(
                rect,
                sm.tpl.auto_sell_fish_cabin_templates(),
                ((0.0, 0.0, 1.0, 1.0),),
            )
            if info and info.get("screen_point"):
                x, y = info["screen_point"]
                if sm._click_screen_point_if_running(x, y, duration=0.06):
                    sm._log("[售鱼] 已点击鱼舱按钮。")
                    self._set_step("one_click")
                return
            if step_elapsed > 9.0:
                best_conf = (info or {}).get("confidence", 0.0)
                self.fail(f"未能定位鱼舱按钮，最高置信度: {best_conf:.2f}", rect=rect)
            return

        if step == "one_click":
            if step_elapsed < 0.5:
                return
            info = self._match_template(
                rect,
                sm.tpl.auto_sell_one_click_templates(),
                ((0.0, 0.0, 1.0, 1.0),),
            )
            if info and info.get("screen_point"):
                x, y = info["screen_point"]
                if sm._click_screen_point_if_running(x, y, duration=0.06):
                    sm._log("[售鱼] 已点击一键出售按钮。")
                    self._set_step("confirm")
                return
            if step_elapsed > 9.0:
                best_conf = (info or {}).get("confidence", 0.0)
                self.fail(f"未能定位一键出售按钮，最高置信度: {best_conf:.2f}", rect=rect)
            return

        if step == "confirm":
            if step_elapsed < 0.4:
                return
            info = self._match_template(
                rect,
                sm.tpl.auto_sell_confirm_templates(),
                self.CONFIRM_BUTTON_ROIS,
            )
            if info and info.get("screen_point"):
                x, y = info["screen_point"]
                if sm._click_screen_point_if_running(x, y, duration=0.06):
                    sm._log("[售鱼] 已点击一键出售确认按钮，等待鱼获处理完成。")
                    self._set_step("wait_after_confirm")
                return
            if step_elapsed > 8.0:
                best_conf = (info or {}).get("confidence", 0.0)
                self.fail(f"未能定位一键出售确认按钮，最高置信度: {best_conf:.2f}", rect=rect)
            return

        if step == "wait_after_confirm":
            if step_elapsed < 2.0:
                return
            if not sm._tap_key_if_running("esc", duration=0.12):
                return
            sm._log("[售鱼] 已发送第一次 ESC，准备退出售鱼界面。")
            self._set_step("second_esc")
            return

        if step == "second_esc":
            if step_elapsed < 0.45:
                return
            if not sm._tap_key_if_running("esc", duration=0.12):
                return
            sm._log("[售鱼] 已发送第二次 ESC，正在确认回到钓鱼初始界面。")
            self._set_step("verify_ready")
            return

        if step == "verify_ready":
            ready_info = sm._detect_ready_to_cast(rect, allow_heavy=False, require_initial_controls=True)
            if ready_info and ready_info.get("location"):
                self.finish()
                return
            if step_elapsed > 5.0:
                sm._log("[售鱼] 售鱼后暂未确认初始界面，不再追加 ESC，回到待机流程继续扫描。")
                self.finish()
            return

        self.fail("自动售鱼步骤异常", press_esc=False)
