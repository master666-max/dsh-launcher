# -*- coding: utf-8 -*-
"""
dsh 工具链回归测试（独立可跑，无需 pytest）
==========================================
    python dsh_tests.py            # 跑全部用例
    python dsh_tests.py -v         # 显示每个用例名

原则：**所有写操作都在 tempfile 副本上做**，绝不碰真实补丁与真实 dsh。
这就是过去"改完说修好了、一跑就崩"的解药 —— 用例固化后可反复回归。
"""
import os, sys, json, time, shutil, subprocess, tempfile, textwrap

T = os.path.dirname(os.path.abspath(__file__))
if T not in sys.path:
    sys.path.insert(0, T)

# 与其余运行入口一致：chcp 936 终端打印非 GBK 字符会抛 UnicodeEncodeError
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

import dsh_env

dsh_plugins = None   # 由 __main__ 用 importlib 动态注入（文件名带连字符）

VERBOSE = "-v" in sys.argv
FAILED = []
COUNT = [0]


def log(m=""):
    print(m)
    sys.stdout.flush()


def case(name):
    def deco(fn):
        def run():
            COUNT[0] += 1
            if VERBOSE:
                log("  > %s" % name)
            try:
                fn()
                log("  [OK]   %s" % name)
            except AssertionError as e:
                log("  [FAIL] %s — %s" % (name, e))
                FAILED.append(name)
            except Exception as e:
                log("  [ERR ] %s — %r" % (name, e))
                FAILED.append(name + " (异常)")
        run.__name__ = name
        return run
    return deco


def expect(cond, msg):
    if not cond:
        raise AssertionError(msg)


# ============================================================
# 1. 锁判定
# ============================================================
@case("lock_is_stale：0字节 / 畸形 / dead-PID / 活node / PID复用 / 无pid / 查不到映像名")
def t_lock():
    d = tempfile.mkdtemp(prefix="dsh_t_lock_")
    try:
        f0 = os.path.join(d, "a.lock"); open(f0, "wb").close()
        stale, why = dsh_env.lock_is_stale(f0)
        expect(stale, "0 字节应判脏，实际 %r" % why)

        f1 = os.path.join(d, "b.lock")
        open(f1, "wb").write(b"not-json-at-all")
        stale, why = dsh_env.lock_is_stale(f1)
        expect(stale, "畸形内容应判脏，实际 %r" % why)

        f2 = os.path.join(d, "c.lock")
        open(f2, "w", encoding="utf-8").write(
            json.dumps({"pid": 999999, "token": "x"}))
        stale, why = dsh_env.lock_is_stale(f2)
        expect(stale, "dead-pid 应判脏，实际 %r" % why)

        # ---- 活 pid 的三种情况 ----
        # 必须【显式传入 names】来模拟真实映像名。旧用例直接拿 os.getpid()
        # （python.exe）当"活 dsh"，是不真实的样本 —— 真实活锁必须由 node.exe 持有。
        f3 = os.path.join(d, "d.lock")
        open(f3, "w", encoding="utf-8").write(
            json.dumps({"pid": os.getpid(), "token": "x"}))
        me = str(os.getpid())

        stale, why = dsh_env.lock_is_stale(f3, {me: "node.exe"})
        expect(not stale, "活 pid 且确实是 node.exe（dsh 在跑）不应判脏，实际 %r" % why)

        # [2026-09-20] PID 被系统复用给别的程序 = 锁属于已死的 dsh，必须判脏。
        # 实测：强杀留下的 pid 31848 被复用成 Nahimic3.exe，
        # 旧判据只看 pid_alive → 误判"正常锁"→ 不清 → 新 dsh 抢锁失败崩。
        stale, why = dsh_env.lock_is_stale(f3, {me: "nahimic3.exe"})
        expect(stale, "PID 被复用为非 node 程序应判脏，实际 %r" % why)

        # 查不到映像名（tasklist 失败/进程刚退出）→ 保守放过，
        # 绝不许把活 dsh 的锁当脏锁删掉
        stale, why = dsh_env.lock_is_stale(f3, {})
        expect(not stale, "映像名查不到时应保守放过（不误删活锁），实际 %r" % why)

        f4 = os.path.join(d, "e.lock")
        open(f4, "w", encoding="utf-8").write(json.dumps({"no": "pid"}))
        stale, why = dsh_env.lock_is_stale(f4)
        expect(not stale, "无 pid 的合法 JSON 不应判脏，实际 %r" % why)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@case("lock_is_stale：不存在的文件返回 False（不误报）")
def t_lock_missing():
    stale, _ = dsh_env.lock_is_stale(os.path.join(tempfile.gettempdir(),
                                                  "no_such_lock_xyz.lock"))
    expect(stale is False, "不存在的文件不该判脏")


# ============================================================
# 2. dump 解析
# ============================================================
SAMPLE_DUMP = textwrap.dedent("""\
    # == @deepseek-ai/dsh-base
    - id: timer
      name: '@deepseek-ai/cordis-plugin-timer'
    - id: hmr
      name: '@deepseek-ai/cordis-plugin-hmr'
      disabled: true
      config:
        root:
          - .
    - id: agent-default-model
      name: '@deepseek-ai/dsh-agent-default-model'
      disabled: false
      config:
        provider: deepseek-official
        name: 嵌套的name不应被当成入口name
        disabled: 嵌套的disabled不应被当成入口disabled
    # == @deepseek-ai/dsh-base, patched by @deepseek-ai/dsh-web-app
    - id: tool-fs
      name: '@deepseek-ai/dsh-tool-fs'
      disabled: true
    # == dsh-mobile
    - id: mobile-access
      name: dsh-mobile
      disabled: !!js process.platform === 'win32'
    """)


@case("parse_dump：id/name/disabled/config 嵌套隔离")
def t_parse_dump():
    ents = dsh_env.parse_dump(SAMPLE_DUMP)
    ids = {e["id"]: e for e in ents}
    expect(len(ents) == 5, "应有 5 个入口，实际 %d" % len(ents))
    expect(ids["timer"]["name"] == "@deepseek-ai/cordis-plugin-timer", "name 解析")
    expect(ids["hmr"]["disabled"] is True, "hmr 应为禁用")
    expect(ids["tool-fs"]["disabled"] is True, "tool-fs 应为禁用")
    expect(ids["mobile-access"]["disabled"] == "expr", "!!js 应为条件")
    expect(ids["agent-default-model"]["disabled"] is False, "false 应为启用")
    # config 块里的同名键不能污染入口属性
    expect(ids["agent-default-model"]["name"] == "@deepseek-ai/dsh-agent-default-model",
           "config 里的 name 不应覆盖入口 name")
    # 分节归属
    expect("dsh-web-app" in ids["tool-fs"]["section"], "tool-fs 应归属 web-app 补丁层")
    expect(ids["hmr"]["section"].startswith("@deepseek-ai/dsh-base"), "hmr 归属 base")


@case("parse_dump：空输入 / 无 id 行不崩")
def t_parse_dump_edge():
    expect(dsh_env.parse_dump("") == [], "空输入应为空列表")
    expect(dsh_env.parse_dump("hello world") == [], "无 id 行应为空列表")


# [!] 真实 dump 里 disabled 常常排在 config: **之后**（如 tool-web）。
#     旧实现用 in_config 开关，一旦进 config 就再也不出来，
#     会把这类入口的 disabled 整条丢掉 → 菜单把「已禁用」显示成「启用中」，
#     用户照着按下去就改错状态。本机实测有 11 条属于此形态。
SAMPLE_DUMP_DIS_AFTER_CONFIG = textwrap.dedent("""\
    # == @deepseek-ai/dsh-base
    - id: pre-config
      name: '@deepseek-ai/dsh-pre-config'
      disabled: true
      config:
        mode: x
    - id: post-config
      name: '@deepseek-ai/dsh-post-config'
      config:
        fetch: true
        searchTimeoutMs: 60000
      disabled: true
    - id: post-config-expr
      name: '@deepseek-ai/dsh-post-config-expr'
      config:
        nested:
          disabled: 子块的disabled不是入口属性
      disabled: !!js process.platform === 'win32'
    - id: cfg-only-disabled
      name: '@deepseek-ai/dsh-cfg-only'
      config:
        disabled: 只有子块有disabled，入口层没有
    """)


@case("parse_dump：disabled 排在 config 之后仍要读到（旧实现漏读 11 条）")
def t_parse_dump_dis_order():
    ids = {e["id"]: e for e in dsh_env.parse_dump(SAMPLE_DUMP_DIS_AFTER_CONFIG)}
    expect(len(ids) == 4, "应有 4 个入口，实际 %d" % len(ids))
    # 核心断言：config 之后的入口属性必须读到
    expect(ids["post-config"]["disabled"] is True,
           "config 之后的 disabled 必须读到（旧实现漏读），实际 %r"
           % (ids["post-config"]["disabled"],))
    expect(ids["post-config-expr"]["disabled"] == "expr",
           "config 之后的 !!js 必须识别为条件，实际 %r"
           % (ids["post-config-expr"]["disabled"],))
    # 反向：只有子块有 disabled 时，入口层必须为 None（不能误读子块）
    expect(ids["cfg-only-disabled"]["disabled"] is None,
           "子块的 disabled 不是入口属性，应为 None，实际 %r"
           % (ids["cfg-only-disabled"]["disabled"],))
    # 对照：config 之前的也要正常
    expect(ids["pre-config"]["disabled"] is True, "config 之前的 disabled 应正常")
    # name 同样不能被子块污染
    expect(ids["post-config"]["name"] == "@deepseek-ai/dsh-post-config",
           "name 应正常")


# ============================================================
# 3. --help 子命令解析
# ============================================================
SAMPLE_HELP = textwrap.dedent("""\
    Usage: dsh [options] [command] [args...]

    Arguments:
      args                           arguments for the booted profile

    Options:
      -V, --version                  output the version number

    Commands:
      web [options] [args...]        boot the web profile (alias of --profile web);
                                     the web app's own flags follow
      plugin [options] [args...]     manage a profile's plugins by forwarding the
                                     remaining arguments to pnpm in the profile
                                     directory

    Examples:
      dsh --profile web              boot the web profile
    """)


@case("parse_help_commands：只取命令名，不吞续行描述词")
def t_help():
    subs = dsh_env.parse_help_commands(SAMPLE_HELP)
    expect(subs == ["web", "plugin"],
           "应解析出 ['web','plugin']，实际 %r" % subs)


@case("parse_help_commands：空输入")
def t_help_edge():
    expect(dsh_env.parse_help_commands("") == [], "空输入应得空列表")


# ============================================================
# 4. 补丁写入（全在副本上做）
# ============================================================
SAMPLE_PATCH = textwrap.dedent("""\
    # 我的补丁层
    - id: connection
      config:
        cookieMaxAgeDays: 3650

    - insert:
        - id: blender-mcp
          name: '@deepseek-ai/dsh-mcp-client'
          config:
            serverName: blender

    - id: dsh-web-mobile
      disabled: true
    """)


def _copy_patch(tmp):
    p = os.path.join(tmp, "cordis.patch.yml")
    open(p, "wb").write(SAMPLE_PATCH.encode("utf-8"))
    return p


@case("set_disabled：正常追加 + 备份 + 无 tmp 残留 + 原内容保留")
def t_set_normal():
    tmp = tempfile.mkdtemp(prefix="dsh_t_set_")
    try:
        p = _copy_patch(tmp)
        before = open(p, "rb").read()
        ok, note = dsh_plugins.set_disabled(p, "tool-fs", True)
        expect(ok, "应成功：%s" % note)
        t = open(p, encoding="utf-8").read()
        expect("- id: tool-fs" in t, "新块已写入")
        expect("cookieMaxAgeDays" in t, "原内容保留")
        residue = [x for x in os.listdir(tmp) if ".tmp-plugins" in x]
        expect(not residue, "无 tmp 残留：%r" % residue)
        baks = [x for x in os.listdir(tmp) if ".bak-plugins-" in x]
        expect(len(baks) == 1, "恰好 1 份备份，实际 %d" % len(baks))
        # 往返
        ok, _ = dsh_plugins.set_disabled(p, "tool-fs", False)
        t2 = open(p, encoding="utf-8").read()
        expect(t2.count("- id: tool-fs") == 1, "不重复追加")
        expect("disabled: false" in t2, "已改为 false")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@case("set_disabled：行尾保真（CRLF→CRLF / LF→LF）")
def t_set_eol():
    tmp = tempfile.mkdtemp(prefix="dsh_t_eol_")
    try:
        for name, eol, want in (("crlf.yml", "\r\n", "CRLF"),
                                ("lf.yml", "\n", "LF")):
            p = os.path.join(tmp, name)
            open(p, "wb").write(SAMPLE_PATCH.replace("\n", eol).encode("utf-8"))
            dsh_plugins.set_disabled(p, "tool-fs", True)
            d = open(p, "rb").read()
            lf, crlf = d.count(b"\n"), d.count(b"\r\n")
            if want == "CRLF":
                expect(lf == crlf and crlf > 0, "应保持 CRLF，实际 LF=%d CRLF=%d" % (lf, crlf))
            else:
                expect(crlf == 0, "应保持 LF，实际 CRLF=%d" % crlf)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@case("set_disabled：安全护栏（不存在 / 空文件 → 拒绝且不写）")
def t_set_guards():
    tmp = tempfile.mkdtemp(prefix="dsh_t_gd_")
    try:
        miss = os.path.join(tmp, "nope.yml")
        ok, _ = dsh_plugins.set_disabled(miss, "x", True)
        expect(ok is False, "不存在的文件应拒绝")
        expect(not os.path.exists(miss), "不应凭空创建文件")

        empty = os.path.join(tmp, "empty.yml")
        open(empty, "wb").close()
        ok, _ = dsh_plugins.set_disabled(empty, "x", True)
        expect(ok is False, "空文件应拒绝（防清空）")
        expect(os.path.getsize(empty) == 0, "空文件不应被写入")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@case("set_disabled：只动入口层 disabled，config 子块同名键分毫不动")
def t_set_indent_guard():
    # 旧实现一旦在 config 子块里撞见 disabled: 就改在那里 ——
    # 那是插件自己的配置，改了插件行为就变了，而且入口层还是没禁用。
    SAMPLE = ("# patch\n"
              "- id: alpha\n"
              "  name: pkg-alpha\n"
              "  config:\n"
              "    mode: fast\n"
              "    disabled: user-set-value\n"
              "- id: beta\n"
              "  name: pkg-beta\n"
              "  disabled: false\n"
              "  config:\n"
              "    nested:\n"
              "      disabled: deep-value\n")
    tmp = tempfile.mkdtemp(prefix="dsh_t_ind_")
    try:
        p = os.path.join(tmp, "cordis.patch.yml")
        open(p, "w", encoding="utf-8", newline="").write(
            SAMPLE.replace("\n", "\r\n"))
        ok, note = dsh_plugins.set_disabled(p, "alpha", True)
        txt = open(p, encoding="utf-8").read()
        expect(ok, "应写入成功：%s" % note)
        lines = txt.splitlines()
        # 子块里的值必须原样保留
        expect("user-set-value" in txt, "子块的 disabled 值必须保留")
        expect("deep-value" in txt, "深层子块的 disabled 值必须保留")
        # 入口层（缩进 2）应新增一条 disabled: true
        ent = [ln for ln in lines
               if ln.startswith("  disabled:") or ln.startswith("  disabled :")]
        expect(any("true" in ln for ln in ent),
               "应在入口层（缩进 2）新增 disabled: true，实际入口层：%r" % ent)
        # 缩进 4/6 的那两条必须还是原值
        for ln in lines:
            if ln.strip() == "disabled: something":
                expect(False, "不应改到子块")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # beta：入口层已有 disabled: false，必须就地改成 true（不能改成子块的）
    tmp2 = tempfile.mkdtemp(prefix="dsh_t_ind2_")
    try:
        p = os.path.join(tmp2, "cordis.patch.yml")
        open(p, "w", encoding="utf-8", newline="").write(
            SAMPLE.replace("\n", "\r\n"))
        ok, note = dsh_plugins.set_disabled(p, "beta", True)
        txt = open(p, encoding="utf-8").read()
        expect(ok, "应写入成功：%s" % note)
        lines = txt.splitlines()
        ent = [ln for ln in lines if ln.startswith("  disabled:")]
        expect(any("true" in ln for ln in ent),
               "beta 入口层应变成 true，实际 %r" % ent)
        expect("deep-value" in txt, "beta 子块的 deep-value 必须保留")
        # 入口层只能有一条 disabled（不能重复插入）
        expect(len(ent) == 1, "入口层应恰好 1 条 disabled，实际 %d：%r"
               % (len(ent), ent))
    finally:
        shutil.rmtree(tmp2, ignore_errors=True)


@case("set_disabled：备份轮转（≤10 份）")
def t_set_rotation():
    tmp = tempfile.mkdtemp(prefix="dsh_t_rot_")
    try:
        p = _copy_patch(tmp)
        for i in range(14):
            dsh_plugins.set_disabled(p, "tool-fs", i % 2 == 0)
        baks = [x for x in os.listdir(tmp) if ".bak-plugins-" in x]
        expect(len(baks) <= 10, "备份应 ≤10 份，实际 %d" % len(baks))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================
# 5. 探测层
# ============================================================
@case("detect：缓存命中返回深拷贝（不与缓存共享对象）")
def t_detect_isolation():
    r1 = dsh_env.detect()
    r2 = dsh_env.detect()
    expect(r1["notes"] is not r2["notes"], "notes 不应共享")
    expect(r1["start"] is not r2["start"], "start 不应共享")
    r1["notes"].append("__probe_marker__")
    r3 = dsh_env.detect()
    expect("__probe_marker__" not in r3["notes"], "改动不应污染缓存")


@case("netstat_table：返回 list 且含 3080 或至少有数据")
def t_netstat():
    rows = dsh_env.netstat_table()
    expect(isinstance(rows, list), "应为 list")
    expect(len(rows) > 0, "本机至少应有监听端口")
    for r in rows[:3]:
        expect("port" in r and "pid" in r, "行结构应为 {port,pid}")


@case("list_pkgs：@scope/name 与顶层包")
def t_list_pkgs():
    d = tempfile.mkdtemp(prefix="dsh_t_pkg_")
    try:
        os.makedirs(os.path.join(d, "@types"))
        os.makedirs(os.path.join(d, "@types", "node"))
        os.makedirs(os.path.join(d, "zod"))
        os.makedirs(os.path.join(d, ".hidden"))
        got = dsh_env.list_pkgs(d)
        expect("@types/node" in got, "scope 包应展开")
        expect("zod" in got, "顶层包应包含")
        expect(not any(x.startswith(".") for x in got), "隐藏目录应排除")
    finally:
        shutil.rmtree(d, ignore_errors=True)


@case("prebuilt_entry：lib 新于 src → 可用；src 新于 lib → 拒绝")
def t_prebuilt():
    d = tempfile.mkdtemp(prefix="dsh_t_pre_")
    try:
        lib = os.path.join(d, "apps", "cli", "lib")
        src = os.path.join(d, "apps", "cli", "src")
        os.makedirs(lib); os.makedirs(src)
        open(os.path.join(lib, "bin.js"), "wb").write(b"x")
        time.sleep(0.05)
        open(os.path.join(src, "bin.ts"), "wb").write(b"x")
        expect(dsh_env.prebuilt_entry(d) is None, "src 较新应拒绝")
        os.utime(os.path.join(lib, "bin.js"), None)      # 把 lib 顶到最新
        expect(dsh_env.prebuilt_entry(d) == os.path.join(lib, "bin.js"),
               "lib 较新应可用")
        expect(dsh_env.prebuilt_entry(os.path.join(d, "nope")) is None, "无仓库应 None")
    finally:
        shutil.rmtree(d, ignore_errors=True)


@case("pkg_manager：按锁文件推断")
def t_pkgmgr():
    d = tempfile.mkdtemp(prefix="dsh_t_pm_")
    try:
        open(os.path.join(d, "yarn.lock"), "wb").close()
        expect(dsh_env.pkg_manager(d) == "yarn", "yarn.lock → yarn")
        os.remove(os.path.join(d, "yarn.lock"))
        open(os.path.join(d, "package-lock.json"), "wb").close()
        expect(dsh_env.pkg_manager(d) == "npm", "package-lock → npm")
        expect(dsh_env.pkg_manager(None) == "pnpm", "无仓库 → 默认 pnpm")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============================================================
# 6. run_heal 的跳过逻辑（启动路径不白干）
# ============================================================
@case("run_heal：启动路径跳过/执行的分支判据与生产代码一致（子进程全拦截）")
def t_run_heal():
    import importlib.util as _ilu
    _s = _ilu.spec_from_file_location('dsh_launcher', os.path.join(T, 'dsh-launcher.py'))
    L = importlib.util.module_from_spec(_s)
    _s.loader.exec_module(L)
    env = L.probe()
    if not env or not (env.get("features") or {}).get("has_fallback"):
        log("    （本机没有 fallback 机制，跳过此用例）")
        return

    # [!] deep 路径会写真实 profile（补 junction，还会拖慢下次 dsh 启动），
    #     回归测试绝不真跑 —— 用替身拦截自愈子进程，只断言「调没调」
    calls = []

    class _FakeSub:
        TimeoutExpired = subprocess.TimeoutExpired

        @staticmethod
        def run(*a, **k):
            calls.append(a)

    real_sub = L.subprocess
    L.subprocess = _FakeSub
    try:
        # ------------------------------------------------------------
        # 关键：不能拿「测试自己算的缺失数」去猜 run_heal 会走哪个分支
        # —— 那等于把生产判据复制一份来对答案，判据一改测试就失效。
        # 这里直接**驱动 run_heal 在四种缺失状态下的真实分支**，
        # 靠替身 list_pkgs 喂入受控数值，断言调用次数 = 生产代码的语义。
        # ------------------------------------------------------------
        for name, (total, before) in [
                ("完整(缺失 0)",      (285, 0)),
                ("正常态(缺 65/285)", (285, 65)),
                ("缺失过半(200/285)", (285, 200)),
                ("探测失败(None)",    (None, None)),
        ]:
            real_lp = dsh_env.list_pkgs
            seq = {"n": 0}

            def fake_lp(path, _t=total, _b=before):
                seq["n"] += 1
                if _t is None:
                    return set()
                # 第一次调用(total) 返回 t 个包；第二次(缺) 返回 t-b 个包
                if seq["n"] % 2 == 1:
                    return set("pkg%d" % i for i in range(_t if _t else 0))
                return set("pkg%d" % i for i in range((_t - _b) if _t else 0))

            calls.clear()
            dsh_env.list_pkgs = fake_lp
            try:
                ok, msg = L.run_heal(env)          # 启动路径
            finally:
                dsh_env.list_pkgs = real_lp

            # 生产语义：total is None / before==0 / before<total*0.5 → 都不调子进程
            if total is None or (total and (before == 0 or before < total * 0.5)):
                expect(len(calls) == 0,
                       "[%s] 启动路径不应执行自愈子进程，实际 %d 次（msg=%s）"
                       % (name, len(calls), msg))
                expect(ok, "[%s] 应返回成功（跳过不算失败）：%s" % (name, msg))
            else:
                expect(len(calls) == 1,
                       "[%s] 缺失过半时启动路径应执行自愈子进程，实际 %d 次（msg=%s）"
                       % (name, len(calls), msg))

        # 手动路径（deep=True）：无视上述判断，必须真调用
        calls.clear()
        L.run_heal(env, deep=True)
        expect(len(calls) == 1, "deep=True 必须真正调用自愈脚本，实际 %d 次" % len(calls))
    finally:
        L.subprocess = real_sub


# ============================================================
# 7. 安全防护（审计修复回归）
# ============================================================
@case("安全防护：cmd 元字符校验 + pid_is_node 击杀前复核")
def t_sec_guards():
    import importlib.util as _ilu
    _s = _ilu.spec_from_file_location('dsh_heal', os.path.join(T, 'dsh-fallback-heal.py'))
    H = importlib.util.module_from_spec(_s)
    _s.loader.exec_module(H)
    expect(H.cmd_arg_safe(r"C:\x\y z (1)"), "普通含空格/括号路径应放行")
    expect(H.cmd_arg_safe(r"C:\x\@scope\pkg.name-1"), "常规包路径应放行")
    for bad in (r"C:\x%TEMP%y", 'a"b', "a&b", "a|b", "a<b", "a>b", "a^b", "a!b",
                "a\nb", "a\tb"):
        expect(not H.cmd_arg_safe(bad), "含 %r 应拒绝" % bad)
    expect(dsh_env.pid_is_node(os.getpid()) is False,
           "当前 python 进程不是 node.exe，应判 False")


# ============================================================
# 8. Windows 命令行分割 / dsh 签名判据（外部审计修复的守护用例）
# ============================================================
@case("split_win_cmdline：去引号 + 保反斜杠（另两种切法都会出错）")
def t_split_win():
    cases = [
        (r'"C:\Program Files\nodejs\pnpm.cmd" dsh web',
         [r"C:\Program Files\nodejs\pnpm.cmd", "dsh", "web"]),
        (r"C:\Users\demo-user\AppData\Roaming\npm\pnpm.cmd dsh web",
         [r"C:\Users\demo-user\AppData\Roaming\npm\pnpm.cmd", "dsh", "web"]),
        (r'"C:\my tools\run.cmd" --profile web --port 3080',
         [r"C:\my tools\run.cmd", "--profile", "web", "--port", "3080"]),
        ('node "apps\\cli\\lib\\bin.js" web',
         ["node", r"apps\cli\lib\bin.js", "web"]),
        ("pnpm dsh web", ["pnpm", "dsh", "web"]),
    ]
    for src, want in cases:
        got = dsh_env.split_win_cmdline(src)
        expect(got == want, "拆分 %r\n            应得 %r\n            实得 %r"
               % (src, want, got))
    # 关键性质：引号必须被去掉（留着会被 cmd.exe 当成命令名的一部分）
    got = dsh_env.split_win_cmdline(r'"C:\Program Files\a\b.cmd" x')
    expect(not any('"' in p for p in got), "结果里不应残留引号：%r" % (got,))
    # 关键性质：反斜杠必须保留（shlex(posix=True) 会全丢）
    expect(r"C:\Users\demo-user\x.cmd" in got[0] or "\\" in got[0],
           "反斜杠必须保留：%r" % (got,))


@case("is_dsh_here：判据必须是 401 + authentication required + dsh web")
def t_is_dsh_here():
    # [!] 不能直接 `"dsh web" in inspect.getsource(...)` —— 注释与 docstring
    #     里也写着这几个字，把代码改回裸 "dsh" 时它们仍在，断言照样通过（实测漏网）。
    #     做法：用 AST 剥掉注释与 docstring，只在**真实代码正文**里查。
    import ast, inspect
    fn = ast.parse(
        textwrap.dedent(inspect.getsource(dsh_env.is_dsh_here))).body[0]

    # 清空 docstring（它是 Constant 表达式语句）
    body = [n for n in fn.body
            if not (isinstance(n, ast.Expr)
                    and isinstance(n.value, ast.Constant)
                    and isinstance(n.value.value, str))]
    fn.body = body
    code = ast.unparse(fn)
    # 注意 ast.unparse 会把字符串统一成单引号，故两种都要认
    expect("dsh web" in code,
           '代码正文必须精确含 "dsh web"（放宽成裸 "dsh" 会危及 taskkill 安全）；'
           "实际：%r" % code[-200:])
    # 反向：去掉正确的 "dsh web" 后，正文里不该再有裸 "dsh"
    stripped = (code.replace('"dsh web"', "").replace("'dsh web'", ""))
    expect("'dsh'" not in stripped and '"dsh"' not in stripped,
           '代码正文不应出现裸 "dsh"；实际：%r' % stripped[-200:])
    # 行为验证：非 dsh 的端口一律 False
    expect(dsh_env.is_dsh_here(9) is False, "未监听端口应 False")
    rows = dsh_env.netstat_table()
    for r in rows[:20]:
        if r["port"] not in (3080, 3081, 3082):
            expect(dsh_env.is_dsh_here(r["port"]) is False,
                   "端口 %d 不是 dsh，不应误判" % r["port"])
            break


@case("clean_stale_locks：netstat 探测失败时整轮放弃（不误删活锁）")
def t_clean_lock_gate():
    real = dsh_env.netstat_table
    real_ok = dsh_env._netstat_cache.get("ok", True)
    try:
        # 伪造一次「探测失败」：ok=False
        def fail(*a, **k):
            dsh_env._netstat_cache["ok"] = False
            return dsh_env._netstat_cache.get("rows", [])
        dsh_env.netstat_table = fail
        msgs = []
        got = dsh_env.clean_stale_locks(log=msgs.append)
        expect(got == [], "探测失败时应返回空列表，实际 %r" % (got,))
        expect(any("netstat" in m and "失败" in m for m in msgs),
               "应打印 netstat 失败提示，实际日志：%r" % (msgs,))
    finally:
        dsh_env.netstat_table = real
        dsh_env._netstat_cache["ok"] = real_ok


@case("clean_stale_locks：PID 被复用给非 node 的锁必须清掉（2026-09-20 真故障）")
def t_clean_lock_pid_reuse():
    """端到端复现用户实际踩到的故障。

    强杀 dsh 留下的锁记着 pid X，系统把 X 复用给了别的程序
    （实测：31848 → Nahimic3.exe）。旧逻辑只看 `pid_alive` → 判「锁正常」
    → 启动器跳过清锁 → 新 dsh 抢 task-board 锁失败
    → `plugin tree failed to load: failed to apply loader entry ui-task-board`
    → dsh 退出码 1（用户看到的就是「第 1 次尝试直接崩」）。

    本用例在 temp 里伪造整套环境，验证新逻辑能认出来并清掉，
    且备份里的 token 已脱敏。
    """
    d = tempfile.mkdtemp(prefix="dsh_t_lockreuse_")
    real = {}
    for k in ("DSH_STATE", "netstat_table", "is_dsh_here",
              "dsh_running_any_port", "image_names"):
        real[k] = getattr(dsh_env, k)
    real_ok = dsh_env._netstat_cache.get("ok", True)
    try:
        sub = os.path.join(d, "task-board")
        os.makedirs(sub, exist_ok=True)
        fp = os.path.join(sub, "ledger-v2.lock")
        me = os.getpid()
        with open(fp, "w", encoding="utf-8") as f:
            json.dump({"pid": me, "token": "SUPER-SECRET-TOKEN"}, f)
        # mtime 拨到 10 秒前，绕开 _too_fresh（那是保护「正在写入的活锁」的）
        old = time.time() - 10
        os.utime(fp, (old, old))

        dsh_env.DSH_STATE = d
        dsh_env.netstat_table = lambda *a, **k: dsh_env._netstat_cache.get("rows", [])
        dsh_env._netstat_cache["ok"] = True          # netstat 探测成功
        dsh_env.is_dsh_here = lambda p: False        # 没有 dsh 在跑
        dsh_env.dsh_running_any_port = lambda: False
        dsh_env.image_names = lambda: {str(me): "nahimic3.exe"}   # ← PID 被复用

        msgs = []
        got = dsh_env.clean_stale_locks(log=msgs.append)

        expect(len(got) == 1,
               "PID 复用的脏锁必须被清理，实际 cleaned=%r，日志=%r" % (got, msgs))
        expect(not os.path.exists(fp), "锁文件应已被删除")
        baks = [f for f in os.listdir(sub) if ".bak-stale-" in f]
        expect(len(baks) == 1, "删除前必须留一份备份，实际 %r" % (baks,))
        body = open(os.path.join(sub, baks[0]), "rb").read().decode("utf-8", "replace")
        expect("SUPER-SECRET-TOKEN" not in body, "备份里的 token 必须已脱敏")
        expect("<redacted>" in body, "备份应含 <redacted> 占位，实际 %r" % body)
    finally:
        for k, v in real.items():
            setattr(dsh_env, k, v)
        dsh_env._netstat_cache["ok"] = real_ok
        shutil.rmtree(d, ignore_errors=True)


@case("clean_stale_locks：pid 是活 node 时不许清（防误删活 dsh 的锁）")
def t_clean_lock_live_node_guard():
    """反向守护：如果锁记的 pid 确实是活着的 node.exe，绝不能清。

    这是「宁可漏判一轮，也不能误删活锁」的红线在端到端链路上的体现。
    """
    d = tempfile.mkdtemp(prefix="dsh_t_locklive_")
    real = {}
    for k in ("DSH_STATE", "netstat_table", "is_dsh_here",
              "dsh_running_any_port", "image_names"):
        real[k] = getattr(dsh_env, k)
    real_ok = dsh_env._netstat_cache.get("ok", True)
    try:
        sub = os.path.join(d, "task-board")
        os.makedirs(sub, exist_ok=True)
        fp = os.path.join(sub, "ledger-v2.lock")
        me = os.getpid()
        with open(fp, "w", encoding="utf-8") as f:
            json.dump({"pid": me, "token": "t"}, f)
        old = time.time() - 10
        os.utime(fp, (old, old))

        dsh_env.DSH_STATE = d
        dsh_env.netstat_table = lambda *a, **k: dsh_env._netstat_cache.get("rows", [])
        dsh_env._netstat_cache["ok"] = True
        dsh_env.is_dsh_here = lambda p: False
        dsh_env.dsh_running_any_port = lambda: False
        dsh_env.image_names = lambda: {str(me): "node.exe"}       # ← 活的 node

        msgs = []
        got = dsh_env.clean_stale_locks(log=msgs.append)
        expect(got == [], "活 node 的锁不该被清，实际 %r" % (got,))
        expect(os.path.exists(fp), "锁文件必须还在")
    finally:
        for k, v in real.items():
            setattr(dsh_env, k, v)
        dsh_env._netstat_cache["ok"] = real_ok
        shutil.rmtree(d, ignore_errors=True)


@case("live_now：端口集合必须覆盖实际配置端口（不止 3080/3081/3082）")
def t_live_now_ports():
    import importlib.util as _ilu
    _s = _ilu.spec_from_file_location('dsh_launcher2',
                                      os.path.join(T, 'dsh-launcher.py'))
    L = _ilu.module_from_spec(_s)
    _s.loader.exec_module(L)

    # 记录被探活的端口
    probed = []
    real = dsh_env.is_dsh_here

    def spy(p):
        probed.append(p)
        return False
    dsh_env.is_dsh_here = spy
    try:
        # [!] 探测端口必须挑一个**不在 DEFAULT_PORTS 里**的 —— 用 3000 会漏网，
        #     因为 3000 本来就在默认表里，变异成「只看默认端口」照样通过（实测漏网）。
        custom = 9999
        for d in dsh_env.DEFAULT_PORTS:
            if custom == d:
                custom += 1
        expect(custom not in dsh_env.DEFAULT_PORTS, "自选端口不应落在默认表里")
        fake_env = {"port": {"value": custom, "source": "config", "alive": False}}
        L.live_now(fake_env)
        expect(custom in probed,
               "必须探活实际配置端口 %d，实际探了 %r（旧实现只看 DEFAULT_PORTS）"
               % (custom, probed))
        for d in dsh_env.DEFAULT_PORTS:
            expect(d in probed, "默认端口 %d 也应被探活，实际 %r" % (d, probed))
    finally:
        dsh_env.is_dsh_here = real



# ============================================================
# 9. 2026-09-19 安全审查修复守护（T1/T2/F2/F4/F7/F8/T9/T11）
# ============================================================
def _load_launcher(name):
    import importlib.util as _ilu
    _s = _ilu.spec_from_file_location(name, os.path.join(T, 'dsh-launcher.py'))
    L = _ilu.module_from_spec(_s)
    _s.loader.exec_module(L)
    return L


@case("实例互斥：二拿被拒、释放后可再拿、死 pid 残锁自愈（T1/F11）")
def t_instance_mutex():
    L = _load_launcher('dsh_launcher_mut')
    lock = os.path.join(tempfile.mkdtemp(prefix="dsh_t_mut_"), "lock")
    real_lock, real_log = L.INSTANCE_LOCK, L.log
    L.INSTANCE_LOCK = lock
    L.log = lambda m: None
    try:
        expect(L.acquire_instance_lock() is True, "首拿应成功")
        expect(os.path.exists(lock), "锁文件应存在")
        expect(L.acquire_instance_lock() is False, "二拿应被拒")
        L.release_instance_lock()
        expect(not os.path.exists(lock), "释放后锁应消失")
        # 上次崩溃的残锁：记录的 pid 已死 → 清掉重拿成功
        open(lock, "w").write("999999")
        expect(L.acquire_instance_lock() is True, "死 pid 残锁应自愈重拿")
        L.release_instance_lock()
        # 活 pid 残锁：必须拒绝（真有另一实例在跑）
        open(lock, "w").write(str(os.getpid()))
        expect(L.acquire_instance_lock() is False, "活 pid 残锁应被拒")
        L.release_instance_lock()
    finally:
        L.INSTANCE_LOCK = real_lock
        L.log = real_log
        try:
            os.remove(lock)
        except OSError:
            pass


@case("kill_leftover：taskkill 超时/失败不炸流程，只记日志（T2/F1）")
def t_kill_guard():
    L = _load_launcher('dsh_launcher_k')
    env = {"port": {"value": 3080, "source": "t", "alive": True}}
    real = (L.find_live_ports, L.dsh_env.verified_listening_pids,
            L.dsh_env.pid_is_node, L.dsh_env.image_names, L.log)
    logs = []
    try:
        L.find_live_ports = lambda e: [3080]
        L.dsh_env.verified_listening_pids = lambda p: {4321}
        L.dsh_env.pid_is_node = lambda pid, names=None: True
        L.dsh_env.image_names = lambda: {}
        L.log = logs.append

        class _Boom:
            @staticmethod
            def run(*a, **k):
                raise subprocess.TimeoutExpired(cmd="taskkill", timeout=30)
        real_sub = L.subprocess
        L.subprocess = _Boom
        try:
            killed = L.kill_leftover(env)   # 旧版在这里 TimeoutExpired 炸穿
        finally:
            L.subprocess = real_sub
        expect(killed == [], "超时不应记入 killed")
        expect(any("taskkill" in m and "失败" in m for m in logs),
               "应打印 taskkill 失败提示：%r" % (logs,))
    finally:
        (L.find_live_ports, L.dsh_env.verified_listening_pids,
         L.dsh_env.pid_is_node, L.dsh_env.image_names, L.log) = real


@case("清锁新鲜度+脱敏：mtime<5s 不删、老死锁照清、备份 token 指纹化（F2/F4）")
def t_clean_lock_fresh():
    d = tempfile.mkdtemp(prefix="dsh_t_fresh_")
    real = (dsh_env.DSH_STATE, dsh_env._netstat_cache.get("ok", True),
            dsh_env.is_dsh_here, dsh_env.dsh_running_any_port)
    try:
        dsh_env.DSH_STATE = d
        dsh_env._netstat_cache["ok"] = True
        dsh_env.is_dsh_here = lambda p: False
        dsh_env.dsh_running_any_port = lambda: False
        sub = os.path.join(d, "task-board")
        os.makedirs(sub)
        fresh = os.path.join(sub, "fresh.lock")
        open(fresh, "wb").close()                      # 0 字节 + mtime 刚刚
        old = os.path.join(sub, "old.lock")
        open(old, "w", encoding="utf-8").write(
            json.dumps({"pid": 999999, "token": "secret-value"}))
        old_ts = time.time() - 7200
        os.utime(old, (old_ts, old_ts))
        cleaned = dsh_env.clean_stale_locks(log=lambda m: None)
        expect(os.path.exists(fresh), "新鲜锁（0 字节但 mtime 刚刚）绝不能删")
        expect(not os.path.exists(old), "老死锁应被清理")
        baks = [x for x in os.listdir(sub) if x.startswith("old.lock.bak-stale-")]
        expect(len(baks) == 1, "应恰好 1 份备份：%r" % (os.listdir(sub),))
        data = open(os.path.join(sub, baks[0]), "rb").read()
        expect(b"secret-value" not in data, "备份里的 token 必须已脱敏")
        expect(b"<redacted>" in data, "token 应替换为 <redacted>：%r" % data)
        expect(len(cleaned) == 1 and cleaned[0][0].endswith("old.lock"),
               "清理清单应只含 old.lock：%r" % (cleaned,))
    finally:
        (dsh_env.DSH_STATE, dsh_env._netstat_cache["ok"],
         dsh_env.is_dsh_here, dsh_env.dsh_running_any_port) = real
        shutil.rmtree(d, ignore_errors=True)


@case("set_disabled：不安全 eid（YAML 指示符/空白/控制符）拒绝且不写（F8）")
def t_eid_guard():
    tmp = tempfile.mkdtemp(prefix="dsh_t_eid_")
    try:
        p = _copy_patch(tmp)
        before = open(p, "rb").read()
        for bad in ("*x", "|", "a b", "x\ny", "!x", "&x", "{x}"):
            ok, note = dsh_plugins.set_disabled(p, bad, True)
            expect(ok is False, "eid %r 应被拒绝：%s" % (bad, note))
        expect(open(p, "rb").read() == before, "被拒后文件必须原封不动")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@case("split_win_cmdline：空参数 / Unicode 空白 / 中缀引号 / 空输入（T11）")
def t_split_win_edge():
    expect(dsh_env.split_win_cmdline('prog "" x') == ["prog", "", "x"],
           "独立 \"\" 是空参数，实得 %r" % (dsh_env.split_win_cmdline('prog "" x'),))
    got = dsh_env.split_win_cmdline('foo"bar baz"qux')
    expect(got == ["foobar bazqux"],
           "中缀引号不该切断 token，实得 %r" % (got,))
    got = dsh_env.split_win_cmdline("a" + chr(0xa0) + "b c")
    expect(got == ["a" + chr(0xa0) + "b", "c"],
           "NBSP 不是分隔符（旧版 isspace 会切碎），实得 %r" % (got,))
    expect(dsh_env.split_win_cmdline("") == [], "空输入应得空列表")
    expect(dsh_env.split_win_cmdline("   ") == [], "纯空白应得空列表")


@case("dump 缓存：内容与 meta 长度不符必须弃用重取（T9/F12）")
def t_dumpcache_size():
    d = tempfile.mkdtemp(prefix="dsh_t_dmp_")
    repo = os.path.join(d, "repo")
    os.makedirs(repo)
    open(os.path.join(repo, "package.json"), "w", encoding="utf-8").write("{}")
    prof = os.path.join(d, "profiles", "web")
    os.makedirs(prof)
    open(os.path.join(prof, "package.json"), "w", encoding="utf-8").write("{}")
    real = (dsh_env.DUMPC, dsh_env.DSH_STATE, dsh_env._run)
    notes = []
    try:
        dsh_env.DUMPC = os.path.join(d, "dump.txt")
        dsh_env.DSH_STATE = d
        calls = {"n": 0}

        def fake_run(argv, **k):
            calls["n"] += 1
            return 0, ("LINE-%d " % calls["n"]) * 40, ""   # >200 字符

        dsh_env._run = fake_run
        t1 = dsh_env.dump_config(repo, "web", notes)
        expect(t1 and "LINE-1" in t1, "首次应真取并返回 LINE-1")
        # 模拟「半截写入」：缓存被截断，meta 仍是旧指纹
        with open(dsh_env.DUMPC, "w", encoding="utf-8") as f:
            f.write("GARBAGE")
        t2 = dsh_env.dump_config(repo, "web", notes)
        expect(t2 and "GARBAGE" not in t2,
               "长度不符的缓存必须弃用（旧版会把 GARBAGE 当权威树）")
        expect(t2 and "LINE-2" in t2, "弃用后应重新取数，实得 %r" % (t2 or "")[:50])
    finally:
        dsh_env.DUMPC, dsh_env.DSH_STATE, dsh_env._run = real
        shutil.rmtree(d, ignore_errors=True)


# ============================================================
# 8. 入口 bat：工具链目录定位（2026-09-26 去硬编码）
# ============================================================
# 背景：旧版把 TOOLS 写死成 %USERPROFILE%\dsh-launcher，与 README 的
# 「零硬编码路径、整个目录可以随意搬动」自相矛盾 —— 换目录 clone 之后，
# 桌面副本只会报「找不到启动器」。
# 这里用【真 cmd.exe 跑真 bat】验证三级解析：显式 DSH_TOOLS > 就近 %~dp0
# > 用户目录兜底，以及三处都没有时必须明确报错（不静默）。
#
# 手法与 dsh-accept.py 一致：把 bat 的【最后一行启动命令】截掉，
# 换成 echo TOOLS=[%TOOLS%] —— 既不拉起 dsh，又能拿到真实解析结果。


def _bat_probe_head():
    """取仓库 start-dsh.bat 去掉最后一行（真正的启动命令）后的行。"""
    p = os.path.join(T, "start-dsh.bat")
    data = open(p, "rb").read()
    lines = data.split(b"\r\n")
    n = len(lines) - 1 if lines and lines[-1] == b"" else len(lines)
    return lines[:n - 1]


def _stub_launcher(d, nested=False):
    """造占位 dsh-launcher.py —— bat 只做存在性判断，不会真去跑它。"""
    target = os.path.join(d, "dsh-launcher", "dsh-launcher.py") if nested \
        else os.path.join(d, "dsh-launcher.py")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    open(target, "wb").close()


def _probe_tools(bat_dir, env_extra):
    """在 bat_dir 放一份探测 bat 跑一遍，返回 (rc, TOOLS 值, stdout)。"""
    head = _bat_probe_head()
    gp = os.path.join(bat_dir, "probe.bat")
    open(gp, "wb").write(b"\r\n".join(head)
                         + b"\r\necho TOOLS=[%TOOLS%]\r\nexit /b 0\r\n")
    env = os.environ.copy()
    env.pop("DSH_TOOLS", None)          # 默认场景：外部没设过
    env.update(env_extra)
    r = subprocess.run(["cmd.exe", "/c", gp], capture_output=True,
                       stdin=subprocess.DEVNULL, timeout=60, env=env)
    out = r.stdout.decode("gbk", "replace")
    val = ""
    for l in out.splitlines():
        if "TOOLS=[" in l:
            val = l.split("TOOLS=[", 1)[1].split("]", 1)[0]
    return r.returncode, val, out


@case("bat 定位 TOOLS：就近（%~dp0）优先 —— 目录可随意搬动")
def t_bat_tools_colocated():
    a = tempfile.mkdtemp(prefix="dsh_bat_a_")
    b = tempfile.mkdtemp(prefix="dsh_bat_b_")
    try:
        _stub_launcher(a)                     # 与 bat 同目录
        _stub_launcher(b, nested=True)        # 用户目录兜底位
        rc, val, out = _probe_tools(a, {"USERPROFILE": b})
        expect(rc == 0, "探测 bat 应 rc=0，实际 %d：%s" % (rc, out[-200:]))
        expect(os.path.normcase(val) == os.path.normcase(a),
               "应优先用 bat 所在目录 %s（可搬移），实得 %r" % (a, val))
        expect(not val.endswith("\\"),
               "TOOLS 不应带尾部反斜杠（否则拼出双反斜杠），实得 %r" % val)
    finally:
        shutil.rmtree(a, ignore_errors=True)
        shutil.rmtree(b, ignore_errors=True)


@case("bat 定位 TOOLS：DSH_TOOLS 显式覆盖优先于就近与兜底")
def t_bat_tools_env_override():
    a = tempfile.mkdtemp(prefix="dsh_bat_a_")
    b = tempfile.mkdtemp(prefix="dsh_bat_b_")
    c = tempfile.mkdtemp(prefix="dsh_bat_c_")
    try:
        _stub_launcher(a)
        _stub_launcher(b, nested=True)
        _stub_launcher(c)                     # 显式指定的那个
        rc, val, out = _probe_tools(a, {"USERPROFILE": b, "DSH_TOOLS": c})
        expect(rc == 0, "探测 bat 应 rc=0，实际 %d：%s" % (rc, out[-200:]))
        expect(os.path.normcase(val) == os.path.normcase(c),
               "DSH_TOOLS=%s 应压过就近 %s，实得 %r" % (c, a, val))
    finally:
        shutil.rmtree(a, ignore_errors=True)
        shutil.rmtree(b, ignore_errors=True)
        shutil.rmtree(c, ignore_errors=True)


@case("bat 定位 TOOLS：bat 旁边没有时回退 %USERPROFILE%\\dsh-launcher（桌面副本场景）")
def t_bat_tools_userprofile_fallback():
    a = tempfile.mkdtemp(prefix="dsh_bat_a_")     # 故意不放 dsh-launcher.py
    b = tempfile.mkdtemp(prefix="dsh_bat_b_")
    try:
        _stub_launcher(b, nested=True)
        rc, val, out = _probe_tools(a, {"USERPROFILE": b})
        expect(rc == 0, "兜底命中时应 rc=0，实际 %d：%s" % (rc, out[-200:]))
        want = os.path.join(b, "dsh-launcher")
        expect(os.path.normcase(val) == os.path.normcase(want),
               "应回退到 %s（桌面副本的老行为，必须向后兼容），实得 %r"
               % (want, val))
    finally:
        shutil.rmtree(a, ignore_errors=True)
        shutil.rmtree(b, ignore_errors=True)


@case("bat 定位 TOOLS：三处都没有 -> 明确报错退出（不静默、不乱跑）")
def t_bat_tools_not_found():
    a = tempfile.mkdtemp(prefix="dsh_bat_a_")
    b = tempfile.mkdtemp(prefix="dsh_bat_b_")
    try:
        rc, val, out = _probe_tools(a, {"USERPROFILE": b})
        expect(rc != 0, "三处都没有时应非 0 退出，实际 rc=%d" % rc)
        expect("找不到启动器" in out,
               "应打印可读报错（而不是静默失败），实际 %r" % out[-200:])
    finally:
        shutil.rmtree(a, ignore_errors=True)
        shutil.rmtree(b, ignore_errors=True)


# ============================================================
# 运行
# ============================================================
def main():
    log("=" * 74)
    log("  dsh 工具链回归测试")
    log("=" * 74)
    log()
    g = globals()
    tests = [v for k, v in sorted(g.items())
             if k.startswith("t_") and callable(v)]
    for fn in tests:
        fn()
    log()
    log("-" * 74)
    log("  用例 %d 个，失败 %d 个" % (COUNT[0], len(FAILED)))
    if FAILED:
        for f in FAILED:
            log("    [x] %s" % f)
        log("=" * 74)
        return 1
    log("  全部通过")
    log("=" * 74)
    return 0


@case("层6：排除端口范围解析 + 命中判断（含边界值）")
def t_l6_excluded_ranges():
    sample = (
        "\n协议 tcp 端口排除范围\n\n开始端口    结束端口\n"
        "----------    --------\n      1183        1282\n"
        "      3066        3165\n      50000       50059     *\n"
        "\n* - 管理的端口排除。\n")
    ranges = dsh_env._parse_excluded_ranges(sample)
    expect((1183, 1282) in ranges, "应解析出 (1183,1282)")
    expect((3066, 3165) in ranges, "应解析出 (3066,3165)")
    expect((50000, 50059) in ranges, "带 * 的管理性排除也要解析出")
    expect(len(ranges) == 3, "标题/分隔线/说明行不应产生假范围（实际 %r）" % ranges)
    expect(dsh_env._port_in_ranges(3080, ranges) is True, "3080 落在 [3066,3165] → 命中")
    expect(dsh_env._port_in_ranges(3066, ranges) is True, "边界值 3066（闭区间）→ 命中")
    expect(dsh_env._port_in_ranges(3165, ranges) is True, "边界值 3165（闭区间）→ 命中")
    expect(dsh_env._port_in_ranges(3300, ranges) is False, "3300 不在任何范围 → 不命中")
    expect(dsh_env._port_in_ranges(1283, ranges) is False, "1283 恰在两范围缝隙 → 不命中")


if __name__ == "__main__":
    # dsh-plugins.py 文件名带连字符，需用 importlib 加载
    import importlib.util
    _spec = importlib.util.spec_from_file_location(
        "dsh_plugins", os.path.join(T, "dsh-plugins.py"))
    _m = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_m)
    globals()["dsh_plugins"] = _m
    sys.exit(main())
