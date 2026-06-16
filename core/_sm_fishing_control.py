"""
溜鱼方向控制：PID + 滞回保持 + 前馈。

从 state_machine 提取 _control_pixels、_choose_fishing_control_direction、
_apply_fishing_control_direction、_send_cast_input 四个方法。
"""

import time
from pathlib import Path


class FishingControl:
    """溜鱼方向控制与抛竿输入。"""

    def __init__(self, sm):
        self._sm = sm

    # ------------------------------------------------------------------
    #  控制像素计算（纯函数）
    # ------------------------------------------------------------------

    def control_pixels(self, target_w):
        """根据目标条宽度计算控制阈值（release_cross、reengage、switch_error）。"""
        sm = self._sm
        width = max(1.0, float(target_w or 0))
        release_cross = width * sm._normalize_ratio_config("control_release_cross_ratio", 0.012, 0.006, 0.12)
        reengage = width * sm._normalize_ratio_config("control_reengage_ratio", 0.018, 0.008, 0.18)
        switch_error = width * sm._normalize_ratio_config("control_switch_ratio", 0.08, 0.035, 0.25)
        try:
            deadzone_pixels = float(sm.config.get("t_deadzone", 1))
        except (TypeError, ValueError):
            deadzone_pixels = 1.0
        deadzone_pixels = max(0.4, min(deadzone_pixels, 30.0))
        release_cross = min(release_cross, max(0.35, deadzone_pixels * 0.55))
        reengage = min(reengage, max(0.60, deadzone_pixels * 0.95))
        return {
            "release_cross": max(0.35, min(release_cross, 8.0)),
            "reengage": max(0.60, min(reengage, 14.0)),
            "switch_error": max(3.0, min(switch_error, 24.0)),
        }

    # ------------------------------------------------------------------
    #  方向决策（接近纯函数，读取 round 状态）
    # ------------------------------------------------------------------

    def choose_direction(self, error, target_w, target_velocity, total_signal, engage_threshold):
        """基于滞回逻辑选择溜鱼方向：1(右/D)、-1(左/A)、0(释放)。"""
        pixels = self.control_pixels(target_w)
        sm = self._sm
        current = int(getattr(sm.round, "fish_control_direction", 0) or 0)
        if current not in (-1, 0, 1):
            current = 0

        now = time.time()
        error = float(error)
        signed_error = error * current if current else 0.0

        if current:
            if signed_error <= -pixels["switch_error"]:
                return -current
            # 零交叉：已越过目标中心，立即释放，无视 hold time
            if signed_error <= 0:
                return 0
            if now < getattr(sm.round, "fish_control_min_hold_until", 0):
                return current
            if signed_error <= -pixels["release_cross"]:
                return 0
            return current

        abs_error = abs(error)
        abs_signal = abs(float(total_signal))
        if abs_error >= pixels["reengage"]:
            return 1 if error > 0 else -1
        if abs_error >= pixels["release_cross"] and abs_signal >= max(1.0, float(engage_threshold)):
            return 1 if error > 0 else -1
        if abs_signal >= max(2.0, float(engage_threshold) * 1.35):
            return 1 if total_signal > 0 else -1
        return 0

    # ------------------------------------------------------------------
    #  方向执行（有副作用：按键）
    # ------------------------------------------------------------------

    def apply_direction(self, direction):
        """执行方向控制：按下 A/D 键或释放全部。"""
        sm = self._sm
        with sm._input_lock:
            if sm._should_stop():
                sm.ctrl.release_all()
                return
            direction = 1 if direction > 0 else (-1 if direction < 0 else 0)
            now = time.time()
            previous = int(getattr(sm.round, "fish_control_direction", 0) or 0)
            if direction != previous:
                sm.round.fish_control_last_change = now
                if direction:
                    hold_time = sm._normalize_ratio_config("control_min_hold_time", 0.14, 0.03, 0.35)
                    sm.round.fish_control_min_hold_until = now + hold_time
                else:
                    sm.round.fish_control_min_hold_until = 0
            sm.round.fish_control_direction = direction

            if direction > 0:
                sm.ctrl.key_up('A')
                sm.ctrl.key_down('D')
            elif direction < 0:
                sm.ctrl.key_up('D')
                sm.ctrl.key_down('A')
            else:
                sm.ctrl.release_all()

    # ------------------------------------------------------------------
    #  抛竿输入（有副作用：按键）
    # ------------------------------------------------------------------

    def send_cast_input(self, ready_info, source_label):
        """发送 F 键抛竿指令。返回 True 表示成功。"""
        sm = self._sm
        with sm._input_lock:
            if sm._should_stop():
                return False
            matched_path = ready_info.get("template") if ready_info else None
            matched_name = Path(matched_path).name if matched_path else "未知模板"
            confidence = float((ready_info or {}).get("confidence") or 0.0)
            strategy = (ready_info or {}).get("strategy") or "默认"
            kind = (ready_info or {}).get("kind") or "可抛钩提示"
            sm._log(f"[{source_label}] 识别到{kind} (置信度: {confidence:.2f}，模板: {matched_name}，策略: {strategy})。准备抛竿。")
            sm._log(f"[{source_label}] > 正在向游戏发送 'F' 键点按指令 (150ms)...")
            sm.ctrl.release_all()
            sm._note_program_input(("F",), duration=0.70)
            sm.ctrl.key_tap('F', duration=0.15)
            sm.round.last_cast_time = time.time()
            sm.round.waiting_start_time = sm.round.last_cast_time
            return True
