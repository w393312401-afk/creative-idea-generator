import os
import sys
import time
from playwright.sync_api import sync_playwright

def run_full_ui_suite():
    print("=== Running Full Replica Navigation & Interaction UI Test Suite ===", flush=True)
    chrome_path = os.getenv('CHROME_PATH', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    test_url = os.getenv('REPLICA_TEST_URL', 'http://127.0.0.1:8085')
    with sync_playwright() as p:
        launch_kwargs = {
            'headless': True,
            'args': ['--disable-gpu', '--no-sandbox']
        }
        if os.path.exists(chrome_path):
            launch_kwargs['executable_path'] = chrome_path
        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(viewport={'width': 1280, 'height': 800})
        page = context.new_page()

        console_errors = []
        page.on('console', lambda msg: console_errors.append(f"[{msg.type}] {msg.text}") if msg.type in ['error', 'warn'] else None)
        page.on('pageerror', lambda err: console_errors.append(f"[PAGE ERROR] {err}"))

        print(f"\n1. Loading application at {test_url}...")
        page.goto(test_url, wait_until='domcontentloaded')
        page.wait_for_timeout(1000)

        print("2. Navigating to Replica workspace...")
        page.click('#main-tab-replica')
        page.wait_for_timeout(1500)

        # -------------------------------------------------------------
        # TEST 1: Real user mouse clicks on all Sticky Nav Bar buttons
        # -------------------------------------------------------------
        print("\n--- [TEST 1] Sticky Section Nav Bar Real Mouse Clicks ---")
        nav_buttons = page.locator('.replica-nav-item').all()
        assert len(nav_buttons) >= 4, f"Expected at least 4 nav buttons, found {len(nav_buttons)}"
        
        for idx, btn in enumerate(nav_buttons):
            target_id = btn.get_attribute('data-nav-target')
            btn_text = btn.inner_text().replace('\n', ' ')
            is_disabled = btn.is_disabled()
            print(f"  Nav button [{idx}] '{btn_text}' -> #{target_id} (disabled={is_disabled})")
            assert not is_disabled, f"Nav button '{btn_text}' should NOT be disabled!"
            
            # Real mouse click
            btn.click()
            # Wait for smooth scroll animation to settle
            page.evaluate('''async () => {
                const shell = document.querySelector('.replica-shell');
                if (!shell) return;
                let last = shell.scrollTop;
                for (let i = 0; i < 30; i++) {
                    await new Promise(r => setTimeout(r, 50));
                    if (Math.abs(shell.scrollTop - last) < 2 && i > 5) break;
                    last = shell.scrollTop;
                }
            }''')
            page.wait_for_timeout(200)
            
            # Verify target position and scroll
            scroll_pos = page.evaluate('() => document.querySelector(".replica-shell").scrollTop')
            panel_top = page.evaluate('() => document.getElementById("panel-replica")?.getBoundingClientRect().top || 0')
            target_top_raw = page.evaluate('(id) => document.getElementById(id)?.getBoundingClientRect().top', target_id)
            target_rel_top = target_top_raw - panel_top if target_top_raw is not None else None
            print(f"    -> Scroll Top: {scroll_pos}px | Target Top relative to panel: {target_rel_top}px")
            assert target_rel_top is not None, f"Target #{target_id} not found in DOM"
            # Target should be visible in the panel viewport (top within panel height and not below bottom)
            panel_height = page.evaluate('() => document.getElementById("panel-replica")?.clientHeight || 600')
            assert target_rel_top < panel_height, f"Target #{target_id} is below viewport (rel_top={target_rel_top}, panel_h={panel_height})"
            assert target_rel_top > -200, f"Target #{target_id} scrolled too far above viewport (rel_top={target_rel_top})"

        # -------------------------------------------------------------
        # TEST 2: Beat Folding (Fold All & Unfold All)
        # -------------------------------------------------------------
        print("\n--- [TEST 2] Beat Cards Fold All & Unfold All ---")
        fold_all_btn = page.locator('#replica-toggle-fold-all')
        if fold_all_btn.count() > 0:
            total_beats = page.locator('.replica-beat').count()
            print(f"  Total beat cards: {total_beats}")
            
            # Fold all
            print("  Clicking Fold All...")
            fold_all_btn.click()
            page.wait_for_timeout(500)
            collapsed_count = page.locator('.replica-beat.is-collapsed').count()
            print(f"    Collapsed count: {collapsed_count}/{total_beats}")
            assert collapsed_count == total_beats, f"Expected all {total_beats} to be collapsed, got {collapsed_count}"

            # Unfold all
            print("  Clicking Unfold All...")
            fold_all_btn.click()
            page.wait_for_timeout(500)
            collapsed_count_after = page.locator('.replica-beat.is-collapsed').count()
            print(f"    Collapsed count after unfold: {collapsed_count_after}/{total_beats}")
            assert collapsed_count_after == 0, f"Expected 0 collapsed, got {collapsed_count_after}"

        # -------------------------------------------------------------
        # TEST 3: Single Beat Card Fold / Unfold
        # -------------------------------------------------------------
        print("\n--- [TEST 3] Single Beat Card Fold / Unfold ---")
        first_beat = page.locator('.replica-beat').first
        if first_beat.count() > 0:
            beat_id = first_beat.get_attribute('data-beat-id')
            fold_btn = first_beat.locator('.replica-beat-fold-btn')
            
            # Fold single beat
            print(f"  Clicking single fold button on {beat_id}...")
            fold_btn.click()
            page.wait_for_timeout(300)
            is_col = page.evaluate('(id) => document.querySelector(`[data-beat-id="${id}"]`)?.classList.contains("is-collapsed")', beat_id)
            print(f"    Is {beat_id} collapsed: {is_col}")
            assert is_col, f"Card {beat_id} should be collapsed"
            
            # Unfold single beat
            print(f"  Clicking single fold button again on {beat_id}...")
            fold_btn.click()
            page.wait_for_timeout(300)
            is_col2 = page.evaluate('(id) => document.querySelector(`[data-beat-id="${id}"]`)?.classList.contains("is-collapsed")', beat_id)
            print(f"    Is {beat_id} collapsed: {is_col2}")
            assert not is_col2, f"Card {beat_id} should be unfolded"

        # -------------------------------------------------------------
        # TEST 4: Beat Jump Chips (B01, B02, B05, etc.)
        # -------------------------------------------------------------
        print("\n--- [TEST 4] Beat Jump Chips Navigation ---")
        jump_chips = page.locator('.replica-beat-jump-chip').all()
        print(f"  Total jump chips found: {len(jump_chips)}")
        test_indices = [0, min(2, len(jump_chips)-1), min(5, len(jump_chips)-1), len(jump_chips)-1]
        test_indices = sorted(list(set(test_indices)))
        
        for idx in test_indices:
            chip = jump_chips[idx]
            beat_id = chip.get_attribute('data-jump-beat')
            print(f"  Clicking jump chip for '{beat_id}'...")
            chip.click()
            page.evaluate('''async () => {
                const shell = document.querySelector('.replica-shell');
                if (!shell) return;
                let last = shell.scrollTop;
                for (let i = 0; i < 30; i++) {
                    await new Promise(r => setTimeout(r, 50));
                    if (Math.abs(shell.scrollTop - last) < 2 && i > 5) break;
                    last = shell.scrollTop;
                }
            }''')
            page.wait_for_timeout(200)
            
            panel_top = page.evaluate('() => document.getElementById("panel-replica")?.getBoundingClientRect().top || 0')
            card_top_raw = page.evaluate('(id) => document.querySelector(`[data-beat-id="${id}"]`)?.getBoundingClientRect().top', beat_id)
            card_rel_top = card_top_raw - panel_top if card_top_raw is not None else None
            scroll_pos = page.evaluate('() => document.querySelector(".replica-shell").scrollTop')
            print(f"    -> Card top relative to panel: {card_rel_top}px | Shell scroll: {scroll_pos}px")
            assert card_rel_top is not None, f"Card {beat_id} not found"
            panel_height = page.evaluate('() => document.getElementById("panel-replica")?.clientHeight || 600')
            assert card_rel_top < panel_height, f"Card {beat_id} is below viewport (rel_top={card_rel_top})"
            assert card_rel_top > -200, f"Card {beat_id} scrolled too far above viewport (rel_top={card_rel_top})"

        # -------------------------------------------------------------
        # TEST 5: Floating Navigation Tools
        # -------------------------------------------------------------
        print("\n--- [TEST 5] Floating Quick Navigation Tools ---")
        # Scroll down to make floating tools visible
        page.evaluate('() => document.querySelector(".replica-shell").scrollTo({ top: 1000 })')
        page.wait_for_timeout(400)
        
        float_visible = page.evaluate('() => document.getElementById("replica-floating-tools")?.classList.contains("is-visible")')
        print(f"  Floating tools visible after scroll > 150px: {float_visible}")
        assert float_visible, "Floating tools should have .is-visible class after scrolling"
        
        # Test clicking 'Top' floating button
        top_btn = page.locator('[data-float-action="top"]')
        if top_btn.count() > 0:
            print("  Clicking Floating 'Top' button...")
            top_btn.click()
            page.wait_for_timeout(600)
            scroll_after_top = page.evaluate('() => document.querySelector(".replica-shell").scrollTop')
            print(f"    Scroll position after 'Top': {scroll_after_top}px")
            assert scroll_after_top < 50, f"Expected scroll near 0, got {scroll_after_top}"

        # -------------------------------------------------------------
        # TEST 6: ScrollSpy dynamic active state
        # -------------------------------------------------------------
        print("\n--- [TEST 6] ScrollSpy Active Item Synchronization ---")
        # Click Beats section
        beats_nav = page.locator('.replica-nav-item[data-nav-target="replica-sec-beats"]')
        if beats_nav.count() > 0:
            beats_nav.click()
            page.wait_for_timeout(600)
            active_target = page.evaluate('() => document.querySelector(".replica-nav-item.active")?.dataset.navTarget')
            print(f"  Active nav item after scrolling to beats: {active_target}")
            assert active_target == 'replica-sec-beats', f"Expected active nav target 'replica-sec-beats', got '{active_target}'"

        print("\n--- Console Errors/Warnings During Test ---")
        if console_errors:
            for err in console_errors:
                print("  ", err)
        else:
            print("  (None! Clean execution)")

        print("\n🎉 [ALL UI NAVIGATION & INTERACTION TESTS PASSED!]")
        browser.close()

if __name__ == '__main__':
    run_full_ui_suite()
