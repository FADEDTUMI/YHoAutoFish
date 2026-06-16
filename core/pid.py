import time

class PIDController:
    def __init__(self, kp, ki, kd, output_limits=(None, None)):
        self.kp = kp  # 比例系数：反应当前的差距
        self.ki = ki  # 积分系数：消除长期静态误差
        self.kd = kd  # 微分系数：预测未来趋势，防止过冲（刹车）
        
        self.output_limits = output_limits
        self.reset()

    def reset(self):
        self.integral = 0
        self.last_error = 0
        self.last_input = 0
        self._filtered_derivative = 0
        self.last_time = time.time()

    def update(self, error, measurement=None):
        now = time.time()
        dt = now - self.last_time

        if dt <= 0.001:
            return self.kp * error

        p_out = self.kp * error

        self.integral += error * dt
        self.integral = max(min(self.integral, 20), -20)
        i_out = self.ki * self.integral

        # Derivative on Measurement + EMA 低通滤波
        inp = measurement if measurement is not None else error
        raw_derivative = -(inp - self.last_input) / dt
        alpha = 0.2
        self._filtered_derivative += alpha * (raw_derivative - self._filtered_derivative)
        d_out = self.kd * self._filtered_derivative

        self.last_error = error
        self.last_input = inp
        self.last_time = now

        output = p_out + i_out + d_out

        min_limit, max_limit = self.output_limits
        if min_limit is not None:
            output = max(output, min_limit)
        if max_limit is not None:
            output = min(output, max_limit)

        return output
