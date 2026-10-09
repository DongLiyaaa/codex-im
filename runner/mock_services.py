"""Local stand-ins for the Anthropic Messages API and an MCP server; used only by the real-CLI test."""
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Recorder:
    def __init__(self):
        self.api: list[dict] = []
        self.mcp: list[dict] = []


def serve(handler_class):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def anthropic(recorder: Recorder):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def reply(self, content_type, body, code=200):
            self.send_response(code)
            self.send_header("content-type", content_type)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.reply("application/json", json.dumps({"data": [{"id": "claude-mock"}]}).encode())

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
            recorder.api.append({"path": self.path, "key": self.headers.get("x-api-key"), "request": request})
            tools = [t["name"] for t in request.get("tools", [])]
            messages = request.get("messages", [])
            text_of = json.dumps(messages)
            answered = any(isinstance(m.get("content"), list) and any(b.get("type") == "tool_result" for b in m["content"])
                           for m in messages)
            events = []

            def event(kind, data):
                events.append(f"event: {kind}\ndata: {json.dumps(data)}\n\n")
            event("message_start", {"type": "message_start", "message": {
                "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-mock", "content": [],
                "stop_reason": None, "usage": {"input_tokens": 5, "output_tokens": 0}}})
            mcp_tools = [t for t in tools if t.startswith("mcp__")]
            wanted = re.search(r"USE_TOOL:(\w+)", text_of)
            chosen = next((t for t in mcp_tools if wanted and t.endswith("__" + wanted.group(1))), None)
            if chosen and not answered:
                event("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {
                    "type": "tool_use", "id": "toolu_1", "name": chosen, "input": {}}})
                event("content_block_delta", {"type": "content_block_delta", "index": 0,
                                              "delta": {"type": "input_json_delta", "partial_json": "{}"}})
                event("content_block_stop", {"type": "content_block_stop", "index": 0})
                event("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
                                        "usage": {"output_tokens": 5}})
            else:
                answer = "MOCK-ANSWER tools=" + (",".join(tools) or "none") + (" TOOLRESULT" if answered else "")
                event("content_block_start", {"type": "content_block_start", "index": 0,
                                              "content_block": {"type": "text", "text": ""}})
                event("content_block_delta", {"type": "content_block_delta", "index": 0,
                                              "delta": {"type": "text_delta", "text": answer}})
                event("content_block_stop", {"type": "content_block_stop", "index": 0})
                event("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                                        "usage": {"output_tokens": 5}})
            event("message_stop", {"type": "message_stop"})
            self.reply("text/event-stream", "".join(events).encode())
    return Handler


def mcp_server(recorder: Recorder):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(405)
            self.send_header("content-length", "0")
            self.end_headers()

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
            recorder.mcp.append({"path": self.path, "authorization": self.headers.get("authorization"),
                                 "method": request.get("method")})
            method, ident = request.get("method"), request.get("id")
            result = None
            if method == "initialize":
                result = {"protocolVersion": request["params"]["protocolVersion"], "capabilities": {"tools": {}},
                          "serverInfo": {"name": "mock", "version": "1"}}
            elif method == "tools/list":
                result = {"tools": [{"name": "read_document", "description": "d",
                                     "inputSchema": {"type": "object", "properties": {}}},
                                    {"name": "not_granted", "description": "d",
                                     "inputSchema": {"type": "object", "properties": {}}}]}
            elif method == "tools/call":
                result = {"content": [{"type": "text", "text": "pong"}]}
            if result is None:
                self.send_response(202)
                self.send_header("content-length", "0")
                self.end_headers()
                return
            body = json.dumps({"jsonrpc": "2.0", "id": ident, "result": result}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    return Handler
