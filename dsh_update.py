# -*- coding: utf-8 -*-
"""
dsh_update.py —— dsh 上游更新检查（**永不阻碍启动**）

它做什么
--------
启动器起来时在**后台线程**里查一次「本机 dsh 仓库相对上游落后几个提交」，
结果落一份小 JSON；菜单每帧读那份 JSON（毫秒级）显示一行状态。
菜单里另有 [9] 由**人**触发才拉取（`git pull --ff-only`）。

四条「不阻碍」纪律（每条都有测试钉住）
--------------------------------------
1. **后台且不等**：检查跑在 daemon 线程里，启动器主流程**不 join**。
   首屏 1 秒窗口照旧；dsh 启动流程照旧。
2. **慢探测不碰**：拿仓库路径只走 `dsh_env.detect()` 的**落盘缓存**
   （毫秒）。缓存不可用/仓库定位不到 => **直接放弃检查**（fail-open），
   绝不为了"给个提示"去跑冷探测（最坏 90 秒）。
3. **超时有界**：每条 git 命令都有 timeout（fetch 20s / 查询 8s）。
   超时 => 当作"检查失败"，静默。
4. **失败静默**：任何异常都不外泄、不打印、不影响菜单；状态写进 JSON，
   菜单据此决定显示什么（失败 => 什么都不显示）。

节流：默认 1 小时内不重复 fetch（避免每次双击都打网络）。
`force=True` / 命令行 `--update-check` 可绕过。

只读与写的边界
--------------
- 检查：**只读**（git fetch 只更新远端引用，不动工作区）。
- 拉取：**只快进**（`--ff-only`），工作区脏 => 拒绝并说明；**不自动重启 dsh**。
"""
import json
import os
import shutil
import subprocess
import threading
import time

import dsh_env

# ── 配置 ────────────────────────────────────────────────────────────────────
STATE_DIR = os.path.join(dsh_env.DSH_STATE, "launcher")
STATE_FILE = os.path.join(STATE_DIR, "update-check.json")
TTL_SECONDS = 3600          # 节流窗口：1 小时
FETCH_TIMEOUT = 20          # git fetch 上限（网络最坏情况）
QUERY_TIMEOUT = 8            # 本地查询上限
STATE_RUNNING = "checking"  # 线程在跑
STATE_OK = "ok"
STATE_FAIL = "fail"

GIT_FALLBACKS = (
    r"C:\Program Files\Git\cmd\git.exe",
    r"C:\Program Files\Git\bin\git.exe",
)


# ── 小工具 ──────────────────────────────────────────────────────────────────
def _now():
    return int(time.time())


def git_exe():
    """定位 git：先问 PATH，再问两个常见安装位。

    找不到就返回 None —— 调用方据此 fail-open（不做无谓的异常处理）。
    """
    g = shutil.which("git")
    if g:
        return g
    for p in GIT_FALLBACKS:
        if os.path.isfile(p):
            return p
    return None


def run_git(repo, args, timeout):
    """在 repo 里跑一条 git。**永不抛**：失败一律返回 (None, "")。

    参数用列表（不过 shell），输出按 utf-8/GBK 兜底解码 —— git 在中文
    Windows 上会输出 GBK。
    """
    g = git_exe()
    if not g or not repo or not os.path.isdir(repo):
        return None, ""
    try:
        r = subprocess.run(
            [g, "-C", repo] + list(args),
            capture_output=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return None, ""          # 超时：当作没查到
    except Exception:
        return None, ""
    out = r.stdout.decode("utf-8", "replace")
    if not out.strip():
        out = r.stdout.decode("gbk", "replace")
    return r.returncode, out.strip()


def read_state():
    """读缓存状态。**只读一个 JSON**（菜单每帧调，必须毫秒级）。"""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def write_state(d):
    """落状态。任何失败都吞掉（状态文件只是提示，不是必需品）。"""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)   # 原子替换：菜单不会读到半个文件
        return True
    except Exception:
        return False


def fresh_enough(state, force=False):
    """节流：上一次成功检查在窗口内 => 跳过。

    只有 **ok** 算"新鲜"：失败不该被缓存一小时（下次启动重试才对）。
    """
    if force:
        return False
    if state.get("state") != STATE_OK:
        return False
    try:
        return (_now() - int(state.get("checked_at") or 0)) < TTL_SECONDS
    except Exception:
        return False


def detached(repo):
    """HEAD 是游离的吗（不在任何分支上）。

    [!] 实测本机 DeepSeek-Harness 常停在 `detached at dsh-v0.1.5-rc.3`
        —— 这种状态下 `@{u}` 不存在，「落后几个提交」没有分支可比较，
        基准要退到 origin/HEAD；拉取则**不能**做（会改 HEAD 指向）。
    """
    rc, out = run_git(repo, ["symbolic-ref", "-q", "HEAD"], QUERY_TIMEOUT)
    return rc != 0


def upstream_ref(repo):
    """比较基准引用：先问本分支上游，没有就退到 origin/HEAD。

    返回 "" 表示连远端默认分支都拿不到（此时不能报"落后数"）。
    """
    rc, out = run_git(repo, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], QUERY_TIMEOUT)
    if rc == 0 and out:
        return out
    # 游离 HEAD（实测常态）或上游未配置 => 用远端默认分支
    rc, out = run_git(repo, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], QUERY_TIMEOUT)
    if rc == 0 and out:
        return out
    # 兜底：origin/master（实测该仓库的默认分支是 master，不是 main）
    rc, out = run_git(repo, ["rev-parse", "--verify", "--quiet", "origin/master"], QUERY_TIMEOUT)
    return "origin/master" if rc == 0 and out else ""


def upstream_remote_branch(repo):
    """把 upstream_ref() 拆成 (remote, branch)，给 `git pull <remote> <branch>` 用。

    [!] `git checkout -b X` 建出来的本地分支**没有 upstream**（实测 2026-10-09：
        `fatal: no upstream configured`），裸 `git pull` 会直接报
        "There is no tracking information for the current branch" —— 光按提示
        建分支根本拉不动。显式给 remote/branch 才走得通。
    拿不到基准 => ("", "")，调用方退回裸 pull（注定失败，但错误信息归 git 说）。
    """
    u = upstream_ref(repo)
    if not u or "/" not in u:
        return "", ""
    remote, branch = u.split("/", 1)
    return remote, branch


def _parse_count(out):
    """把 `git rev-list --count` 的输出解析成 int。**解析不了 => None**（不是 0）。

    单独抽出来是为了能直接测它：原先这段埋在 behind_count 的 try 里，
    「输出畸形」那条分支在正常仓库路径上**不可达**（变异体等于没测）。
    「查不到」和「0」是两件事 —— 这里返回 None 就是「查不到」。
    """
    s = (out or "").strip()
    if not s:
        return None
    try:
        return int(s.split()[0])
    except Exception:
        return None


def behind_count(repo):
    """落后基准分支几个提交。**没有基准/命令失败 => None**（不是 0！

    「查不到」和「已是最新」是两件事 —— 把 None 当 0 会撒谎。
    [!] 基准来自 upstream_ref()（可能是 origin/HEAD → origin/master），
        不能在这里写死 @{u}：游离 HEAD 下它不存在。
    """
    ref = upstream_ref(repo)
    if not ref:
        return None
    rc, out = run_git(repo, ["rev-list", "--count", "HEAD.." + ref], QUERY_TIMEOUT)
    if rc != 0 or not out:
        return None
    try:
        return int(out.split()[0])
    except Exception:
        return None


def head_short(repo):
    """本机 HEAD 短 hash（给用户看"现在在哪个版本"）。"""
    rc, out = run_git(repo, ["rev-parse", "--short", "HEAD"], QUERY_TIMEOUT)
    return out if rc == 0 and out else ""


def head_full(repo):
    """本机 HEAD **完整** sha —— 回退要用（短 hash 可能撞车，git 也不接受）。"""
    rc, out = run_git(repo, ["rev-parse", "HEAD"], QUERY_TIMEOUT)
    return out if rc == 0 and out else ""


def commit_exists(repo, sha):
    """这个 commit 还在吗（可能被 gc / 从未有过）—— 回退前必须问。"""
    if not sha:
        return False
    rc, _ = run_git(repo, ["cat-file", "-e", "%s^{commit}" % sha], QUERY_TIMEOUT)
    return rc == 0


def upstream_subject(repo):
    """上游最新一条提交标题（给用户看"要更新到什么"）。"""
    u = upstream_ref(repo)
    if not u:
        return ""
    rc, out = run_git(repo, ["log", "-1", "--format=%s", u], QUERY_TIMEOUT)
    return out if rc == 0 else ""


def is_dirty(repo):
    """工作区有**已跟踪文件**的未提交改动吗（拉取/回退前的安全闸）。

    [!] 只看已跟踪文件（--untracked-files=no）。未跟踪的草稿/数据文件
        不会因 ff-only 拉取而丢失（真撞上同名路径，git 自己会拒绝覆盖、
        而不是默默覆盖），但把它们也算脏的话，本机常年躺着的
        _draft_*.json / data/ 会让**更新闸门永远关着**：
        2026-10-09 实测 8 个 untracked 条目 => do_pull 恒拒绝。
    """
    rc, out = run_git(repo, ["status", "--porcelain", "--untracked-files=no"], QUERY_TIMEOUT)
    if rc != 0:
        return False
    return bool(out)


# ── 检查（后台跑的那个）────────────────────────────────────────────────────
def repo_path_quick():
    """**毫秒级**拿仓库路径：只用 detect 的落盘缓存，绝不冷探测。

    拿不到 => 返回 None（调用方 fail-open）。
    """
    try:
        env = dsh_env.detect()               # 有进程内/落盘缓存，命中即返回
    except Exception:
        return None
    if not isinstance(env, dict):
        return None
    repo = (env.get("repo") or {}).get("path")
    return repo if repo and os.path.isdir(repo) else None


def do_check(force=False, fetch=True):
    """跑一次检查（同步）。**不抛异常**，返回状态 dict。

    正常路径只做本地查询（毫秒~秒级）；fetch 才打网络（限 20s）。
    """
    repo = repo_path_quick()
    if not repo:
        write_state({"state": STATE_FAIL, "checked_at": _now(), "why": "定位不到仓库"})
        return read_state()
    if fresh_enough(read_state(), force=force):
        return read_state()

    if fetch:
        # 只更新远端引用（origin/main 等），**不动工作区** —— 这一步慢也无妨，
        # 它在后台线程里，主流程不 join。
        run_git(repo, ["fetch", "--quiet", "--prune"], FETCH_TIMEOUT)

    behind = behind_count(repo)
    if behind is None:
        write_state({"state": STATE_FAIL, "checked_at": _now(), "why": "没有上游或查询失败",
                     "head": head_short(repo)})
        return read_state()

    st = {"state": STATE_OK, "checked_at": _now(), "behind": behind,
          "head": head_short(repo), "repo": repo,
          "upstream": upstream_ref(repo), "detached": detached(repo)}
    if behind > 0:
        st["subject"] = upstream_subject(repo)
    write_state(st)
    return st


def spawn_background_check():
    """启动后台检查线程。**立即返回**，调用方不 join。

    线程是 daemon：即使它还没跑完，启动器退出也不会被挂住。
    已经新鲜（1h 内查过）=> 连线程都不起。
    """
    if fresh_enough(read_state()):
        return None
    write_state({"state": STATE_RUNNING, "checked_at": _now()})

    def worker():
        try:
            do_check()
        except Exception:
            write_state({"state": STATE_FAIL, "checked_at": _now(), "why": "后台线程异常"})

    t = threading.Thread(target=worker, name="dsh-update-check", daemon=True)
    try:
        t.start()
    except Exception:
        write_state({"state": STATE_FAIL, "checked_at": _now(), "why": "线程起不来"})
        return None
    return t


# ── 拉取（只有人点 [9] 才走这里）──────────────────────────────────────────
def do_pull():
    """快进拉取。**永不抛**，返回 (ok: bool, msg: str)。

    三道闸：① 没有落后 ② 工作区脏 ③ 只 --ff-only（不产生合并提交）。
    拉完**不重启 dsh** —— 重启是人的决定（启动器 [8]）。
    """
    st = read_state()
    repo = st.get("repo") or repo_path_quick()
    if not repo:
        return False, "定位不到仓库"
    behind = st.get("behind")
    if not isinstance(behind, int):
        # 只有「状态里没有可用数字」才重查；**behind=0 是明确结论，不能当成过期**
        # （2026-10-05 被回归用例抓到：原先 behind<=0 就重查 → 0 被重查成 2 → 免拉失效）
        behind = behind_count(repo)
    if behind is None:
        return False, "没有上游分支（或查询失败）—— 检查一下 remote"
    if behind == 0:
        return True, "已是最新，无需拉取"
    if detached(repo):
        return False, ("当前是游离 HEAD（detached），拉取会改动 HEAD 指向 —— 已拒绝。"
                       "先建一个本地分支站上去（留在现在的提交上），再回来拉：\n"
                       "         git checkout -b main\n"
                       "     （建完本工具会按上游默认分支比对/拉取，不用手动配 upstream）")
    if is_dirty(repo):
        n = len(run_git(repo, ["status", "--porcelain", "--untracked-files=no"],
                        QUERY_TIMEOUT)[1].splitlines())
        return False, ("工作区有未提交改动（%d 项，只算已跟踪文件），已拒绝拉取 —— "
                       "先 commit 或 stash 再试" % n)
    # [!] **先记住"从哪来"** —— 更新后要是不满意，回退就靠这条记录。
    #     必须**在 pull 之前**取（pull 后 HEAD 已变）；记在状态文件里，
    #     不动仓库（不打 tag、不建分支、不碰用户的 git 状态）。
    prev_full = head_full(repo)
    # 显式 remote/branch：无 upstream 的本地分支（checkout -b 的常态）上，
    # 裸 `git pull` 报 "no tracking information"，永远拉不动（2026-10-09 实测）。
    remote, branch = upstream_remote_branch(repo)
    if remote and branch:
        rc, out = run_git(repo, ["pull", "--ff-only", "--quiet", remote, branch], FETCH_TIMEOUT)
    else:
        rc, out = run_git(repo, ["pull", "--ff-only", "--quiet"], FETCH_TIMEOUT)
    if rc != 0:
        hint = ""
        if "untracked working tree files would be overwritten" in (out or ""):
            hint = "（有未跟踪文件与要拉入的文件同名冲突 —— 先挪走/改名那个文件再试）"
        return False, "拉取失败：%s%s" % (out or "git pull 返回 %s" % rc, hint)
    new_head = head_short(repo)
    new_full = head_full(repo)
    st = {"state": STATE_OK, "checked_at": _now(), "behind": 0,
          "head": new_head, "repo": repo}
    if prev_full and new_full and prev_full != new_full:
        st["last_pull"] = {"from": prev_full, "from_short": prev_full[:9],
                           "to": new_full, "to_short": new_head,
                           "count": behind, "at": _now()}
    write_state(st)
    msg = "已快进到 %s（拉了 %d 个提交）。**重启 dsh 后生效**：启动器 [8]" % (new_head or "?", behind)
    if st.get("last_pull"):
        msg += "\n     万一不合适：菜单 [9] 可回退到 %s" % st["last_pull"]["from_short"]
    return True, msg


# ── 回退（更新后不满意 ⇒ 回到更新前的那个 commit）──────────────────────────
def rollback_target():
    """可回退的目标：上次更新前的那个 commit。没记录返回 ""。

    存在状态文件的 `last_pull.from`；**不额外查 git**（菜单渲染要毫秒级，
    真校验留到真的要回退时）。
    """
    try:
        lp = read_state().get("last_pull") or {}
        return str(lp.get("from") or "")
    except Exception:
        return ""


def can_rollback():
    """能不能回退：有记录 + 那个 commit 还在 + 当前 HEAD 不是它。"""
    sha = rollback_target()
    if not sha:
        return False, "没有回退记录（上次更新是本工具之外做的，或还没更新过）"
    repo = read_state().get("repo") or repo_path_quick()
    if not repo:
        return False, "定位不到仓库"
    if not commit_exists(repo, sha):
        return False, "记录里的版本 %s 已不在仓库里（可能被 gc 了）—— 无法回退" % sha[:9]
    cur = head_full(repo)
    if cur and cur == sha:
        return False, "当前已经在 %s 上，没什么可回退的" % sha[:9]
    return True, repo


def do_rollback():
    """回退到上次更新前的版本。**永不抛**，返回 (ok, msg)。

    两道闸 + 一个选择：
    ① 工作区脏 ⇒ 拒绝（回退不该吃掉别人的改动）；
    ② 目标 commit 不存在 ⇒ 拒绝；
    ③ 用 `git checkout --detach <sha>` 而不是 `reset --hard`：
       **不改动任何分支的历史**，出问题也能再 checkout 回来。
    """
    ok, info = can_rollback()
    if not ok:
        return False, info
    repo = info
    sha = rollback_target()
    if is_dirty(repo):
        n = len(run_git(repo, ["status", "--porcelain"], QUERY_TIMEOUT)[1].splitlines())
        return False, ("工作区有未提交改动（%d 项），已拒绝回退 —— "
                       "先 commit 或 stash 再试" % n)
    rc, out = run_git(repo, ["checkout", "--detach", sha], FETCH_TIMEOUT)
    if rc != 0:
        return False, "回退失败：%s" % (out or "git checkout 返回 %s" % rc)
    now = head_short(repo)
    st = read_state()
    st["state"] = STATE_OK
    st["checked_at"] = _now()
    st["head"] = now
    st["behind"] = behind_count(repo)
    st["rolled_back_at"] = _now()
    write_state(st)
    return True, ("已回退到 %s（游离 HEAD）。**重启 dsh 后生效**：启动器 [8]\n"
                  "     想回到分支：git checkout main" % (now or sha[:9]))


# ── 菜单呈现（毫秒级）──────────────────────────────────────────────────────
def badge():
    """菜单里那一行状态。**失败/无 => 返回空串**（静默，不制造噪音）。

    读缓存 JSON 而非跑 git —— 与 _safe_badge 同纪律：菜单第一屏不能被拖慢。
    """
    st = read_state()
    s = st.get("state")
    if s == STATE_RUNNING:
        return "  ⏳ 正在后台检查 dsh 更新…\n"
    if s != STATE_OK:
        return ""
    behind = st.get("behind")
    head = st.get("head") or ""
    if not isinstance(behind, int):
        return ""
    if behind <= 0:
        return "  √ dsh 已是最新%s\n" % ("（%s）" % head if head else "")
    ref = st.get("upstream") or "上游"
    if st.get("detached"):
        return ("  ⬆ 落后 %s %d 个提交（当前游离 HEAD %s）—— 拉取需先切分支\n"
                % (ref, behind, head or "?"))
    line = "  ⬆ %s 有 %d 个新提交（当前 %s）—— 菜单 [9] 可拉取\n" % (ref, behind, head or "?")
    if st.get("last_pull") and st["last_pull"].get("from_short"):
        line += "     上次更新：%s → %s（不合适可用 [9] 回退）\n" % (
            st["last_pull"].get("from_short"), st["last_pull"].get("to_short") or "?")
    return line


def summary_lines():
    """`--update-check` 的输出（给命令行/脚本看）。"""
    st = do_check(force=True)
    if st.get("state") != STATE_OK:
        return ["检查失败：%s" % (st.get("why") or "未知原因")]
    behind = st.get("behind", 0)
    lines = ["当前 HEAD：%s" % (st.get("head") or "?"),
             "上游：%s" % (st.get("upstream") or "?"),
             "落后：%d 个提交" % behind]
    if behind > 0 and st.get("subject"):
        lines.append("上游最新：%s" % st["subject"])
    lp = st.get("last_pull") or {}
    if lp.get("from_short"):
        lines.append("上次更新：%s -> %s（%d 个提交，%s）"
                     % (lp.get("from_short"), lp.get("to_short") or "?",
                        lp.get("count", 0),
                        time.strftime("%Y-%m-%d %H:%M", time.localtime(lp.get("at") or 0))))
        lines.append("要回退：python dsh-launcher.py --update-rollback")
    return lines


if __name__ == "__main__":
    import sys
    if "--json" in sys.argv:
        print(json.dumps(do_check(force=True), ensure_ascii=False, indent=2))
    else:
        for ln in summary_lines():
            print(ln)