# -*- coding: utf-8 -*-
"""
dsh 启动器（终端菜单）
======================
双击桌面 start-dsh.bat → 1 秒内不做任何输入 → 直接启动 dsh；
这 1 秒内按任意键 → 进入菜单。

  [1] 启动 dsh
  [2] 插件管理
  [3] 只检查插件冲突
  [4] 补齐模块链接
  [5] 环境探测报告
  [0] 退出

本文件**只做编排与交互**：所有 dsh 相关知识（仓库/profile/端口/命令/锁/进程）
都在 `dsh_env.py` 探测层里。本文件不 import dsh 的任何东西。
"""
import os, sys, time, subprocess, tempfile

TOOLS = os.path.dirname(os.path.abspath(__file__))
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import dsh_env                       # 探测层（单例）

AUTO_START_SECONDS = 1.0
FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass


def log(m=""):
    print(m)
    sys.stdout.flush()


def pause(msg="  回车继续..."):
    try:
        input(msg)
    except (EOFError, KeyboardInterrupt):
        print()


def ask(msg):
    try:
        return input(msg)
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def line(c="-"):
    log(c * 66)


def probe(force=False):
    try:
        return dsh_env.detect(force=force)
    except Exception as e:
        log("  [!] 环境探测失败：%s" % e)
        return None


def pinned_env():
    return dsh_env.pinned_env()


def port_open(p):
    """端口是否有人监听。无论如何都关 socket。"""
    import socket
    s = socket.socket()
    try:
        s.settimeout(1.0)
        s.connect(("127.0.0.1", p))
        return True
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def find_live_ports(env):
    """候选端口里确实跑着 dsh 的那些。"""
    ports = []
    if env and env.get("port"):
        ports.append(env["port"]["value"])
    ports += list(dsh_env.DEFAULT_PORTS)
    live = dsh_env.listening_ports()
    return [p for p in dict.fromkeys(ports) if p in live and dsh_env.is_dsh_here(p)]


INSTANCE_LOCK = os.path.join(tempfile.gettempdir(), "dsh-launcher.instance.lock")


def acquire_instance_lock():
    """跨实例互斥：拿不到 = 另一个启动器正在启动 dsh。

    [!] 防的是「双击两次、两个实例都过了『未运行』守卫 → 两个 dsh 抢启动
        → junction 并发竞态（EPERM 崩溃家族）+ 清锁误删活锁」。
        上次崩溃的残锁（记录的 pid 已死）自愈后重试一次。
    """
    for attempt in (1, 2):
        try:
            fd = os.open(INSTANCE_LOCK, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            other = ""
            try:
                with open(INSTANCE_LOCK) as f:
                    other = f.read().strip()
            except Exception:
                pass
            if attempt == 1 and other.isdigit() \
                    and dsh_env.pid_alive(int(other)) is False:
                release_instance_lock()
                continue
            log("  [X] 另一个启动器实例正在启动 dsh（pid %s）—— 不并发拉起。" % other)
            return False
        except Exception as e:
            log("  [!] 实例锁创建失败（%s）—— 保守起见本次不启动。" % e)
            return False
    return False


def release_instance_lock():
    try:
        os.remove(INSTANCE_LOCK)
    except OSError:
        pass


def kill_leftover(env):
    """结束残留的 dsh 实例（只对确认是 dsh 的端口动手）。"""
    killed = []
    names = dsh_env.image_names()
    for p in find_live_ports(env):
        # [!] 击杀用【强制刷新】的端口表 —— 旧版吃 ≤3 秒陈旧快照，
        #     PID 复用时连 pid_is_node 复核都救不回「一开始就查错了人」
        for pid in sorted(dsh_env.verified_listening_pids(p)):
            if not dsh_env.pid_is_node(pid, names):
                log("  [跳过] PID %s 已不是 node.exe（疑似 PID 复用），不动" % pid)
                continue
            log("  [清理] 结束残留 dsh 实例（端口 %d，PID=%s）" % (p, pid))
            try:
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, creationflags=FLAGS,
                               timeout=30)
                killed.append(pid)
            except Exception as e:
                # [!] taskkill 挂起/失败绝不能炸掉整个启动流程
                #     （旧版 TimeoutExpired 会一路抛穿只捕 KeyboardInterrupt
                #     的顶层守卫，窗口秒退、dsh 状态不明）
                log("  [!] taskkill 失败（PID=%s）：%s" % (pid, e))
    if killed:
        time.sleep(2)
    return killed
    if killed:
        time.sleep(2)
    return killed


def run_heal(env, deep=False):
    """自愈 fallback 链接。

    deep=False（启动路径）：dsh 正常只需 220/285，缺 65 个是它的正常状态，
                            且实测补齐反而慢 4 秒 —— 因此跳过。
    deep=True （菜单 [4]）：无视该判断，强制补齐。
    """
    heal = os.path.join(TOOLS, "dsh-fallback-heal.py")
    if not os.path.exists(heal):
        return False, "找不到自愈脚本"

    def counts():
        try:
            pdir = env["profile"]["dir"]
            s = dsh_env.list_pkgs(os.path.join(pdir, "node_modules"))
            f = dsh_env.list_pkgs(os.path.join(pdir, ".dsh-module-fallback",
                                               "node_modules"))
            return len(s), len(s - f)
        except Exception:
            return None, None

    total, before = counts()
    if total is None:
        return True, "无法核对链接状态（跳过）"
    if before == 0 and not deep:
        return True, "链接完整，无需处理"
    if not deep and total and before < total * 0.5:
        return True, ("属 dsh 的正常状态（它只需 %d/%d 个链接），跳过补齐"
                      % (total - before, total))

    try:
        subprocess.run([sys.executable, heal], capture_output=True,
                       creationflags=FLAGS, timeout=600)
    except subprocess.TimeoutExpired:
        return False, "自愈脚本 600 秒未完成，已放弃（可单独运行它排查）"
    except Exception as e:
        return False, "自愈脚本执行失败：%s" % e

    _t2, after = counts()
    if after is None:
        return True, "已执行（无法核对缺失数）"
    if after == 0:
        return True, ("本来就完整" if before == 0
                      else "原有 %s 个缺失，现已补齐" % before)
    return False, "仍有 %s 个缺失" % after


# ==================== 动作 ====================
def action_start(assume_yes=False):
    if not acquire_instance_lock():
        pause()
        return
    try:
        _action_start_impl(assume_yes)
    finally:
        release_instance_lock()


def _action_start_impl(assume_yes):
    env = probe()
    line("=")
    log("  启动 dsh")
    line("=")
    log()

    def confirm(msg):
        if assume_yes:
            log("  （--yes：自动确认）%s" % msg.strip())
            return True
        return ask(msg).strip().lower() == "y"

    repo = (env or {}).get("repo") or {}
    rpath = repo.get("path")
    if not rpath or not os.path.isdir(rpath):
        log("  [X] 没能定位到 dsh 源码仓库。")
        log("      可以手动指定：编辑 %s" % os.path.join(TOOLS, "dsh-config.json"))
        log('      写入：{ "repo": "D:\\\\path\\\\to\\\\deepseek-harness" }')
        log("      或先看探测详情：菜单 [5] 环境探测报告")
        log()
        pause()
        return
    log("  仓库   : %s（v%s）" % (rpath, repo.get("version") or "?"))

    cmds = (env or {}).get("start") or []
    if not cmds:
        log("  [!] 没有可用的启动命令候选，按仓库的锁文件推导包管理器重试。")
        try:
            pm = dsh_env.pkg_manager(rpath)
        except Exception:
            pm = "pnpm"
        cmds = [{"label": "%s dsh web" % pm,
                 "argv": ["cmd.exe", "/c", pm, "dsh", "web"]}]

    # ---- 前置：残留实例 ----
    live = find_live_ports(env)
    if live:
        log("  [!] 检测到 dsh 正在运行（端口 %s）" % ", ".join(map(str, live)))
        log()
        if not confirm("      先结束它再启动？(y/N) > "):
            return
        kill_leftover(env)
    else:
        # 只看 dsh 将要用的那一个端口；被占但不是 dsh 就只提醒、绝不动它
        primary = ((env or {}).get("port") or {}).get("value") or \
                  (dsh_env.DEFAULT_PORTS[0])
        if port_open(primary):
            log("  [!] 端口 %d 已被占用，但【确认不是 dsh】—— 本工具不会去动它。"
                % primary)
            log("      如果是你别的服务，请忽略；dsh 可能会启动失败或另找端口。")
            log()
            if not confirm("      仍要继续启动吗？(y/N) > "):
                return

    # ---- 前置：清掉强杀留下的脏锁（否则插件树会崩）----
    log("  正在检查强杀残留锁（有 dsh 运行时不清理，最坏约 15 秒）...")
    dsh_env.clean_stale_locks(log=log)

    # ---- 第 1 步：自愈（只在机制存在时做；正常状态会跳过）----
    feats = (env or {}).get("features") or {}
    if feats.get("has_fallback"):
        log("  [1/2] 检查模块链接 ...")
        ok, msg = run_heal(env)
        log("        %s %s" % ("[OK]" if ok else "[!]", msg))
    else:
        log("  [1/2] 跳过模块链接检查（该机制在当前 dsh 版本中不存在）")

    # ---- 第 2 步：按候选顺序尝试启动 ----
    log("  [2/2] 拉起 dsh")
    log()
    line("-")
    log("  提示：浏览器里若提示 authentication required，")
    log("        把下面那行 dsh web: 的完整地址（含 ?token=）复制过去即可。")
    line("-")
    log()

    penv = pinned_env()
    chosen = None
    rc = None
    interrupted = False
    was_live = False
    for i, c in enumerate(cmds, 1):
        log("  尝试 %d/%d：%s" % (i, len(cmds), c["label"]))
        t0 = time.time()
        try:
            # cmdline（字符串命令行）优先：cmd /s /c 的整体外引号只有用
            # 字符串直传才不会被 list2cmdline 转义坏
            rc = subprocess.call(c.get("cmdline") or c["argv"],
                                 cwd=rpath, env=penv)
        except KeyboardInterrupt:
            log()
            log("  已中断。")
            rc, interrupted = -1, True
            break
        except OSError as e:
            log("  [!] 无法执行该候选：%s" % e)
            rc = -2
            continue
        dt = time.time() - t0
        # 判据：【端口探活】而不是运行时长 ——
        #   起来了才算成功；崩得再晚也要换下一个候选
        if live_now(env):
            chosen = c["label"]
            was_live = True
            break
        if rc == 0:
            chosen = c["label"]
            break
        log("  [!] 该命令 %.1f 秒就退出（退出码 %s），换下一个候选..." % (dt, rc))
        log()

    log()
    line("=")
    if interrupted:
        log("  已中断（dsh 未启动或已停止）")
    elif chosen:
        if was_live:
            log("  dsh 已在后台运行    使用的命令：%s（启动器退出码 %s）"
                % (chosen, rc))
        else:
            log("  dsh 已退出    使用的命令：%s    退出码 = %s" % (chosen, rc))
    else:
        log("  所有候选命令都失败了，退出码 = %s" % rc)
        log("  建议：菜单 [5] 看环境探测报告；或检查 dsh 是否已安装依赖。")
    line("=")
    log()
    pause("  回车关闭窗口...")


def live_now(env=None):
    """dsh 是否真的起来了（实际配置端口 + 全部候选端口探活）。

    [!] 不能只看 DEFAULT_PORTS 前几个 —— 用户把端口配成 3000/8080 时
        会误判「没起来」而接着拉起第二个实例。
    """
    ports = []
    if env and env.get("port"):
        ports.append(env["port"]["value"])
    ports += dsh_env.DEFAULT_PORTS
    for p in dict.fromkeys(ports):
        if dsh_env.is_dsh_here(p):
            return True
    return False


def run_plugins(args=None):
    p = os.path.join(TOOLS, "dsh-plugins.py")
    if not os.path.exists(p):
        log("  [X] 找不到插件工具：%s" % p)
        pause()
        return
    subprocess.call([sys.executable, p] + (args or []))


def action_check():
    line("=")
    log("  插件冲突检查")
    line("=")
    log()
    p = os.path.join(TOOLS, "dsh-plugins.py")
    if not os.path.exists(p):
        log("  [X] 找不到插件工具：%s" % p)
    else:
        try:
            r = subprocess.run([sys.executable, p, "--check"],
                               capture_output=True, creationflags=FLAGS,
                               timeout=600)
            log(r.stdout.decode("gbk", errors="replace").rstrip())
            log()
            log("  → 存在高危项。可在主菜单选 [2] 进入插件管理处理。"
                if r.returncode == 2 else "  → 未发现高危冲突。")
        except subprocess.TimeoutExpired:
            log("  [X] 冲突检查超时（600 秒）")
    log()
    pause()


def action_heal():
    env = probe()
    line("=")
    log("  补齐模块链接")
    line("=")
    log()
    feats = (env or {}).get("features") or {}
    if not feats.get("has_fallback"):
        log("  当前 dsh 版本没有 .dsh-module-fallback 机制，无需此操作。")
        log()
        pause()
        return

    log("  说明：dsh 每次启动只建它需要的那部分链接（实测 220/285），并会在下次")
    log("        启动时把它不要的删掉。所以「缺 65 个」是它的正常状态，不影响使用。")
    log()
    log("  [!] 实测：预先全部补齐【反而慢约 4 秒】（dsh 启动后要删掉多余的），")
    log("      所以启动流程里已跳过这一步。这里保留手动入口供排查链接问题。")
    log()
    ok, msg = run_heal(env, deep=True)
    log("  %s %s" % ("[OK]" if ok else "[X]", msg))
    log()
    pause()


def action_env():
    line("=")
    log("  环境探测报告")
    line("=")
    log()
    # [!] force 路径会串行跑 `pnpm dsh --help`（最坏 90s）和 --dump-config
    #     （最坏 240s）—— 必须有护栏和预告，否则异常直接炸穿菜单、
    #     正常时又是几分钟无输出的「死窗口」
    log("  正在强制重探（`dsh --help` + `--dump-config`，最坏约 5 分钟）...")
    try:
        log(dsh_env.report(dsh_env.detect(force=True, want_dump=True)))
    except Exception as e:
        log("  [X] 探测失败：%s" % e)
        log("      可尝试删除缓存后重试：%s" % dsh_env.CACHE)
    log()
    log("  配置覆盖：%s" % dsh_env.CFG)
    log("  （该文件可选；不存在时全部自动探测）")
    log()
    pause()


def wait_any_key(seconds=1.0, prompt="  即将启动 dsh，按任意键进入菜单"):
    """等 seconds 秒。有按键 → True（进菜单）；超时 → False（自动启动）。"""
    try:
        import msvcrt
    except Exception:
        return False
    try:
        while msvcrt.kbhit():
            msvcrt.getch()
    except Exception:
        pass

    def clear():
        sys.stdout.write("\r" + " " * 70 + "\r")
        sys.stdout.flush()

    t0 = time.time()
    shown = None
    try:
        while True:
            el = time.time() - t0
            if el >= seconds:
                break
            if msvcrt.kbhit():
                msvcrt.getch()
                clear()
                return True
            tenth = int(round((seconds - el) * 10))
            if tenth != shown:
                shown = tenth
                sys.stdout.write("\r  %s … %d.%d 秒   "
                                 % (prompt, tenth // 10, tenth % 10))
                sys.stdout.flush()
            time.sleep(0.02)
    except Exception:
        clear()
        return False
    clear()
    return False


MENU = """
============================================================
  DeepSeek Harness  启动器
============================================================

  [1] 启动 dsh                        （默认，直接回车）
  [2] 插件管理 —— 冲突检查 / 启用禁用
  [3] 只检查插件冲突
  [4] 补齐模块链接
  [5] 环境探测报告

  [0] 退出
"""


def main():
    argv = sys.argv[1:]

    if "--start" in argv:
        action_start(assume_yes="--yes" in argv)
        return 0
    if "--check" in argv:
        action_check()
        return 0
    if "--env" in argv:
        action_env()
        return 0

    # ---- 首屏：1 秒窗口 ----
    if "--menu" not in argv:
        if os.name == "nt":
            os.system("title DeepSeek Harness")
            os.system("cls")
        log("=" * 60)
        log("  DeepSeek Harness  启动器")
        log("=" * 60)
        log()
        if not wait_any_key(AUTO_START_SECONDS):
            log("  未检测到按键 —— 自动启动 dsh。")
            log()
            action_start(assume_yes="--yes" in argv)
            return 0
        log()
        log("  检测到按键 —— 进入菜单。")
        time.sleep(0.4)

    return _main_menu()


def _main_menu():
    while True:
        if os.name == "nt":
            os.system("title DeepSeek Harness")
            os.system("cls")
        else:
            os.system("clear")
        log(MENU)
        try:
            cmd = input("  选择 [1] > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        low = cmd.lower()
        if low in ("0", "q", "quit", "exit"):
            return 0
        # [!] 各动作期间的 Ctrl+C 一律返回菜单，不再整个退出
        #     （旧行为：检查/探测 600 秒内按 Ctrl+C 会直接关掉启动器）
        try:
            if low in ("", "1"):
                action_start()
                return 0
            if low == "2":
                run_plugins()
            elif low == "3":
                action_check()
            elif low == "4":
                action_heal()
            elif low == "5":
                action_env()
            else:
                log("  无效输入：%s" % cmd)
                time.sleep(1.2)
        except KeyboardInterrupt:
            print()
            log("  已中断，返回菜单。")
            time.sleep(0.6)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        sys.exit(0)
