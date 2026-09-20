# -*- coding: utf-8 -*-
"""
dsh 环境探测层（可 import 的单例模块）
======================================
本模块是整个工具链**唯一**认识 dsh 内部细节的地方：
仓库定位 / profile 定位 / 端口探活 / 启动命令 / 权威插件树 / 进程与锁管理。

命名说明：文件名是 `dsh_env.py`（下划线），这样 launcher / plugins 可以直接
`import dsh_env`，**三处共享同一个模块实例**（_MEMO 只有一份，结论一致）。
`dsh-env.py`（连字符）只是给命令行用的薄壳。

设计约定：
  * 所有子进程调用都经 `_run()`：永不抛异常、默认 PATH 钉扎、必带 timeout。
  * netstat/tasklist 只跑一次，结果存 `_netstat_cache`（TTL 3 秒）共享给
    listening_ports / listening_pids / node_listening_ports —— 一次启动预检
    从 3-4 次 netstat 降到 1 次。
  * detect() 的缓存命中返回**深拷贝**，调用方随便改都不会污染缓存。
"""
import os, sys, json, time, glob, copy, shutil, socket, threading, subprocess, shlex
import http.client
import re as _re_mod

HOME      = os.path.expanduser("~")
# 本模块自己的目录：配置/缓存都放在这里，文件夹挪到哪都能跑
TOOLS     = os.path.dirname(os.path.abspath(__file__))
CFG       = os.path.join(TOOLS, "dsh-config.json")
CACHE     = os.path.join(TOOLS, "dsh-env.cache.json")
DUMPC     = os.path.join(TOOLS, "dsh-dump.cache.txt")
DSH_STATE = os.environ.get("DSH_HOME") or os.path.join(HOME, ".dsh")

FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

DEFAULT_PORTS = [3080, 3081, 3082, 3000, 8080, 8000, 5173]
WEB_WORDS     = ("web", "serve", "start", "dev", "ui", "http")
NETSTAT_TTL   = 3.0            # 秒；同一次启动预检里重复调用直接吃缓存

try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

# [!] id 捕获整段剩余（旧版 [^\s]+ 会把 `- id: a b` 这类含空格 id 整行丢掉）
_re_id   = _re_mod.compile(r"^- id:\s*(.+?)\s*$")
_re_name = _re_mod.compile(r"^name:\s*(.+)$")
_re_dis  = _re_mod.compile(r"^disabled:\s*(.+)$")


# ============================ 基础工具 ============================
def pinned_env():
    """PATH 钉扎：System32 最前 + 补 node/pnpm 落点。

    不钉扎时 `pnpm` 会报『不是内部或外部命令』，`dsh --help` 探测直接失败。
    """
    env = os.environ.copy()
    pins = [r"C:\WINDOWS\system32", r"C:\WINDOWS",
            r"C:\WINDOWS\System32\Wbem",
            r"C:\WINDOWS\System32\WindowsPowerShell\v1.0"]
    extra = [os.path.join(HOME, "AppData", "Roaming", "npm"),
             os.path.join(HOME, "AppData", "Local", "pnpm"),
             r"C:\Program Files\nodejs"]
    env["PATH"] = ";".join(pins + [env.get("PATH", "")] + extra)
    return env


def _run(argv, timeout=30, cwd=None, pin=True, try_utf8=False):
    """永不抛异常的 subprocess.run；默认带 PATH 钉扎。

    try_utf8=True：输出先按 UTF-8 严格解码、失败再退 GBK —— 专给 node
    子进程用（dump-config 输出是 UTF-8），避免非 ASCII 入口 id 变乱码
    导致「切换显示 [OK] 实为空操作」；netstat/tasklist 等系统工具仍走 GBK。
    """
    def dec(b):
        b = b or b""
        if try_utf8:
            try:
                return b.decode("utf-8")
            except UnicodeDecodeError:
                pass
        return b.decode("gbk", "replace")

    try:
        r = subprocess.run(argv, capture_output=True, timeout=timeout, cwd=cwd,
                           env=pinned_env() if pin else None,
                           creationflags=FLAGS)
        return (r.returncode, dec(r.stdout), dec(r.stderr))
    except Exception as e:
        return -1, "", str(e)


def _read_json(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _read_text(p):
    try:
        with open(p, "rb") as f:
            return f.read().decode("utf-8", "replace")
    except Exception:
        return ""


def load_config():
    """读 dsh-config.json；缺字段/类型不对一律 None = 自动探测。

    [!] 每个字段都必须验类型 —— 配置是手编文件，`start_command: 123` 这类
        笔记曾一路炸到探测层把菜单打崩。
    """
    c = _read_json(CFG) or {}
    if not isinstance(c, dict):
        c = {}
    pc = c.get("port_candidates")
    return {
        "repo": c.get("repo") if isinstance(c.get("repo"), str) else None,
        "profile": c.get("profile") if isinstance(c.get("profile"), str) else None,
        "port": c.get("port") if isinstance(c.get("port"), int) else None,
        "start_command": c.get("start_command")
                         if isinstance(c.get("start_command"), str) else None,
        "port_candidates": [p for p in pc if isinstance(p, int)]
                           if isinstance(pc, list) else [],
    }


def pkg_manager(repo):
    """按仓库里的锁文件推断包管理器。"""
    if not repo:
        return "pnpm"
    if os.path.exists(os.path.join(repo, "pnpm-workspace.yaml")) or \
       os.path.exists(os.path.join(repo, "pnpm-lock.yaml")):
        return "pnpm"
    if os.path.exists(os.path.join(repo, "yarn.lock")):
        return "yarn"
    if os.path.exists(os.path.join(repo, "package-lock.json")):
        return "npm"
    return "pnpm"


def find_node():
    """node.exe 绝对路径（找不到就交给 PATH）。"""
    for cand in (r"C:\Program Files\nodejs\node.exe",
                 r"C:\Program Files (x86)\nodejs\node.exe",
                 os.path.join(HOME, "AppData", "Roaming", "npm", "node.exe"),
                 os.path.join(HOME, "AppData", "Local", "Programs", "nodejs",
                              "node.exe")):
        if os.path.exists(cand):
            return cand
    return "node"


def list_pkgs(root):
    """列出 node_modules 里的包名（含 @scope/name）。三处共用这一份实现。"""
    out = set()
    if not root or not os.path.isdir(root):
        return out
    try:
        names = os.listdir(root)
    except OSError:
        return out
    for name in names:
        if name.startswith("."):
            continue
        p = os.path.join(root, name)
        if not os.path.isdir(p):
            continue
        if name.startswith("@"):
            try:
                for s in os.listdir(p):
                    if not s.startswith("."):
                        out.add(name + "/" + s)
            except OSError:
                pass
        else:
            out.add(name)
    return out


# ============================ cmd.exe 安全闸 ============================
# cmd 对【引号内】参数仍做 %VAR% 展开（本机实测），且 & | < > ^ ! " 与
# 控制字符任何一个都会改变命令语义（0x1A 还会静默终止 cmd 的 stdin 读取）。
# 凡送进 cmd.exe（参数列表或 stdin 脚本）的路径/名字必须先过这道闸；
# 不过闸的条目跳过并记日志，绝不硬拼。
_CMD_UNSAFE = _re_mod.compile(r"[\x00-\x1f\x7f%&|<>^!\"]")


def cmd_arg_safe(p):
    """路径/名字能否安全送进 cmd.exe（参数列表或 stdin 脚本）。"""
    return isinstance(p, str) and _CMD_UNSAFE.search(p) is None


# ============================ 进程 / 端口 ============================
_netstat_cache = {"ts": 0.0, "rows": [], "ok": True}
_netstat_lock = threading.Lock()


def netstat_table(refresh=False):
    """一次 netstat 拿到全部 LISTENING 行 → [{port, pid}]，带 3 秒 TTL 缓存。

    整个工具链所有端口相关判断都吃这一份快照，
    把「一次启动预检 3-4 次 netstat」压到 1 次。
    另记录最近一次是否成功（`ok`）——清脏锁这类「判断错就删文件」的
    操作必须先确认探测本身没失败，否则宁可不动作。
    """
    now = time.time()
    with _netstat_lock:
        if not refresh and (now - _netstat_cache["ts"]) < NETSTAT_TTL:
            return _netstat_cache["rows"]
        rc, txt, _ = _run(["netstat", "-ano"], timeout=20, pin=False)
        rows = []
        if rc == 0:
            for l in txt.splitlines():
                parts = l.split()
                if len(parts) < 5 or "LISTENING" not in parts:
                    continue
                try:
                    port = int(parts[1].rsplit(":", 1)[-1])
                except Exception:
                    continue
                pid = parts[-1]
                if pid.isdigit():
                    rows.append({"port": port, "pid": int(pid)})
        _netstat_cache["ts"] = time.time()
        _netstat_cache["rows"] = rows
        _netstat_cache["ok"] = (rc == 0)
        return rows


def listening_ports():
    """当前处于 LISTENING 的本地端口集合。"""
    return {r["port"] for r in netstat_table()}


def listening_pids(port):
    """占用该端口（LISTENING）的 PID 集合（可能来自 ≤3 秒的缓存快照）。"""
    return {r["pid"] for r in netstat_table() if r["port"] == port}


def verified_listening_pids(port):
    """击杀专用：强制刷新 netstat 后再取端口 PID。

    [!] 绝不在 ≤3 秒的陈旧快照上动手 —— 旧表里的 PID 可能已被系统复用，
        后续任何 pid_is_node 复核都救不回「一开始就查错了人」。
    """
    netstat_table(refresh=True)
    return listening_pids(port)


def image_names():
    """一次 tasklist：PID → 进程名（小写）。"""
    names = {}
    try:
        r = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                           capture_output=True, timeout=30, creationflags=FLAGS)
        out = (r.stdout or b"").decode("gbk", "replace")
        for l in out.splitlines():
            if not l.startswith('"'):
                continue
            f = [x.strip('"') for x in l.split('","')]
            if len(f) >= 2 and f[1].isdigit():
                names[f[1]] = f[0].lower()
    except Exception:
        pass
    return names


def pid_is_node(pid, names=None):
    """PID 当前是否还是 node.exe —— 一切 taskkill /F 前的最后一道闸。

    「端口签名探活 → 取 PID → 击杀」之间隔着几百毫秒，进程可能刚好退出、
    PID 被系统复用给别的程序；不复核就会误杀无辜进程。
    查不到映像名时一律按「不是 node」处理（宁可放过）。
    """
    if names is None:
        names = image_names()
    return names.get(str(pid)) == "node.exe"


def node_listening_ports():
    """所有由 node.exe 监听的端口（dsh 一定是 node 进程）。"""
    names = image_names()
    return sorted({r["port"] for r in netstat_table()
                   if names.get(str(r["pid"])) == "node.exe"})


def pid_alive(pid):
    """PID 是否存活。查不动时返回 None（调用方须保守处理）。"""
    try:
        pid = int(pid)
    except Exception:
        return None
    if pid <= 0:
        return False
    try:
        r = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid,
                            "/FO", "CSV", "/NH"],
                           capture_output=True, timeout=25, creationflags=FLAGS)
        out = (r.stdout or b"").decode("gbk", "replace")
        rows = [l for l in out.splitlines() if l.startswith('"')]
        for l in rows:
            f = [x.strip('"') for x in l.split('","')]
            if len(f) >= 2 and f[1] == str(pid):
                return True
        return False
    except Exception:
        return None


def _probe_http(port, timeout=1):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        c.request("GET", "/")
        r = c.getresponse()
        body = r.read(2048)
        return r.status, body
    finally:
        try:
            c.close()
        except Exception:
            pass


def is_dsh_here(port):
    """该端口上跑的是不是 dsh —— 必须严格。

    只认一个签名：HTTP 401 + 正文含 "authentication required" + 含 "dsh web"。
    绝不能只看正文里有没有 "dsh" 三个字母，那会误杀用户的服务。
    """
    s = socket.socket()
    try:
        s.settimeout(1.2)
        s.connect(("127.0.0.1", port))
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass
    try:
        st, body = _probe_http(port)
        low = body.decode("utf-8", "replace").lower()
        # [!] 第三条必须是 "dsh web"（正文原文如此，已实测）——放宽成 "dsh"
        #     会把任何恰好 401 且正文带 dsh 字样的本地服务误判成 dsh，
        #     kill_leftover / kill_node 就敢对它 taskkill /F 了
        return (st == 401 and "authentication required" in low
                and "dsh web" in low)
    except Exception:
        return False


def find_port(cfg, notes):
    """从候选端口里找出正在跑的 dsh。"""
    live = listening_ports()
    cands, seen = [], set()
    if cfg.get("port"):
        cands.append((cfg["port"], "config"))
    cands += [(p, "known") for p in DEFAULT_PORTS]
    cands += [(p, "config-extra") for p in cfg.get("port_candidates", [])]

    for port, src in cands:
        if port in seen:
            continue
        seen.add(port)
        if port not in live:
            continue
        if is_dsh_here(port):
            notes.append("端口 %d 上检测到正在运行的 dsh" % port)
            return {"value": port, "source": src, "alive": True}

    val = cfg.get("port") or DEFAULT_PORTS[0]
    return {"value": val, "source": "config" if cfg.get("port") else "default",
            "alive": False}


# ============================ 锁管理 ============================
def pid_alive_guard(pid):
    return pid_alive(pid)


def lock_is_stale(fp, names=None):
    """锁文件是否为脏。四种情形都算：
      ① 0 字节；② 内容不是合法 JSON（残片）；
      ③ 合法 JSON 但里面记录的 pid 已经死了（强杀后最常留下的形态）；
      ④ 合法 JSON、pid 也活着，但**那个 pid 已被系统复用给别的程序**（不是 node.exe）。
    返回 (是否脏, 原因)。

    [!] ④ 是 2026-09-20 补的，此前的漏判会造成真实故障：
        强杀 dsh 留下的锁记着 pid 31848，系统把该 PID 复用给了
        Nahimic3.exe（音频驱动）→ 只按 ①②③ 判会得出「pid 还活着 = 锁正常」
        → 启动器跳过清锁 → 新 dsh 抢 task-board 锁失败 →
        `plugin tree failed to load: failed to apply loader entry ui-task-board`
        → 整个 dsh 退出码 1。用户看到的就是「第 1 次尝试直接崩」。

    [!] 判据从严：只在【明确查到映像名、且不是 node.exe】时才判脏。
        查不到映像名（tasklist 失败/进程刚好退出）时一律保守放过 ——
        宁可漏判一轮，也绝不给「把活 dsh 的锁当脏锁删掉」留机会。
    """
    try:
        size = os.path.getsize(fp)
    except OSError:
        return False, "读不到大小"
    if size == 0:
        return True, "0 字节"

    try:
        with open(fp, "rb") as f:
            data = json.loads(f.read().decode("utf-8", "replace"))
    except Exception:
        return True, "内容不是合法 JSON"

    if isinstance(data, dict) and "pid" in data:
        pid = data.get("pid")
        alive = pid_alive(pid)
        if alive is False:
            return True, "记录的 pid %s 已不存在" % pid
        if alive is True:
            # names 允许调用方传入（批量扫描时复用一次 tasklist）
            if names is None:
                names = image_names()
            actual = names.get(str(pid))
            if actual is not None and actual.lower() != "node.exe":
                return True, ("记录的 pid %s 已被系统复用为 %s（非 node）"
                              "—— 该锁属于已死的 dsh" % (pid, actual))
    return False, "看起来是正常锁"


def dsh_running_any_port():
    """有没有 dsh 在【任意】端口上跑（不只候选端口）。

    不能只看候选端口 —— dsh-mobile 的网关在 3443，光看 3080 会漏。
    做法：node.exe 监听的端口 → 并行探活，【首中即返】。
    [!] 旧实现用 `with ThreadPoolExecutor` + ex.map，提前 return 也要在
        __exit__ 里等全部 future 收尾 —— 端口多时白等十几秒，用户以为死机。
    """
    ports = node_listening_ports()
    if not ports:
        return False
    from concurrent.futures import ThreadPoolExecutor, as_completed
    ex = ThreadPoolExecutor(max_workers=8)
    try:
        futs = [ex.submit(is_dsh_here, p) for p in ports]
        try:
            for f in as_completed(futs):
                if f.result():
                    return True
            return False
        except Exception:
            return True      # 探不动时按「在跑」处理：宁可不清理，不能误删
    finally:
        # 不等收尾：剩余探测直接取消，调用方立刻拿到结论
        ex.shutdown(wait=False, cancel_futures=True)


def _too_fresh(fp, max_age=5.0):
    """锁文件 mtime 距今不足 max_age 秒 = 可能正被 dsh 写入，绝不碰。

    并发启动的 dsh 写锁的瞬间就是 0 字节/半截 JSON —— 恰好命中判脏条件。
    摸不到 mtime 时按「新鲜」处理（宁可漏一轮，不能误删活锁）。
    """
    try:
        return (time.time() - os.path.getmtime(fp)) < max_age
    except OSError:
        return True


def _lock_backup_name(fp):
    """锁备份名：带毫秒与 pid —— 同一秒内两次清理不再互相覆盖。"""
    return "%s.bak-stale-%s-%d-%d" % (fp, time.strftime("%Y%m%d-%H%M%S"),
                                      int(time.time() * 1000) % 1000000,
                                      os.getpid())


def _backup_lock_redacted(fp, bak):
    """复制锁文件到备份，但把 JSON 里的 token 值替换为占位符。

    [!] 锁内容含会话 token（真凭据）—— 备份只为排查看结构，凭据不许落盘。
        含 token 键却一次都没替换成功（结构出乎意料）→ 抛错放弃备份与删除。
    """
    with open(fp, "rb") as f:
        raw = f.read()
    scrubbed, n = _re_mod.subn(rb'"token"\s*:\s*"[^"]*"',
                               b'"token":"<redacted>"', raw)
    if b"token" in raw.lower() and n == 0:
        raise ValueError("token 字段未能清洗，放弃备份")
    with open(bak, "wb") as f:
        f.write(scrubbed)


def clean_stale_locks(log=None):
    """清掉「强杀」留下的脏锁。返回 [(子路径, 原因)]。

    把关（缺一不可）：
      ① 任意端口上都没有 dsh 在跑（先查候选端口，再全端口兜底），
         且 netstat 本身必须成功（失败 ≠ 没有监听）；
      ② 文件位于 ~/.dsh/<子目录>/ 且以 .lock 结尾；
      ③ mtime 距今 ≥ 5 秒（正在写入的活锁就是 0 字节，恰好会判脏）；
      ④ lock_is_stale 判定为脏（0 字节 / 畸形 / dead-PID / **PID 被复用**）。
    删除前一定先备份（token 已脱敏）；每把锁只保留最近 5 份备份。
    """
    def _log(m):
        (log or print)(m)

    # [!] 本函数的一切删除动作都以「确认没有 dsh 在跑」为前提；
    #     而「没有在跑」只能靠 netstat 证明 —— 探测本身失败时（rc != 0，
    #     空表≠没有监听），前提无法成立，宁可不清理也不能误删活锁。
    netstat_table()
    if not _netstat_cache.get("ok", True):
        _log("  [!] netstat 探测失败 —— 无法可靠判断 dsh 是否在跑，为安全起见不清锁")
        return []

    if is_dsh_here(DEFAULT_PORTS[0]) or dsh_running_any_port():
        _log("  [!] 检测到 dsh 正在运行 —— 为安全起见不清锁")
        return []

    root = DSH_STATE
    if not os.path.isdir(root):
        return []

    cleaned = []
    # 惰性取一次 PID→映像名 映射：只有真遇到「pid 还活着」的锁才跑 tasklist，
    # 目录里没有活 pid 锁时零开销。
    _names_cache = {}

    def _names():
        if not _names_cache:
            _names_cache.update(image_names())
        return _names_cache

    try:
        subs = os.listdir(root)
    except OSError:
        return []
    for sub in subs:
        d = os.path.join(root, sub)
        if not os.path.isdir(d) or sub.startswith("."):
            continue
        try:
            files = os.listdir(d)
        except OSError:
            continue
        for fn in files:
            if not fn.endswith(".lock"):
                continue
            # 每删一批前再确认一次没有 dsh 起来（扫描可能耗时数秒，
            # 期间用户完全可能双击了启动器）
            if len(cleaned) and len(cleaned) % 10 == 0:
                if is_dsh_here(DEFAULT_PORTS[0]) or dsh_running_any_port():
                    _log("  [!] 扫描期间检测到 dsh 启动 —— 立即中止清锁")
                    return cleaned
            fp = os.path.join(d, fn)
            if _too_fresh(fp):
                continue
            stale, why = lock_is_stale(fp, _names())
            if not stale:
                continue
            bak = _lock_backup_name(fp)
            try:
                _backup_lock_redacted(fp, bak)
                os.remove(fp)
                cleaned.append((sub + "/" + fn, why))
                _log("  [清理] 脏锁 %s/%s（%s，已脱敏备份）" % (sub, fn, why))
            except Exception as e:
                _log("  [!] 无法清理 %s/%s：%s" % (sub, fn, e))

    # 备份轮转：按【每把锁】保留最近 5 份（旧版整目录混着数，
    # 多锁同目录时会清掉 A 的唯一备份而留 B 的 5 份）
    try:
        for sub2 in os.listdir(root):
            d2 = os.path.join(root, sub2)
            if not os.path.isdir(d2):
                continue
            groups = {}
            for b in glob.glob(os.path.join(d2, "*.bak-stale-*")):
                key = os.path.basename(b).split(".bak-stale-")[0]
                groups.setdefault(key, []).append(b)
            for paths in groups.values():
                for old in sorted(paths)[:-5]:
                    os.remove(old)
    except Exception:
        pass
    return cleaned


# ============================ 1. 仓库定位 ============================
REPO_BAD = ("broken", "old", "backup", "bak", "trash", "tmp", "copy")


def _score_repo(p):
    if not os.path.isdir(p):
        return -9999, ["不是目录"]
    pj = _read_json(os.path.join(p, "package.json"))
    if not isinstance(pj, dict):
        return -9999, ["无 package.json 或不是 JSON 对象"]

    score, why = 0, []
    name = str(pj.get("name") or "")
    if name.startswith("@deepseek-ai/dsh"):
        score += 100; why.append("name=%s" % name)
    elif "dsh" in name.lower() or "harness" in name.lower():
        score += 40; why.append("name=%s（弱匹配）" % name)
    if pj.get("version"):
        why.append("version=%s" % pj["version"])
    if os.path.isdir(os.path.join(p, "apps", "cli")):
        score += 40; why.append("有 apps/cli")
    if os.path.isdir(os.path.join(p, "packages", "boot")):
        score += 20; why.append("有 packages/boot")
    if os.path.isdir(os.path.join(p, "node_modules")):
        score += 20; why.append("有 node_modules")
    if os.path.isdir(os.path.join(p, ".git")):
        score += 5

    base = os.path.basename(p).lower()
    for w in REPO_BAD:
        if w in base:
            score -= 60; why.append("目录名含 %s（-60）" % w); break

    try:
        age_h = (time.time() - os.path.getmtime(p)) / 3600.0
        score += max(0, 8 - int(age_h / 24))
    except OSError:
        pass
    return score, why


def find_repo(cfg, notes):
    cands = []
    if cfg.get("repo"):
        cands.append((cfg["repo"], "config"))
    for k in ("DSH_REPO", "DSH_SRC", "DSH_ROOT"):
        if os.environ.get(k):
            cands.append((os.environ[k], "env:%s" % k))

    roots = [HOME]
    try:
        for n in os.listdir(HOME):
            fp = os.path.join(HOME, n)
            if os.path.isdir(fp) and not n.startswith("."):
                roots.append(fp)
    except OSError:
        pass

    seen = set()
    for root in roots:
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for n in names:
            low = n.lower()
            if not ("harness" in low or low.startswith("dsh") or "deepseek" in low):
                continue
            fp = os.path.join(root, n)
            if fp in seen or not os.path.isdir(fp):
                continue
            seen.add(fp)
            cands.append((fp, "scan"))

    ranked = []
    for path, src in cands:
        sc, why = _score_repo(path)
        ranked.append((sc, path, src, why))
    ranked.sort(key=lambda x: -x[0])

    if not ranked:
        notes.append("未找到任何 dsh 仓库候选目录")
        return None

    top = ranked[0]
    if top[0] < 100:
        notes.append("最佳候选得分仅 %d（<100），可能是残缺仓库：%s" % (top[0], top[1]))
    others = [r for r in ranked[1:] if r[0] >= 100]
    if others:
        notes.append("另有 %d 个可信候选未采用：%s"
                     % (len(others), ", ".join(os.path.basename(o[1]) for o in others[:4])))

    pj = _read_json(os.path.join(top[1], "package.json"))
    if not isinstance(pj, dict):
        pj = {}
    return {"path": top[1], "score": top[0], "source": top[2],
            "version": pj.get("version"), "why": top[3],
            "candidates": [(r[0], r[1]) for r in ranked[:5]]}


# ============================ 2. profile 定位 ============================
def find_profile(cfg, notes):
    root = os.path.join(DSH_STATE, "profiles")
    if not os.path.isdir(root):
        notes.append("找不到 profiles 目录：%s" % root)
        return None
    try:
        names = [n for n in os.listdir(root)
                 if os.path.isdir(os.path.join(root, n))
                 and os.path.exists(os.path.join(root, n, "package.json"))]
    except OSError as e:
        notes.append("读 profiles 失败：%s" % e)
        return None
    if not names:
        notes.append("profiles 下没有带 package.json 的 profile")
        return None

    want = cfg.get("profile")
    if want and want in names:
        name = want
    elif "web" in names:
        name = "web"
    else:
        name = sorted(names)[0]
        notes.append("没有 web profile，回退到：%s" % name)

    pdir = os.path.join(root, name)
    patch = None
    for pat in ("cordis.patch.y*ml", "cordis.y*ml", "cordis*.y*ml"):
        hits = [h for h in sorted(glob.glob(os.path.join(pdir, pat)))
                if ".bak" not in os.path.basename(h)
                and ".tmp" not in os.path.basename(h)]
        if hits:
            patch = hits[0]; break
    if patch is None:
        notes.append("profile 里没有 cordis 补丁文件（插件启停会不可用）")

    pj = _read_json(os.path.join(pdir, "package.json"))
    if not isinstance(pj, dict):
        pj = {}
    prof = ((pj.get("dsh") or {}).get("profile") or {})
    bundles = prof.get("bundles")
    has_bundles = isinstance(bundles, list) and len(bundles) > 0
    if not has_bundles:
        notes.append("package.json 里没有 dsh.profile.bundles")
    if "patchReload" in prof:
        notes.append("patchReload=%s（改动可能热重载）" % prof["patchReload"])

    return {"name": name, "dir": pdir, "patch_file": patch,
            "has_bundles": has_bundles,
            "bundle_count": len(bundles) if isinstance(bundles, list) else 0,
            "bundles": bundles if isinstance(bundles, list) else [],
            "all_profiles": names}


# ============================ 4. 启动命令探测 ============================
def parse_help_commands(help_text):
    """解析 `dsh --help` 的 Commands 段子命令名。

    续行的缩进比命令名更深，必须排除，
    否则会把 the / remaining / directory 这类词当子命令。
    """
    out, in_cmd, cmd_indent = [], False, None
    for line in help_text.splitlines():
        if not line.strip():
            continue
        s = line.strip()
        ind = len(line) - len(line.lstrip(" "))

        if s.lower().startswith("commands:"):
            in_cmd, cmd_indent = True, None
            continue
        if not in_cmd:
            continue
        if ind == 0 and s.endswith(":"):
            in_cmd = False
            continue
        if s.startswith("-"):
            continue
        if cmd_indent is None:
            cmd_indent = ind
        if ind != cmd_indent:
            continue

        tok = s.split()[0].strip(":|,")
        if tok and tok[0].isalpha() and tok.isprintable() and len(tok) < 24:
            out.append(tok)
    return out


def prebuilt_entry(repo):
    """dsh 的预编译入口 apps/cli/lib/bin.js（比 tsx 现编译快约 6 秒）。

    [!] 必须做过期检测：apps/cli/src 下只要有 .ts 比 lib/bin.js 新就回退，
        否则会跑旧代码。
    """
    if not repo:
        return None
    lib = os.path.join(repo, "apps", "cli", "lib", "bin.js")
    if not os.path.exists(lib):
        return None
    try:
        lib_m = os.path.getmtime(lib)
    except OSError:
        return None

    src = os.path.join(repo, "apps", "cli", "src")
    newest = 0.0
    if os.path.isdir(src):
        for dp, _dn, fns in os.walk(src):
            for f in fns:
                if f.endswith((".ts", ".mts", ".cts")):
                    try:
                        newest = max(newest, os.path.getmtime(os.path.join(dp, f)))
                    except OSError:
                        pass
    if newest and newest > lib_m:
        return None
    return lib


def split_win_cmdline(s):
    """把 Windows 命令行拆成 argv —— 去引号、保反斜杠、保空格。

    Windows 的规则与 POSIX 不同：
      * `"` 只用来**分组**（让空格属于同一个参数），不是转义字符，分组后要去掉；
      * `\\` 是普通路径分隔符，**绝不能**像 shlex(posix=True) 那样吞掉；
      * 独立的 `""` 是一个**空参数**（旧版会把它吞掉导致位置参数错位）；
      * 只按 ASCII 空格/制表切分 —— `\\xa0` 等 Unicode 空白 cmd 并不当分隔符，
        旧版用 ch.isspace() 会把 `C:\\x\\xa0y` 这类名字切碎。

    三个反例（本机实测）：
        "C:\\Program Files\\nodejs\\pnpm.cmd" dsh web
          .split()                 → ['"C:\\Program', 'Files\\nodejs\\pnpm.cmd"', ...]  切碎
          shlex.split(posix=False) → ['"C:\\Program Files\\nodejs\\pnpm.cmd"', ...]     引号残留
          shlex.split(posix=True)  → ['C:\\Program Files\\nodejs\\pnpm.cmd', ...]       ← 这个恰好对
        C:\\Users\\<user>\\...\\pnpm.cmd dsh web
          shlex.split(posix=True)  → ['C:Users<user>...pnpm.cmd', ...]                 反斜杠全丢
    所以两者都不能单用，这里自己实现，两组用例都正确。
    """
    out, cur, in_q, has_q = [], [], False, False
    for ch in s:
        if ch == '"':
            in_q = not in_q                 # 引号只切换分组状态，本身不保留
            has_q = True
        elif ch in (" ", "\t") and not in_q:
            if cur or has_q:                # has_q 且 cur 空 = 独立 "" 空参数
                out.append("".join(cur))
            cur, has_q = [], False
        else:
            cur.append(ch)
    if cur or has_q:
        out.append("".join(cur))
    return out


def discover_start_cmds(repo, profile_name, cfg, notes):
    """返回 [{label, argv}]，按可靠性排序。

    顺序：配置指定 > 官方子命令 > --profile 形式 > 已知子命令 >
          tsx 源码入口 > 预编译入口（兜底）。
    """
    out, seen = [], set()

    def push(label, argv, cmdline=None):
        if label not in seen:
            seen.add(label)
            item = {"label": label, "argv": argv}
            if cmdline:
                item["cmdline"] = cmdline   # 启动器须以【字符串命令行】执行它
            out.append(item)

    if cfg.get("start_command"):
        parts = split_win_cmdline(cfg["start_command"])
        first = parts[0] if parts else ""
        if first and os.path.isfile(first):
            # [!] 首段是真实文件 → 绕开 cmd /c 直接以列表参数启动。
            #     cmd /c 对「≥4 个引号」的命令行会剥掉首尾引号，把
            #     `"C:\my prog\x.cmd" --title "my h"` 截成命令名 `C:\my`。
            push("%s（直接启动）" % cfg["start_command"], parts)
        elif parts:
            # [!] 其余形式走 cmd /s /c，且必须给启动器【字符串命令行】——
            #     列表参数会被 list2cmdline 把内部引号转义成 \" ，cmd /s
            #     剥掉外层引号后命令名带上 \" 前缀照样失败。手工整体外引号
            #     才能让 cmd 剥完外层后得到原始命令。
            push(cfg["start_command"], ["cmd.exe", "/s", "/c"],
                 cmdline='cmd.exe /s /c "%s"' % cfg["start_command"])
        else:
            notes.append("start_command 是空白，忽略")

    if not repo or not os.path.isdir(repo):
        return out

    pm = pkg_manager(repo)
    prof = profile_name or "web"
    # [!] profile 名是 os.listdir 出来的目录名 —— 带 cmd 元字符的名字拼进
    #     `--profile x&calc` 会被 cmd 当成第二条命令执行；不安全就只用
    #     默认的 web 形式，绝不让脏名字进 argv
    if not cmd_arg_safe(prof):
        notes.append("profile 名含 cmd 元字符，--profile 形式候选跳过：%r" % prof)
        prof = "web"
        prof_ok = False
    else:
        prof_ok = True

    rc, help_out, _ = _run(["cmd.exe", "/c", pm, "dsh", "--help"],
                           timeout=90, cwd=repo)
    subs = parse_help_commands(help_out) if rc == 0 else []
    if subs:
        notes.append("`%s dsh --help` 列出子命令：%s" % (pm, ", ".join(subs)))
    else:
        notes.append("`%s dsh --help` 探测失败（rc=%d），退回已知形式" % (pm, rc))

    chosen = None
    for w in WEB_WORDS:
        if w in subs:
            chosen = w; break

    if chosen:
        push("dsh %s" % chosen, ["cmd.exe", "/c", pm, "dsh", chosen])
    if prof_ok and prof != chosen:
        push("dsh --profile %s %s" % (prof, chosen or "web"),
             ["cmd.exe", "/c", pm, "dsh", "--profile", prof, chosen or "web"])

    for s in ("web", "serve", "start"):
        push("dsh %s" % s, ["cmd.exe", "/c", pm, "dsh", s])

    # tsx 源码入口（一定是最新代码，但每次现编译，慢）
    for entry in (os.path.join("apps", "cli", "src", "bin.ts"),
                  os.path.join("apps", "cli", "src", "bin.js"),
                  os.path.join("apps", "cli", "bin.js")):
        if os.path.exists(os.path.join(repo, entry)):
            push("node %s %s" % (entry, prof),
                 ["cmd.exe", "/c", "node", "--import", "tsx/esm", entry, prof])
            break

    # 预编译入口（最快，但非官方路径）—— 兜底
    try:
        pre = prebuilt_entry(repo)
    except Exception:
        pre = None
    if pre and cmd_arg_safe(pre):
        push("node apps/cli/lib/bin.js %s（兜底：预编译入口）" % prof,
             ["cmd.exe", "/c", find_node(), pre, prof])
        notes.append("预编译入口已降为兜底候选（官方方式优先）")
    elif pre:
        notes.append("预编译入口路径含 cmd 元字符，跳过该兜底候选")

    for c in out:
        a = c["argv"]
        if "cmdline" not in c and len(a) > 2 and a[0] == "cmd.exe":
            c["label"] = " ".join(a[2:])
    return out


# ============================ 5. 权威插件树 ============================
def dump_config(repo, profile_name, notes, force=False, timeout=240):
    """跑 `dsh --profile X --dump-config` 拿合成后的完整 loader 树（权威）。

    缓存指纹 = 仓库 package.json + profile package.json + 【补丁文件】 的 mtime。
    [!] 补丁文件必须算进去：用户或 dsh 自己改 cordis.patch.yml 时，
        前两个 mtime 都不变，指纹若不含它，插件树会一直显示旧状态。
    """
    if not repo or not profile_name:
        return None
    # [!] profile 名要进 cmd.exe 的 argv —— 带 & | % 之类的目录名会在这里
    #     变成第二条命令；不安全直接拒绝（返回 None = 降级为自行解析补丁）
    if not cmd_arg_safe(profile_name):
        notes.append("profile 名含 cmd 元字符，拒绝执行 --dump-config：%r"
                     % profile_name)
        return None

    fp = []
    for p in (os.path.join(repo, "package.json"),
              os.path.join(DSH_STATE, "profiles", profile_name, "package.json")):
        try:
            fp.append(str(int(os.path.getmtime(p))))
        except OSError:
            fp.append("0")
    prof_dir = os.path.join(DSH_STATE, "profiles", profile_name)
    patch_m = []
    for pat in ("cordis*.yml", "cordis*.yaml"):
        for f in sorted(glob.glob(os.path.join(prof_dir, pat))):
            base = os.path.basename(f)
            if ".bak" in base or ".tmp" in base:
                continue
            try:
                patch_m.append(str(int(os.path.getmtime(f))))
            except OSError:
                pass
    fp.append(",".join(patch_m))
    fp.append(profile_name)
    fp = "|".join(fp)

    meta = _read_json(DUMPC + ".meta.json")
    if not isinstance(meta, dict):
        meta = {}
    if not force and meta.get("fp") == fp and os.path.exists(DUMPC):
        try:
            with open(DUMPC, "rb") as f:
                data = f.read()
            # [!] 长度必须与 meta 记录一致 —— 旧版只看「文件存在」，
            #     半截写入的 dump 会被旧 meta 放行，插件树静默失真
            if meta.get("bytes") == len(data):
                return data.decode("utf-8", "replace")
        except Exception:
            pass

    pm = pkg_manager(repo)
    rc, out, err = _run(["cmd.exe", "/c", pm, "dsh", "--profile",
                         profile_name, "--dump-config"],
                        timeout=timeout, cwd=repo, try_utf8=True)
    if rc != 0 or len(out) < 200:
        notes.append("`dsh --profile %s --dump-config` 不可用（rc=%d）"
                     "，退回自行解析补丁文件" % (profile_name, rc))
        return None

    # [!] 原子写：先写临时文件再 os.replace —— 直接覆写时崩溃/双实例交错
    #     会留下半截 dump 被 meta 放行（meta 最后写，带长度校验）
    try:
        tmp = "%s.tmp-%d" % (DUMPC, os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(out)
        os.replace(tmp, DUMPC)
        mtmp = "%s.meta.tmp-%d" % (DUMPC, os.getpid())
        with open(mtmp, "w", encoding="utf-8") as f:
            json.dump({"fp": fp, "bytes": len(out.encode("utf-8")),
                       "ts": time.strftime("%Y-%m-%d %H:%M:%S")}, f)
        os.replace(mtmp, DUMPC + ".meta.json")
    except Exception:
        pass
    notes.append("已用 `--dump-config` 取到权威插件树（%d 行）" % len(out.splitlines()))
    return out


def parse_dump(text):
    """dump-config 输出 → [{id, name, disabled, section}]

    [!] 入口属性（name/disabled）只认【入口层缩进】——由该入口第一个属性行
        定义；比它深的一律是 config: 等子块，里面的同名键不是入口属性。
        旧实现用 in_config 开关，属性出现在 config: 之后会被整段丢掉；
        现在只看缩进层，与键序无关。
    """
    entries = []
    section = "(unknown)"
    cur = None
    k = None
    for raw in text.splitlines():
        if raw.startswith("# =="):
            section = raw[4:].strip()
            continue
        if raw.startswith("#==="):     # 无空格的分节头也算（别把归属算错层）
            section = raw[3:].strip()
            continue
        m = _re_id.match(raw)
        if m:
            cur = {"id": m.group(1), "name": None, "disabled": None,
                   "section": section}
            entries.append(cur)
            k = None
            continue
        if cur is None or not raw.strip() or raw.lstrip().startswith("#"):
            continue
        ind = len(raw) - len(raw.lstrip(" "))
        if k is None:
            k = ind                  # 该入口第一个属性行定义入口层缩进
        if ind != k:
            continue                 # 子块内部（config: 的孩子等），一律不看
        s = raw.strip()
        m2 = _re_name.match(s)
        if m2 and cur["name"] is None:
            cur["name"] = m2.group(1).strip().strip("'\"")
            continue
        m3 = _re_dis.match(s)
        if m3 and cur["disabled"] is None:
            v = m3.group(1).strip()
            cur["disabled"] = "expr" if v.startswith("!!js") else (v.lower() == "true")
    return entries


# ============================ 6. 能力探测 ============================
def builtin_map(repo=None):
    """内置组合包 → cordis 补丁文件 路径表。

    内置包从 dsh 安装目录解析、不走 pnpm，所以不在 profile 的
    dependencies / node_modules 里 —— 这是正常的，不是「缺失依赖」。
    """
    m = {}
    if not repo:
        c = _read_json(CACHE)
        if isinstance(c, dict):
            repo = ((c.get("result") or {}).get("repo") or {}).get("path")
    if not repo:
        return m
    bdir = os.path.join(repo, "packages", "bundle")
    if not os.path.isdir(bdir):
        return m
    try:
        names = os.listdir(bdir)
    except OSError:
        return m
    for d in names:
        pkg = os.path.join(bdir, d)
        pj = _read_json(os.path.join(pkg, "package.json"))
        if not pj:
            continue
        name = pj.get("name")
        if not name:
            continue
        for fn in ("cordis.patch.yml", "cordis.yml"):
            f = os.path.join(pkg, fn)
            if os.path.exists(f):
                m[name] = f
                break
    return m


def find_features(profile, notes):
    f = {"fallback_dir": None, "has_fallback": False, "has_cordis_yaml": False}
    if not profile:
        return f
    pdir = profile["dir"]
    fb = os.path.join(pdir, ".dsh-module-fallback")
    f["fallback_dir"] = fb
    f["has_fallback"] = os.path.isdir(fb)
    if not f["has_fallback"]:
        notes.append(".dsh-module-fallback 不存在（dsh 可能已修好，自愈步骤会自动跳过）")
    f["has_cordis_yaml"] = os.path.exists(os.path.join(pdir, "cordis.yml"))
    f["has_bundles"] = bool(profile.get("has_bundles"))
    return f


# ============================ 汇总 ============================
_MEMO = {}


def _cache_valid(c):
    """落盘缓存是否可用：结构是预期形状、仓库/profile 还在、仓库
    package.json 没被改过。

    [!] c 与 c["result"] 都必须 isinstance dict —— 缓存被外部改坏成
        「合法 JSON 但顶层是列表/数字」时，旧版会 AttributeError；
        detect 又经 probe() 被吞，菜单[1] 就误报「没定位到仓库」。
    """
    if not isinstance(c, dict):
        return False
    r = c.get("result")
    if not isinstance(r, dict):
        return False
    repo = (r.get("repo") or {}).get("path") if isinstance(r.get("repo"), dict) else None
    prof = (r.get("profile") or {}).get("dir") if isinstance(r.get("profile"), dict) else None
    if not repo or not os.path.isdir(repo):
        return False
    if not prof or not os.path.isdir(prof):
        return False
    try:
        m = str(int(os.path.getmtime(os.path.join(repo, "package.json"))))
    except OSError:
        return False
    return r.get("repo_mtime") == m


def detect(force=False, want_dump=False):
    # 进程内缓存命中
    if not force and "r" in _MEMO:
        r = copy.deepcopy(_MEMO["r"])
        try:
            r["port"] = find_port(load_config(), [])
        except Exception:
            pass
        return r

    # 落盘缓存命中（省掉扫仓库 + `dsh --help` 探测各约 6 秒）
    if not force:
        c = _read_json(CACHE)
        if _cache_valid(c):
            r = copy.deepcopy(c["result"])
            try:
                r["port"] = find_port(load_config(), [])
            except Exception:
                pass
            r["cache_hit"] = True
            if not any("缓存" in x for x in (r.get("notes") or [])):
                r.setdefault("notes", []).append(
                    "使用上次探测结果的缓存（仓库未变动）；菜单[5]可强制重探")
            _MEMO["r"] = copy.deepcopy(r)
            return r

    cfg = load_config()
    notes = []
    repo = find_repo(cfg, notes)
    profile = find_profile(cfg, notes)
    port = find_port(cfg, notes)
    feats = find_features(profile, notes)
    start = discover_start_cmds(repo["path"] if repo else None,
                                profile["name"] if profile else None, cfg, notes)

    if repo:
        notes.append("仓库：%s（得分 %d，来源 %s）"
                     % (repo["path"], repo["score"], repo["source"]))
    else:
        notes.append("未能定位 dsh 仓库 —— 启动不可用")
    if profile:
        notes.append("profile：%s（%d 个 bundle）"
                     % (profile["name"], profile["bundle_count"]))
    if start:
        notes.append("启动命令候选 %d 个，首选：%s" % (len(start), start[0]["label"]))
    else:
        notes.append("没有任何可用的启动命令候选")

    dump_text = None
    if want_dump and repo and profile:
        dump_text = dump_config(repo["path"], profile["name"], notes, force=force)

    repo_mtime = None
    if repo:
        try:
            repo_mtime = str(int(os.path.getmtime(
                os.path.join(repo["path"], "package.json"))))
        except OSError:
            repo_mtime = None

    result = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "repo": repo, "profile": profile, "port": port,
        "start": start, "features": feats, "notes": notes,
        "config_file": CFG if os.path.exists(CFG) else None,
        "repo_mtime": repo_mtime,
    }
    if dump_text:
        result["dump_lines"] = len(dump_text.splitlines())
    try:
        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump({"result": result}, f, ensure_ascii=False,
                      indent=2, default=str)
    except Exception:
        pass
    _MEMO["r"] = copy.deepcopy(result)
    return result


# ============================ 报告 ============================
def report(env):
    o = ["=" * 70, "  dsh 环境探测报告", "=" * 70, ""]

    r = env.get("repo")
    o.append("[仓库]")
    if r:
        o.append("  路径 : %s" % r.get("path"))
        o.append("  版本 : %s" % (r.get("version") or "?"))
        o.append("  来源 : %s（得分 %s）" % (r.get("source"), r.get("score")))
        o.append("  判据 : %s" % "; ".join(r.get("why") or []))
    else:
        o.append("  未定位到（启动不可用）")

    p = env.get("profile")
    o += ["", "[profile]"]
    if p:
        o.append("  名称    : %s" % p.get("name"))
        o.append("  目录    : %s" % p.get("dir"))
        o.append("  补丁    : %s" % (p.get("patch_file") or "（无）"))
        o.append("  bundles : %d 个" % (p.get("bundle_count") or 0))
        if len(p.get("all_profiles") or []) > 1:
            o.append("  其它    : %s" % ", ".join(p["all_profiles"]))
    else:
        o.append("  未定位到")

    pt = env.get("port") or {}
    o += ["", "[端口]",
          "  使用 : %s" % pt.get("value"),
          "  来源 : %s" % pt.get("source"),
          "  当前 : %s" % ("有 dsh 在跑" if pt.get("alive") else "空闲"),
          "  识别 : 靠响应特征（401 + body 含 dsh），不靠端口号"]

    o += ["", "[启动命令候选]"]
    for i, c in enumerate(env.get("start") or [], 1):
        o.append("  %d. %s" % (i, c["label"]))
    if not env.get("start"):
        o.append("  （无）")

    f = env.get("features") or {}
    o += ["", "[能力]",
          "  .dsh-module-fallback : %s"
          % ("存在" if f.get("has_fallback") else "不存在（自愈会跳过）"),
          "  cordis.yml           : %s" % ("存在" if f.get("has_cordis_yaml") else "不存在")]

    if env.get("dump_lines"):
        o += ["", "[权威插件树]", "  --dump-config 行数：%d" % env["dump_lines"]]

    o += ["", "[探测说明]"]
    for n in env.get("notes") or []:
        o.append("  - %s" % n)
    o += ["", "=" * 70]
    return "\n".join(o)
