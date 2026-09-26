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
    # [层6] 2026-09-25：端口被系统排除范围吞掉（listen EACCES）——
    # 命中判断改坏后，3080 被吞时启动器不再提前报警（用户白等两分钟）。
    ("[env] 层6 排除范围命中判断恒 False", "dsh_env.py",
     "        if lo <= port <= hi:",
     "        if False:"),
    ("[plugins] set_disabled 退回不看缩进层", "dsh-plugins.py",
     '                if ci == k and re.match(r"^\\s*disabled:\\s*", cur):',
     '                if re.match(r"^\\s*disabled:\\s*", cur):'),
    ("[launcher] run_heal 跳过阈值放宽到 0.9", "dsh-launcher.py",
     "if not deep and total and before < total * 0.5:",
     "if not deep and total and before < total * 0.9:"),
    ("[launcher] run_heal deep 也被跳过", "dsh-launcher.py",
     "if not deep and total and before < total * 0.5:",
     "if total and before < total * 0.5:"),
    # [2026-09-26] 这条以前**抓不到**，不是漏网，是根本没覆盖：
    # 旧替身在"探测失败"时返回空集 → counts() 给出 total=0/before=0 →
    # 走的是"本来就完整"分支，`total is None` 那条提前返回从没被执行过。
    # 改成抛异常模拟探测失败之后，这条才真正有断言撑着。
    ("[launcher] run_heal 计数失败时不提前返回（瞎补链接）", "dsh-launcher.py",
     '    if total is None:\n        return True, "无法核对链接状态（跳过）"',
     '    if False:\n        return True, "无法核对链接状态（跳过）"'),
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
    # ---- 2026-09-26 新增：入口 bat 的 TOOLS 三级解析守护 ----
    # [!] .bat 的锚点按 GBK 字节替换（见主循环里的字节分支）——
    #     文本模式会把 CRLF 折成 LF，变异体会因「裸 LF 秒退」被假抓到，
    #     看着是 [抓到]，守护的却是一条与锚点无关的性质。
    ("[bat] 去掉就近（dp0）分支 —— 目录又变回不可搬移", "start-dsh.bat",
     'if not defined TOOLS if exist "%~dp0dsh-launcher.py" for %%I in ("%~dp0.") do set "TOOLS=%%~fI"',
     "rem MUTANT: colocated branch removed"),
    ("[bat] DSH_TOOLS 显式覆盖被忽略", "start-dsh.bat",
     'if defined DSH_TOOLS if exist "%DSH_TOOLS%\\dsh-launcher.py" for %%I in ("%DSH_TOOLS%.") do set "TOOLS=%%~fI"',
     "rem MUTANT: env override removed"),
]

caught = missed = skipped = 0
for name, fname, old, new in MUTANTS:
    sp = os.path.join(T, fname)
    if not os.path.exists(sp):
        print("[跳过] 无文件 %s" % fname); skipped += 1; continue
    # [!] .bat 是 GBK + 纯 CRLF：锚点比对与改写都必须走【字节】。
    #     用文本模式读写会把 CRLF 折成 LF，变异体就变成「裸 LF 秒退」——
    #     测试确实会红，但红的原因与锚点无关，等于骗自己。
    is_bat = fname.endswith(".bat")
    if is_bat:
        raw = open(sp, "rb").read()
        if old.encode("gbk") not in raw:
            print("[跳过] 锚点不匹配：%s" % name); skipped += 1; continue
    else:
        src = open(sp, encoding="utf-8").read()
        if old not in src:
            print("[跳过] 锚点不匹配：%s" % name); skipped += 1; continue
    d = tempfile.mkdtemp()
    try:
        for f in os.listdir(T):
            p = os.path.join(T, f)
            if os.path.isfile(p) and f.endswith((".py", ".bat")):
                shutil.copy2(p, os.path.join(d, f))
        if is_bat:
            open(os.path.join(d, fname), "wb").write(
                raw.replace(old.encode("gbk"), new.encode("gbk"), 1))
        else:
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
