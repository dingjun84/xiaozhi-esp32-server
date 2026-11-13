#!/usr/bin/env python3
"""模拟数字人页面的 OTA 请求；仅使用 Python 标准库。"""

import argparse
import json
import ssl
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import ProxyHandler, Request, build_opener, getproxies, urlopen


def redact(value):
    if isinstance(value, dict):
        return {
            key: "***" if any(word in key.lower() for word in
                              ("token", "password", "authorization", "secret"))
            else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str) and value.startswith(("http://", "https://", "ws://", "wss://")):
        parts = urlsplit(value)
        query = urlencode([(key, "***") for key, _ in parse_qsl(parts.query)])
        netloc = parts.netloc.rsplit("@", 1)[-1]
        return urlunsplit((parts.scheme, netloc, parts.path, query, ""))
    return value


def request(url, method, headers, timeout, body=None, no_proxy=False):
    start = time.monotonic()
    req = Request(url, data=body, headers=headers, method=method)
    try:
        if no_proxy:
            response = build_opener(ProxyHandler({})).open(req, timeout=timeout)
        else:
            response = urlopen(req, timeout=timeout)
    except HTTPError as exc:
        response = exc
    with response:
        raw = response.read(1024 * 1024).decode("utf-8", errors="replace")
        print(f"\n{method}: HTTP {response.status} ({time.monotonic() - start:.2f}s)")
        print("最终地址:", redact(response.geturl()))
        for name in ("Content-Type", "Location", "Access-Control-Allow-Origin",
                     "Access-Control-Allow-Methods", "Access-Control-Allow-Headers"):
            if response.headers.get(name):
                print(f"{name}: {redact(response.headers[name])}")
        return response.status, response.headers, raw


def explain_connection_error(exc):
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, ssl.SSLCertVerificationError):
        print("TLS 证书校验失败：检查证书域名、有效期、完整证书链及 Python 的 CA 信任配置。")
    elif isinstance(reason, ssl.SSLError) or "SSL" in str(reason):
        print("TLS 连接异常中断，尚未取得有效 HTTP 响应；不能据此认定 OTA 路径错误。")
        print("先对比默认请求和 --no-proxy 请求；直连仍失败时，检查 Nginx 443 ssl、SNI、TLS 协议配置及错误日志。")
        print("--no-proxy 仅禁用 urllib 的显式代理，无法绕过 VPN/TUN 或透明代理。")
    else:
        print("请检查 DNS、网络、代理、目标端口以及服务是否启动。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", nargs="?", default="https://agent.fangbaobaochina.com/xiaozhi/ota/")
    parser.add_argument("--device-id", default="02:00:00:00:00:01",
                        help="建议填写页面设置里的设备 MAC，以复现绑定/认证问题")
    parser.add_argument("--client-id", default="web_test_client")
    parser.add_argument("--device-name", default="Web测试设备")
    parser.add_argument("--origin", default="https://agent.fangbaobaochina.com")
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--no-proxy", action="store_true",
                        help="禁用 urllib 自动读取的环境变量/系统代理，保留 TLS 证书校验")
    args = parser.parse_args()
    try:
        parts = urlsplit(args.url)
        valid_url = parts.scheme in ("http", "https") and parts.hostname and parts.port != 0
    except ValueError:
        valid_url = False
    if not valid_url or args.timeout <= 0:
        parser.error("请提供有效的 HTTP/HTTPS OTA 地址和大于 0 的超时时间")

    print("OTA 地址:", redact(args.url))
    print("页面来源:", args.origin)
    print("设备 MAC:", args.device_id)
    print("TLS 库:", ssl.OPENSSL_VERSION)
    if args.no_proxy:
        print("代理模式: 禁用 urllib 显式代理（VPN/TUN 仍可能生效）")
    else:
        proxy = getproxies().get(parts.scheme)
        if proxy:
            try:
                proxy_parts = urlsplit(proxy if "://" in proxy else "http://" + proxy)
                print("自动检测到代理:", proxy_parts.hostname, proxy_parts.port,
                      "（若命中 no_proxy 则会绕过）")
            except ValueError:
                print("自动检测到代理，但地址格式异常；可使用 --no-proxy 对比。")
        else:
            print("代理模式: 未检测到当前协议的显式代理")
    print("提示：POST 可能触发服务端设备注册或激活流程。")
    if parts.port == 8006 or parts.path.endswith("index.html"):
        print("提示：该地址看起来是页面服务；默认 OTA 服务使用 8003/xiaozhi/ota/。")

    cors_ok = False
    try:
        status, headers, _ = request(args.url, "OPTIONS", {
            "Origin": args.origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,device-id,client-id",
        }, args.timeout, no_proxy=args.no_proxy)
        methods = {v.strip().upper() for v in headers.get("Access-Control-Allow-Methods", "").split(",")}
        allowed = {v.strip().lower() for v in headers.get("Access-Control-Allow-Headers", "").split(",")}
        cors_ok = (200 <= status < 300
                   and headers.get("Access-Control-Allow-Origin") in ("*", args.origin)
                   and bool({"POST", "*"} & methods)
                   and ("*" in allowed or {"content-type", "device-id", "client-id"} <= allowed))
        print("跨域预检:", "通过" if cors_ok else "未通过；浏览器可能阻止 OTA 请求")
    except (OSError, URLError, ValueError) as exc:
        print("跨域预检失败:", exc)

    payload = {
        "version": 0, "uuid": "",
        "application": {
            "name": "xiaozhi-web-test", "version": "1.0.0",
            "compile_time": "2025-04-16 10:00:00", "idf_version": "4.4.3",
            "elf_sha256": "1234567890abcdef1234567890abcdef1234567890abcdef",
        },
        "ota": {"label": "xiaozhi-web-test"},
        "board": {"type": args.device_name, "ssid": "xiaozhi-web-test",
                  "rssi": 0, "channel": 0, "ip": "192.168.1.1", "mac": args.device_id},
        "flash_size": 0, "minimum_free_heap_size": 0,
        "mac_address": args.device_id, "chip_model_name": "",
        "chip_info": {"model": 0, "cores": 0, "revision": 0, "features": 0},
        "partition_table": [{"label": "", "type": 0, "subtype": 0, "address": 0, "size": 0}],
    }
    try:
        status, headers, raw = request(args.url, "POST", {
            "Content-Type": "application/json", "Device-Id": args.device_id,
            "Client-Id": args.client_id, "Origin": args.origin,
        }, args.timeout, json.dumps(payload).encode("utf-8"), no_proxy=args.no_proxy)
    except (OSError, URLError, ValueError) as exc:
        print(f"\n连接失败: {exc}")
        explain_connection_error(exc)
        return 1

    try:
        data = json.loads(raw)
    except ValueError:
        print("响应不是 JSON，正文前 2000 字符：\n" + raw[:2000])
        print("可能请求到了页面、登录入口或代理错误页。")
        return 1
    print("响应 JSON（凭据已隐藏）:")
    print(json.dumps(redact(data), ensure_ascii=False, indent=2))
    if not 200 <= status < 300:
        print("OTA HTTP 请求失败；请根据状态码和响应正文检查路径、权限及服务端日志。")
        return 1
    websocket = data.get("websocket") if isinstance(data, dict) else None
    ws_url = websocket.get("url") if isinstance(websocket, dict) else None
    try:
        ws_parts = urlsplit(ws_url) if isinstance(ws_url, str) else None
        valid_ws = ws_parts and ws_parts.scheme in ("ws", "wss") and ws_parts.hostname
    except ValueError:
        valid_ws = False
    if not valid_ws:
        print("失败：响应缺少有效的 websocket.url，数字人无法继续连接。")
        return 1
    print("\nOTA 已返回 WebSocket 地址:", redact(ws_url))
    cors_ok = cors_ok and headers.get("Access-Control-Allow-Origin") in ("*", args.origin)
    if not cors_ok:
        print("HTTP 请求成功，但跨域检查未通过；Python 可请求成功，浏览器仍可能失败。")
        return 2
    print("OTA 响应和跨域检查通过。尚未测试 WebSocket 握手或语音交互。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
