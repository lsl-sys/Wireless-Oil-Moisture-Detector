# -*- coding: utf-8 -*-
"""脚本化配网：电脑连板子热点 OIL-CFG-XXXX → POST WiFi 凭据 → 板子重启连网

用法：
    D:\\anaconda3\\python.exe provision_wifi.py "我是哈基桐_5G" "00000000"

流程：
  1) netsh 扫描并连上板子热点（密码 oil12345，自动建 profile）
  2) POST http://192.168.4.1/save 提交 ssid/pass
  3) 板子保存后自动重启；电脑切回原来的 WiFi
"""
from __future__ import annotations

import subprocess
import sys
import time
import urllib.parse
import urllib.request

AP_PASS = "oil12345"
AP_IP = "192.168.4.1"

PROFILE_XML = """<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
  <name>{ssid}</name>
  <SSIDConfig><SSID><name>{ssid}</name></SSID></SSIDConfig>
  <connectionType>ESS</connectionType>
  <connectionMode>auto</connectionMode>
  <MSM><security>
    <authEncryption>
      <authentication>WPA2PSK</authentication>
      <encryption>AES</encryption>
      <useOneX>false</useOneX>
    </authEncryption>
    <sharedKey>
      <keyType>passPhrase</keyType>
      <protected>false</protected>
      <keyMaterial>{pwd}</keyMaterial>
    </sharedKey>
  </security></MSM>
</WLANProfile>
"""


def sh(cmd: str) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", shell=True)
    return (r.stdout or "") + (r.stderr or "")


def current_ssid() -> str:
    out = sh("netsh wlan show interfaces")
    for line in out.splitlines():
        if " SSID" in line and "BSSID" not in line:
            return line.split(":", 1)[1].strip()
    return ""


def main() -> int:
    target_ssid = sys.argv[1] if len(sys.argv) > 1 else "我是哈基桐_5G"
    target_pass = sys.argv[2] if len(sys.argv) > 2 else "00000000"

    home_ssid = current_ssid()
    print("[1/5] 当前 WiFi：%s" % (home_ssid or "(未连接)"))

    # 找板子热点
    print("[2/5] 扫描板子热点 OIL-CFG-* ...")
    ap_ssid = None
    for _ in range(10):
        out = sh("netsh wlan show networks")
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("SSID") and "OIL-CFG-" in line:
                ap_ssid = line.split(":", 1)[1].strip()
                break
        if ap_ssid:
            break
        time.sleep(2)
    if not ap_ssid:
        print("未找到 OIL-CFG-* 热点（板子不在配网模式？）")
        return 1
    print("      找到热点：%s" % ap_ssid)

    # 建 profile 并连接
    with open("_oil_ap_profile.xml", "w", encoding="utf-8") as f:
        f.write(PROFILE_XML.format(ssid=ap_ssid, pwd=AP_PASS))
    print(sh('netsh wlan add profile filename="_oil_ap_profile.xml" user=current').strip())
    print("[3/5] 连接 %s ..." % ap_ssid)
    print(sh('netsh wlan connect name="%s"' % ap_ssid).strip())
    for _ in range(20):
        if current_ssid() == ap_ssid:
            break
        time.sleep(1)
    if current_ssid() != ap_ssid:
        print("连接热点失败")
        return 1
    print("      已连接")

    # POST 凭据
    print("[4/5] 提交 WiFi 凭据 ssid=%s ..." % target_ssid)
    body = urllib.parse.urlencode({"ssid": target_ssid, "pass": target_pass}).encode()
    try:
        req = urllib.request.Request("http://%s/save" % AP_IP, data=body, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            print("      HTTP %d，板子保存中并重启" % resp.status)
    except Exception as e:
        # 板子重启可能直接断连导致读响应失败，属正常
        print("      请求结束（板子重启中断属正常）：%s" % e)

    # 切回原 WiFi
    time.sleep(3)
    if home_ssid:
        print("[5/5] 电脑切回 %s ..." % home_ssid)
        print(sh('netsh wlan connect name="%s"' % home_ssid).strip())
    else:
        print("[5/5] 原无 WiFi 连接，跳过切回")
    print("完成。板子日志请监控 COM6@921600")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
