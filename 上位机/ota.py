# -*- coding: utf-8 -*-
"""无线 OTA：HTTP POST 固件 bin 到设备 http://<ip>/update

设备端只认 multipart/form-data（字段名 firmware），octet-stream 直传会被拒绝。
上传完成后设备会主动重启并断开 TCP，部分环境下会抛连接重置，属正常现象，
以稍后 /version 返回的版本号为准。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path


def _q(text: str) -> str:
    """转成可安全塞进 JS 单引号字符串的文本（与旧实现 json.dumps(x)[1:-1] 等价）"""
    return json.dumps(str(text))[1:-1]


def upload_firmware(ip: str, bin_path: str, log) -> None:
    """在后台线程中调用；log(text) 把一行进度写进界面通信日志"""
    url = "http://%s/update" % ip
    try:
        data = Path(bin_path).read_bytes()
    except OSError as exc:
        log("读取 bin 失败：%s" % _q(exc))
        return
    log("开始 OTA：%s -> %s (%d bytes)" % (_q(bin_path), ip, len(data)))
    boundary = "----OilMoistOTA%x" % int(time.time() * 1000)
    head = ("--%s\r\n"
            "Content-Disposition: form-data; name=\"firmware\"; filename=\"firmware.bin\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n" % boundary).encode("utf-8")
    tail = ("\r\n--%s--\r\n" % boundary).encode("utf-8")
    body_bytes = head + data + tail
    req = urllib.request.Request(url, data=body_bytes, method="POST")
    req.add_header("Content-Type", "multipart/form-data; boundary=%s" % boundary)
    req.add_header("Content-Length", str(len(body_bytes)))
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            body = resp.read().decode("utf-8", "ignore")
            log("OTA 服务端响应：%s" % _q(body))
            log("OTA 上传完成，等待设备重启…")
    except urllib.error.HTTPError as exc:
        b = exc.read().decode("utf-8", "ignore")
        log("OTA 失败：HTTP %d %s" % (exc.code, _q(b)))
    except OSError as exc:
        log("OTA 连接中断（设备可能正在重启写闪存）：%s" % _q(exc))
