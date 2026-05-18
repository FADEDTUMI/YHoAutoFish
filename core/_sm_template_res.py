"""
模板资源加载与匹配策略定义。

从 state_machine 的 _resolve_asset_templates 系列方法和
_f_button_match_strategies 系列方法提取。
"""

from pathlib import Path
from core.paths import resource_path


class TemplateResources:
    """模板资源加载与匹配策略定义。"""

    def __init__(self, log_fn=None):
        self._cache = {}
        self._log = log_fn or (lambda msg: None)

    # ------------------------------------------------------------------
    #  资源加载
    # ------------------------------------------------------------------

    def resolve(self, cache_key, exact_names=(), required_keywords=()):
        if cache_key in self._cache:
            return self._cache[cache_key]

        assets_dir = Path(resource_path("assets"))
        paths = []
        seen = set()

        def add_path(path):
            normalized = str(path)
            if path.exists() and normalized not in seen:
                seen.add(normalized)
                paths.append(normalized)

        for name in exact_names:
            add_path(assets_dir / name)

        if assets_dir.exists():
            for path in assets_dir.glob("*.png"):
                filename = path.name
                if all(keyword in filename for keyword in required_keywords):
                    add_path(path)

        self._cache[cache_key] = paths
        if not paths:
            self._log(f"[识别] 未找到模板资源: {cache_key}，请检查 assets 目录。")
        return paths

    # ------------------------------------------------------------------
    #  模板访问器
    # ------------------------------------------------------------------

    def f_button_templates(self):
        return self.resolve(
            "f_button",
            exact_names=("F键图标.png", "F键图标2.png", "F键图标3.png"),
            required_keywords=("F键图标",),
        )

    def initial_q_button_templates(self):
        return self.resolve(
            "initial_q_button",
            exact_names=("初始钓鱼界面的Q键进入售鱼界面按钮图标（暗色）.png", "初始钓鱼界面的Q键进入售鱼界面按钮图标（亮色）.png"),
            required_keywords=("初始钓鱼界面", "Q键"),
        )

    def initial_e_button_templates(self):
        return self.resolve(
            "initial_e_button",
            exact_names=("初始钓鱼界面的E键更换鱼饵按钮图标（暗色）.png", "初始钓鱼界面的E键更换鱼饵按钮图标（亮色）.png"),
            required_keywords=("初始钓鱼界面", "E键"),
        )

    def initial_r_button_templates(self):
        return self.resolve(
            "initial_r_button",
            exact_names=("初始钓鱼界面的R键进入钓鱼商店按钮图标（暗色）.png", "初始钓鱼界面的R键进入钓鱼商店按钮图标（亮色）.png"),
            required_keywords=("初始钓鱼界面", "R键"),
        )

    def ready_start_button_templates(self):
        return self.resolve(
            "ready_start_button",
            exact_names=("钓鱼准备界面开始钓鱼按钮.png",),
            required_keywords=("开始钓鱼",),
        )

    def ready_panel_templates(self):
        return self.resolve(
            "ready_panel",
            exact_names=("钓鱼准备界面右侧UI.png",),
            required_keywords=("钓鱼准备界面", "右侧UI"),
        )

    def hook_text_templates(self):
        return self.resolve(
            "hook_text",
            exact_names=("上钩文字.png", "钓鱼上钩文字.png"),
            required_keywords=("上钩文字",),
        )

    def failed_text_templates(self):
        return self.resolve(
            "failed_text",
            exact_names=("鱼儿溜走了.png", "钓鱼结算界面鱼儿溜走了.png"),
            required_keywords=("鱼儿溜走了",),
        )

    def weight_unit_templates(self):
        return self.resolve(
            "weight_unit_g",
            exact_names=("成功上鱼结算画面重量单位银色的g.png",),
            required_keywords=("重量单位", "g"),
        )

    def success_close_prompt_templates(self):
        return self.resolve(
            "success_close_prompt",
            exact_names=("成功上鱼结算画面点击关闭提示（辅助判断成功上鱼）.png",),
            required_keywords=("成功上鱼结算画面", "点击关闭提示"),
        )

    def success_exp_templates(self):
        return self.resolve(
            "success_exp",
            exact_names=("成功上鱼结算画面获得经验（辅助判断成功上鱼）.png",),
            required_keywords=("成功上鱼结算画面", "获得经验"),
        )

    def cursor_templates(self):
        return self.resolve(
            "fishing_cursor",
            exact_names=("溜鱼游标1.png", "溜鱼游标2.png", "溜鱼游标3.png", "溜鱼游标4.png", "溜鱼游标5.png"),
            required_keywords=("溜鱼", "游标"),
        )

    def target_bar_templates(self):
        return self.resolve(
            "fishing_target_bar",
            exact_names=("溜鱼耐力条1.png",),
            required_keywords=("溜鱼", "耐力条"),
        )

    def auto_sell_fish_cabin_templates(self):
        return self.resolve(
            "auto_sell_fish_cabin",
            exact_names=("鱼获出售界面点击鱼舱按钮.png",),
        )

    def auto_sell_one_click_templates(self):
        return self.resolve(
            "auto_sell_one_click",
            exact_names=("鱼获出售界面鱼舱子界面一键出售按钮.png",),
        )

    def auto_sell_confirm_templates(self):
        return self.resolve(
            "auto_sell_confirm",
            exact_names=("鱼获出售界面鱼舱子界面一键出售后确认弹窗的确认按钮.png",),
        )

    # ------------------------------------------------------------------
    #  缩放范围
    # ------------------------------------------------------------------

    def scale_range(self, rect, low_factor=0.65, high_factor=1.45):
        if not rect:
            base_scale = 1.0
        else:
            base_scale = max(0.40, min(float(rect[3]) / 900.0, 3.00))
        return max(0.25, base_scale * low_factor), min(4.00, base_scale * high_factor)

    # ------------------------------------------------------------------
    #  匹配策略定义
    # ------------------------------------------------------------------

    def f_button_match_strategies(self):
        return (
            {"name": "binary-145-mask", "threshold": 0.58, "use_binary": True, "binary_threshold": 145, "use_mask": True},
            {"name": "binary-115-mask", "threshold": 0.56, "use_binary": True, "binary_threshold": 115, "use_mask": True},
            {"name": "binary-175-mask", "threshold": 0.58, "use_binary": True, "binary_threshold": 175, "use_mask": True},
            {"name": "edge", "threshold": 0.52, "use_edge": True, "use_binary": False, "use_mask": False},
            {"name": "gray-mask", "threshold": 0.55, "use_edge": False, "use_binary": False, "use_mask": True},
        )

    def f_button_fast_match_strategies(self):
        return (
            {"name": "gray-mask-fast", "threshold": 0.60, "use_mask": True, "mask_threshold": 6, "early_accept": 0.94},
            {"name": "edge-fast", "threshold": 0.55, "use_edge": True, "early_accept": 0.92},
        )

    def initial_control_match_strategies(self):
        return (
            {"name": "control-gray-mask", "threshold": 0.60, "use_mask": True, "mask_threshold": 6, "early_accept": 0.90},
            {"name": "control-edge", "threshold": 0.54, "use_edge": True, "early_accept": 0.88},
            {"name": "control-plain", "threshold": 0.62, "early_accept": 0.90},
        )
