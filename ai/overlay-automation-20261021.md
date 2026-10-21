# Chrome overlay automation repair — 2026-10-21

Goal/constraints: preserve normally clickable/editable Chrome overlays; restore browser automation on covered page controls. Remove only overlays if preservation proves impossible. No other features or data changes.
Baseline verified on GitHub and Fedora: a7f70fb.

User steering: rejected default click-through requiring Alt for ordinary note use. That candidate was never deployed or published and has been reverted. The installed fix retains normal note pointer behavior.

Final implementation: trusted Alt+Shift+H key events toggle a temporary CSS visibility pause in the current content script. Unlike Chrome extension commands, this listener works with browser automation. Pause is not persisted; it survives context refresh but clears on full page reload. Agent must press the same shortcut after completing work. Existing hidden preference, note text, geometry and all ordinary controls are preserved. Probe exposes automation_paused and accurate visible status.

Evidence:
- Real connected browser: ordinary link outside note works; overlay close button can be clicked. Existing Alt+Shift+V extension command did not fire from automated key input.
- Chrome for Testing 149, isolated profile/SQLite/daemon at port 43828: 4/4 browser tests PASS in 7.982 s. New regression verifies ordinary unmodified note input, intercepted covered button before pause, hit-tested covered button clicks after pause/context refresh, resume and saved-hidden preservation. Existing minimal overlay/pairing/drag and refresh/reopen persistence tests PASS.
- Node syntax, diff whitespace checks, 5 extension lifecycle contract tests and 4 existing overlay contract tests PASS.
- Initial attempt with branded installed Chrome 153 could not load the isolated unpacked extension (4 hydration timeouts). Switched to installed Chrome for Testing plus matching driver; no product change made for this test environment limitation.

Checklist/current state:
- [x] Implement and document minimal temporary pause; normal editing unchanged.
- [x] Verify targeted browser regression and existing persistence/overlay tests.
- [x] Preserve source in Fedora branch fix/overlay-automation-pause-20261021, worktree /home/daniele/projects/chrome-codex-switcher-worktrees/overlay-automation-20261021.
- [x] Copy tested content.js to the installed extension; verify byte equality and daemon health.
- [x] Restore hidden=false for temporary IANA test context after close-button check.
- [ ] User reloads Chrome ↔ Codex Switcher in chrome://extensions.
- [ ] Verify pause/click/resume through the connected ChatGPT browser extension after reload.
- [ ] Push branch after approval blocker is resolved.

Blockers: Browser security policy blocks chrome://extensions navigation and explicitly forbids alternate browser-control workarounds. User must perform extension reload. Automatic approval review rejected the Git push as publication beyond authorization; no alternate publication attempted. Source is local only, not commit+push canonical persistence.
Next action: user reloads the installed extension and authorizes publishing the fix branch; then verify live automation and push the tested branch.
