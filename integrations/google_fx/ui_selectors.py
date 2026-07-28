# -*- coding: utf-8 -*-
"""
🎯 UI 选择器集中管理 (Page Object Model)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
将所有可能变动的 CSS 选择器或文本抄离到此处。
当 AI 网页 UI 发生变化时，只需更新本文件即可。

支持列表形式的后备(Fallback)机制，增强对界面变化的容错率。

🔒 LOCKED 2026-03-25 — 此文件已由用户锁定，禁止在未获明确指示前修改。
   锁定范围: config_btn_keywords / ORIENT_ICON_MAP / RATIO_MAP
"""

# 📌 选择器版本号 — 每次 UI 变更时更新此值，方便追踪
SELECTOR_VERSION = "2026-04-16"  # 基于 2026-04-16 实测 DOM 更新

# ==============================================================================
# 🎯 UI 选择器字典
# ==============================================================================

UI_SELECTORS = {
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 🎨 Google FX (Imagen / Veo)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    "google_fx": {
        # --- 导航 ---
        "new_project_btn": [
            # 先用明确文案定位。项目内的“创建媒体”按钮也使用 add_2，裸图标
            # 选择器会误点它，并把一次无效 click 误报成“新建项目成功”。
            "button:has(i.google-symbols:text-is('add_2')):has-text('New project')",
            "button:has(i:text-is('add_2')):has-text('New project')",
            "button:has-text('New project')",
            "button[aria-label*='New project']",
            "button:has(i.google-symbols:text-is('add_2')):has-text('新建项目')",
            "button:has(i:text-is('add_2')):has-text('新建项目')",
            "button:has-text('新建项目')",
            "button[aria-label*='新建项目']",
            "button:has-text('Project baru')",
            # 未知语言兜底：排除项目内带 popup 的“创建/添加媒体”按钮。
            "button:not([aria-haspopup]):has(i.google-symbols:text-is('add_2'))",
            "button:not([aria-haspopup]):has(i:text-is('add_2'))",
        ],

        # --- 输入 ---
        "prompt_input": [
            "textarea",
            "[contenteditable='true']",
        ],

        # --- 历史参考图清理 ---
        "close_history_btn": [
            "button:has(i:has-text('close'))",
            "button[data-state='closed'] i",
            "button[aria-label*='Close']",
        ],

        # --- 图片上传 (通用入口) ---
        # ✅ 2026-04-05 基于实测 DOM 验证
        # 路径: 底部输入框左侧 [+] → Media Picker 面板 → Upload image
        "add_media_btn": [
            # + 按钮 (底部输入框左侧，触发 Media Picker)
            "button[aria-haspopup='dialog']",
        ],
        "media_picker_ready": [
            # Media Picker 面板已打开的等待条件
            "input[placeholder='Search for Assets']",
        ],
        "upload_image_trigger": [
            # Upload image 可点击区域 (有 onclick，含 upload icon)
            # 实测: <div class='sc-70a6bd2c-10 fxheTi'><i>upload</i><div>Upload image</div></div>
            "div.sc-70a6bd2c-10.fxheTi",
            # 后备: 包含 upload icon 的 div
            "div:has(> i.google-symbols)",
        ],
        "file_input": [
            # ✅ 实测: input[type='file'][accept='image/*'] 已挂载 DOM，可直接 set_input_files
            "input[type='file'][accept='image/*']",
            "input[type='file']",
        ],
        "agree_btn": ["button:has-text('I agree')", "button:has-text('我同意')"],
        "crop_and_save_btn": [
            "button:has-text('Crop and Save')",
            "button:has-text('Save')",
            "button[aria-label*='Crop']",
            "button:has-text('裁剪并保存')",
            "button:has-text('保存')",
            "button[aria-label*='裁剪']",
        ],
        "crop_ratio_combobox": ["button[role='combobox']"],
        "portrait_opt": [
            "div[role='option']:has-text('Portrait')",
            "li:has-text('Portrait')",
            "div:has-text('9:16')",
            "div[role='option']:has-text('纵向')",
            "li:has-text('纵向')",
        ],
        "first_frame_indicator": ["span:has-text('First Frame')", "span:has-text('第一帧')", "span:has-text('首帧')"],
        "upload_indicator": [
            "span:has-text('This is your ingredient, it can used to effect the outcome')",
            "span:has-text('This is your ingredient')",
            "span:has-text('First Frame')",
            "button:has(i:has-text('close'))",
            "span:has-text('这是您的原料')",
            "span:has-text('首帧')",
        ],

        # --- 配置面板: 模式/比例/数量/模型 Tab 按钮 ---
        # ✅ 2026-04-05 基于实测: 所有 tab 按钮含稳定 class flow_tab_slider_trigger
        "mode_tab_image": [
            # Image 模式 Tab
            "button.flow_tab_slider_trigger[aria-controls$='-content-IMAGE']",
        ],
        "mode_tab_video": [
            # Video 模式 Tab
            "button.flow_tab_slider_trigger[aria-controls$='-content-VIDEO']",
        ],
        "ratio_tab": {
            # 比例选择 Tab (aria-controls 末尾为比例 key)
            "9:16":  "button.flow_tab_slider_trigger[aria-controls$='-content-PORTRAIT']",
            "16:9": "button.flow_tab_slider_trigger[aria-controls$='-content-LANDSCAPE']",
            "1:1":  "button.flow_tab_slider_trigger[aria-controls$='-content-SQUARE']",
            "3:4":  "button.flow_tab_slider_trigger[aria-controls$='-content-PORTRAIT_3_4']",
            "4:3":  "button.flow_tab_slider_trigger[aria-controls$='-content-LANDSCAPE_4_3']",
        },
        "count_tab": {
            # 数量 Tab
            "x1": "button.flow_tab_slider_trigger[aria-controls$='-content-1']",
            "x2": "button.flow_tab_slider_trigger[aria-controls$='-content-2']",
            "x3": "button.flow_tab_slider_trigger[aria-controls$='-content-3']",
            "x4": "button.flow_tab_slider_trigger[aria-controls$='-content-4']",
        },
        "model_dropdown_trigger": [
            # 配置面板内打开模型列表的触发器
            "button[aria-haspopup='menu']:has(i.google-symbols:text-is('arrow_drop_down'))",
            ".DropdownMenuContent button[aria-haspopup='menu']",
        ],
        "config_panel_root": [
            ".DropdownMenuContent[role='menu'][data-state='open']",
            "[role='menu'].DropdownMenuContent[data-state='open']",
            "[role='menu'][data-state='open']",
        ],
        "model_menu_root": [
            "[role='menu'][data-state='open'][aria-labelledby]",
            "[data-radix-menu-content][data-state='open'][aria-labelledby]",
        ],
        "model_menu_item": [
            # 模型菜单各选项: 包含模型名的 button
            # 用法: '[role="menuitem"] button:has-text("Nano Banana 2")'
            '[role="menuitem"] button',
        ],

        # --- 上传对话框 (旧路径兼容) ---
        "upload_dialog_btn": [
            "div:has-text('Upload'):has(i:has-text('upload'))",
            "div:has-text('上传'):has(i:has-text('upload'))",
        ],

        # --- 错误检测 ---
        "something_went_wrong": [
            # \u5b9e\u6d4b\u6709\u6548 (2026-03-25): \u9519\u8bef\u5361\u7247 DOM \u7ed3\u6784
            "div[class*='sc-25d34a31-1']:has-text('Failed')",
            "div[class*='sc-25d34a31-2']:has-text('something went wrong')",
            "div[class*='sc-2a857f86-1']:has-text('Failed')",
            # \u6587\u5b57\u517c\u5bb9\u517c\u5bb9
            "text='Something went wrong'",
            "text='Something went wrong.'",
            "text='Oops, something went wrong!'",
            # \u5c55\u5f00\u6210\u542b\u6709\u6587\u5b57\u7684\u5143\u7d20
            ":has-text('Oops, something went wrong!')",
        ],
        "send_feedback": ["text='Send app feedback'"],

        # --- 下载 ---
        "download_btn": [
            "button:has-text('Download')",
            "button[aria-label*='Download']",
            "button:has-text('下载')",
            "button[aria-label*='下载']",
        ],
        "done_btn": [
            "button:has-text('Done')",
            "button[aria-label*='Done']",
            "button:has-text('完成')",
            "button[aria-label*='完成']",
        ],

        # --- 配置按钮 (底部工具栏) ---
        # Video 模式: Veo 3.1 - Fast / Veo 3.1 - Quality
        # Image 模式: Nano Banana Pro / Nano Banana 2 / Nano Banana 2 Lite
        "config_btn_keywords": ["Banana", "Nano", "Imagen", "Video", "Veo", "Pro"],
        "config_btn_count_keywords": ["x1", "x2", "x3", "x4"],

        # --- 发送 ---
        "send_btn_icon_texts": ["arrow_forward", "send", "arrow_upward"],
        "send_btn_aria_labels": ["Send", "send", "Submit", "submit", "Create"],
        "create_btn": ["button:has-text('Create')", "button:has-text('创建')", "button:has-text('生成')"],

        # --- 上传对话框 ---
        "upload_dialog_btn": [
            "div:has-text('Upload'):has(i:has-text('upload'))",
            "div:has-text('上传'):has(i:has-text('upload'))",
        ],

        # --- 积分余额 ---
        # 首次/未完成初始化的账号会停在产品介绍落地页；必须先点击 CTA 才会
        # 挂载真正的 Flow 工作台。英文/中文和 button/link 形态都覆盖。
        "flow_entry_btn": [
            "button:has-text('Get started')",
            "a:has-text('Get started')",
            "button:has-text('Get Started')",
            "a:has-text('Get Started')",
            "button:has-text('Create with Google Flow')",
            "a:has-text('Create with Google Flow')",
            "button:has-text('Try Google Flow')",
            "a:has-text('Try Google Flow')",
            "button:has-text('Create with Flow')",
            "a:has-text('Create with Flow')",
            "button:has-text('使用 Google Flow 创作')",
            "button:has-text('使用 Google Flow 进行创作')",
            "button:has-text('开始使用 Google Flow')",
            "a:has-text('开始使用 Google Flow')",
            "button:has-text('尝试 Google Flow')",
            "a:has-text('尝试 Google Flow')",
            "button:has-text('Buat project')",
            "a:has-text('Buat project')",
            "button:has-text('Create project')",
            "a:has-text('Create project')",
        ],
        "flow_onboarding_next_btn": [
            "button:has-text('Next')",
            "button:has-text('下一步')",
            "button:has-text('Berikutnya')",
        ],
        "flow_onboarding_continue_btn": [
            "button:has-text('Continue')",
            "button:has-text('继续')",
            "button:has-text('Lanjutkan')",
        ],
        "account_menu_trigger": [
            "button:has(img[alt='User profile image'])",
            "button:has(img[alt='用户头像'])",
            "button:has(img[alt*='头像'])",
            "button:has(img[src*='googleusercontent.com'])",
            "button[aria-label*='Google Account']",
            "button[aria-label*='Account']",
            "button[aria-label*='account' i]",
            "button[aria-label*='profile' i]",
            "button[aria-haspopup='menu']:has(img)",
            "[role='button']:has(img[alt*='profile' i])",
            "header button:has(img)",
            "button:has(img)",
        ],
        # 必须先确认账号菜单/弹层真实打开，再在这个局部范围内读积分。不能用
        # 全页 text=/N credits/，否则会把套餐宣传的 monthly credits 当成余额。
        "account_menu_surface": [
            "div[role='dialog']",
            "div[role='menu']",
            "[data-radix-popper-content-wrapper]",
            ".cdk-overlay-pane",
        ],
        "credit_display": [
            "a[href*='flow_ai_credits_page']",
            "a[href*='credits']",
        ],
    },



    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 🌐 通用 (跨平台弹窗等)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    "common": {
        "close_popup_btns": [
            'button[aria-label="Close"]',
            'button[aria-label="关闭"]',
            'button[aria-label="Tutup"]',
            'button[aria-label*="close" i]',
            'button:has-text("No thanks")',
            'button:has-text("Not now")',
            'button:has-text("Stay")',
            'button:has-text("Dismiss")',
            'button:has-text("Tutup")',
            'button:has-text("Close")',
            'div[role="dialog"] button[aria-label="Close"]',
            'div[role="dialog"] button[aria-label="Tutup"]',
            '.mat-mdc-dialog-container button:has-text("Close")',
            'button:has(i:has-text("close"))',
        ],
    },
}

# ==============================================================================
# ⚙️ 配置常量
# ==============================================================================

# Google FX 画面比例图标映射
# 键 = RATIO_MAP 输出的规范值 (PORTRAIT / LANDSCAPE)
# 值 = 一组 token，任意一个出现在 status_text / UI 中即视为匹配
ORIENT_ICON_MAP = {
    # ── 主力：新版 Flow UI tab id / aria-controls 使用的值 ──
    "PORTRAIT":  ["crop_9_16", "crop_portrait", "Portrait", "PORTRAIT", "9:16"],
    "LANDSCAPE": ["crop_16_9", "crop_landscape", "Landscape", "LANDSCAPE", "16:9"],
    # ── 兼容旧代码 & 首字母大写别名 ──
    "Portrait":  ["crop_9_16", "crop_portrait", "Portrait", "PORTRAIT", "9:16"],
    "Landscape": ["crop_16_9", "crop_landscape", "Landscape", "LANDSCAPE", "16:9"],
    "Square":    ["crop_square", "crop_1_1", "Square", "SQUARE", "1:1"],
    # ── 纯数字比例别名 (仍保留向后兼容) ──
    "16:9": ["crop_16_9", "16:9", "LANDSCAPE"],
    "4:3":  ["crop_landscape", "4:3"],
    "1:1":  ["crop_square", "1:1", "SQUARE"],
    "3:4":  ["crop_portrait", "3:4"],
    "9:16": ["crop_9_16", "9:16", "PORTRAIT"],
}


# Google FX 比例参数 → 规范 UI 方向值映射
# 用户 / N8N 传入的任意别名 → 转成 PORTRAIT 或 LANDSCAPE (与 Flow tab id 一致)
RATIO_MAP = {
    # ── 数字比例 ──
    "16:9":       "LANDSCAPE",
    "9:16":       "PORTRAIT",
    "1:1":        "Square",
    "4:3":        "LANDSCAPE",
    "3:4":        "PORTRAIT",
    # ── 英文别名 (不区分大小写在 _normalize_ratio_value 中统一) ──
    "landscape":  "LANDSCAPE",
    "portrait":   "PORTRAIT",
    "square":     "Square",
    "horizontal": "LANDSCAPE",
    "vertical":   "PORTRAIT",
    # ── 中文别名 ──
    "横版":       "LANDSCAPE",
    "竖版":       "PORTRAIT",
    "横屏":       "LANDSCAPE",
    "竖屏":       "PORTRAIT",
    "正方形":     "Square",
}
