# -*- coding: utf-8 -*-
"""
dsh 启动自救脚本 —— 修复 .dsh-module-fallback 里缺失的 junction。

背景：dsh 0.1.5-rc.2 每次启动会调用 healProfileModuleFallback()，
用 symlinkSync(target, link, 'junction') 把 node_modules 里的包投影到
profiles/<name>/.dsh-module-fallback/node_modules/。这个投影过程存在并发竞态
（源码注释自认："Concurrent launches heal the same fallback"），偶发 EPERM/EEXIST，
一旦中断就留下缺失的链接，下次启动直接崩：
    Error: EPERM: operation not permitted, symlink ...
    at ensureSymlink (packages/boot/app-boot/src/profile.ts:228)

本脚本兜底：比对两侧差异，用 mklink /J 补齐。

用法：
    python dsh-fallback-heal.py            # 自动修复所有 profile
    python dsh-fallback-heal.py --dry-run  # 只看差异不改
"""
import os, re, sys, subprocess, time

_TOOLS = os.path.dirname(os.path.abspath(__file__))
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

import dsh_env as _env_mod          # 探测层单例（与 launcher/plugins 共享）

FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# cmd.exe 安全闸统一收口到探测层（含全部 C0/DEL 控制符，覆盖 stdin 脚本
# 的 %VAR% 展开、& 拼接、0x1A 终止读取等全部形态）。此处只是转调别名，
# 让旧调用点与既有用例不用改。
def cmd_arg_safe(p):
    """路径能否安全拼进 cmd.exe 脚本行（mklink / rmdir 的 stdin 脚本）。"""
    return _env_mod.cmd_arg_safe(p)

# 终端是 chcp 936（GBK）时，打印非 GBK 字符会抛 UnicodeEncodeError。
# 这里统一降级为 replace，保证任何情况下都不会因此崩溃。
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

# dsh 自己用 DSH_HOME 定位状态目录，优先尊重它；没设才退回 ~/.dsh。
# （与 dsh-env.py 保持一致，这样 dsh 改了状态目录位置也不用改本脚本。）
DSH_HOME = os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh")
DRY = "--dry-run" in sys.argv
# [!] 默认【不】结束任何进程 —— 只有显式传 --kill 才允许。
# 否则用户从菜单选「补齐模块链接」时，会把正在运行的 dsh 悄悄杀掉。
KILL = "--kill" in sys.argv
# [!] 删除 pnpm 残留临时目录也需要显式许可 ——
#     启动器每次启动都会静默调用本脚本，不通知用户就删目录是不能接受的。
CLEAN = "--clean" in sys.argv


def log(*a):
    print(*a); sys.stdout.flush()


def list_pkgs(root):
    """列出包里所有包名（含 @scope/name 形式）"""
    out = []
    if not os.path.isdir(root):
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
                        out.append(name + "/" + s)
            except OSError:
                pass
        else:
            out.append(name)
    return sorted(set(out))


def kill_node():
    """只结束占用 3080 端口（dsh web）的进程。

    [!] 绝不按镜像名杀 node.exe —— 那会误杀用户的工具链
    （用户本机的其它工具链：各类 MCP 服务、编辑器插件等）。
    dsh 只监听的端口是 3080，按端口精确定位才安全。
    """
    killed = []

    # 探测层单例：严格判据 + 【强制刷新】的 netstat 快照 ——
    # 击杀绝不吃 ≤3 秒的陈旧表（PID 可能已被系统复用）
    ports = sorted({r["port"] for r in _env_mod.netstat_table(refresh=True)
                    if _env_mod.is_dsh_here(r["port"])})
    try:
        for port in ports:
            for pid in sorted(_env_mod.listening_pids(port)):
                # [!] 探活与击杀之间 PID 可能被系统复用 —— 不是 node.exe 绝不 taskkill
                if not _env_mod.pid_is_node(pid):
                    log("    [跳过] PID %s 已不是 node.exe（疑似 PID 复用），不动" % pid)
                    continue
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, creationflags=FLAGS,
                               timeout=30)
                killed.append(str(pid))
    except Exception:
        pass
    if killed:
        time.sleep(1.5)
    return killed


def heal_profile(profile_dir):
    src = os.path.join(profile_dir, "node_modules")
    fb = os.path.join(profile_dir, ".dsh-module-fallback", "node_modules")
    if not os.path.isdir(src):
        return 0, 0
    src_pkgs = set(list_pkgs(src))
    fb_pkgs = set(list_pkgs(fb))
    missing = sorted(src_pkgs - fb_pkgs)
    log("  源 %d 个包 | fallback %d 个包 | 缺失 %d 个"
        % (len(src_pkgs), len(fb_pkgs), len(missing)))
    if DRY or not missing:
        for m in missing[:40]:
            log("    - %s" % m)
        return 0, len(missing)
    # 先把该建的都算出来，再【一次性】跑一个 .bat。
    # 原先是每个链接起一次 cmd.exe（实测每次 0.6 秒），65 个就要 39 秒。
    todo, todo_u = [], []
    for name in missing:
        target = os.path.join(src, *name.split("/"))
        link = os.path.join(fb, *name.split("/"))
        if not os.path.isdir(target):
            continue
        if not (cmd_arg_safe(link) and cmd_arg_safe(target)):
            log("    [跳过] 路径含 cmd 元字符，拒绝进 mklink 脚本：%s" % name)
            continue
        try:
            (link + target).encode("gbk")
        except UnicodeEncodeError:
            # [!] stdin 脚本按 GBK 编码，非 GBK 名会被折叠成「?」→ mklink
            #     必失败。这些条目直接走 Unicode 参数列表逐条建，保住正确性
            todo_u.append((link, target))
            continue
        os.makedirs(os.path.dirname(link), exist_ok=True)
        # [!] 用 lexists：断链（目标丢失的 junction）在 exists 眼里是"不存在"，
        #     于是 mklink 会因"已存在"失败，这个链接就永远补不上。
        if os.path.lexists(link):
            if os.path.isdir(link):
                continue          # 正常链接，跳过
            # 断链 → 先清掉再重建
            try:
                subprocess.run(["cmd.exe", "/c", "rmdir", "/S", "/Q", link],
                               capture_output=True, creationflags=FLAGS,
                               timeout=120)
            except Exception:
                pass          # 清理失败不炸整个自愈流程（下方放弃该条）
            if os.path.lexists(link):
                continue          # 清不掉就放弃这个，不硬来
        todo.append((link, target))

    if todo and not DRY:
        # [!] 建链接的三种方式与实测（29 条用时）：
        #     常驻 cmd.exe + stdin 喂全部命令 ... 1.24 秒  ← 用这个
        #     并发直连 mklink（8 线程） ......... 3.61 秒
        #     逐条直连 mklink（原方案） ........ 18.78 秒
        #     ⚠️ 试过「生成 .bat 再 cmd /c 跑」，本环境下 bat 内的 mklink
        #        会静默失效（rc=1、零输出、链接建不出来），已放弃。
        script = "".join('mklink /J "%s" "%s"\r\n' % (link, target)
                         for link, target in todo)
        script += "exit\r\n"
        done = False
        proc = None
        try:
            proc = subprocess.Popen(["cmd.exe"], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE,
                                    creationflags=FLAGS)
            proc.communicate(script.encode("gbk", "replace"), timeout=600)
            done = True
        except subprocess.TimeoutExpired:
            # [!] communicate 超时【不会】杀子进程 —— 不杀的话已喂入的
            #     mklink 脚本会继续跑，launcher 报「已放弃」后 junction
            #     还在陆续出现，用户此刻启动 dsh 就撞上自愈竞态窗口
            try:
                if proc:
                    proc.kill()
                    proc.communicate(timeout=30)
            except Exception:
                pass
            done = False
        except Exception:
            done = False

        if not done:
            # 兜底：退回逐条直连（慢，但单独验证过一定可用）
            for link, target in todo:
                try:
                    subprocess.run(["cmd.exe", "/c", "mklink", "/J",
                                    link, target],
                                   capture_output=True, creationflags=FLAGS,
                                   timeout=120)
                except Exception:
                    pass

    if todo_u and not DRY:
        # 非 GBK 名的条目：Unicode 参数列表逐条建（不经 stdin 脚本，
        # 没有编码折叠问题；cmd_arg_safe 已过闸，无元字符风险）
        for link, target in todo_u:
            try:
                subprocess.run(["cmd.exe", "/c", "mklink", "/J", link, target],
                               capture_output=True, creationflags=FLAGS,
                               timeout=120)
            except Exception:
                pass

    created = sum(1 for link, _ in todo + todo_u if os.path.isdir(link))
    return created, len(missing)


def clean_pnpm_leftovers(profile_dir, max_depth=2):
    """找出（并可选清除）pnpm 中断残留的 <pkg>_tmp_<pid>_<hex> 临时目录

    严格匹配（形如 `zod_tmp_50832_240`），避免误删正常目录。
    [!] 只有显式传 --clean 才真正删除；否则仅列出。
    [!] 用 os.scandir 限深扫描 —— 原先全量 os.walk 要遍历十几万个文件，
        而本函数每次启动都会被调用，是启动慢的主因之一。
    """
    src = os.path.join(profile_dir, "node_modules")
    found = []
    if not os.path.isdir(src):
        return found
    pat = re.compile(r"^.+_tmp_\d+_[0-9a-f]+$", re.I)

    def scan(d, depth):
        try:
            entries = list(os.scandir(d))
        except OSError:
            return
        for e in entries:
            try:
                if not e.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if pat.match(e.name):
                found.append(e.path)
                # [!] 只删「长得像 pnpm 残留」的：目录里有 package.json，
                #     或本身是 reparse point（junction/symlink）。空目录只列
                #     不删 —— 名字撞车（如 foo_tmp_12_ab）的合法包不能误伤
                looks_like_pkg = os.path.exists(os.path.join(e.path, "package.json"))
                try:
                    is_reparse = os.path.realpath(e.path) != os.path.abspath(e.path)
                except OSError:
                    is_reparse = False
                if not looks_like_pkg and not is_reparse:
                    log("    [跳过] 无 package.json 也不是链接，不像 pnpm 残留，只列不删：%s"
                        % e.name)
                elif not DRY and CLEAN:
                    if cmd_arg_safe(e.path):
                        try:
                            subprocess.run(["cmd.exe", "/c", "rmdir", "/S", "/Q", e.path],
                                           capture_output=True, creationflags=FLAGS,
                                           timeout=120)
                        except Exception:
                            pass      # 超时/失败不炸整个自愈流程
                    else:
                        log("    [跳过] 残留目录路径含 cmd 元字符，拒绝删除：%s"
                            % e.path)
                continue          # 命中的不再下钻
            if depth < max_depth:
                scan(e.path, depth + 1)

    scan(src, 0)
    return found


def main():
    log("=" * 66)
    log("dsh fallback 自愈" + ("（演练模式，不改动）" if DRY else ""))
    log("=" * 66)
    if not DRY:
        if KILL:
            log("\n[1] 清理残留 dsh 实例（自动识别端口，不按镜像名杀进程）")
            pids = kill_node()
            log("    已结束 %d 个" % len(pids))
        else:
            log("\n[1] 跳过进程清理（未传 --kill）")

    prof_root = os.path.join(DSH_HOME, "profiles")
    if not os.path.isdir(prof_root):
        log("找不到 profiles 目录: %s" % prof_root)
        return 1

    total_created = 0
    scanned = 0
    no_fallback = 0
    for name in sorted(os.listdir(prof_root)):
        pd = os.path.join(prof_root, name)
        if not os.path.isdir(pd) or not os.path.exists(os.path.join(pd, "package.json")):
            continue
        scanned += 1
        # 机制不存在就跳过（dsh 未来可能移除或改名）
        if not os.path.isdir(os.path.join(pd, ".dsh-module-fallback")):
            no_fallback += 1
            log("\n[profile] %s — 没有 .dsh-module-fallback，跳过" % name)
            continue
        log("\n[profile] %s" % name)
        c, _missing_cnt = heal_profile(pd)   # 第二值仅用于日志，此处不需要
        total_created += c
        if c:
            log("    [OK] 补齐 %d 个 junction" % c)

        left = clean_pnpm_leftovers(pd)
        if left:
            verb = "已清理" if (CLEAN and not DRY) else "发现（未删除，需加 --clean）"
            log("    pnpm 残留临时目录 %d 个：%s" % (len(left), verb))
            for l in left[:20]:
                log("      - %s" % os.path.basename(l))

    if scanned == 0:
        log("\n没有找到任何 profile，无事可做。")
    elif no_fallback == scanned:
        log("\n所有 profile 都没有 .dsh-module-fallback 机制 —— 无需自愈。")

    log("\n" + "=" * 66)
    if DRY:
        log("演练结束。去掉 --dry-run 即执行修复。")
    else:
        log("完成。共补齐 %d 个 junction。" % total_created)
        log("现在可以双击启动器了。")
    log("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
