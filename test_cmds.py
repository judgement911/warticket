import asyncio, json, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
sys.path.insert(0, "/home/user/warticket")
import main, httpx

SENT = []
CMDS = ["/status", "/links", "/next", "/ack", "/hot 3", "/cool", "/here", "/bogus"]
served = {"done": False}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if served["done"]:
            body = {"ok": True, "result": []}
        else:
            served["done"] = True
            body = {"ok": True, "result": [
                {"update_id": i, "message": {"text": c, "chat": {"id": 999, "type": "private"}}}
                for i, c in enumerate(CMDS)]}
        raw = json.dumps(body).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw))); self.end_headers()
        self.wfile.write(raw)
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        SENT.append(json.loads(self.rfile.read(n))["text"])
        raw = b'{"ok":true}'
        self.send_response(200); self.send_header("Content-Length", str(len(raw)))
        self.end_headers(); self.wfile.write(raw)

srv = HTTPServer(("127.0.0.1", 8796), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()

main.BOT_TOKEN = "stub"
main.DRY_RUN = False
main.CHAT_IDS = ["999"]
main.TG = "http://127.0.0.1:8796/botstub"

config = json.load(open("/home/user/warticket/config.json"))
state = {
    "targets": {config["targets"][0]["name"]: {
        "last_check": main.now() - 4, "latency_ms": 132, "fails": 0,
        "rules": {"0": {"links": ["https://www.tiket.com/queue/live-abc"],
                        "link_baseline": True}},
    }},
    "pending_ack": {"name": "x", "url": "u", "left": 5, "every": 30, "next": 0},
}

async def go():
    async with httpx.AsyncClient() as c:
        try:
            await asyncio.wait_for(main.poll_commands(c, config, state), timeout=6)
        except asyncio.TimeoutError:
            pass

asyncio.run(go())
srv.shutdown()

print(f"replies captured: {len(SENT)} / commands sent: {len(CMDS)}")
for t in SENT:
    print("-" * 60); print(t)
print("=" * 60)
assert len(SENT) >= len(CMDS), f"MISSING REPLIES: {len(SENT)} < {len(CMDS)}"
assert state["pending_ack"] is None, "/ack did not clear pending_ack"
assert any("tiket.com/queue/live-abc" in t for t in SENT), "/links did not list the link"
assert any("132ms" in t for t in SENT), "/status did not show latency"
assert any("upcoming drops" in t for t in SENT), "/next produced nothing"
assert any("/ack" in t and "/links" in t for t in SENT), "help text not updated"
print("ALL COMMAND ASSERTIONS PASSED")
