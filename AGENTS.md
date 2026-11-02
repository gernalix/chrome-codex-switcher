# chrome-codex-switcher

Read project-capsule.yaml and README.md. Use isolated task worktrees and the
installed Git single writer. Reuse existing modules and preserve their tested
boundaries.

store.py owns SQLite note/context persistence; context_bindings.py and clipboard_binding.py isolate binding policies. broker.py and daemon.py route bounded local events; extension/ owns Chrome surfaces, while the GNOME companion owns compositor clipboard/focus events without desktop overlays. verifier and identity watchers fail closed on ambiguous active threads.

- Never infer the canonical prompt binding from URLs/titles/deep links or show a stale note on another thread.
- Preserve existing note state and exact pairings; ambiguous identity fails closed.
- Keep the local HTTP boundary loopback-only and scoped; no private note or conversation bodies in Git/output.
- Never focus real windows, synthesize keystrokes or read the live clipboard for source verification.
- Historical prompt cards do not authorize restarting retired C2/C3 control planes.
- Browser-extension persistence E2E in CI is distinct from live GNOME/Wayland acceptance.

Run the declared isolated fixture checks. Do not focus windows, synthesize
keyboard input, read the real clipboard or access private profiles/data for
verification. Live browser/compositor acceptance and installation are separate.
