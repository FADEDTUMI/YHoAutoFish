"""
轮次状态管理 — 每轮钓鱼的状态变量与重置逻辑。

从 state_machine._reset_round_state / _prepare_fishing_round_state 提取，
消除 92% 的代码重复。
"""

import time


class RoundState:
    """管理每轮钓鱼的状态变量。"""

    def __init__(self):
        # ── 等待/抛竿阶段 ──
        self.waiting_start_time = 0
        self.last_cast_time = 0
        self.waiting_recast_count = 0
        self.waiting_ready_recheck_last = 0

        # ── 溜鱼阶段 - 时间 ──
        self.fishing_start_time = 0

        # ── 溜鱼阶段 - 目标条检测 ──
        self.missing_start_time = 0
        self.last_cursor_x = None
        self.seen_fishing_bar = False
        self.last_target_time = 0
        self.last_target_x = None
        self.target_velocity = 0
        self.last_valid_target_x = None
        self.last_valid_target_w = None
        self.last_valid_bar_time = 0
        self.last_valid_cursor_x = None
        self.last_valid_cursor_time = 0
        self.last_cursor_template_time = 0
        self.bar_cursor_jump_reject_count = 0
        self.bar_jump_reject_count = 0

        # ── 溜鱼阶段 - 控制状态 ──
        self.fish_control_direction = 0
        self.fish_control_min_hold_until = 0
        self.fish_control_last_change = 0
        self.confirmed_fishing_bar = False
        self.bar_seen_streak = 0
        self.bar_first_seen_time = 0
        self.last_bar_seen_time = 0
        self.fishing_bar_confirmed_time = 0
        self.fishing_control_started = False
        self.fishing_control_started_time = 0
        self.fishing_control_frame_count = 0
        self.capture_missing_start_time = 0
        self.last_bar_capture_failed = False
        self.last_control_error = 0
        self.last_control_target_w = None
        self.round_had_fishing_bar = False

        # ── 结果判定阶段 ──
        self.result_empty_recorded = False
        self.result_quick_check_last = 0
        self.result_full_check_last = 0
        self.fishing_result_check_last = 0
        self.fishing_failed_check_last = 0
        self.result_ready_seen_time = 0
        self.result_ready_confirm_count = 0
        self.result_ready_last_kind = ""
        self.result_ready_debug_saved = False
        self.result_text_probe_done = False
        self.success_recorded_pending_close = False
        self.success_close_retry_count = 0
        self.success_close_last_esc = 0
        self.failed_result_candidate_seen_time = 0
        self.failed_result_candidate_count = 0
        self.failed_result_candidate_signature = ""

        # ── 恢复阶段 ──
        self.recovery_start_time = 0
        self.recovery_reason = ""
        self.recovery_esc_requested = False
        self.recovery_esc_sent = False
        self.recovery_second_esc_sent = False
        self.recovery_allow_second_esc = True
        self.recovery_empty_recorded = False
        self.recovery_first_esc_time = 0

    # ------------------------------------------------------------------
    #  完整重置（原 _reset_round_state）
    # ------------------------------------------------------------------

    def reset(self, release_keys_fn=None, pid=None):
        """完整重置所有轮次变量。

        Parameters
        ----------
        release_keys_fn : callable, optional
            释放按键的回调（通常是 ctrl.release_all）。
        pid : PIDController, optional
            PID 控制器实例，重置时调用 pid.reset()。
        """
        if release_keys_fn is not None:
            release_keys_fn()
        if pid is not None:
            pid.reset()
        self._reset_waiting()
        self._reset_fishing()
        self._reset_result()
        self._reset_recovery()

    # ------------------------------------------------------------------
    #  部分重置：进入溜鱼前（原 _prepare_fishing_round_state）
    # ------------------------------------------------------------------

    def prepare_fishing(self, pid=None, start_time=None):
        """进入溜鱼前的部分重置，保留等待阶段变量。

        Parameters
        ----------
        pid : PIDController, optional
            PID 控制器实例，重置时调用 pid.reset()。
        start_time : float, optional
            溜鱼开始时间，默认为 time.time()。
        """
        if pid is not None:
            pid.reset()
        self._reset_fishing()
        self._reset_result()
        self.fishing_start_time = start_time if start_time is not None else time.time()

    # ------------------------------------------------------------------
    #  内部分组重置
    # ------------------------------------------------------------------

    def _reset_waiting(self):
        self.waiting_start_time = 0
        self.last_cast_time = 0
        self.waiting_recast_count = 0
        self.waiting_ready_recheck_last = 0

    def _reset_fishing(self):
        self.fishing_start_time = 0
        self.missing_start_time = 0
        self.last_cursor_x = None
        self.seen_fishing_bar = False
        self.last_target_time = 0
        self.last_target_x = None
        self.target_velocity = 0
        self.last_valid_target_x = None
        self.last_valid_target_w = None
        self.last_valid_bar_time = 0
        self.last_valid_cursor_x = None
        self.last_valid_cursor_time = 0
        self.last_cursor_template_time = 0
        self.bar_cursor_jump_reject_count = 0
        self.bar_jump_reject_count = 0
        self.fish_control_direction = 0
        self.fish_control_min_hold_until = 0
        self.fish_control_last_change = 0
        self.confirmed_fishing_bar = False
        self.bar_seen_streak = 0
        self.bar_first_seen_time = 0
        self.last_bar_seen_time = 0
        self.fishing_bar_confirmed_time = 0
        self.fishing_control_started = False
        self.fishing_control_started_time = 0
        self.fishing_control_frame_count = 0
        self.capture_missing_start_time = 0
        self.last_bar_capture_failed = False
        self.last_control_error = 0
        self.last_control_target_w = None
        self.round_had_fishing_bar = False

    def _reset_result(self):
        self.result_empty_recorded = False
        self.result_quick_check_last = 0
        self.result_full_check_last = 0
        self.fishing_result_check_last = 0
        self.fishing_failed_check_last = 0
        self.result_ready_seen_time = 0
        self.result_ready_confirm_count = 0
        self.result_ready_last_kind = ""
        self.result_ready_debug_saved = False
        self.result_text_probe_done = False
        self.success_recorded_pending_close = False
        self.success_close_retry_count = 0
        self.success_close_last_esc = 0
        self.failed_result_candidate_seen_time = 0
        self.failed_result_candidate_count = 0
        self.failed_result_candidate_signature = ""

    def _reset_recovery(self):
        self.recovery_start_time = 0
        self.recovery_reason = ""
        self.recovery_esc_requested = False
        self.recovery_esc_sent = False
        self.recovery_second_esc_sent = False
        self.recovery_allow_second_esc = True
        self.recovery_empty_recorded = False
        self.recovery_first_esc_time = 0
