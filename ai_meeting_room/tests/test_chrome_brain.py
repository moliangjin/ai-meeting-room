from __future__ import annotations

import inspect
import os
import stat
import tempfile
import unittest
from pathlib import Path

from ai_meeting_room.brain.chrome_brain import (
    CHROME_BRAIN_PROFILE,
    CHROME_DEBUG_PORT,
    CHROME_DEBUG_HOST,
    ChromeBrainProfileManager,
    ChromeBrainRuntime,
    DedicatedChromeProcessResolver,
    DedicatedProfileSingletonInspector,
    DedicatedChromeProcess,
    _SafeChromeOutputTail,
    ChatGPTChromeDomAdapter,
    ChatGPTTargetResolver,
    ChromeWebBrainHost,
    ChromeWebBrainTransport,
    find_google_chrome,
)


class ChromeBrainTests(unittest.TestCase):
    def test_chrome_brain_uses_dedicated_profile(self) -> None:
        self.assertIn("AI Meeting Room", str(CHROME_BRAIN_PROFILE))
        self.assertEqual(CHROME_BRAIN_PROFILE.name, "chrome-brain-profile")

    def test_chrome_brain_never_uses_default_user_profile(self) -> None:
        source = inspect.getsource(ChromeBrainRuntime.start)
        self.assertIn("--user-data-dir=", source)
        self.assertNotIn("Default", source)
        self.assertNotIn("~/Library/Application Support/Google/Chrome", source)

    def test_cdp_binds_loopback_only(self) -> None:
        self.assertEqual(CHROME_DEBUG_HOST, "127.0.0.1")
        source = inspect.getsource(ChromeBrainRuntime.start)
        self.assertIn("--remote-debugging-address={CHROME_DEBUG_HOST}", source)

    def test_auth_required_opens_chrome_brain(self) -> None:
        source = inspect.getsource(ChromeWebBrainHost.open)
        self.assertIn("self.start()", source)
        self.assertIn("bring_to_front", inspect.getsource(ChromeWebBrainHost._apply_health))

    def test_authenticated_composer_maps_ready(self) -> None:
        self.assertIn("AUTHENTICATED", inspect.getsource(ChatGPTChromeDomAdapter.status))
        self.assertIn('self.state = "READY"', inspect.getsource(ChromeWebBrainHost._apply_health))

    def test_challenge_pauses_meeting(self) -> None:
        source = inspect.getsource(ChromeWebBrainHost._apply_health)
        self.assertIn('"CHALLENGE_REQUIRED"', source)
        self.assertIn("on_failure", inspect.getsource(ChromeWebBrainHost._fail))

    def test_chrome_process_loss_pauses_meeting(self) -> None:
        source = inspect.getsource(ChromeWebBrainHost.refresh)
        self.assertIn("CHROME_PROCESS_LOST", source)
        self.assertIn("_fail(\"ERROR\"", source)

    def test_chrome_profile_persists_across_restart(self) -> None:
        source = inspect.getsource(ChromeBrainProfileManager.create)
        self.assertIn("mkdir", source)
        self.assertIn("chmod", source)
        self.assertNotIn("rmtree", source)

    def test_meeting_conversation_binding_isolated(self) -> None:
        source = inspect.getsource(ChromeWebBrainTransport.open_conversation)
        self.assertIn("conversation_id", source)
        self.assertIn("INVALID_CONVERSATION_BINDING", source)

    def test_chrome_transport_cannot_mutate_core_directly(self) -> None:
        source = inspect.getsource(ChromeWebBrainTransport)
        self.assertNotIn("SQLiteStore", source)
        self.assertNotIn("MeetingCore", source)
        self.assertNotIn("BrainInbox", source)

    def test_no_cookie_export(self) -> None:
        source = inspect.getsource(ChromeWebBrainTransport) + inspect.getsource(ChromeWebBrainHost)
        self.assertNotIn("storage_state", source)
        self.assertNotIn("document.cookie", source)

    def test_no_token_extraction(self) -> None:
        source = inspect.getsource(ChromeBrainRuntime) + inspect.getsource(ChromeWebBrainTransport)
        self.assertNotIn("Authorization", source)
        self.assertNotIn("refreshToken", source)

    def test_profile_permissions_are_private(self) -> None:
        self.assertEqual(stat.S_IMODE(os.stat(Path(__file__).parent).st_mode) & 0o700, 0o700)

    def test_target_resolver_selects_authenticated_chatgpt_page(self) -> None:
        authenticated = _FakePage("https://chatgpt.com/", composer=True, active=True)
        oauth = _FakePage("https://accounts.google.com/o/oauth2/auth", composer=False, active=True)
        self.assertIs(ChatGPTTargetResolver().resolve([oauth, authenticated]), authenticated)

    def test_target_resolver_ignores_google_oauth_page(self) -> None:
        resolver = ChatGPTTargetResolver()
        self.assertIsNone(resolver.resolve([_FakePage("https://accounts.google.com/", active=True)]))

    def test_target_resolver_ignores_chrome_internal_pages(self) -> None:
        resolver = ChatGPTTargetResolver()
        self.assertIsNone(resolver.resolve([_FakePage("chrome://newtab/", active=True)]))

    def test_multiple_chatgpt_tabs_selects_composer_ready_target(self) -> None:
        loading = _FakePage("https://chatgpt.com/", composer=False, active=True, ready_state="interactive")
        ready = _FakePage("https://chatgpt.com/c/ready", composer=True, active=False)
        self.assertIs(ChatGPTTargetResolver().resolve([loading, ready]), ready)

    def test_authenticated_chatgpt_dom_maps_ready(self) -> None:
        page = _FakePage("https://chatgpt.com/", composer=True)
        self.assertEqual(ChatGPTChromeDomAdapter.status(page), "AUTHENTICATED")

    def test_current_chatgpt_composer_detected(self) -> None:
        page = _FakePage("https://chatgpt.com/", composer=True)
        self.assertIsNotNone(ChatGPTChromeDomAdapter.find_composer(page))

    def test_contenteditable_composer_supported(self) -> None:
        page = _FakePage("https://chatgpt.com/", composer=True, contenteditable=True)
        self.assertIsNotNone(ChatGPTChromeDomAdapter.find_composer(page))

    def test_loading_page_not_dom_unknown(self) -> None:
        page = _FakePage("https://chatgpt.com/", ready_state="loading")
        self.assertEqual(ChatGPTChromeDomAdapter.status(page), "LOADING")

    def test_oauth_redirect_rebinds_chatgpt_target(self) -> None:
        page = _FakePage("https://accounts.google.com/", active=True)
        resolver = ChatGPTTargetResolver()
        self.assertIsNone(resolver.resolve([page]))
        page.url = "https://chatgpt.com/"
        page.composer = True
        self.assertIs(resolver.resolve([page]), page)

    def test_destroyed_target_triggers_re_resolve(self) -> None:
        first = _FakePage("https://chatgpt.com/c/first", composer=True)
        second = _FakePage("https://chatgpt.com/c/second", composer=True)
        resolver = ChatGPTTargetResolver()
        self.assertIs(resolver.resolve([first, second]), second)
        self.assertIs(resolver.resolve([first]), first)

    def test_dom_unknown_diagnostics_do_not_expose_secrets(self) -> None:
        page = _FakePage("https://chatgpt.com/", composer=False)
        resolver = ChatGPTTargetResolver()
        resolver.inspect([page])
        diagnostics = resolver.diagnostics()
        self.assertEqual(diagnostics[0]["hostname"], "chatgpt.com")
        self.assertNotIn("cookie", str(diagnostics).lower())
        self.assertNotIn("token", str(diagnostics).lower())

    def test_open_brain_reuses_existing_chrome_instance(self) -> None:
        source = inspect.getsource(ChromeBrainRuntime.start)
        self.assertIn("process_resolver.find()", source)
        self.assertIn("self._adopted = True", source)

    def test_start_or_attach_starts_missing_chrome(self) -> None:
        self.assertIn("self.start()", inspect.getsource(ChromeBrainRuntime.start_or_attach))

    def test_start_or_attach_reuses_existing_chrome(self) -> None:
        source = inspect.getsource(ChromeBrainRuntime.start)
        self.assertIn("self._adopted", source)
        self.assertIn("process_resolver.find()", source)

    def test_stale_pid_triggers_clean_restart(self) -> None:
        source = inspect.getsource(ChromeBrainRuntime.start)
        self.assertIn("self.process.poll() is None", source)
        self.assertIn("Popen", source)

    def test_stale_cdp_endpoint_not_reused(self) -> None:
        source = inspect.getsource(ChromeBrainRuntime.start)
        self.assertIn("self.port = None", source)
        self.assertIn("DevToolsActivePort", inspect.getsource(ChromeBrainRuntime._read_devtools_active_port))

    def test_dynamic_cdp_endpoint_rediscovered(self) -> None:
        source = inspect.getsource(ChromeBrainRuntime) + inspect.getsource(ChromeWebBrainTransport.connect)
        self.assertIn("/json/version", source)
        self.assertIn("self.runtime.endpoint", source)

    def test_same_dedicated_profile_used_after_restart(self) -> None:
        source = inspect.getsource(ChromeBrainRuntime.start)
        self.assertIn("self.profile.create()", source)
        self.assertIn("--user-data-dir=", source)

    def test_v1_app_restart_does_not_probe_experimental_brain_automatically(self) -> None:
        desktop_main = Path(__file__).parents[2] / "desktop" / "main.js"
        source = desktop_main.read_text()
        boot = source.split("async function boot()", 1)[1].split("const singleInstanceLock", 1)[0]
        self.assertNotIn("startFormalChromeBrain()", boot)
        self.assertIn("if (process.env.AIMR_ENABLE_ELECTRON_BRAIN_LEGACY === '1') createBrainWindow()", boot)
        self.assertIn("api/brain/real-chrome/open", source)

    def test_authenticated_profile_becomes_ready_after_restart(self) -> None:
        source = inspect.getsource(ChromeWebBrainHost._apply_health)
        self.assertIn('auth_state == "AUTHENTICATED"', source)
        self.assertIn('self.state = "READY"', source)

    def test_chrome_runtime_crash_maps_fail_closed(self) -> None:
        source = inspect.getsource(ChromeWebBrainHost.refresh)
        self.assertIn('"ERROR", "CHROME_PROCESS_LOST"', source)
        self.assertIn("_fail", source)

    def test_chrome_uses_dynamic_debug_port(self) -> None:
        source = inspect.getsource(ChromeBrainRuntime.start)
        self.assertIn('"--remote-debugging-port=0"', source)
        self.assertEqual(CHROME_DEBUG_PORT, 0)

    def test_devtools_active_port_is_authoritative(self) -> None:
        source = inspect.getsource(ChromeBrainRuntime._discover_cdp_endpoint)
        self.assertIn("_read_devtools_active_port", source)
        self.assertIn("self.port = port", source)

    def test_port_conflict_selects_new_port(self) -> None:
        source = inspect.getsource(ChromeBrainRuntime.start)
        self.assertIn('"--remote-debugging-port=0"', source)
        self.assertNotIn("kill(", source)

    def test_unknown_process_on_old_port_is_never_killed(self) -> None:
        source = inspect.getsource(DedicatedChromeProcessResolver.gracefully_stop)
        self.assertIn("SIGTERM", source)
        self.assertNotIn("kill -9", source.lower())

    def test_stale_runtime_metadata_is_recovered(self) -> None:
        source = inspect.getsource(ChromeBrainRuntime.start) + inspect.getsource(ChromeBrainRuntime.wait_until_ready)
        self.assertIn("self.port = None", source)
        self.assertIn("_discover_cdp_endpoint", source)

    def test_same_dedicated_profile_survives_port_change(self) -> None:
        source = inspect.getsource(ChromeBrainRuntime.start)
        self.assertIn("self.profile.create()", source)
        self.assertIn("--user-data-dir=", source)

    def test_reconnect_rediscovers_debug_endpoint(self) -> None:
        source = inspect.getsource(ChromeWebBrainTransport.connect)
        self.assertIn("wait_until_ready", source)
        self.assertIn("self.runtime.endpoint", source)

    def test_duplicate_runtime_owner_prevented(self) -> None:
        source = inspect.getsource(ChromeBrainRuntime.start)
        self.assertIn("existing = self.process_resolver.find()", source)
        self.assertIn("start_or_attach", inspect.getsource(ChromeBrainRuntime.start_or_attach))

    def test_launch_pid_is_not_browser_identity(self) -> None:
        source = inspect.getsource(ChromeBrainRuntime.wait_until_ready)
        self.assertIn("_capture_process_exit", source)
        self.assertIn("browser_handoff_detected", source)
        self.assertIn("owned_browser_pid", source)

    def test_existing_dedicated_chrome_without_cdp_is_reported_without_termination(self) -> None:
        source = inspect.getsource(ChromeBrainRuntime.start)
        self.assertIn("ATTACHING_CHROME", source)
        self.assertIn("EXISTING_PROFILE_INSTANCE_WITHOUT_CDP", source)
        self.assertNotIn("gracefully_stop", source)

    def test_cdp_requires_owned_process_and_live_endpoint(self) -> None:
        source = inspect.getsource(ChromeBrainRuntime._discover_cdp_endpoint)
        self.assertIn("self.process_resolver.find()", source)
        self.assertIn("_read_devtools_active_port", source)
        self.assertIn("_debug_endpoint_busy", source)

    def test_runtime_diagnostics_separate_launch_and_browser_pids(self) -> None:
        runtime = ChromeBrainRuntime()
        diagnostics = runtime.diagnostics()
        self.assertIn("launchPid", diagnostics)
        self.assertIn("ownedBrowserPid", diagnostics)
        self.assertIn("browserHandoffDetected", diagnostics)

    def test_chrome_launch_captures_exit_code(self) -> None:
        source = inspect.getsource(ChromeBrainRuntime._capture_process_exit)
        self.assertIn("chrome_exit_code", source)
        self.assertIn("launch_exit_code", source)

    def test_chrome_launch_captures_signal(self) -> None:
        source = inspect.getsource(ChromeBrainRuntime._capture_process_exit)
        self.assertIn("chrome_exit_signal", source)
        self.assertIn("signal.Signals", source)

    def test_chrome_launch_captures_safe_stderr_tail(self) -> None:
        source = inspect.getsource(ChromeBrainRuntime._start_output_capture)
        self.assertIn("stderr", source)
        self.assertIn("_SafeChromeOutputTail", source)
        self.assertIn("chromeStderrTail", inspect.getsource(ChromeBrainRuntime.diagnostics))

    def test_chrome_launch_args_are_diagnostic_and_secret_safe(self) -> None:
        source = inspect.getsource(ChromeBrainRuntime.start)
        self.assertIn("--enable-logging=stderr", source)
        self.assertIn("--user-data-dir=", source)
        self.assertNotIn("Authorization", source)
        self.assertNotIn("Cookie", source)

    def test_profile_lock_diagnostics_are_read_only(self) -> None:
        source = inspect.getsource(DedicatedChromeProcessResolver.profile_lock_diagnostics)
        self.assertIn("SingletonLock", source)
        self.assertNotIn("unlink", source)
        self.assertNotIn("rmtree", source)

    def test_existing_profile_chrome_is_reported(self) -> None:
        source = inspect.getsource(ChromeBrainRuntime._inspect_profile_state)
        self.assertIn("existing_profile_chrome", source)
        self.assertIn("profile_process_diagnostics", source)

    def test_existing_profile_without_cdp_is_reported(self) -> None:
        source = inspect.getsource(ChromeBrainRuntime.start)
        self.assertIn("EXISTING_PROFILE_INSTANCE_WITHOUT_CDP", source)
        self.assertIn("remoteDebuggingArgumentPresent", source)

    def test_launch_timeline_records_active_port_and_exit(self) -> None:
        source = inspect.getsource(ChromeBrainRuntime)
        self.assertIn("DevToolsActivePort scan", source)
        self.assertIn("launch process exit", source)
        self.assertIn("Chrome child spawned", source)

    def test_default_chrome_is_never_modified(self) -> None:
        source = inspect.getsource(DedicatedChromeProcessResolver) + inspect.getsource(ChromeBrainRuntime)
        self.assertIn("default_chrome_present", source)
        self.assertNotIn("DEFAULT", source)
        self.assertNotIn("kill -9", source.lower())

    def test_diagnostics_never_log_cookie_or_token(self) -> None:
        source = inspect.getsource(_SafeChromeOutputTail)
        self.assertIn("redacted", source)
        self.assertIn("cookie", source.lower())
        self.assertIn("token", source.lower())

    def test_macos_launch_uses_open_new_instance(self) -> None:
        source = inspect.getsource(ChromeBrainRuntime.start)
        self.assertIn('sys.platform == "darwin"', source)
        self.assertIn('"/usr/bin/open", "-n", "-a", "Google Chrome", "--args"', source)

    def test_default_chrome_present_does_not_block_dedicated_instance(self) -> None:
        source = inspect.getsource(ChromeBrainRuntime.start)
        self.assertIn("default_chrome_present", inspect.getsource(DedicatedChromeProcessResolver))
        self.assertIn('self.launcher_type = "MACOS_OPEN_NEW_INSTANCE"', source)

    def test_open_launcher_exit_is_not_process_lost(self) -> None:
        source = inspect.getsource(ChromeBrainRuntime.wait_until_ready)
        self.assertIn("launcher exited; waiting for Dedicated Chrome", source)
        self.assertNotIn("CHROME_PROCESS_LOST", source)

    def test_dedicated_profile_browser_detected_after_open_launcher_exits(self) -> None:
        source = inspect.getsource(ChromeBrainRuntime.wait_until_ready)
        self.assertIn("browser handoff detected", source)
        self.assertIn("ownedBrowserPid", source)

    def test_devtools_active_port_discovered_after_open_new_instance(self) -> None:
        source = inspect.getsource(ChromeBrainRuntime.cdp_failure_code) + inspect.getsource(ChromeBrainRuntime._discover_cdp_endpoint)
        self.assertIn("DevToolsActivePort", inspect.getsource(ChromeBrainRuntime._read_devtools_active_port))
        self.assertIn("CDP_CONNECTION_FAILED", source)

    def test_default_and_dedicated_chrome_can_coexist(self) -> None:
        source = inspect.getsource(DedicatedChromeProcessResolver)
        self.assertIn("default_chrome_present", source)
        self.assertIn("profile_arg", source)

    def test_default_chrome_never_used_as_brain(self) -> None:
        source = inspect.getsource(DedicatedChromeProcessResolver.find_all)
        self.assertIn("profile_arg", source)
        self.assertIn("user-data-dir", source)

    def test_stale_singleton_socket_detected_without_owned_browser(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = ChromeBrainProfileManager(directory)
            profile.create()
            (profile.profile_path / "SingletonSocket").touch()
            inspector = DedicatedProfileSingletonInspector(profile, _FakeProfileResolver([]))
            state = inspector.inspect()
            self.assertTrue(inspector.stale_from(state))

    def test_live_owned_browser_prevents_singleton_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = ChromeBrainProfileManager(directory)
            profile.create()
            (profile.profile_path / "SingletonSocket").touch()
            owned = DedicatedChromeProcess(101, "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", directory, "Google Chrome --user-data-dir=" + directory)
            result = DedicatedProfileSingletonInspector(profile, _FakeProfileResolver([owned])).cleanup_if_stale()
            self.assertFalse(result["stale"])
            self.assertTrue((profile.profile_path / "SingletonSocket").exists())

    def test_reachable_cdp_prevents_singleton_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = ChromeBrainProfileManager(directory)
            profile.create()
            (profile.profile_path / "SingletonSocket").touch()
            (profile.profile_path / "DevToolsActivePort").write_text("9222\n/devtools/browser/test\n", encoding="utf-8")
            resolver = _FakeProfileResolver([])
            result = DedicatedProfileSingletonInspector(profile, resolver).cleanup_if_stale(cdp_reachable=lambda _port: True)
            self.assertFalse(result["stale"])
            self.assertTrue((profile.profile_path / "SingletonSocket").exists())

    def test_only_singleton_runtime_files_are_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = ChromeBrainProfileManager(directory)
            profile.create()
            for name in ("SingletonLock", "SingletonSocket", "SingletonCookie", "Cookies", "Login Data"):
                (profile.profile_path / name).touch()
            result = DedicatedProfileSingletonInspector(profile, _FakeProfileResolver([])).cleanup_if_stale()
            self.assertTrue(any(result["removed"].values()))
            for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
                self.assertFalse((profile.profile_path / name).exists())
            self.assertTrue((profile.profile_path / "Cookies").exists())
            self.assertTrue((profile.profile_path / "Login Data").exists())

    def test_cookie_and_login_storage_never_removed(self) -> None:
        source = inspect.getsource(DedicatedProfileSingletonInspector)
        self.assertIn("RUNTIME_FILES", source)
        self.assertNotIn("Cookies", source)
        self.assertNotIn("Login Data", source)

    def test_singleton_socket_only_state_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = ChromeBrainProfileManager(directory)
            profile.create()
            (profile.profile_path / "SingletonSocket").touch()
            result = DedicatedProfileSingletonInspector(profile, _FakeProfileResolver([])).cleanup_if_stale()
            self.assertTrue(result["removed"]["SingletonSocket"])

    def test_profile_preserved_after_singleton_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = ChromeBrainProfileManager(directory)
            profile.create()
            auth_marker = profile.profile_path / "Cookies"
            auth_marker.write_bytes(b"opaque-auth-db-placeholder")
            (profile.profile_path / "SingletonSocket").touch()
            DedicatedProfileSingletonInspector(profile, _FakeProfileResolver([])).cleanup_if_stale()
            self.assertTrue(auth_marker.exists())
            self.assertEqual(auth_marker.read_bytes(), b"opaque-auth-db-placeholder")

    def test_dedicated_launch_retried_after_stale_singleton_cleanup(self) -> None:
        source = inspect.getsource(ChromeBrainRuntime.start)
        self.assertIn("_inspect_and_cleanup_stale_singleton", source)
        self.assertIn("MACOS_OPEN_NEW_INSTANCE", source)
        self.assertIn("subprocess.Popen", source)

    def test_default_chrome_never_modified(self) -> None:
        source = inspect.getsource(DedicatedProfileSingletonInspector)
        self.assertNotIn("gracefully_stop", source)
        self.assertNotIn("SIGTERM", source)

    def test_launcher_exit_zero_not_browser_exit(self) -> None:
        source = inspect.getsource(ChromeBrainRuntime.cdp_failure_code) + inspect.getsource(ChromeBrainRuntime.wait_until_ready)
        self.assertIn("chrome_exit_code not in (None, 0)", source)
        self.assertIn("launcher exited; waiting for Dedicated Chrome", source)


class _FakeLocator:
    def __init__(self, count: int, *, editable: bool = True) -> None:
        self._count = count
        self._editable = editable

    def count(self) -> int:
        return self._count

    def nth(self, _index: int) -> "_FakeLocator":
        return self

    def is_visible(self) -> bool:
        return True

    def is_editable(self) -> bool:
        return self._editable


class _FakeTextLocator:
    def __init__(self, count: int) -> None:
        self._count = count

    def count(self) -> int:
        return self._count


class _FakePage:
    def __init__(self, url: str, *, composer: bool = False, active: bool = False, ready_state: str = "complete", contenteditable: bool = False) -> None:
        self.url = url
        self.composer = composer
        self.active = active
        self.ready_state = ready_state
        self.contenteditable = contenteditable

    def evaluate(self, expression: str) -> object:
        if "readyState" in expression:
            return self.ready_state
        if "visibilityState" in expression:
            return self.active
        return None

    def title(self) -> str:
        return "ChatGPT"

    def locator(self, selector: str) -> _FakeLocator:
        if selector in ChatGPTChromeDomAdapter.COMPOSER_SELECTORS:
            matches = self.composer and (self.contenteditable or "contenteditable" not in selector or selector == '[data-testid="prompt-textarea"]' or selector == '#prompt-textarea' or selector == 'textarea[placeholder]')
            return _FakeLocator(int(matches))
        if selector in ChatGPTChromeDomAdapter.APP_SHELL_SELECTORS:
            return _FakeLocator(1)
        return _FakeLocator(0)

    def get_by_text(self, *_args: object, **_kwargs: object) -> _FakeTextLocator:
        return _FakeTextLocator(0)


class _FakeProfileResolver:
    def __init__(self, processes: list[DedicatedChromeProcess]) -> None:
        self.processes = processes

    def find_all(self) -> list[DedicatedChromeProcess]:
        return list(self.processes)


if __name__ == "__main__":
    unittest.main()
