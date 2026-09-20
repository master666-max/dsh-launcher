#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一键跑齐三段验收：自检 / 回归用例 / bat 语法闸。

为什么需要它：三段都要用【真文件】方式跑，且都要处理
「bat 会拉起 dsh 导致管道挂住」的问题，所以统一在这里做。
"""
import pathlib
import subprocess
import sys
import tempfile
import time

TOOLS = pathlib.Path(__file__).resolve().parent
# [!] 旧版硬编码了某 AI 工作台目录下的解释器路径 —— 违反本项目解耦红线
#     （selfcheck 扫描面扩大后会被直接报出），且对方一卸载本脚本就废。
#     改用「谁跑验收就用谁」：sys.executable。
PYEXE = sys.executable
BAT = pathlib.Path(pathlib.Path.home() / "Desktop" / "start-dsh.bat")


def run_py(script, timeout=300):
    t0 = time.time()
    r = subprocess.run([PYEXE, "-X", "utf8", str(TOOLS / script)],
                       capture_output=True, cwd=str(TOOLS), timeout=timeout)
    return r.returncode, r.stdout.decode("utf-8", "replace"), time.time() - t0


print("=" * 70)
print("  [1/3] dsh-selfcheck.py（静态自检）")
print("=" * 70)
rc, out, dt = run_py("dsh-selfcheck.py")
print(out.strip())
print(f"  -> rc={rc}  用时 {dt:.1f}s")
sc_ok = "未发现问题" in out or "合计 0 项" in out

print()
print("=" * 70)
print("  [2/3] dsh_tests.py（回归用例）")
print("=" * 70)
rc2, out2, dt2 = run_py("dsh_tests.py")
tail = [l for l in out2.splitlines()
        if ("通过" in l or "失败" in l or "PASS" in l or "FAIL" in l
            or "用例" in l or "共" in l)]
for l in tail[-15:]:
    print("  " + l.strip())
print(f"  -> rc={rc2}  用时 {dt2:.1f}s")
tests_ok = rc2 == 0 and "FAIL" not in out2

print()
print("=" * 70)
print("  [3/3] start-dsh.bat 语法闸 + 探测（不启动 dsh）")
print("=" * 70)
data = BAT.read_bytes()
lines = data.split(b"\r\n")
nlines = len(lines) - 1 if lines and lines[-1] == b"" else len(lines)
base = pathlib.Path(tempfile.mkdtemp(prefix="acc_"))

# 语法闸：截掉最后一行真正的启动命令，改为 exit /b 0
head = b"\r\n".join(lines[:nlines - 1])
gp = base / "gate.bat"
gp.write_bytes(head + b"\r\nexit /b 0\r\n")
r = subprocess.run(["cmd.exe", "/c", str(gp)], capture_output=True,
                   stdin=subprocess.DEVNULL, timeout=60)
se = r.stderr.decode("gbk", "replace")
print(f"  语法闸 rc = {r.returncode}   stderr = {se.strip()!r}")
gate_ok = r.returncode == 0 and "命令语法不正确" not in se

# 探测：PYEXE 是否选中真实 python
pp = base / "probe.bat"
pp.write_bytes(head + b"\r\necho PYEXE=[%PYEXE%]\r\nexit /b 0\r\n")
r2 = subprocess.run(["cmd.exe", "/c", str(pp)], capture_output=True,
                    stdin=subprocess.DEVNULL, timeout=60)
so2 = r2.stdout.decode("gbk", "replace").strip()
print(f"  探测 rc = {r2.returncode}   {so2}")
probe_ok = "python.exe" in so2.lower()

# 行尾
crlf, lf = data.count(b"\r\n"), data.count(b"\n")
eol_ok = crlf == lf
print(f"  行尾 CRLF={crlf} 裸LF={lf-crlf} -> {'纯 CRLF OK' if eol_ok else 'BAD'}")

print()
print("=" * 70)
print("  总判定")
print("=" * 70)
for name, ok in (("静态自检 0 问题", sc_ok), ("回归用例全绿", tests_ok),
                 ("bat 语法闸通过", gate_ok), ("解释器探测有效", probe_ok),
                 ("bat 行尾纯 CRLF", eol_ok)):
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}")
print()
print("  全部通过（ALL PASS）" if all([sc_ok, tests_ok, gate_ok, probe_ok, eol_ok])
      else "  存在失败项（见上）")
sys.exit(0 if all([sc_ok, tests_ok, gate_ok, probe_ok, eol_ok]) else 1)
