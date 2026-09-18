# -*- coding: utf-8 -*-
"""
dsh 工具链回归测试（独立可跑，无需 pytest）
==========================================
    python dsh_tests.py            # 跑全部用例
    python dsh_tests.py -v         # 显示每个用例名

原则：**所有写操作都在 tempfile 副本上做**，绝不碰真实补丁与真实 dsh。
这就是过去"改完说修好了、一跑就崩"的解药 —— 用例固化后可反复回归。
"""
import os, sys, json, time, shutil, tempfile, textwrap

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
        expect(not os.path.exists(p + ".tmp-plugins"), "无 tmp 残留")
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
@case("run_heal：正常状态（缺 65/285）跳过，deep=True 强制")
def t_run_heal():
    import importlib.util as _ilu
    _s = _ilu.spec_from_file_location('dsh_launcher', os.path.join(T, 'dsh-launcher.py'))
    L = importlib.util.module_from_spec(_s)
    _s.loader.exec_module(L)
    env = L.probe()
    if not env or not (env.get("features") or {}).get("has_fallback"):
        log("    （本机没有 fallback 机制，跳过此用例）")
        return
    ok, msg = L.run_heal(env)                       # 启动路径
    expect("跳过" in msg or "完整" in msg, "启动路径应跳过：%s" % msg)
    ok2, msg2 = L.run_heal(env, deep=True)          # 手动路径
    expect(ok2, "deep=True 应执行：%s" % msg2)


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
