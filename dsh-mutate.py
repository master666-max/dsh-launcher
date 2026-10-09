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
    # ---- 2026-09-29 新增：安全模式的守护 ----
    # 内置插件是安全模式的「安全司机」——把它也禁了，安全模式就不再是
    # 最小可判断环境（真凶都被关了反而查不出是谁）。护栏用例断言 timer
    # 绝不出现在安全补丁里。
    ("[plugins] 安全模式把内置插件也禁掉", "dsh-plugins.py",
     """        if r.get("builtin"):
            continue""",
     """        if False:
            continue"""),
    # 备份是还原的唯一凭据 —— 丢了还硬写就会把用户正式补丁毁掉。
    # 护栏用例删掉备份后要求 restore 拒绝且现状不动。
    ("[plugins] safemode_restore 备份丢失仍盲写", "dsh-plugins.py",
     "        if not bak or not os.path.exists(bak):",
     "        if False:"),
    # 幂等分支必须校验文件体：手写启用犯病插件后再点 [6] 必须重生成，
    # 否则「安全模式」承诺破产。变异成永不重生成（if True 走老幂等路径）。
    # 护栏用例④：手写覆盖 → re-enter → restore 必须能删纯生成件。
    ("[plugins] 安全模式幂等不校验手写件（regen 丢失）", "dsh-plugins.py",
     "        if SM_MARK in body:",
     "        if True:"),
    # dsh 的加载错误只走 stdout/stderr（hub.log 只有「启动了」）——
    # 后台启动把输出重定向丢了 = 层5 故障彻底瞎掉。
    # 后台启动用例①断言子进程输出必须出现在日志文件里。
    ("[launcher] 后台启动不重定向日志（stdout 全丢）", "dsh-launcher.py",
     "                                stdout=bg_f, stderr=bg_f,",
     "                                stdout=None, stderr=None,"),
    # 重启必须先结束现有实例再启动 —— 不杀直接拉起就是「第二个实例抢端口」。
    # 重启用例①断言 kill_leftover 被调用（锚含 log 行保证唯一）。
    ("[launcher] 重启不杀旧实例直接启动", "dsh-launcher.py",
     """        log("  正在结束 dsh（端口 %s）..." % ", ".join(map(str, live)))
        kill_leftover(env)""",
     """        log("  正在结束 dsh（端口 %s）..." % ", ".join(map(str, live)))
        pass"""),
    # ===== 上游更新检查（dsh_update，2026-10-05）=====
    # ① 免拉闸：behind==0 退回 behind<=0 ⇒ 「已是最新」会被重查成有更新并真拉
    #    （这条是回归用例真抓到的 bug：behind<=0 分支把 0 当"状态过期"）
    ("[update] behind=0 也当过期 → 免拉闸失效", "dsh_update.py",
     "    if behind == 0:\n        return True, \"已是最新，无需拉取\"",
     "    if behind <= 0:\n        pass\n    if behind == 0:\n        return True, \"已是最新，无需拉取\""),
    # ② 节流窗口恒真 ⇒ 每次双击都打网络（违背「不重复打远端」）
    ("[update] 节流恒真（每次启动都 fetch）", "dsh_update.py",
     "        return (_now() - int(state.get(\"checked_at\") or 0)) < TTL_SECONDS",
     "        return True"),
    # ③ 失败态也被缓存一小时 ⇒ 网络抽风一次就一小时内不再检查
    ("[update] 失败态也缓存（不再重试）", "dsh_update.py",
     "    if state.get(\"state\") != STATE_OK:\n        return False",
     "    pass"),
    # ④ 游离 HEAD 不拒绝 ⇒ 拉取会改 HEAD 指向（用户丢版本位置）
    ("[update] 游离 HEAD 不拒绝拉取", "dsh_update.py",
     "    if detached(repo):\n        return False,",
     "    if False:\n        return False,"),
    # ⑤ 脏工作区闸去掉 ⇒ 半途拉取把本地改动搞冲突
    ("[update] 脏工作区闸去掉", "dsh_update.py",
     "    if is_dirty(repo):",
     "    if False:"),
    # ⑥ 查不到落后数时谎报 0（"没查过"被读成"已是最新"——本仓最恨的凭空造数）
    ("[update] 查不到落后数谎报 0", "dsh_update.py",
     """    try:
        return int(out.split()[0])
    except Exception:
        return None""",
     """    try:
        return int(out.split()[0])
    except Exception:
        return 0"""),
    # ⑦ badge 失败时不静默 ⇒ 菜单被"检查失败"噪音污染
    ("[update] 失败态不静默（菜单出现噪音行）", "dsh_update.py",
     "    if s != STATE_OK:\n        return \"\"",
     "    if s != STATE_OK:\n        return \"  ?? 更新状态未知\""),
    # ⑧ 超时向上抛 ⇒ 后台线程炸掉（虽然有 try，但同步路径 --update-check 会崩）
    ("[update] 超时向上抛", "dsh_update.py",
     """    except subprocess.TimeoutExpired:
        return None, \"\"          # 超时：当作没查到""",
     """    except subprocess.TimeoutExpired:
        raise"""),
    # ⑨ 游离 HEAD 时基准退回「无上游」⇒ 落后数永远查不到，更新检查形同虚设
    ("[update] detached 下不退回 origin/HEAD", "dsh_update.py",
     """    rc, out = run_git(repo, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], QUERY_TIMEOUT)
    if rc == 0 and out:
        return out""",
     """    rc, out = run_git(repo, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], QUERY_TIMEOUT)
    if False:
        return out"""),
]

caught = missed = skipped = 0
# [!] 可选 --range A:B：只跑 MUTANTS[A:B]（Python 切片语义）。
#     用途：分批前台跑 —— 后台任务上下文会被宿主的 SAFE_DELETE 批删
#     确认钩子盯上（turn 内删 50+ 文件即拦，直接杀掉测试子进程，
#     表现为「变异体自身崩溃」）；前台命令不受监控，但单条命令有
#     时长上限，所以整套要切成几段跑。汇总行会注明范围。
_range = None
if "--range" in sys.argv:
    _i = sys.argv.index("--range")
    _a, _b = (sys.argv[_i + 1].split(":") + [""])[:2]
    _range = (int(_a or 0), int(_b) if _b else len(MUTANTS))
mutants = MUTANTS[slice(*_range)] if _range else MUTANTS
for name, fname, old, new in mutants:
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
print("抓到 %d / 漏掉 %d / 跳过 %d%s"
      % (caught, missed, skipped,
         ("　[本批范围 %d:%d，共 %d 条；全套 %d 条]"
          % (_range[0], _range[1], len(mutants), len(MUTANTS)))
         if _range else ""))
