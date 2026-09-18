# -*- coding: utf-8 -*-
"""
dsh 插件管理（终端界面）
========================
功能：
  1. 列出所有插件入口及其启用/禁用状态
  2. 冲突检查（悬空补丁 / 重复 id / 缺 junction / 命名空间冲突 / 功能重叠）
  3. 交互式启用 / 禁用（写入 profile 的 cordis 补丁，官方机制，可逆，热重载）
  4. 一键补齐 .dsh-module-fallback 缺失链接

数据来源【优先级】：
  ① `dsh --profile X --dump-config` —— 【权威】dsh 自己输出的合成后 loader 树。
     含真实入口 id、实现包名、解析后的 disabled、以及每个入口来自哪个 bundle。
     （2026-09-17 起改为主数据源：自行解析 YAML 会漏掉跨 bundle 的 override，
       实测曾把 29 个禁用误报成 5 个。）
  ② 自行解析 profile + 各 bundle 的 cordis 补丁（dump 不可用时的降级路径）

启停 = 在 profile 补丁里写 `- id: <入口id>` + `disabled: true/false`。
[!] loader 入口 id 常常 ≠ 包名（dsh-mobile 的入口是 mobile-access）。

用法：
    python dsh-plugins.py            # 交互界面
    python dsh-plugins.py --check    # 只做冲突检查（有高危返回码 2）
    python dsh-plugins.py --list     # 只列清单
    python dsh-plugins.py --dump     # 显示数据源与统计
"""
import os, re, sys, json, time, glob, shutil, subprocess

TOOLS = os.path.dirname(os.path.abspath(__file__))
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import dsh_env as ENV             # 探测层单例（与 launcher 共享同一实例）
FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass


def log(m=""):
    print(m)
    sys.stdout.flush()


def pause_in(msg="  回车继续..."):
    try:
        input(msg)
    except (EOFError, KeyboardInterrupt):
        print()


def read_text(p):
    try:
        with open(p, "rb") as f:
            return f.read().decode("utf-8", "replace")
    except Exception:
        return ""


def read_json(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ==================== 降级：YAML 解析 ====================
ID_RE = re.compile(r"^(\s*)-\s*id:\s*([A-Za-z0-9@/_.\-]+)\s*$")


def indent_of(line):
    return len(line) - len(line.lstrip(" "))


def parse_cordis(text):
    """→ [{id,name,disabled,kind}]；kind: insert | override"""
    lines = text.splitlines()
    entries, insert_indent = [], None
    for i, raw in enumerate(lines):
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m_ins = re.match(r"^(\s*)-\s*insert:\s*$", line)
        if m_ins:
            insert_indent = indent_of(line)
            continue
        m = ID_RE.match(raw)
        if not m:
            continue
        ind = indent_of(raw)
        if insert_indent is not None and ind <= insert_indent:
            insert_indent = None
        kind = "insert" if (insert_indent is not None and ind > insert_indent) else "override"
        e = {"id": m.group(2), "name": None, "disabled": None, "kind": kind}
        j = i + 1
        k = None
        while j < len(lines):
            nxt = lines[j]
            if not nxt.strip() or nxt.lstrip().startswith("#"):
                j += 1
                continue
            nind = indent_of(nxt)
            if nind < ind or (nind == ind and re.match(r"^\s*-\s", nxt)):
                break
            if k is None:
                k = nind            # 第一个属性行定义入口层缩进
            # 只认入口层的 name/disabled，config: 子块里的同名键不算
            if nind == k:
                s = nxt.strip()
                mn = re.match(r"^name:\s*(.+)$", s)
                if mn and e["name"] is None:
                    e["name"] = mn.group(1).strip().strip("'\"")
                md = re.match(r"^disabled:\s*(.+)$", s)
                if md and e["disabled"] is None:
                    v = md.group(1).strip()
                    e["disabled"] = "expr" if v.startswith("!!js") else (v.lower() == "true")
            j += 1
        entries.append(e)
    return entries


def list_pkgs(root):
    """转调探测层实现（全工具链只保留一份 list_pkgs）。"""
    return ENV.list_pkgs(root)


# ==================== 收集状态 ====================
def collect(env=None, force=False):
    st = {"source": "yaml", "notes": [], "deps": {}, "bundles": [],
          "rows": [], "profile": None, "patch": None,
          "nm": None, "fb": None, "patchReload": None}

    if env is None:
        env = ENV.detect(force=force, want_dump=True) if ENV else None
    st["env"] = env

    if env and env.get("profile"):
        st["profile"] = env["profile"]
        st["patch"] = env["profile"].get("patch_file")
        st["notes"] += (env.get("notes") or [])
    else:
        st["notes"].append("探测层不可用，回退默认路径")
        base = os.path.join(os.path.expanduser("~"), ".dsh", "profiles", "web")
        st["profile"] = {"name": "web", "dir": base}
        st["patch"] = None

    pdir = (st["profile"] or {}).get("dir")
    st["nm"] = os.path.join(pdir, "node_modules") if pdir else None
    st["fb"] = (os.path.join(pdir, ".dsh-module-fallback", "node_modules")
                if pdir else None)

    pj = read_json(os.path.join(pdir, "package.json")) if pdir else None
    if pj:
        st["deps"] = pj.get("dependencies") or {}
        prof = ((pj.get("dsh") or {}).get("profile") or {})
        st["bundles"] = prof.get("bundles") or []
        st["patchReload"] = prof.get("patchReload")

    if not st["patch"] and pdir:
        # 不写死文件名：glob 掉 cordis 系列 yml/yaml，排除备份与临时文件
        for pat in ("cordis.patch.y*ml", "cordis.y*ml", "cordis*.y*ml"):
            hits = [h for h in sorted(glob.glob(os.path.join(pdir, pat)))
                    if ".bak" not in os.path.basename(h)
                    and ".tmp" not in os.path.basename(h)]
            if hits:
                st["patch"] = hits[0]
                break

    # 主路径：权威树
    dump = None
    if env and env.get("repo") and (st["profile"] or {}).get("name"):
        try:
            # [!] detect(force=True, want_dump=True) 刚强制跑过一遍权威 dump 并
            # 刷新了缓存；这里必须 force=False 走指纹缓存，否则 30 秒的
            # --dump-config 会被跑两遍（菜单 r / --check 都中招）
            dump = ENV.dump_config(env["repo"]["path"],
                                   st["profile"]["name"], st["notes"],
                                   force=False)
        except Exception as e:
            st["notes"].append("dump-config 异常：%s" % e)

    if dump:
        st["source"] = "dump"
        st["rows"] = build_rows_from_dump(ENV.parse_dump(dump))
    else:
        st["notes"].append("降级为自行解析补丁（状态可能不全）")
        st["rows"] = build_rows_from_yaml(st)
    return st


def build_rows_from_dump(ents):
    rows = []
    for e in ents:
        sec = e.get("section") or ""
        bundle = sec.split(",")[0].strip()
        patcher = sec.split("patched by", 1)[1].strip() if "patched by" in sec else None
        dis = e.get("disabled")
        state = {"expr": "条件", True: "禁用", False: "启用", None: "启用"}[dis]
        rows.append({
            "id": e["id"], "pkg": bundle, "impl": e.get("name"), "state": state,
            "source": ("被 %s 覆盖" % os.path.basename(patcher)) if patcher
                      else "来自 %s" % bundle,
            "builtin": bundle.startswith("@deepseek-ai/"), "patcher": patcher,
        })
    return rows


def build_rows_from_yaml(st):
    """降级路径：按层序解析，**后面的层覆盖前面的**（含 override 条目）"""
    layers = []
    if ENV:
        bm = getattr(ENV, "builtin_map", None)
        if callable(bm):
            for name, f in bm().items():
                layers.append((name, parse_cordis(read_text(f)), True))
    for b in st["bundles"]:
        d = os.path.join(st["nm"], *b.split("/"))
        f = None
        for fn in ("cordis.patch.yml", "cordis.yml", "cordis.patch.yaml"):
            c = os.path.join(d, fn)
            if os.path.exists(c):
                f = c
                break
        layers.append((b, parse_cordis(read_text(f)) if f else [],
                       b.startswith("@deepseek-ai/")))
    if st["patch"]:
        layers.append(("(profile 补丁)", parse_cordis(read_text(st["patch"])), False))

    index = {}
    for lname, ents, is_builtin in layers:
        for e in ents:
            if e["kind"] == "insert":
                index.setdefault(e["id"], {
                    "id": e["id"], "pkg": lname, "impl": e.get("name"),
                    "state": "启用", "source": "来自 %s" % lname,
                    "builtin": is_builtin, "patcher": None})
            row = index.get(e["id"])
            if row is None:
                continue
            if e["kind"] == "override":
                row["patcher"] = lname
                row["source"] = "被 %s 覆盖" % lname
                if e["disabled"] is True:
                    row["state"] = "禁用"
                elif e["disabled"] is False:
                    row["state"] = "启用"
                elif e["disabled"] == "expr":
                    row["state"] = "条件"
            else:
                if e["disabled"] is True:
                    row["state"] = "禁用"
                elif e["disabled"] == "expr":
                    row["state"] = "条件"
    return list(index.values())


# ==================== 冲突检查 ====================
MOBILE_KW = ("mobile", "pocket")
MARKET_KW = ("market",)
KNOWN_PAIRS = [
    ("dsh-pocket", "dsh-web-mobile", "很高", "C7b 已知命名空间冲突",
     "两者都注册 locale 命名空间 mobileNav",
     "同时启用会让移动前端整页 Failed to load plugins，必须禁其一"),
    ("dsh-pocket", "dsh-mobile", "中", "C7c 移动通道重复",
     "dsh-pocket（3081 代理+扫码 PIN）与 dsh-mobile（移动网关 3443）功能重叠",
     "统一保留一套，减少排障变量"),
]


def check_conflicts(st):
    out = []
    rows = st["rows"]
    by_id = {}
    for r in rows:
        by_id.setdefault(r["id"], []).append(r)
    enabled = set(r["pkg"] for r in rows if r["state"] == "启用")
    installed = set(st["bundles"])

    for i, rs in by_id.items():
        if len(rs) > 1:
            out.append(("很高", "C1 重复入口 id",
                        "id `%s` 出现 %d 次" % (i, len(rs)), "loader 会冲突"))

    if st["patch"] and st["source"] == "dump":
        known = set(by_id.keys())
        for e in parse_cordis(read_text(st["patch"])):
            if e["kind"] == "override" and e["id"] not in known:
                out.append(("中", "C2 悬空补丁",
                            "profile 补丁里的 `%s` 找不到对应入口" % e["id"],
                            "核对真实入口 id；写错只会静默空转"))

    for b in st["bundles"]:
        if b.startswith("@deepseek-ai/"):
            continue
        if b not in st["deps"]:
            out.append(("很高", "C3 缺失依赖声明",
                        "bundles 里的 `%s` 不在 dependencies" % b, "loader 会加载失败"))

    if st["nm"]:
        for b in st["bundles"]:
            if b.startswith("@deepseek-ai/"):
                continue
            if not os.path.isdir(os.path.join(st["nm"], *b.split("/"))):
                out.append(("很高", "C4 包未安装",
                            "bundles 里的 `%s` 不在 node_modules" % b, "运行 pnpm install"))

    idle = [d for d in st["deps"] if d not in st["bundles"]]
    if idle:
        out.append(("低", "C5 已装未启用",
                    "在 dependencies 但不在 bundles：%s" % "、".join(idle),
                    "想用就加进 bundles；不用可卸载"))

    miss = []
    if st["nm"] and st["fb"]:
        miss = sorted(list_pkgs(st["nm"]) - list_pkgs(st["fb"]))
    if miss:
        out.append(("低", "C6 fallback 未建链接（正常现象）",
                    "有 %d 个包未建 fallback 链接。dsh 每次启动后都会是这个状态，"
                    "实测带着它可正常启动。" % len(miss),
                    "预防性项：补齐可消除 dsh 启动时创建链接的 EPERM 崩溃窗口，按 f 补齐"))

    for a, b, lvl, code, desc, sug in KNOWN_PAIRS:
        if not (a in installed and b in installed):
            continue
        if a in enabled and b in enabled:
            out.append((lvl, code, desc, sug))
        else:
            off = b if b not in enabled else a
            on = a if off == b else b
            out.append(("低", code + "（已压制）",
                        "%s 与 %s 存在冲突，当前 %s 已禁用" % (a, b, off),
                        "保持现状；要启用 %s 需先禁用 %s" % (off, on)))

    def group(kws):
        g = {}
        for b in enabled:
            core = b.split("/")[-1].lower()
            for k in kws:
                if k in core:
                    g.setdefault(k, []).append(b)
        for k, mem in g.items():
            if len(mem) > 1:
                out.append(("中", "C8 功能可能重叠",
                            "关键词 `%s` 命中多个已启用包：%s" % (k, "、".join(mem)),
                            "若出现路由/命名空间冲突，保留一个"))
    group(MOBILE_KW)
    group(MARKET_KW)

    order = {"很高": 0, "高": 1, "中": 2, "低": 3}
    out.sort(key=lambda x: order.get(x[0], 9))
    return out, miss


# ==================== 写回 ====================
def backup_patch(patch, keep=10):
    """备份补丁文件；只保留最近 keep 份，避免无限堆积。"""
    if not patch or not os.path.exists(patch):
        return None
    dst = patch + ".bak-plugins-" + time.strftime("%Y%m%d-%H%M%S")
    try:
        shutil.copy2(patch, dst)
    except Exception:
        return None
    try:
        olds = sorted(glob.glob(patch + ".bak-plugins-*"))
        for old in olds[:-keep]:
            os.remove(old)
    except Exception:
        pass
    return dst


def set_disabled(patch, eid, flag):
    if not patch:
        return False, "没有可写的补丁文件"
    if not os.path.exists(patch):
        return False, "补丁文件不存在：%s" % patch

    # [!] 关键防护：read_text 读失败时返回 ""，若照此写回会【清空用户全部补丁】。
    #     必须先确认真的读到了内容，否则中止。
    raw = read_text(patch)
    if not raw.strip():
        return False, "补丁文件读不到内容，已中止（避免清空你的补丁）"

    lines = raw.splitlines()          # 注意：raw 是【全文】，下面循环变量不能叫 raw
    target = None
    for i, ln in enumerate(lines):
        m = ID_RE.match(ln)
        if m and m.group(2) == eid and indent_of(ln) == 0:
            target = i
            break
    val = "true" if flag else "false"

    if target is None:
        new = lines + ["",
                       "# 由 dsh-plugins.py 于 %s 设置" % time.strftime("%Y-%m-%d %H:%M"),
                       "- id: %s" % eid, "  disabled: %s" % val]
        note = "追加新块 `- id: %s` / disabled: %s" % (eid, val)
    else:
        ind = indent_of(lines[target])
        j, replaced, k = target + 1, False, None
        while j < len(lines):
            cur = lines[j]
            if cur.strip() and not cur.lstrip().startswith("#"):
                ci = indent_of(cur)
                if ci <= ind:
                    break            # 下一个入口
                if k is None:
                    k = ci           # 第一个属性行定义入口键的缩进层
                # [!] 只认入口层的 disabled —— config: 子块里嵌套的同名键
                #     是插件自己的配置，改它会静默改错地方
                if ci == k and re.match(r"^\s*disabled:\s*", cur):
                    lines[j] = " " * ci + "disabled: " + val
                    replaced = True
                    break
            j += 1
        if not replaced:
            # 插在与其它入口属性相同的缩进层上（无属性时按惯例 +2）
            lines.insert(target + 1,
                         " " * (k if k is not None else ind + 2)
                         + "disabled: " + val)
            note = "为 `%s` 新增 disabled: %s" % (eid, val)
        else:
            note = "把 `%s` 的 disabled 改为 %s" % (eid, val)
        new = lines

    # [!] 备份失败就不许写 —— 宁可不动，也不冒险
    bak = backup_patch(patch)
    if bak is None:
        return False, "备份失败，已中止写入（不冒险改你的补丁）"

    # [A2] 保留原行尾：原文件是 CRLF 就写 CRLF，别把整份文件的行尾改掉
    eol = "\r\n" if "\r\n" in raw else "\n"
    out_text = eol.join(new).rstrip() + eol

    # [A3] 写入前校验：入口行数绝不能变少（变少说明拼接逻辑出问题）
    before_ids = len([l for l in raw.splitlines() if ID_RE.match(l)])
    after_ids = len([l for l in new if ID_RE.match(l)])
    if after_ids < before_ids:
        return False, ("校验失败：入口行数 %d -> %d，已中止（原文件未改动）"
                       % (before_ids, after_ids))

    # [A1] 原子替换：先写同目录临时文件，再 os.replace。
    #      直接覆写时，dsh（patchReload=live）可能在写入中途读到半截 YAML。
    #      后缀带 pid+时间戳：固定可预测的临时名会被预置的符号链接重定向写入。
    tmp = "%s.tmp-plugins-%d-%d" % (patch, os.getpid(),
                                    int(time.time() * 1000) % 100000)
    try:
        with open(tmp, "wb") as f:
            f.write(out_text.encode("utf-8"))
        os.replace(tmp, patch)
    except Exception as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False, "写入失败：%s（原文件未改动）" % e

    # 让 dump 缓存失效
    try:
        meta = os.path.join(TOOLS, "dsh-dump.cache.txt.meta.json")
        if os.path.exists(meta):
            os.remove(meta)
    except Exception:
        pass
    return True, (note + "（已备份 → %s；入口 %d -> %d）"
                  % (os.path.basename(bak), before_ids, after_ids))


def run_heal():
    heal = os.path.join(TOOLS, "dsh-fallback-heal.py")
    if not os.path.exists(heal):
        return False, "找不到自愈脚本"

    def missing():
        if not ENV:
            return None
        try:
            e = ENV.detect()
            pdir = e["profile"]["dir"]
            return len(list_pkgs(os.path.join(pdir, "node_modules"))
                       - list_pkgs(os.path.join(pdir, ".dsh-module-fallback",
                                                "node_modules")))
        except Exception:
            return None

    before = missing()
    try:
        subprocess.run([sys.executable, heal], capture_output=True,
                       creationflags=FLAGS, timeout=600)
    except subprocess.TimeoutExpired:
        return False, "自愈脚本 600 秒未完成，已放弃"
    except Exception as e:
        return False, "自愈脚本执行失败：%s" % e
    after = missing()
    if after is None:
        # 核对不了就读不了 —— 不能谎报"仍有 None 个"
        return True, "已执行（无法核对缺失数）"
    if after == 0:
        return True, ("本来就没有缺失" if before == 0
                      else "原有 %s 个缺失，现已全部补齐" % before)
    return False, "仍有 %s 个缺失" % after


# ==================== 界面 ====================
PAGE_SIZE = 40
LINE = "-" * 74


def render(st, rows, conflicts, page, per_page):
    src = ("dsh --dump-config（权威）" if st["source"] == "dump"
           else "自行解析补丁（降级）")
    o = ["=" * 74,
         "  dsh 插件管理     profile: %s     数据源: %s"
         % ((st["profile"] or {}).get("name", "?"), src),
         "=" * 74, ""]

    if not conflicts:
        o.append("  冲突检查：未发现问题")
    else:
        hi = sum(1 for c in conflicts if c[0] in ("很高", "高"))
        o.append("  冲突检查：%d 项，高危 %d 项" % (len(conflicts), hi))
        o.append("")
        for lvl, code, desc, sug in conflicts:
            mark = {"很高": "!!", "高": "! ", "中": "* ", "低": "- "}.get(lvl, "? ")
            o.append("  %s %s" % (mark, desc))
            o.append("       [%s | %s]" % (lvl, code))
            o.append("       建议：%s" % sug)

    total = len(rows)
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(0, min(page, pages - 1))
    lo = page * per_page
    hi_ = min(total, lo + per_page)

    o += ["", LINE,
          "  #    状态   入口 id (loader)                 实现包", LINE]
    for i in range(lo, hi_):
        r = rows[i]
        o.append("  %-4d %-6s %-32s %s"
                 % (i + 1, r["state"], r["id"][:32],
                    (r.get("impl") or r["pkg"])[:30]))
    o += [LINE,
          "  第 %d/%d 页   共 %d 个入口（启用 %d / 禁用 %d / 条件 %d）"
          % (page + 1, pages, total,
             sum(1 for r in rows if r["state"] == "启用"),
             sum(1 for r in rows if r["state"] == "禁用"),
             sum(1 for r in rows if r["state"] == "条件")),
          "",
          "  输入编号 = 切换启用/禁用（按屏幕上的全局编号）",
          "  n 下页  p 上页  c 冲突  f 补链接  e 环境探测  r 刷新  q 退出",
          ""]
    return "\n".join(o), page


def show_env_report():
    if not ENV:
        log("  探测层不可用（dsh-env.py 缺失）")
        return
    try:
        log(ENV.report(ENV.detect(want_dump=True)))
    except Exception as e:
        log("  探测失败：%s" % e)


def main():
    argv = sys.argv[1:]
    check_only = "--check" in argv
    list_only = "--list" in argv
    dump_info = "--dump" in argv

    try:
        st = collect()
    except Exception as e:
        log("收集状态失败：%s" % e)
        return 1
    rows = st["rows"]
    conflicts, miss = check_conflicts(st)

    if dump_info:
        log("数据源  : %s" % st["source"])
        log("profile : %s" % (st["profile"] or {}).get("dir"))
        log("补丁    : %s" % st["patch"])
        log("入口数  : %d" % len(rows))
        log("禁用    : %d" % sum(1 for r in rows if r["state"] == "禁用"))
        log("")
        log("探测说明：")
        for n in st["notes"]:
            log("  - %s" % n)
        return 0

    if check_only:
        log("dsh 插件冲突检查")
        log("  profile: %s" % (st["profile"] or {}).get("dir"))
        log("  数据源 : %s" % ("dsh --dump-config（权威）" if st["source"] == "dump"
                               else "自行解析补丁（降级，状态可能不全）"))
        log("  入口   : %d 个（启用 %d / 禁用 %d / 条件 %d）"
            % (len(rows),
               sum(1 for r in rows if r["state"] == "启用"),
               sum(1 for r in rows if r["state"] == "禁用"),
               sum(1 for r in rows if r["state"] == "条件")))
        if not conflicts:
            log("  未发现冲突")
            return 0
        for lvl, code, desc, sug in conflicts:
            log("  [%s] %s — %s" % (lvl, code, desc))
        hi = [c for c in conflicts if c[0] in ("很高", "高")]
        log("  合计 %d 项，高危 %d 项" % (len(conflicts), len(hi)))
        return 2 if hi else 0

    if list_only:
        for i, r in enumerate(rows, 1):
            log("%-4d %-6s %-32s %s"
                % (i, r["state"], r["id"], r.get("impl") or r["pkg"]))
        return 0

    page = 0
    while True:
        if os.name == "nt":
            os.system("title dsh 插件管理")
            os.system("cls")
        else:
            os.system("clear")
        panel, page = render(st, rows, conflicts, page, PAGE_SIZE)
        print(panel)
        try:
            cmd = input("  选择 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        low = cmd.lower()

        if low in ("q", "quit", "exit"):
            break
        if low == "n":
            page += 1
            continue
        if low == "p":
            page -= 1
            continue
        if low == "r":
            # 显式刷新：强制重跑探测（会花几十秒，但能拿到权威树）
            print("\n  重新探测中（约 30 秒）...")
            st = collect(force=True)
            rows = st["rows"]
            conflicts, miss = check_conflicts(st)
            continue
        if low == "e":
            print()
            show_env_report()
            pause_in()
            continue
        if low == "c":
            print()
            if not conflicts:
                log("  未发现冲突。")
            for lvl, code, desc, sug in conflicts:
                log("  [%s | %s]" % (lvl, code))
                log("      %s" % desc)
                log("      → %s" % sug)
            print()
            pause_in()
            continue
        if low == "f":
            if not miss:
                print("\n  所有链接都已建好。")
                pause_in()
                continue
            print("\n  将补齐 %d 个 fallback 链接。" % len(miss))
            print("  说明：dsh 平时只需 220 个，这 %d 个是它每次启动后会留空的。" % len(miss))
            print("        补齐属预防性操作，不影响插件配置。")
            try:
                if input("  确认？(y/N) > ").strip().lower() != "y":
                    continue
            except (EOFError, KeyboardInterrupt):
                print()
                break
            ok, msg = run_heal()
            print("  %s %s" % ("[OK]" if ok else "[X]", msg))
            conflicts, miss = check_conflicts(st)   # 缺失数变了，重算即可
            pause_in()
            continue

        if not cmd.isdigit():
            print("  无效输入")
            time.sleep(1)
            continue
        # [!] 列表显示的是全局编号（第 2 页从 41 开始），输入必须按同一套编号；
        #     旧逻辑只收页内 1-40，照屏幕输入会被拒、输页内编号又改错行
        n = int(cmd)
        idx = n - 1
        if not (0 <= idx < len(rows)):
            print("  超出范围（有效编号 1 - %d）" % len(rows))
            time.sleep(1.4)
            continue

        r = rows[idx]
        if r["state"] == "条件":
            print("  `%s` 是条件启用（!!js 表达式），本工具不自动改。" % r["id"])
            pause_in()
            continue

        flag = (r["state"] != "禁用")
        print()
        print("  编号 %d →  入口 id: %s" % (n, r["id"]))
        print("  实现包  : %s" % (r.get("impl") or "?"))
        print("  所属    : %s%s" % (r["pkg"], "（内置）" if r["builtin"] else ""))
        print("  当前    : %s（%s）" % (r["state"], r["source"]))
        print("  将执行  : %s" % ("禁用" if flag else "启用"))
        if flag:
            print("  [!] 禁用后该功能立即失效（patchReload=live 会热重载）")
        print("  [!] 会先备份补丁文件")
        try:
            if input("  确认？(y/N) > ").strip().lower() != "y":
                continue
        except (EOFError, KeyboardInterrupt):
            print()
            break
        ok, note = set_disabled(st["patch"], r["id"], flag)
        print()
        print("  %s %s" % ("[OK]" if ok else "[X]", note))
        if ok:
            # 瞬时生效：直接改内存里的行状态并重算冲突，
            # 不重跑 dump（那要 30 秒）。下次按 r 会以权威树复核。
            r["state"] = "禁用" if flag else "启用"
            r["source"] = "刚由本工具设置"
            conflicts, miss = check_conflicts(st)
            print("  （界面已按新状态刷新；按 r 可拿 dsh 的权威树复核）")
        pause_in()

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        sys.exit(0)
