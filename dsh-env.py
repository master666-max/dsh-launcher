# -*- coding: utf-8 -*-
"""命令行薄壳：真正的实现都在 dsh_env.py（可 import 的单例模块）。

保留这个文件只是为了 `python dsh-env.py --dump` 这类命令行用法。
"""
import os, sys, json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dsh_env

if __name__ == "__main__":
    argv = sys.argv[1:]
    want_dump = "--dump" in argv
    env = dsh_env.detect(force="--refresh" in argv, want_dump=want_dump)

    if "--json" in argv:
        print(json.dumps(env, ensure_ascii=False, indent=2, default=str))
    elif "--dump" in argv:
        # [!] 守卫：仓库或 profile 探测失败时不能直接取下标（会 TypeError）
        repo = (env.get("repo") or {}).get("path")
        prof = (env.get("profile") or {}).get("name")
        if not repo or not prof:
            print("（仓库或 profile 未定位到，无法生成 dump）")
        else:
            print(dsh_env.dump_config(repo, prof, [], force=True)
                  or "（--dump-config 不可用）")
    else:
        print(dsh_env.report(env))
