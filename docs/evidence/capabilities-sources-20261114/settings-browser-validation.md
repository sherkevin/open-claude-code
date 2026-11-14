# Capability settings browser validation - school cloud

Commit: 845a9b96b688481ad52cc572bfa703207e44df1f

Environment: isolated loopback 127.0.0.1:18143, actual compiled App.tsx, synthetic admin/member users and isolated SQLite under this directory. No real customer data, no real credential reading, no model/provider calls, no shared service restart. preview.py is the isolated test host; browser.sh is the isolated agent-browser launcher. Both test host and browser were stopped after validation.

Verified:
- Actual administrator App navigation exposes capability settings. Member navigation does not; member direct GET returns HTTP 403.
- Real UI save persists and increments revision (version 5 in saved.png). Saved state explicitly does not claim provider verification.
- Another authenticated PUT increments revision without refreshing the page. UI save receives HTTP 409 with Chinese reload guidance and retains its draft (conflict.png).
- Reload recovers the persisted current version (version 4 was verified after the first conflict).
- Loading state screenshot uses a browser-only delayed fetch fixture for /capability-settings. The original fetch was restored afterward.
- Initial loading error screenshot uses a browser-only rejected fetch fixture for /capability-settings. Error is visible, save button is absent, and restoring fetch plus reload recovers the real configuration.
- Unconfigured SearXNG/Brave/DeepSeek options are disabled. Only search/fetch have checkboxes; OCR and image search explicitly show not integrated.
- 1440x1080 desktop and 390x844 viewport: usable two-column / one-column layout, no horizontal document overflow. Mobile measured innerWidth=390, document.body.scrollWidth=383.
- Found and corrected an existing CSS class-name collision: original capability-grid rules imposed 270px inner scroll. New page uses dedicated capability-settings-grid/card classes.

Evidence:
- desktop-fixed.png: corrected desktop layout
- mobile-top.png: 390px view
- loading.png: pending configuration request
- load-error.png: initial load failure, retry, no save action
- saved.png: acknowledged real save
- conflict.png: actual revision conflict
- member.png: member navigation

Key reproduction commands after launching preview.py with uvicorn (fresh synthetic browser profile):

    bash browser.sh open http://127.0.0.1:18143
    bash browser.sh snapshot -i
    # Fill synthetic test account fields, click Login, then actual navigation.
    bash browser.sh find role button click --name '能力与来源' --exact
    bash browser.sh snapshot -i
    # Use scrollintoview before interacting with buttons outside current viewport.
    bash browser.sh find role button click --name '保存并应用'
    bash browser.sh set viewport 390 844
    bash browser.sh eval 'document.querySelector(".main").scrollTop=0'
    bash browser.sh screenshot mobile-top.png

Source regression commands (all provider transports mocked):

    PYTHONPATH=server /media/data3/dengkw/projects/open-claude-code/.venv/bin/python -m pytest server/tests/test_capability_settings.py server/tests/test_web_tools.py server/tests/test_deepseek_api.py -q --basetemp=/media/data3/dengkw/tmp/capability-tests-final
    npm --prefix client test -- --run
    npm --prefix client run build

The browser screenshots validate only this local synthetic environment, not a Tencent deployment or real provider connectivity. Independent OCR, image search, key editing, quotas, per-capability assistant toggles and domain allowlists remain outside this slice.
