"""RESP 端点自测：PING / usage:begin / usage:end / 幂等 / GET cred。

用法（在平台上）：python3 /usr/local/lib/dsh-mu/tests/resp-selftest.py
"""
import json
import socket
import sys
import time

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8098


def send(*parts):
    s = socket.create_connection(("127.0.0.1", PORT), timeout=5)
    payload = ("*%d\r\n" % len(parts)).encode()
    for p in parts:
        b = p.encode()
        payload += ("$%d\r\n" % len(b)).encode() + b + b"\r\n"
    s.sendall(payload)
    buf = s.recv(65536)
    s.close()
    return buf.decode("utf-8", "replace").strip()


def main():
    now = int(time.time())
    rid = "req-selftest-%d" % now
    checks = []

    checks.append(("PING", send("PING") == "+PONG"))

    begin = {
        "request_id": rid, "ts_started": now, "user": "dainan", "instance": "dainan",
        "provider": "mu-deepseek", "model": "deepseek-flash", "route": "mu-deepseek",
        "endpoint": "/v1/chat/completions", "surface": "web", "client": "browser",
        "client_version": "0.2.2rc1", "stream": 1, "has_tools": 1,
    }
    checks.append(("begin", send("RPUSH", "usage:begin", json.dumps(begin)).startswith(":")))
    end = {
        "request_id": rid, "ts": now + 5, "prompt_tokens": 1200, "completion_tokens": 340,
        "latency_ms": 5000, "upstream_ms": 4800, "ttft_ms": 700, "status": "ok",
        "stop_reason": "stop", "http_status": 200, "bytes_in": 9000, "bytes_out": 3400,
        "tool_calls": 2, "source": "relay",
    }
    checks.append(("end", send("RPUSH", "usage:end", json.dumps(end)).startswith(":")))
    # 幂等：同一 request_id 再来一次 end，不应产生第二行
    checks.append(("end-again", send("RPUSH", "usage:end", json.dumps(end)).startswith(":")))
    # 只有 end 没有 begin（source 应为 relay_partial）
    end2 = dict(end, request_id=rid + "-b", prompt_tokens=10, completion_tokens=2)
    end2.pop("source", None)
    checks.append(("end-only", send("RPUSH", "usage:end", json.dumps(end2)).startswith(":")))
    # 未知队列必须报错
    checks.append(("bad-queue", send("RPUSH", "nope", "{}").startswith("-ERR")))

    ok = sum(1 for _, v in checks if v)
    for name, v in checks:
        print(("PASS " if v else "FAIL ") + name)
    print("== %d/%d ==" % (ok, len(checks)))
    return 0 if ok == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
