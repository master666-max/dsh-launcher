# -*- coding: utf-8 -*-
"""快速变异验证：每个变异只跑它对应的那一条用例。

[!] 为什么不用 dsh-mutate.py：全量版在本机（WorkBuddy 批量删除保护）
    还原源码时会被拦 —— 变异体留在生产文件里，测试在污染状态上跑，
    结论不可信（实测「抓到 6 / 漏掉 23」且污染了 dsh_update.py）。
    本脚本**只 write 不 delete**，还原可靠；代价是只验与每个变异对应的
    那一条用例（验证力等价，快一个数量级）。
[!] 跑它之前先确认没有别的变异进程在跑：后台还原会覆盖并行的手工修复。

用法：python _quick_mutate.py
原理与 dsh-mutate.py 相同（改锚点 → 相关用例必须失败 → 还原），
只是把「跑全量」换成「跑该变异对应的那一条」，快一个数量级。
"""
import importlib.util
import io
import os
import sys
import contextlib

T = os.path.dirname(os.path.abspath(__file__))
U = os.path.join(T, "dsh_update.py")

MUT = [
    ("behind=0 也当过期 -> 免拉闸失效", "t_update_pull_guards",
     '    if behind == 0:\n        return True, "已是最新，无需拉取"',
     '    if behind <= 0:\n        behind = behind_count(repo)'),
    ("节流恒真（每次启动都 fetch）", "t_update_throttle",
     '        return (_now() - int(state.get("checked_at") or 0)) < TTL_SECONDS',
     '        return True'),
    ("失败态也缓存（不再重试）", "t_update_throttle",
     '    if state.get("state") != STATE_OK:\n        return False',
     '    pass'),
    ("游离 HEAD 不拒绝拉取", "t_update_pull_guards",
     '    if detached(repo):\n        return False,',
     '    if False:\n        return False,'),
    ("脏工作区闸去掉", "t_update_pull_guards",
     '    if is_dirty(repo):',
     '    if False:'),
    ("查不到落后数谎报 0", "t_update_no_upstream",
     '    try:\n        return int(out.split()[0])\n    except Exception:\n        return None',
     '    try:\n        return int(out.split()[0])\n    except Exception:\n        return 0'),
    ("失败态不静默（菜单噪音）", "t_update_badge_states",
     '    if s != STATE_OK:\n        return ""',
     '    if s != STATE_OK:\n        return "  ?? 更新状态未知"'),
    ("超时向上抛", "t_update_timeout_no_raise",
     '    except subprocess.TimeoutExpired:\n        return None, ""',
     '    except subprocess.TimeoutExpired:\n        raise'),
    ("detached 下不退回 origin/HEAD", "t_update_detached",
     '    rc, out = run_git(repo, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], QUERY_TIMEOUT)\n    if rc == 0 and out:\n        return out',
     '    rc, out = run_git(repo, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], QUERY_TIMEOUT)\n    if False:\n        return out'),
]


def load_tests():
    spec = importlib.util.spec_from_file_location("dsh_tests_mod", os.path.join(T, "dsh_tests.py"))
    m = importlib.util.module_from_spec(spec)
    # 避免 __main__ 分支执行：spec 用的是普通名字，__name__ != "__main__"
    spec.loader.exec_module(m)
    return m


def run_case(fn, mod):
    """跑一条用例：返回 (是否失败, 失败原因)。

    [!] @case 装饰器**捕获**断言异常并记进 mod.FAILED（不外抛），
        所以这里必须比对 FAILED 长度，不能靠捕获异常判断。
    """
    buf = io.StringIO()
    before = len(mod.FAILED)
    try:
        with contextlib.redirect_stdout(buf):
            fn()
    except Exception as e:
        return True, "用例本身异常：%r" % e
    if len(mod.FAILED) > before:
        return True, str(mod.FAILED[-1])
    return False, ""


def main():
    tests = load_tests()
    src0 = open(U, "r", encoding="utf-8").read()
    caught, missed, broken = 0, [], []
    for desc, case_name, anchor, mutant in MUT:
        if anchor not in src0:
            broken.append((desc, "锚点失配"))
            continue
        with open(U, "w", encoding="utf-8", newline="") as f:
            f.write(src0.replace(anchor, mutant, 1))
        try:
            for m in list(sys.modules):
                if m == "dsh_update":
                    del sys.modules[m]      # 让用例重新 import 变异体
            fn = getattr(tests, case_name, None)
            if fn is None:
                broken.append((desc, "找不到用例 %s" % case_name))
                continue
            failed, why = run_case(fn, tests)
            if failed:
                caught += 1
                print("  [抓到了] %-34s <- %s" % (desc, case_name))
            else:
                missed.append(desc)
                print("  [漏了！] %-34s <- %s" % (desc, case_name))
        finally:
            with open(U, "w", encoding="utf-8", newline="") as f:
                f.write(src0)
            sys.modules.pop("dsh_update", None)
    print()
    print("变异 %d 条：抓到 %d，漏 %d，异常 %d" % (len(MUT), caught, len(missed), len(broken)))
    for d in missed:
        print("    [漏] %s" % d)
    for d, why in broken:
        print("    [异常] %s — %s" % (d, why))
    return 1 if (missed or broken) else 0


if __name__ == "__main__":
    sys.exit(main())