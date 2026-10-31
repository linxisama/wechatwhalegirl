"""HTTP 层自检：真的起一个 uvicorn，用真实 HTTP 往返打一遍控制台要用的接口。

用法：.venv-wechat\\Scripts\\python.exe tools\\selfcheck_http.py

为什么需要它：`tools/selfcheck_backend.py` 是直接调用路由函数（不经过 HTTP），
而控制台的每次调用都要先过 `/api/runtime` 身份校验与"仅本机访问"中间件 ——
这两处只有真的发 HTTP 才能验证。

用**模拟微信后端**跑，不会接触真实微信；端口用 8092，不占用控制台的 8091。
"""
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PORT = 8092


def main() -> int:
    from src.runtime_identity import APP_ID, APP_VERSION, PORT as CONSOLE_PORT
    from src.wechat_app import WeChatRuntime, create_app

    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"{'通过' if ok else '失败'}  {name}{'' if ok else '  ← ' + detail}")
        if not ok:
            failures.append(name)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "styles").mkdir()
        for style in (ROOT / "styles").glob("*.yaml"):
            (root / "styles" / style.name).write_text(style.read_text(encoding="utf-8"),
                                                      encoding="utf-8")
        config = root / "config.wechat.yaml"
        config.write_text(
            "ai:\n  api_url: https://api.deepseek.com/v1/chat/completions\n"
            "  api_key: http-check\n  model: deepseek-chat\n"
            "wechat_only:\n  backend: mock\nwechat:\n  backend: mock\n  send_interval: 0\n",
            encoding="utf-8")
        runtime = WeChatRuntime(root=root, config_path=config, db_path=root / "messages.sqlite")
        app = create_app(runtime)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT,
                                               log_level="warning", log_config=None))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        for _ in range(50):
            time.sleep(0.1)
            if server.started:
                break
        check("uvicorn 已启动", bool(server.started))

        base = f"http://127.0.0.1:{PORT}"
        try:
            # 打的是本机回环地址，绝不能走系统代理：机器挂着全局代理
            # （http_proxy/https_proxy 环境变量，常见 127.0.0.1:xxxx）时，
            # httpx 默认 trust_env=True 会把本机请求也转给代理 → 假 404。
            with httpx.Client(timeout=10, trust_env=False) as client:
                identity = client.get(f"{base}/api/runtime").json()
                check("GET /api/runtime 身份",
                      identity.get("app") == APP_ID and identity.get("version") == APP_VERSION,
                      json.dumps(identity, ensure_ascii=False))
                check("GET /api/runtime 端口", identity.get("port") == CONSOLE_PORT,
                      str(identity.get("port")))

                settings = client.get(f"{base}/api/settings").json()
                check("GET /api/settings 字段齐全",
                      set(settings) >= {"backend", "db_dir", "groups", "listen_private",
                                        "api_url", "api_key_set", "model", "api_style"},
                      json.dumps(settings, ensure_ascii=False))

                status = client.get(f"{base}/api/status").json()
                check("GET /api/status 结构",
                      "connection" in status and "messages" in status and "events" in status)

                plugins = client.get(f"{base}/api/plugins").json()
                check("GET /api/plugins 数量", isinstance(plugins, list) and len(plugins) >= 25,
                      str(len(plugins) if isinstance(plugins, list) else plugins))

                toggle = client.post(f"{base}/api/plugins/keyword_reply",
                                     json={"enabled": True}).json()
                check("POST /api/plugins/{id} 开关", toggle.get("success") is True,
                      json.dumps(toggle, ensure_ascii=False))

                bad = client.post(f"{base}/api/plugins/keyword_reply", json={"nope": 1})
                check("POST 非法字段返回 400 且带 detail",
                      bad.status_code == 400 and "detail" in bad.json(), str(bad.status_code))

                personas = client.get(f"{base}/api/personas").json()
                check("GET /api/personas", bool(personas.get("personas")),
                      json.dumps(personas, ensure_ascii=False)[:200])

                started = client.post(f"{base}/api/start", json={}).json()
                check("POST /api/start（模拟后端）", started.get("success") is True,
                      json.dumps(started, ensure_ascii=False))

                probe = client.post(f"{base}/api/test-wechat", json={}).json()
                check("POST /api/test-wechat", probe.get("success") is True,
                      json.dumps(probe, ensure_ascii=False))

                stopped = client.post(f"{base}/api/stop", json={}).json()
                check("POST /api/stop", stopped.get("success") is True,
                      json.dumps(stopped, ensure_ascii=False))

                page = client.get(f"{base}/")
                check("GET / 返回微信控制台页面",
                      page.status_code == 200 and "微信控制台" in page.text, str(page.status_code))
                script = client.get(f"{base}/wechat.js")
                check("GET /wechat.js", script.status_code == 200 and "wechat" in script.text)

                groups = client.get(f"{base}/api/wechat/groups").json()
                check("GET /api/wechat/groups（模拟后端返回空列表）",
                      groups.get("success") is True and isinstance(groups.get("groups"), list),
                      json.dumps(groups, ensure_ascii=False))

                # 非本机来源必须被拦住：用局域网 IP 访问本机端口会被中间件判为非本机
                blocked = client.get(f"{base}/api/status",
                                     headers={"X-Forwarded-For": "10.0.0.1"})
                check("带 X-Forwarded-For 的请求被拒绝", blocked.status_code == 403,
                      str(blocked.status_code))
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            import asyncio

            asyncio.run(runtime.close())

    print()
    if failures:
        print("HTTP 自检失败项：" + "、".join(failures))
        return 1
    print("HTTP 自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
