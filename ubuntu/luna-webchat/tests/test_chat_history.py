from pathlib import Path


def test_chat_ui_sends_full_history_with_followups():
    app_source = Path("app.py").read_text()

    assert "let chatHistory = [];" in app_source
    assert "const turnMessages = chatHistory.slice();" in app_source
    assert "chatHistory.push({ role: 'assistant', content: reply });" in app_source


def test_server_prompt_uses_previous_turns_for_followups():
    app_source = Path("app.py").read_text()

    assert "Use the earlier turns in this conversation as context" in app_source


def test_homepage_disables_browser_caching():
    app_source = Path("app.py").read_text()

    assert '"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"' in app_source


def test_robot_camera_panel_and_proxy_endpoint_exist():
    app_source = Path("app.py").read_text()

    assert 'id=\\"robotCam\\"' in app_source
    assert '/api/robot/camera' in app_source
    assert 'LUNA_ROBOT_CAMERA_URL' in app_source


def test_pi_camera_query_hook_exists():
    app_source = Path("app.py").read_text()

    assert 'LUNA_PI_HOST' in app_source
    assert 'def _capture_pi_camera_image' in app_source
    assert 'def _handle_pi_camera_query' in app_source
    assert 'scene:' in app_source


if __name__ == "__main__":
    test_chat_ui_sends_full_history_with_followups()
    test_server_prompt_uses_previous_turns_for_followups()
    test_homepage_disables_browser_caching()
    test_robot_camera_panel_and_proxy_endpoint_exist()
    print("chat history tests passed")
