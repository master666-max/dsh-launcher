# 完整变异测试：21 项用例对全部关键修复的守护能力
import sys, os, shutil, tempfile, re, subprocess
T = os.path.dirname(os.path.abspath(__file__))
sys.stdout.reconfigure(errors="replace")
PY = sys.executable

MUTANTS = [
    ("[env] parse_dump 退回 in_config 开关", "dsh_env.py",
     """        ind = len(raw) - len(raw.lstrip(" "))
        if k is None:
            k = ind                  # 该入口第一个属性行定义入口层缩进
        if ind != k:
            continue                 # 子块内部（config: 的孩子等），一律不看""",
     """        if raw.strip().startswith("config:"):
            k = "IN_CONFIG"
            continue
        if k == "IN_CONFIG":
            continue"""),
    ("[env] is_dsh_here 判据放宽回裸 dsh", "dsh_env.py",
     'and "dsh web" in low)', 'and "dsh" in low)'),
    ("[env] pid_is_node 恒 True（击杀复核失效）", "dsh_env.py",
     'return names.get(str(pid)) == "node.exe"', 'return True'),
    # 锚点随实现演进而更新：旧锚点用的是 has_q 之前的实现，早已不匹配 →
    # 变异体被静默跳过 = 这条修复失去守护。锚点只取函数体第一行，最稳。
    ("[env] split_win_cmdline 退回裸 split", "dsh_env.py",
     """    out, cur, in_q, has_q = [], [], False, False""",
     """    return s.split()
    out, cur, in_q, has_q = [], [], False, False"""),
    ("[env] clean_stale_locks 去掉 netstat ok 闸", "dsh_env.py",
     """    netstat_table()
    if not _netstat_cache.get("ok", True):""",
     """    netstat_table()
    if False:"""),
    # cmd_arg_safe 已从 dsh-fallback-heal.py 下沉到 dsh_env.py（heal 里只剩转调别名），
    # 旧锚点指向 heal 的旧实现 → 静默跳过。改指真身。
    ("[env] cmd_arg_safe 恒 True（元字符校验失效）", "dsh_env.py",
     "return isinstance(p, str) and _CMD_UNSAFE.search(p) is None",
     "return True"),
    ("[plugins] set_disabled 退回不看缩进层", "dsh-plugins.py",
     '                if ci == k and re.match(r"^\\s*disabled:\\s*", cur):',
     '                if re.match(r"^\\s*disabled:\\s*", cur):'),
    ("[launcher] run_heal 跳过阈值放宽到 0.9", "dsh-launcher.py",
     "if not deep and total and before < total * 0.5:",
     "if not deep and total and before < total * 0.9:"),
    ("[launcher] run_heal deep 也被跳过", "dsh-launcher.py",
     "if not deep and total and before < total * 0.5:",
     "if total and before < total * 0.5:"),
    ("[launcher] live_now 只看 DEFAULT_PORTS", "dsh-launcher.py",
     """    ports = []
    if env and env.get("port"):
        ports.append(env["port"]["value"])
    ports += dsh_env.DEFAULT_PORTS""",
     """    ports = list(dsh_env.DEFAULT_PORTS)"""),
    # ---- 2026-09-20 新增：PID 复用判据的守护 ----
    ("[env] lock_is_stale 删掉 PID 复用复核（退回只看 pid_alive）", "dsh_env.py",
     """        if alive is True:
            # names 允许调用方传入（批量扫描时复用一次 tasklist）
            if names is None:
                names = image_names()
            actual = names.get(str(pid))
            if actual is not None and actual.lower() != "node.exe":
                return True, ("记录的 pid %s 已被系统复用为 %s（非 node）"
                              "—— 该锁属于已死的 dsh" % (pid, actual))""",
     """        if alive is True:
            pass"""),
    ("[env] lock_is_stale 复用复核恒失效（!= node.exe 改成 False）", "dsh_env.py",
     '            if actual is not None and actual.lower() != "node.exe":',
     '            if False:'),
]

caught = missed = skipped = 0
for name, fname, old, new in MUTANTS:
    sp = os.path.join(T, fname)
    if not os.path.exists(sp):
        print("[跳过] 无文件 %s" % fname); skipped += 1; continue
    src = open(sp, encoding="utf-8").read()
    if old not in src:
        print("[跳过] 锚点不匹配：%s" % name); skipped += 1; continue
    d = tempfile.mkdtemp()
    try:
        for f in os.listdir(T):
            p = os.path.join(T, f)
            if os.path.isfile(p) and f.endswith(".py"):
                shutil.copy2(p, os.path.join(d, f))
        open(os.path.join(d, fname), "w", encoding="utf-8").write(
            src.replace(old, new, 1))
        r = subprocess.run([PY, os.path.join(d, "dsh_tests.py")],
                           capture_output=True, timeout=300)
        out = (r.stdout + r.stderr).decode("utf-8", "replace")
        m = re.search(r"用例 (\d+) 个，失败 (\d+) 个", out)
        if not m:
            print("[?  ] %s —— 变异体自身崩溃" % name); missed += 1
            print("      " + (out.strip().splitlines() or ["(空)"])[-1][:110])
            continue
        nf = int(m.group(2))
        if nf:
            caught += 1
            print("[抓到] %s（失败 %d 条）" % (name, nf))
            for ln in out.splitlines():
                if "[FAIL]" in ln:
                    print("        " + ln.strip()[:135]); break
        else:
            missed += 1
            print("[漏掉] %s" % name)
    finally:
        shutil.rmtree(d, ignore_errors=True)

print()
print("=" * 62)
print("抓到 %d / 漏掉 %d / 跳过 %d" % (caught, missed, skipped))
