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
  [6] 安全模式启动（禁用全部外部插件）
  [7] 恢复正式模式（还原安全模式禁掉的插件）
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
# 后台启动 dsh：无窗口 + 新进程组（Ctrl+C 只打断启动器，不会顺手杀掉 dsh）
BG_FLAGS = FLAGS | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
BG_WAIT_MAX = 180        # 后台启动等端口探活的上限（秒；冷启动 15s 基线，最坏 120s+）
BG_POLL = 2              # 探活轮询间隔（秒）
BG_LOG_KEEP = 10         # 后台日志保留份数（多了删最旧）

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


def _action_start_impl(assume_yes, safe=False):
    env = probe()
    line("=")
    log("  启动 dsh" + ("（安全模式）" if safe else ""))
    line("=")
    log()

    def confirm(msg):
        if assume_yes:
            log("  （--yes：自动确认）%s" % msg.strip())
            return True
        return ask(msg).strip().lower() == "y"

    # ---- 安全模式状态提示：普通启动撞上安全模式状态时必须说清楚 ----
    if not safe:
        try:
            P = _plugins_mod()
            if P:
                prof = (env or {}).get("profile") or {}
                in_safe, meta = P.safemode_status(prof.get("patch_file"))
                if in_safe:
                    log("  [!] 当前处于【安全模式】（%s 进入，禁用 %s 个外部插件）。"
                        % (meta.get("ts"), meta.get("disabled")))
                    log("      继续将以安全模式启动；要恢复正式模式请用菜单 [7]。")
                    if not confirm("      仍要继续启动吗？(y/N) > "):
                        return
                    log()
        except Exception:
            pass

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

    # ---- 第 2 步：按候选顺序尝试启动（后台 + 端口探活）----
    log("  [2/2] 拉起 dsh（后台运行，日志落盘）")
    log()
    line("-")
    log("  提示：dsh 将在后台运行，本终端稍后回到菜单。")
    log("        日志在 ~/.dsh/logs/dsh-bg-*.log（报错先看它）")
    log("        停止 dsh：菜单 [8] 重启，或桌面 结束-dsh.bat")
    log("        浏览器若提示 authentication required，")
    log("        把日志里那行 dsh web: 的完整地址（含 ?token=）复制过去即可。")
    line("-")
    log()

    penv = pinned_env()
    chosen = None
    rc = None
    interrupted = False
    was_live = False
    timed_out = False
    bg_logpath = None
    bg_proc = None
    for i, c in enumerate(cmds, 1):
        log("  尝试 %d/%d：%s" % (i, len(cmds), c["label"]))
        t0 = time.time()
        try:
            status, rc, bg_logpath, bg_proc = _spawn_bg_and_wait(
                c, rpath, penv, env)
        except KeyboardInterrupt:
            # [!] 后台模式下 Ctrl+C 只打断「等待」—— dsh 在新进程组里
            #     收不到控制台的 SIGINT，若已起来就让它继续跑
            log()
            log("  已停止等待（dsh 若已在后台运行，不受影响）。")
            log("  要停 dsh：菜单 [8] 重启，或桌面 结束-dsh.bat。")
            interrupted = True
            break
        except OSError as e:
            log("  [!] 无法执行该候选：%s" % e)
            rc = -2
            continue
        dt = time.time() - t0
        # 判据：【端口探活】而不是运行时长 ——
        #   起来了才算成功；崩得再晚也要换下一个候选
        if status == "bg":
            chosen = c["label"]
            was_live = True
            break
        if status == "timeout":
            chosen = c["label"]
            timed_out = True
            break
        # status == "exit"：进程自己退出了
        if rc == 0:
            chosen = c["label"]
            break
        log("  [!] 该命令 %.1f 秒就退出（退出码 %s），日志尾部：" % (dt, rc))
        _log_tail(bg_logpath)
        log("  换下一个候选...")
        log()

    log()
    line("=")
    if interrupted:
        log("  已停止等待（dsh 可能仍在后台；看日志 %s）"
            % (bg_logpath or "~/.dsh/logs/"))
    elif chosen and was_live:
        log("  dsh 已在后台运行    使用的命令：%s" % chosen)
        if bg_proc is not None and bg_proc.pid:
            log("  进程 PID：%s（树根，结束用菜单 [8] 或 结束-dsh.bat）" % bg_proc.pid)
        log("  日志：%s" % bg_logpath)
        log("  本终端已交还菜单 —— 停止 dsh 用菜单 [8] 或桌面 结束-dsh.bat。")
    elif chosen and timed_out:
        log("  等了 %d 秒还没探到端口，但进程仍在（可能正在慢启动）。" % BG_WAIT_MAX)
        log("  日志：%s" % bg_logpath)
        log("  稍后可用浏览器直接访问；要重来一次用菜单 [8] 重启。")
    elif chosen:
        log("  dsh 已退出    使用的命令：%s    退出码 = %s" % (chosen, rc))
    else:
        log("  所有候选命令都失败了，退出码 = %s" % rc)
        log("  建议：菜单 [5] 看环境探测报告；或检查 dsh 是否已安装依赖。")
    if safe:
        log("  （安全模式）外部插件已临时禁用；恢复正常请用菜单 [7] 恢复正式模式。")
    line("=")
    log()
    pause("  回车返回菜单...")


def live_now(env=None):
    """dsh 是否真的起来了（配置端口 + 候选端口 + node 全端口兜底）。

    [!] 不能只看 DEFAULT_PORTS 前几个 —— 用户把端口配成 3000/8080 时
        会误判「没起来」而接着拉起第二个实例。
    [!] 候选端口全落空时再扫所有 node.exe 监听端口兜底 —— dsh 若改绑
        表外端口（dsh-mobile 的 3443 那类），不能被判成「启动失败」
        而去拉第二个实例。扫描内部按签名判定，探不动时保守按「在跑」。
    """
    ports = []
    if env and env.get("port"):
        ports.append(env["port"]["value"])
    ports += dsh_env.DEFAULT_PORTS
    for p in dict.fromkeys(ports):
        if dsh_env.is_dsh_here(p):
            return True
    return dsh_env.dsh_running_any_port()


def _bg_logfile():
    """开一个后台启动日志：~/.dsh/logs/dsh-bg-<时间戳>.log，返回 (路径, 句柄)。

    [!] dsh 的加载错误只走 stdout/stderr（hub.log 只有「启动了」三件套），
        所以 dsh 转后台后输出必须落文件 —— 丢日志 = 层5 故障瞎掉。
        旧日志按份数轮转（BG_LOG_KEEP）。
    """
    d = os.path.join(dsh_env.DSH_STATE, "logs")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        d = tempfile.gettempdir()
    ts = time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(d, "dsh-bg-%s.log" % ts)
    try:
        olds = sorted(f for f in os.listdir(d)
                      if f.startswith("dsh-bg-") and f.endswith(".log"))
        for f in olds[:-BG_LOG_KEEP] if len(olds) > BG_LOG_KEEP else []:
            try:
                os.remove(os.path.join(d, f))
            except OSError:
                pass
    except Exception:
        pass
    return path, open(path, "ab")


def _spawn_bg_and_wait(c, rpath, penv, env, wait_max=BG_WAIT_MAX):
    """后台拉起一个启动候选并等它真的起来。

    返回 (status, rc, logpath, proc)：
      ("bg",      None, path, proc) —— 端口探活成功，dsh 已在后台跑
      ("exit",    rc,   path, proc) —— 进程自己退出了（rc==0 视为 dsh 主动退出）
      ("timeout", None, path, proc) —— 等满 wait_max 秒还没探活，但进程仍活着
    KeyboardInterrupt 直接向上抛（由调用方决定「只中断等待、不杀 dsh」）。
    """
    logpath, bg_f = _bg_logfile()
    try:
        proc = subprocess.Popen(c.get("cmdline") or c["argv"],
                                cwd=rpath, env=penv,
                                stdin=subprocess.DEVNULL,
                                stdout=bg_f, stderr=bg_f,
                                creationflags=BG_FLAGS)
    finally:
        # 子进程已继承句柄，启动器这边的可以关了
        try:
            bg_f.close()
        except Exception:
            pass
    deadline = time.time() + max(5, wait_max)
    while True:
        if live_now(env):
            return ("bg", None, logpath, proc)
        rc = proc.poll()
        if rc is not None:
            return ("exit", rc, logpath, proc)
        if time.time() >= deadline:
            return ("timeout", None, logpath, proc)
        time.sleep(BG_POLL)


def _log_tail(path, n=12):
    """读后台日志尾部给用户看（层5 的真错误只在这里）。"""
    try:
        with open(path, "rb") as f:
            data = f.read()
        lines = data.decode("utf-8", errors="replace").strip().splitlines()
        for ln in lines[-n:]:
            log("    | %s" % ln)
    except Exception as e:
        log("    | （日志读不出：%s）" % e)


def action_restart():
    """重启 dsh：结束现有实例 → 等端口真释放 → 走完整启动流程。

    [!] 结束后必须等端口释放再启动 —— taskkill 是异步的，锁与监听
        收尾要几秒，撞上残留就是一轮新的脏锁故障。
    """
    line("=")
    log("  重启 dsh")
    line("=")
    log()
    env = probe()
    live = find_live_ports(env)
    if live:
        log("  正在结束 dsh（端口 %s）..." % ", ".join(map(str, live)))
        kill_leftover(env)
        if not _wait_ports_free(env):
            log("  [!] 等了 %d 秒端口仍未释放 —— 继续可能撞残留。" % BG_WAIT_MAX)
            log("      也可以稍后重试 [8]，或用结束-dsh.bat 后再 [1]。")
            log()
            if ask("      仍要继续启动吗？(y/N) > ").strip().lower() != "y":
                return
    else:
        log("  dsh 当前没在跑 —— 直接启动。")
    log()
    if not acquire_instance_lock():
        pause()
        return
    try:
        _action_start_impl(assume_yes=False)
    finally:
        release_instance_lock()


def _wait_ports_free(env, timeout=15):
    """等 dsh 的端口真正释放（live_now 彻底安静），返回是否释放。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not live_now(env):
            return True
        time.sleep(1)
    return not live_now(env)


def run_plugins(args=None):
    p = os.path.join(TOOLS, "dsh-plugins.py")
    if not os.path.exists(p):
        log("  [X] 找不到插件工具：%s" % p)
        pause()
        return
    subprocess.call([sys.executable, p] + (args or []))


def action_safe_start():
    """安全模式启动：备份补丁 → 整份替换为「只禁外部插件」的生成补丁 → 启动。

    类比 Windows 安全模式：某个外部插件把 dsh 搞挂时，仍能最小化进系统。
    全程可逆（菜单 [7] 还原）；进入失败（拿不到权威树等）绝不半途启动。
    """
    if not acquire_instance_lock():
        pause()
        return
    try:
        line("=")
        log("  安全模式启动 dsh")
        line("=")
        log()
        P = _plugins_mod()
        if not P:
            log("  [X] 找不到 dsh-plugins.py，安全模式不可用。")
            log()
            pause()
            return
        # 已在安全模式也照走 enter（内部幂等）：若补丁被手写改过（不再含生成标记），
        # 会重新生成全禁补丁 —— 每次点 [6] 出来的必须是最小集，不许带私货。
        # 首次最坏要跑一次 --dump-config（约 30 秒，上限 240 秒）
        log("  正在获取权威插件树并生成安全补丁（首次最坏约 4 分钟）...")
        log()
        try:
            st = P.collect()
        except Exception as e:
            log("  [X] 收集插件树失败：%s" % e)
            log()
            pause()
            return
        ok, note = P.safemode_enter(st.get("patch"),
                                    (st.get("profile") or {}).get("dir"),
                                    st.get("rows"), st.get("source"))
        log("  %s %s" % ("[OK]" if ok else "[X]", note))
        log()
        if not ok:
            pause()
            return
        _action_start_impl(assume_yes=False, safe=True)
    finally:
        release_instance_lock()


def action_restore():
    """恢复正式模式：按安全模式状态文件把原补丁原样还原。"""
    line("=")
    log("  恢复正式模式（还原安全模式禁用的插件）")
    line("=")
    log()
    P = _plugins_mod()
    if not P:
        log("  [X] 找不到 dsh-plugins.py。")
        log()
        pause()
        return
    env = probe()
    prof = (env or {}).get("profile") or {}
    in_safe, meta = P.safemode_status(prof.get("patch_file"))
    if not in_safe:
        log("  当前不在安全模式，无需还原。")
        log()
        pause()
        return
    log("  安全模式信息：进入于 %s，禁用了 %s 个外部插件。"
        % (meta.get("ts"), meta.get("disabled")))
    # dsh 在跑时建议先停：还原会立刻热重载回全部插件，
    # 安全模式下正「犯病」的那个插件会当场复活
    live = find_live_ports(env)
    if live:
        log("  [!] dsh 正在运行（端口 %s）。" % ", ".join(map(str, live)))
        if ask("      先结束 dsh 再还原？（推荐，Y/n）> ").strip().lower() in ("", "y"):
            kill_leftover(env)
    ok, note = P.safemode_restore(prof.get("patch_file"), prof.get("dir"))
    log("  %s %s" % ("[OK]" if ok else "[X]", note))
    log()
    if ok and ask("  要现在以正式模式启动 dsh 吗？(y/N) > ").strip().lower() == "y":
        action_start()
        return
    pause()


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

  [1] 启动 dsh                        （后台运行，回菜单）
  [2] 插件管理 —— 冲突检查 / 启用禁用
  [3] 只检查插件冲突
  [4] 补齐模块链接
  [5] 环境探测报告
  [6] 安全模式启动 —— 只留内置插件（dsh 起不来时的救命模式）
  [7] 恢复正式模式 —— 还原安全模式禁用的插件
  [8] 重启 dsh —— 结束现有实例再启动

  [0] 退出
"""

_PLUGINS = None


def _plugins_mod():
    """importlib 加载 dsh-plugins.py（文件名带连字符），进程内缓存。

    安全模式的补丁机械（备份/替换/还原）都在那边 —— 与 [2]/[3] 的
    子进程方式不同，这里直接 import：共享 dsh_env 单例缓存，
    collect() 不用重新探测，还能拿到结构化结果控制流程。
    """
    global _PLUGINS
    if _PLUGINS is None:
        p = os.path.join(TOOLS, "dsh-plugins.py")
        if not os.path.exists(p):
            return None
        import importlib.util
        spec = importlib.util.spec_from_file_location("dsh_plugins_lm", p)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        _PLUGINS = m
    return _PLUGINS


def _safe_badge():
    """菜单角标：是否处于安全模式。

    [!] 只做 glob + 读一个 JSON（毫秒级）—— 绝不能在这里 probe()
        （冷探测最坏 90 秒+，会把菜单第一屏拖成死窗口）。
    """
    try:
        import glob as _g
        pdir = os.path.join(dsh_env.DSH_STATE, "profiles", "web")
        if not os.path.isdir(pdir):
            return ""
        hits = [h for h in sorted(_g.glob(os.path.join(pdir, "cordis*.y*ml")))
                if ".bak" not in os.path.basename(h)
                and ".tmp" not in os.path.basename(h)]
        if not hits:
            return ""
        P = _plugins_mod()
        if not P:
            return ""
        in_safe, meta = P.safemode_status(hits[0])
        if in_safe:
            return ("\n  [!] 当前处于【安全模式】（%s 进入，禁用 %s 个外部插件）"
                    " —— 恢复用 [7]\n" % (meta.get("ts"), meta.get("disabled")))
    except Exception:
        pass
    return ""


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
            # 启动完回菜单（dsh 已在后台），不再整个退出
            return _main_menu()
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
        log(MENU + _safe_badge())
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
            # [!] [1]/[6] 启动成功后不再退出启动器 —— dsh 已转后台，
            #     回车即返回菜单（旧行为：dsh 一退整个窗口直接关掉）
            if low in ("", "1"):
                action_start()
            elif low == "2":
                run_plugins()
            elif low == "3":
                action_check()
            elif low == "4":
                action_heal()
            elif low == "5":
                action_env()
            elif low == "6":
                action_safe_start()
            elif low == "7":
                action_restore()
            elif low == "8":
                action_restart()
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
