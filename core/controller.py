import ctypes
import time
import pydirectinput


class Controller:
    """键盘鼠标控制器。
    前台模式：pydirectinput (DirectInput 级别，3D 游戏兼容)
    后台模式：Win32 窗口消息 (PostMessage，无需前台焦点)
    """

    # 虚拟键码映射表
    _VK_TABLE = {
        'a': 0x41, 'b': 0x42, 'c': 0x43, 'd': 0x44, 'e': 0x45,
        'f': 0x46, 'g': 0x47, 'h': 0x48, 'i': 0x49, 'j': 0x4A,
        'k': 0x4B, 'l': 0x4C, 'm': 0x4D, 'n': 0x4E, 'o': 0x4F,
        'p': 0x50, 'q': 0x51, 'r': 0x52, 's': 0x53, 't': 0x54,
        'u': 0x55, 'v': 0x56, 'w': 0x57, 'x': 0x58, 'y': 0x59,
        'z': 0x5A, '0': 0x30, '1': 0x31, '2': 0x32, '3': 0x33,
        '4': 0x34, '5': 0x35, '6': 0x36, '7': 0x37, '8': 0x38,
        '9': 0x39, 'esc': 0x1B, 'escape': 0x1B, 'space': 0x20,
        'enter': 0x0D, 'tab': 0x09, 'shift': 0xA0, 'ctrl': 0xA2,
        'alt': 0xA4,
    }

    def __init__(self):
        self.pressed_keys = set()
        pydirectinput.PAUSE = 0.0
        # 后台模式状态
        self._bg_mode = False
        self._bg_hwnd = None
        self._bg_origin_x = 0
        self._bg_origin_y = 0
        self._user32 = ctypes.windll.user32

    # ------------------------------------------------------------------
    #  后台模式配置
    # ------------------------------------------------------------------

    def set_background_mode(self, enabled, hwnd=None):
        """切换后台输入模式。启用后按键通过窗口消息发送，无需前台焦点。"""
        self._bg_mode = bool(enabled)
        self._bg_hwnd = hwnd if enabled else None

    def set_click_origin(self, client_left, client_top):
        """设置客户区左上角的屏幕绝对坐标（鼠标坐标转换用）。"""
        self._bg_origin_x = int(client_left)
        self._bg_origin_y = int(client_top)

    # ------------------------------------------------------------------
    #  后台输入核心方法
    # ------------------------------------------------------------------

    def _bg_wake_window(self):
        """向目标窗口发送 WM_ACTIVATE 唤醒其消息循环。
        某些游戏引擎在窗口未激活时会忽略键盘/鼠标消息，
        通过发送 WA_ACTIVE 可以让消息循环正常处理输入，
        同时不会将窗口切到前台，用户无感知。"""
        if self._bg_hwnd:
            try:
                # WM_ACTIVATE=0x0006, WA_ACTIVE=0x0001, WA_CLICKACTIVE=0x0002
                self._user32.PostMessageW(self._bg_hwnd, 0x0006, 0x0001, 0)
            except Exception:
                pass

    def _bg_resolve_vk(self, key_char):
        """将键名转换为虚拟键码。优先查表，单字符回退到 VkKeyScan。"""
        k = str(key_char).lower()
        if k in self._VK_TABLE:
            return self._VK_TABLE[k]
        if len(k) == 1:
            try:
                return self._user32.VkKeyScanW(ord(k)) & 0xFF
            except Exception:
                pass
        return None

    def _bg_build_lparam(self, vk_code, key_up=False):
        """构造键盘消息的 lParam。
        位布局: [31]transition [30]prev_state [29]context [24]extended
                [23:16]scan_code [15:0]repeat_count=1"""
        scan = self._user32.MapVirtualKeyW(vk_code, 0)  # MAPVK_VK_TO_VSC
        lp = 1  # repeat count
        lp |= (scan & 0xFF) << 16
        if key_up:
            lp |= (1 << 30) | (1 << 31)  # previous=1, transition=1
        return lp

    def _bg_send_key(self, vk_code, key_up=False):
        """通过 PostMessage 向后台窗口发送按键消息。"""
        if not self._bg_hwnd:
            return
        lparam = self._bg_build_lparam(vk_code, key_up)
        msg = 0x0101 if key_up else 0x0100  # WM_KEYUP / WM_KEYDOWN
        try:
            self._user32.PostMessageW(self._bg_hwnd, msg, vk_code, lparam)
        except Exception:
            pass

    def _bg_send_click(self, screen_x, screen_y, duration=0.05):
        """通过 PostMessage 向后台窗口发送鼠标点击。"""
        if not self._bg_hwnd:
            return False
        cx = max(0, int(screen_x) - self._bg_origin_x)
        cy = max(0, int(screen_y) - self._bg_origin_y)
        lparam = (cy << 16) | (cx & 0xFFFF)  # MAKELONG
        try:
            # WM_LBUTTONDOWN=0x0201, MK_LBUTTON=0x0001
            self._user32.PostMessageW(self._bg_hwnd, 0x0201, 0x0001, lparam)
            if duration > 0:
                time.sleep(duration)
            # WM_LBUTTONUP=0x0202
            self._user32.PostMessageW(self._bg_hwnd, 0x0202, 0, lparam)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    #  公共接口（前台/后台自动切换）
    # ------------------------------------------------------------------

    def key_down(self, key_char):
        """按下并保持某键"""
        if not key_char or not isinstance(key_char, str):
            return
        try:
            key = key_char.lower()
            if key not in self.pressed_keys:
                if self._bg_mode:
                    self._bg_wake_window()
                    vk = self._bg_resolve_vk(key_char)
                    if vk is not None:
                        self._bg_send_key(vk, key_up=False)
                        self.pressed_keys.add(key)
                    return
                pydirectinput.keyDown(key)
                self.pressed_keys.add(key)
        except Exception as e:
            print(f"[Controller] KeyDown error: {e}")

    def key_up(self, key_char):
        """释放某键"""
        if not key_char or not isinstance(key_char, str):
            return
        try:
            key = key_char.lower()
            if key in self.pressed_keys:
                if self._bg_mode:
                    vk = self._bg_resolve_vk(key_char)
                    if vk is not None:
                        self._bg_send_key(vk, key_up=True)
                    self.pressed_keys.remove(key)
                    return
                pydirectinput.keyUp(key)
                self.pressed_keys.remove(key)
        except Exception as e:
            print(f"[Controller] KeyUp error: {e}")

    def key_tap(self, key_char, duration=0.01):
        """短促点击某键"""
        try:
            self.key_down(key_char)
            if duration > 0:
                time.sleep(duration)
            self.key_up(key_char)
        except Exception:
            pass

    def mouse_click(self, x, y, duration=0.05):
        """移动到屏幕坐标后执行一次左键点击。"""
        try:
            x = int(round(x))
            y = int(round(y))
            if self._bg_mode:
                self._bg_wake_window()
                return self._bg_send_click(x, y, duration)
            # 前台模式
            try:
                ctypes.windll.user32.SetCursorPos(x, y)
            except Exception:
                pass
            time.sleep(0.02)
            pydirectinput.mouseDown(x=x, y=y, button="left")
            if duration > 0:
                time.sleep(duration)
            pydirectinput.mouseUp(x=x, y=y, button="left")
            return True
        except Exception as e:
            print(f"[Controller] MouseClick error: {e}")
            return False

    def release_all(self):
        """释放所有记录在案的被按下的键 (安全保护)"""
        for key in list(self.pressed_keys):
            self.key_up(key)
