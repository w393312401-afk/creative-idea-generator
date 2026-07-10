/* ============================================================
   暗夜模式 toggle(双前端共享)
   —— 主 app(index.html)与控制台(console.html)原本各持一份逐字节相同的拷贝
   (app.js / console.js);此处合并为单一来源。主题存 localStorage('spark_theme'),
   同源共享,两页保持同步。
   自执行 IIFE:按钮不存在时 (!btn) 直接返回,故在任意加载位置都安全;
   两个 HTML 均在按钮已解析后加载本脚本(index 用 defer,console 置于 body 末)。
   ============================================================ */
(function initThemeToggle() {
    const btn = document.getElementById('theme-toggle-btn');
    const icon = document.getElementById('theme-toggle-icon');
    if (!btn) return;
    const isDark = () => document.documentElement.getAttribute('data-theme') === 'dark';
    const sync = () => {
        if (icon) icon.textContent = isDark() ? '☀️' : '🌙';
        btn.title = isDark() ? '切换到明亮模式' : '切换到暗夜模式';
    };
    sync();
    btn.addEventListener('click', () => {
        const next = !isDark();
        document.documentElement.setAttribute('data-theme', next ? 'dark' : 'light');
        try { localStorage.setItem('spark_theme', next ? 'dark' : 'light'); } catch (e) {}
        sync();
    });
})();
