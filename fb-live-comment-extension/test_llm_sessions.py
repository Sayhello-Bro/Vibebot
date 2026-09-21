import ast
import tempfile
import threading
import unittest
from pathlib import Path


SOURCE = Path(__file__).with_name("launcher.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def isolated_function(name, namespace):
    function = next(
        node for node in TREE.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )
    module = ast.Module(body=[function], type_ignores=[])
    exec(compile(module, "launcher.py", "exec"), namespace)
    return namespace[name]


class LlmSessionRoutingTests(unittest.TestCase):
    def test_stopping_during_startup_cleans_up_that_live_row(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cancelled = [False]
            cleaned = []
            namespace = {
                "SESSION_TEXT_DIR": root / "sessions",
                "REPLY_DIR": root / "replies",
                "PROFILES": {"C": "Profile 2"},
                "PROFILE_ACCOUNTS": {"C": "fb_account_003"},
                "llm_processes": {},
                "process_running": lambda proc: False,
                "set_latest_reply": lambda *args: None,
                "ensure_llm_ready": lambda **kwargs: None,
                "refresh_reply_list": lambda: None,
                "build_stream_file": lambda *args: (root / "sessions" / "stream.jsonl", "stream"),
                "start_stt": lambda *args: "stt",
                "create_llm_session": lambda *args: None,
                "with_extension_config": lambda *args: "configured_url",
                "llm_port_for_profile": lambda *args: 5012,
                "open_chrome": lambda *args: cancelled.__setitem__(0, True) or "browser",
                "close_browser_resources": lambda resources: cleaned.append(("browser", resources)),
                "terminate_processes": lambda resources: cleaned.append(("stt", resources)),
                "stop_llm_targets": lambda targets: cleaned.append(("llm", list(targets))),
            }
            pipeline = isolated_function("start_pipeline", namespace)
            with self.assertRaises(InterruptedError):
                pipeline(
                    [{"profile_name": "C", "url": "https://facebook.com/one", "stream_index": 1}],
                    cancelled=lambda: cancelled[0],
                )
            self.assertEqual(cleaned, [
                ("browser", ["browser"]), ("stt", ["stt"]), ("llm", [("C", 0)])
            ])

    def test_pipeline_keeps_urls_separate_and_accounts_on_one_url_together(self):
        profiles = {"C": "Profile 2", "D": "Profile 3"}
        port_for = isolated_function("llm_port_for_profile", {
            "MAX_URL_FIELDS": 5,
            "PROFILES": profiles,
            "profile_number": lambda profile: int(profile.split()[-1]),
        })

        def run(tasks):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                stt_calls, llm_calls, chrome_calls, ready_calls = [], [], [], []
                namespace = {
                    "SESSION_TEXT_DIR": root / "sessions",
                    "REPLY_DIR": root / "replies",
                    "PROFILES": profiles,
                    "PROFILE_ACCOUNTS": {"C": "fb_account_003", "D": "fb_account_004"},
                    "llm_processes": {},
                    "process_running": lambda proc: False,
                    "set_latest_reply": lambda *args: None,
                    "ensure_llm_ready": lambda **kwargs: ready_calls.append(kwargs["targets"]),
                    "refresh_reply_list": lambda: None,
                    "build_stream_file": lambda profile, url, row, channel: (
                        root / "sessions" / f"{profile}_live_{row:02d}.jsonl", f"{profile}_live_{row:02d}"
                    ),
                    "start_stt": lambda url, profile, file, stream: stt_calls.append((url, file)) or object(),
                    "create_llm_session": lambda profile, file, row: llm_calls.append((profile, file, row)),
                    "with_extension_config": lambda url, account, file, port: (url, account, file, port),
                    "llm_port_for_profile": port_for,
                    "open_chrome": lambda config, profile: chrome_calls.append((config, profile)) or object(),
                }
                pipeline = isolated_function("start_pipeline", namespace)
                _, _, targets = pipeline(tasks)
                return targets, stt_calls, llm_calls, chrome_calls, ready_calls

        one_to_many = run([
            {"profile_name": "C", "url": "https://facebook.com/one", "stream_index": 1},
            {"profile_name": "C", "url": "https://facebook.com/two", "stream_index": 2},
        ])
        self.assertEqual(one_to_many[0], [("C", 0), ("C", 1)])
        self.assertEqual(len(one_to_many[1]), 2)
        self.assertNotEqual(one_to_many[2][0][1], one_to_many[2][1][1])
        self.assertEqual([call[0][3] for call in one_to_many[3]], [5012, 5013])

        many_to_one = run([
            {"profile_name": "C", "url": "https://facebook.com/one", "stream_index": 1},
            {"profile_name": "D", "url": "https://facebook.com/one", "stream_index": 1},
        ])
        self.assertEqual(many_to_one[0], [("C", 0), ("D", 0)])
        self.assertEqual(len(many_to_one[1]), 1)
        self.assertEqual(many_to_one[2][0][1], many_to_one[2][1][1])
        self.assertEqual([call[0][3] for call in many_to_one[3]], [5012, 5017])

    def test_every_account_and_live_row_gets_its_own_port(self):
        profiles = {"C": "Profile 2", "D": "Profile 3"}
        port_for = isolated_function("llm_port_for_profile", {
            "MAX_URL_FIELDS": 5,
            "PROFILES": profiles,
            "profile_number": lambda profile: int(profile.split()[-1]),
        })
        ports = {port_for(name, row) for name in profiles for row in range(5)}
        self.assertEqual(len(ports), 10)
        self.assertEqual(port_for("C", 0), 5012)
        self.assertEqual(port_for("C", 1), 5013)
        self.assertEqual(port_for("D", 0), 5017)

    def test_stopping_one_row_keeps_other_llm_terminals(self):
        c_first, c_second, d_first = object(), object(), object()
        active = {("C", 0): c_first, ("C", 1): c_second, ("D", 0): d_first}
        processes = list(active.values())
        stopped = []
        stop = isolated_function("stop_llm_targets", {
            "service_start_lock": threading.RLock(),
            "llm_processes": active,
            "processes": processes,
            "terminate_process_tree": stopped.append,
        })

        stop([("C", 1)])

        self.assertEqual(stopped, [c_second])
        self.assertEqual(set(active), {("C", 0), ("D", 0)})
        self.assertEqual(processes, [c_first, d_first])


if __name__ == "__main__":
    unittest.main()
