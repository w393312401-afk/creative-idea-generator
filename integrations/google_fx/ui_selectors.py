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

        # --- 图片上传 (通用入口) ---
        # + 按钮 (底部输入框左侧，触发 Media Picker)。这份列表就是
        # google_fx_helpers._find_add2_btn 实际在用的那份，两边共用同一个源，
        # 所以选择器探针探的和生产代码点的是同一批选择器。
        "add_media_btn": [
            "button[aria-haspopup='dialog']:has(span:text('Create'))",
            "button[aria-haspopup='dialog']:has(i.google-symbols:text('add_2'))",
            "button[aria-haspopup='dialog']:has(i:text('add_2'))",
            "button[aria-haspopup='dialog']",
            "button[aria-haspopup='menu']:has(span:text('Create'))",
            "button[aria-haspopup='menu']:has(span:text('添加媒体'))",
            "button[aria-haspopup='menu']:has(i:text('add'))",
            "button[aria-haspopup='menu']",
        ],

        # --- 配置面板: 比例/数量/模型 Tab 按钮 ---
        # ✅ 2026-04-05 基于实测: 所有 tab 按钮含稳定 class flow_tab_slider_trigger
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
        "config_panel_root": [
            ".DropdownMenuContent[role='menu'][data-state='open']",
            "[role='menu'].DropdownMenuContent[data-state='open']",
            "[role='menu'][data-state='open']",
        ],

        # --- 配置按钮 (底部工具栏) ---
        # Video 模式: Veo 3.1 - Fast / Veo 3.1 - Quality
        # Image 模式: Nano Banana Pro / Nano Banana 2 / Nano Banana 2 Lite
        # ⚠️ 非选择器：这是文本关键词表，不能当 CSS 选择器用（选择器探针会跳过）。
        "config_btn_keywords": ["Banana", "Nano", "Imagen", "Video", "Veo", "Pro"],

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
    # 🔐 Google 账号登录页 (accounts.google.com) — 供 utils/auto_login.py 使用
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 掉登录后自动重新登进去要走「选号 → 邮箱 → 密码 → 两步验证」几步表单。
    # Google 这几页的 class 名是构建期混淆产物（每次发版都变），所以这里一律
    # 只用**语义属性**定位：input 的 type/name/autocomplete、以及 Google 十几年
    # 没换过的 id（#identifierId / #totpPin）。文案兜底放最后，且中英都列。
    #
    # 新增于 2026-07-30。不涉及本文件顶部 LOCKED 声明的三项
    # (config_btn_keywords / ORIENT_ICON_MAP / RATIO_MAP)。
    "google_login": {
        # 邮箱输入页 (signin/identifier)
        "email_input": [
            "input#identifierId",
            "input[name='identifier']",
            "input[type='email']",
        ],
        # 密码页 (signin/challenge/pwd)。autocomplete 属性比 name 稳。
        "password_input": [
            "input[type='password'][name='Passwd']",
            "input[name='Passwd']",
            "input[type='password'][autocomplete='current-password']",
            "input[type='password']:not([aria-hidden='true'])",
        ],
        # 两步验证的动态码输入框 (challenge/totp)。Google 的备用码输入框
        # (challenge/backup-code) 用的是 name='backupCodePin'，故意不列——
        # 备用码是一次性的，自动填等于烧掉用户的应急手段。
        "totp_input": [
            "input#totpPin",
            "input[name='totpPin']",
            "input[type='tel'][autocomplete='one-time-code']",
            "input[autocomplete='one-time-code']",
        ],
        # 「下一步 / Next」。Google 把它渲染成 div[role=button] 已经很多年，
        # 但 #identifierNext / #passwordNext 这两个容器 id 一直在。
        "next_btn": [
            "#identifierNext button",
            "#passwordNext button",
            "#totpNext button",
            "#identifierNext",
            "#passwordNext",
            "#totpNext",
            "button:has-text('Next')",
            "button:has-text('下一步')",
            "button:has-text('Berikutnya')",
            "div[role='button']:has-text('Next')",
            "div[role='button']:has-text('下一步')",
        ],
        # 账号选择页 (signin/accountchooser) 上的「使用其他账号」。
        # 目标邮箱本身在列表里时优先直接点它（auto_login 动态构造选择器），
        # 只有找不到才退到这个入口重新走邮箱流程。
        "use_another_account": [
            "li:has-text('Use another account')",
            "li:has-text('使用其他账号')",
            "li:has-text('使用其他帳戶')",
            "div[role='link']:has-text('Use another account')",
            "div[role='link']:has-text('使用其他账号')",
            "*:has-text('Gunakan akun lain')",
        ],
        # 「换一种验证方式」页上通往身份验证器 App 的那一项。Google 默认可能
        # 先推手机点确认（Tap Yes），那种自动化处理不了，必须切到 TOTP。
        "try_another_way": [
            "button:has-text('Try another way')",
            "button:has-text('尝试其他方式')",
            "div[role='button']:has-text('Try another way')",
            "div[role='button']:has-text('尝试其他方式')",
            "*[jsname]:has-text('More ways to verify')",
        ],
        "authenticator_option": [
            # challenge picker 的稳定语义属性；6 是 Google 的 TOTP challenge type。
            "[data-challengetype='6']",
            "li:has-text('Google Authenticator')",
            "li:has-text('authenticator app')",
            "li:has-text('身份验证器')",
            "li:has-text('验证码应用')",
            "div[role='link']:has-text('Google Authenticator')",
            "div[role='button']:has-text('Google Authenticator')",
            "div[role='button']:has-text('authenticator app')",
            "div[role='link']:has-text('身份验证器')",
            "div[role='button']:has-text('身份验证器')",
        ],
        # 登录页上表示「这一步出错了」的提示区。用来把「密码错」跟「网络慢
        # 还没跳转」区分开——分不清就会在密码错的情况下不停重试，把号锁掉。
        "error_text": [
            "div[aria-live='assertive']",
            "div[jsname='B34EJ'] span",
            "div.o6cuMc",
            "span.OyEIQ",
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
