"""共享的 keep-alive JSON POST 连接池。

从 `src/generate.py` 抽出来的：那层生成代码已随生成层一起移除，但 `judge_jev`
还要用它发判断与排序请求，所以传输层单独成模块，不跟着生成层陪葬。

urllib.urlopen 每次请求都要新建 DNS+TCP+TLS（一条连接 ~0.1–0.3 s 白付掉），
而判断一次至少发两次（判断 + 排序）。这里按 (scheme, host, port) 复用
http.client 连接，空闲表有锁；一条连接同一时刻只属于一个请求，所以并发调用
天然各拿各的连接。
"""

from __future__ import annotations

import http.client
import io
import json
import threading
import urllib.error
import urllib.parse


class _KeepAlivePool:
    """std 库 keep-alive 连接池：按 (scheme, host, port) 复用 http.client 连接。

    从池里取出的连接可能是服务端已悄悄关掉的（keep-alive 超时），因此网络类异常
    换新连接重试一次——与 urllib3 的做法一致。HTTP >= 300 不重试，按调用方依赖的
    urllib.error.HTTPError 形状抛出（e.read() 仍能拿到错误正文）。不跟随重定向：
    LLM 端点不会 30x，真遇到就以 HTTPError 形式可见，而不是静默 GET 掉。
    """

    def __init__(self, max_idle: int = 4):
        self._lock = threading.Lock()
        self._idle: dict[tuple, list] = {}
        self._max_idle = max_idle

    def _checkout(self, scheme, host, port, timeout):
        key = (scheme, host, port)
        with self._lock:
            idle = self._idle.get(key)
            if idle:
                return key, idle.pop()
        cls = (http.client.HTTPSConnection if scheme == "https"
               else http.client.HTTPConnection)
        return key, cls(host, port, timeout=timeout)

    def _checkin(self, key, conn):
        with self._lock:
            idle = self._idle.setdefault(key, [])
            if len(idle) < self._max_idle:
                idle.append(conn)
                return
        conn.close()

    def post_json(self, url: str, headers: dict, body: dict, timeout: float) -> dict:
        p = urllib.parse.urlparse(url)
        scheme = p.scheme or "https"
        port = p.port or (443 if scheme == "https" else 80)
        path = p.path + (("?" + p.query) if p.query else "")
        payload = json.dumps(body).encode()
        last_exc: Exception | None = None
        for _attempt in range(2):
            key, conn = self._checkout(scheme, p.hostname, port, timeout)
            try:
                conn.request("POST", path, body=payload, headers=headers)
                resp = conn.getresponse()
                data = resp.read()
            except (http.client.HTTPException, OSError) as e:
                conn.close()
                last_exc = e
                continue
            if resp.will_close:
                conn.close()
            else:
                self._checkin(key, conn)
            if resp.status >= 300:
                raise urllib.error.HTTPError(
                    url, resp.status, resp.reason, resp.headers, io.BytesIO(data))
            return json.loads(data)
        assert last_exc is not None
        raise last_exc


_POOL = _KeepAlivePool()


def http_post_json(url: str, headers: dict, body: dict, timeout: float) -> dict:
    """模块级 POST 入口：同一进程内所有调用共用上面这一个连接池。"""
    return _POOL.post_json(url, headers, body, timeout)
