"""Exercise the actual CLI/worker/evaluator path with loopback model response replay."""

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expect", choices=("baseline", "fixed"), required=True)
    args = parser.parse_args()
    checkout = args.checkout.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    source = checkout / "openevolve/utils/code_utils.py"
    records = []

    for name, suffix, newline in (
        ("control-lf", "", "\n"),
        ("space-lf", " ", "\n"),
        ("tab-lf", "\t", "\n"),
        ("space-crlf", " \t", "\r\n"),
    ):
        case = args.output / name
        case.mkdir()
        (case / "initial.py").write_text("def run():\n    return 0\n", encoding="utf-8")
        (case / "evaluate.py").write_text(
            "from pathlib import Path\n\n"
            "def evaluate(program_path):\n"
            "    namespace = {}\n"
            "    code = Path(program_path).read_text(encoding='utf-8')\n"
            "    exec(compile(code, program_path, 'exec'), namespace)\n"
            "    return {'combined_score': float(namespace['run']())}\n",
            encoding="utf-8",
        )
        replay = (
            "```python"
            + suffix
            + newline
            + "def run():"
            + newline
            + "    return 42"
            + newline
            + "```"
        )
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append({"path": self.path, "body": payload})
                body = json.dumps(
                    {
                        "id": "offline-response",
                        "object": "chat.completion",
                        "created": 0,
                        "model": "offline-fence-replay",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": replay},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        config = {
            "max_iterations": 1,
            "checkpoint_interval": 1,
            "random_seed": 7,
            "language": "python",
            "diff_based_evolution": False,
            "llm": {
                "api_base": f"http://127.0.0.1:{server.server_port}/v1",
                "api_key": "offline-replay-key",
                "models": [{"name": "offline-fence-replay", "weight": 1.0}],
                "timeout": 10,
                "retries": 0,
            },
            "database": {"num_islands": 1, "population_size": 10, "archive_size": 5},
            "evaluator": {
                "parallel_evaluations": 1,
                "timeout": 5,
                "cascade_evaluation": False,
                "use_llm_feedback": False,
                "max_retries": 0,
            },
        }
        (case / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
        command = [
            sys.executable,
            "-m",
            "openevolve.cli",
            str(case / "initial.py"),
            str(case / "evaluate.py"),
            "--config",
            str(case / "config.yaml"),
            "--output",
            str(case / "evolution"),
            "--iterations",
            "1",
        ]
        environment = os.environ.copy()
        environment["OPENAI_API_KEY"] = "offline-replay-key"
        environment["PYTHONUTF8"] = "1"
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                cwd=checkout,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=60,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        (case / "stdout.log").write_text(completed.stdout, encoding="utf-8")
        (case / "stderr.log").write_text(completed.stderr, encoding="utf-8")
        (case / "transport.json").write_text(
            json.dumps({"requests": requests, "response": replay}, indent=2), encoding="utf-8"
        )
        info_path = case / "evolution/best/best_program_info.json"
        info = json.loads(info_path.read_text()) if info_path.exists() else {}
        score = info.get("metrics", {}).get("combined_score")
        records.append(
            {
                "case": name,
                "command": command,
                "exitCode": completed.returncode,
                "modelRequests": len(requests),
                "score": score,
                "elapsedSeconds": round(time.monotonic() - started, 3),
                "artifactDirectory": str(case),
            }
        )

    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip()
    report = {
        "python": sys.version,
        "platform": platform.platform(),
        "sourceCommit": commit,
        "sourceSha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "modelBoundary": "loopback HTTP response replay; no live model or paid API",
        "expected": args.expect,
        "cases": records,
    }
    (args.output / "proof.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    assert all(
        case["exitCode"] == 0 and case["modelRequests"] == 1 for case in records
    ), "Actual CLI path did not complete as designed"
    expected_scores = [42.0] * 4 if args.expect == "fixed" else [42.0, 0.0, 0.0, 0.0]
    assert [case["score"] for case in records] == expected_scores


if __name__ == "__main__":
    main()
