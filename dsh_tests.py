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
@case("lock_is_stale：0 字节 / 畸形 JSON / dead-PID / 活 pid / 非 JSON 数字")
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

        f3 = os.path.join(d, "d.lock")
        open(f3, "w", encoding="utf-8").write(
            json.dumps({"pid": os.getpid(), "token": "x"}))
        stale, why = dsh_env.lock_is_stale(f3)
        expect(not stale, "活 pid 不应判脏，实际 %r" % why)

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
        (r"C:\Users\26672\AppData\Roaming\npm\pnpm.cmd dsh web",
         [r"C:\Users\26672\AppData\Roaming\npm\pnpm.cmd", "dsh", "web"]),
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
    expect(r"C:\Users\26672\x.cmd" in got[0] or "\\" in got[0],
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


if __name__ == "__main__":
    # dsh-plugins.py 文件名带连字符，需用 importlib 加载
    import importlib.util
    _spec = importlib.util.spec_from_file_location(
        "dsh_plugins", os.path.join(T, "dsh-plugins.py"))
    _m = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_m)
    globals()["dsh_plugins"] = _m
    sys.exit(main())
