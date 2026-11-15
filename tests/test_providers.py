"""Wire-format tests: real HTTP against local fake Anthropic and OpenAI-compatible servers."""
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

import medha
from medha.errors import MedhaError


class FakeAPI:
    """A tiny HTTP server that records requests and answers from a script."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        api = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                api.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                                     "body": body})
                status, payload = api.replies.pop(0)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                if status == 429:
                    self.send_header("retry-after", "0")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def anthropic_text(text, tokens=(10, 5)):
    return 200, {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                 "usage": {"input_tokens": tokens[0], "output_tokens": tokens[1]}}


def openai_text(text):
    return 200, {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 7, "completion_tokens": 3}}


PROGRAM = '''
model m = "%s" %s
type Out { city: string, celsius: float }
/// Current temperature for a city.
fn weather(city: string) -> float { 21.5 }
agent ask(q: string) -> Out using m {
  system "Use the tool."
  tools [weather]
  q
}
let result = ask("Weather in Pune?")
let used = usage()
'''


class AnthropicWire(unittest.TestCase):
    def setUp(self):
        self.saved = dict(os.environ)
        os.environ["ANTHROPIC_API_KEY"] = "test-key"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)

    def run_with(self, replies, options='{ effort: "low", max_tokens: 2048 }'):
        api = FakeAPI(replies)
        os.environ["ANTHROPIC_BASE_URL"] = api.url
        try:
            interp = medha.run(PROGRAM % ("anthropic:claude-opus-5-5", options))
        finally:
            api.close()
        return api, interp.last_env.vars

    def test_tool_loop_and_typed_answer(self):
        thinking = {"type": "thinking", "thinking": "", "signature": "sig"}
        tool_use = {"type": "tool_use", "id": "toolu_1", "name": "weather", "input": {"city": "Pune"}}
        api, env = self.run_with([
            (200, {"content": [thinking, tool_use], "stop_reason": "tool_use",
                   "usage": {"input_tokens": 100, "output_tokens": 20}}),
            anthropic_text('{"city": "Pune", "celsius": 21.5}', (130, 15)),
        ])
        self.assertEqual(env["result"], {"city": "Pune", "celsius": 21.5})
        self.assertEqual(env["used"]["input_tokens"], 230)
        first, second = [r["body"] for r in api.requests]
        head = api.requests[0]["headers"]
        self.assertEqual(api.requests[0]["path"], "/v1/messages")
        self.assertEqual(head["x-api-key"], "test-key")
        self.assertEqual(head["anthropic-version"], "2026-10-21")
        self.assertEqual(first["model"], "claude-opus-5-5")
        self.assertEqual(first["max_tokens"], 2048)
        self.assertEqual(first["output_config"], {"effort": "low"})
        self.assertIn("JSON Schema", first["system"])
        self.assertTrue(first["system"].startswith("Use the tool."))
        tool = first["tools"][0]
        self.assertEqual(tool["name"], "weather")
        self.assertEqual(tool["description"], "Current temperature for a city.")
        self.assertEqual(tool["input_schema"]["properties"], {"city": {"type": "string"}})
        self.assertEqual(tool["input_schema"]["required"], ["city"])
        # The assistant turn is echoed back unchanged, thinking block included.
        self.assertEqual(second["messages"][1], {"role": "assistant", "content": [thinking, tool_use]})
        self.assertEqual(second["messages"][2], {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "21.5"}]})

    def test_sampling_parameters_are_not_sent(self):
        api, _ = self.run_with([anthropic_text('{"city": "Pune", "celsius": 1}')], "{ temperature: 0.2 }")
        body = api.requests[0]["body"]
        self.assertNotIn("temperature", body)
        self.assertEqual(body["max_tokens"], 16000)

    def test_refusal_becomes_an_error(self):
        with self.assertRaises(MedhaError) as ctx:
            self.run_with([(200, {"content": [], "stop_reason": "refusal",
                                  "stop_details": {"type": "refusal", "category": "cyber"},
                                  "usage": {"input_tokens": 1, "output_tokens": 0}})])
        self.assertEqual(ctx.exception.kind, "ModelRefusal")

    def test_rate_limits_are_retried_and_errors_surface(self):
        api, env = self.run_with([(429, {"error": {"message": "slow down"}}),
                                  anthropic_text('{"city": "Pune", "celsius": 3}')])
        self.assertEqual(env["result"]["celsius"], 3.0)
        self.assertEqual(len(api.requests), 2)
        with self.assertRaises(MedhaError) as ctx:
            self.run_with([(400, {"error": {"type": "invalid_request_error", "message": "bad model"}})])
        self.assertEqual(ctx.exception.kind, "ModelError")
        self.assertIn("HTTP 400: bad model", ctx.exception.message)

    def test_missing_key_is_a_clear_error(self):
        del os.environ["ANTHROPIC_API_KEY"]
        with self.assertRaises(MedhaError) as ctx:
            medha.run('model m = "anthropic:claude-opus-5-5"\nm("hi")')
        self.assertIn("ANTHROPIC_API_KEY", ctx.exception.message)


class OpenAIWire(unittest.TestCase):
    def setUp(self):
        self.saved = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)

    def test_tool_loop_and_typed_answer(self):
        call = {"id": "call_1", "type": "function",
                "function": {"name": "weather", "arguments": '{"city": "Pune"}'}}
        api = FakeAPI([
            (200, {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [call]},
                                "finish_reason": "tool_calls"}],
                   "usage": {"prompt_tokens": 50, "completion_tokens": 9}}),
            openai_text('```json\n{"city": "Pune", "celsius": "21.5"}\n```'),
        ])
        os.environ["OPENAI_API_KEY"] = "sk-test"
        os.environ["OPENAI_BASE_URL"] = api.url + "/v1"
        try:
            interp = medha.run(PROGRAM % ("openai:gpt-test", "{ temperature: 0, max_tokens: 500 }"))
        finally:
            api.close()
        env = interp.last_env.vars
        self.assertEqual(env["result"], {"city": "Pune", "celsius": 21.5})
        first, second = [r["body"] for r in api.requests]
        self.assertEqual(api.requests[0]["path"], "/v1/chat/completions")
        self.assertEqual(api.requests[0]["headers"]["authorization"], "Bearer sk-test")
        self.assertEqual(first["temperature"], 0)
        self.assertEqual(first["max_completion_tokens"], 500)
        self.assertEqual(first["messages"][0]["role"], "system")
        self.assertEqual(first["tools"][0]["function"]["name"], "weather")
        self.assertEqual(second["messages"][2]["tool_calls"][0]["function"]["arguments"], '{"city": "Pune"}')
        self.assertEqual(second["messages"][3], {"role": "tool", "tool_call_id": "call_1", "content": "21.5"})

    def test_ollama_needs_no_key_and_embeddings_work(self):
        api = FakeAPI([openai_text("hello"),
                       (200, {"data": [{"index": 1, "embedding": [0, 1]}, {"index": 0, "embedding": [1, 0]}]})])
        os.environ["OLLAMA_BASE_URL"] = api.url + "/v1"
        try:
            interp = medha.run('model m = "ollama:llama3"\nlet a = m("hi")\nlet v = embed(["x", "y"], m)')
        finally:
            api.close()
        env = interp.last_env.vars
        self.assertEqual(env["a"], "hello")
        self.assertEqual(env["v"], [[1, 0], [0, 1]])
        self.assertNotIn("authorization", api.requests[0]["headers"])
        self.assertEqual(api.requests[0]["body"]["model"], "llama3")
        self.assertEqual(api.requests[1]["path"], "/v1/embeddings")


if __name__ == "__main__":
    unittest.main()
