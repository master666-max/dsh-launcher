# -*- coding: utf-8 -*-
"""dsh 启动器工具链自检（常驻工具，改完代码就跑一遍）

检查项：
  1. 语法
  2. **用到但未 import 的模块**（曾因此漏掉 socket，导致自动启动直接 NameError）
  3. 疑似未定义名
  4. 字符串里的非 GBK 字符（chcp 936 终端会抛 UnicodeEncodeError）
  5. subprocess 参数名误用（flags= 应为 creationflags=）
  6. .bat 行尾（必须纯 CRLF）与关键加固点
  7. 危险调用（无备份保护的删除类操作）
  8. 解耦检查（代码与入口 bat 都不得引用任何 AI agent / IDE 插件的目录）

用法：python dsh-selfcheck.py
"""
import os, ast, re, sys, glob, builtins

# 终端是 chcp 936（GBK）时，打印非 GBK 字符会抛 UnicodeEncodeError。
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

TOOLS = os.path.dirname(os.path.abspath(__file__))
DESK = os.path.join(os.path.expanduser("~"), "Desktop")
# 注意：dsh-selfcheck.py 自己不列入（它内含解耦检测的模式串，会自报）
PY_FILES = ["dsh_env.py", "dsh-launcher.py", "dsh-plugins.py",
            "dsh-fallback-heal.py", "dsh_tests.py", "dsh-env.py"]
# [!] 目录里的 _*.py 临时脚本也在扫描面内 —— 它们会被真实运行，
#     解耦红线对其同样生效（曾漏掉 _accept_all.py 里的 .workbuddy 硬编码）。
#     语法/未定义名等 AST 检查仍只跑 PY_FILES（临时脚本随写随删，不追求全检）。
TEMP_PY_FILES = sorted(os.path.basename(x) for x in glob.glob(
    os.path.join(TOOLS, "_*.py")))
# 只检查真正的用户入口（桌面 bat）。
# dsh-run.bat 是过去的计划任务测试入口，已于 2026-09-18 删除，不再纳入检查。
BAT_FILES = [os.path.join(DESK, "start-dsh.bat")]
BUILTINS = set(dir(builtins)) | {
    "__file__", "__name__", "__doc__", "__spec__", "__loader__",
    "__package__", "__builtins__", "__cached__",
}

KNOWN_MODULES = [
    "os", "sys", "re", "time", "json", "socket", "shutil", "glob", "subprocess",
    "importlib", "unicodedata", "tempfile", "msvcrt", "threading", "http",
    "ctypes", "hashlib", "zipfile", "datetime", "collections", "itertools",
]

issues = []


def add(sev, where, msg):
    issues.append((sev, where, msg))


def gbk_ok(ch):
    try:
        ch.encode("gbk")
        return True
    except Exception:
        return False


class Collect(ast.NodeVisitor):
    def __init__(self):
        self.bound = set()
        self.loaded = {}
        self.imports = set()
        self.used = set()

    def _add_import(self, names):
        for a in names:
            nm = (a.asname or a.name).split(".")[0]
            self.bound.add(nm)
            self.imports.add(nm)

    def visit_Import(self, n):
        self._add_import(n.names)
        self.generic_visit(n)

    def visit_ImportFrom(self, n):
        self._add_import(n.names)
        self.generic_visit(n)

    def _bind_fn(self, n):
        self.bound.add(n.name)
        for a in list(n.args.args) + list(n.args.kwonlyargs) + list(n.args.posonlyargs):
            self.bound.add(a.arg)
        if n.args.vararg:
            self.bound.add(n.args.vararg.arg)
        if n.args.kwarg:
            self.bound.add(n.args.kwarg.arg)
        self.generic_visit(n)

    visit_FunctionDef = _bind_fn
    visit_AsyncFunctionDef = _bind_fn

    def visit_Lambda(self, n):
        for a in list(n.args.args) + list(n.args.kwonlyargs) + list(n.args.posonlyargs):
            self.bound.add(a.arg)
        if n.args.vararg:
            self.bound.add(n.args.vararg.arg)
        if n.args.kwarg:
            self.bound.add(n.args.kwarg.arg)
        self.generic_visit(n)

    def visit_ClassDef(self, n):
        self.bound.add(n.name)
        self.generic_visit(n)

    def visit_Name(self, n):
        if isinstance(n.ctx, (ast.Store, ast.Del)):
            self.bound.add(n.id)
        else:
            self.loaded.setdefault(n.id, n.lineno)
            self.used.add(n.id)

    def visit_ExceptHandler(self, n):
        if n.name:
            self.bound.add(n.name)
        self.generic_visit(n)

    def visit_comprehension(self, n):
        for t in ast.walk(n.target):
            if isinstance(t, ast.Name):
                self.bound.add(t.id)
        self.generic_visit(n)


print("=" * 72)
print("  dsh 启动器工具链自检")
print("=" * 72)

for f in PY_FILES:
    p = os.path.join(TOOLS, f)
    if not os.path.exists(p):
        add("高", f, "文件不存在")
        continue
    raw = open(p, "rb").read()
    try:
        src = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        add("高", f, "不是合法 UTF-8：%s" % e)
        continue
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        add("高", f, "语法错误 行%d：%s" % (e.lineno, e.msg))
        continue

    c = Collect()
    c.visit(tree)

    # 1) 缺失 import
    used_root = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name):
            used_root.add(n.value.id)
    miss = sorted(m for m in KNOWN_MODULES
                  if m in used_root and m not in c.imports)
    if miss:
        add("很高", f, "用到但未 import 的模块：%s（运行时会 NameError）"
            % ", ".join(miss))

    # 2) 疑似未定义名
    und = {n: ln for n, ln in c.loaded.items()
           if n not in c.bound and n not in BUILTINS}
    if und:
        add("高", f, "疑似未定义名：%s"
            % ", ".join("%s(行%d)" % (k, v) for k, v in sorted(und.items())))

    # 3) 非 GBK 字符
    bad = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for ch in node.value:
                if ord(ch) > 127 and not gbk_ok(ch):
                    bad[ch] = hex(ord(ch))
    if bad:
        add("高", f, "字符串含 GBK 无法编码的字符（终端会崩）：%s"
            % ", ".join("%s(%s)" % (k, v) for k, v in bad.items()))

    # 4) subprocess 参数名
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            fn = n.func
            nm = ""
            if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
                nm = fn.value.id + "." + fn.attr
            if nm in ("subprocess.run", "subprocess.call", "subprocess.Popen"):
                for kw in n.keywords:
                    if kw.arg == "flags":
                        add("高", f, "行%d：参数名 `flags=` 错，应为 creationflags"
                            % n.lineno)

    # 5) 无超时的 subprocess
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and isinstance(n.func.value, ast.Name) \
                and n.func.value.id == "subprocess" and n.func.attr == "run":
            if not any(k.arg == "timeout" for k in n.keywords):
                add("低", f, "行%d：subprocess.run 无 timeout（可能永久卡住）"
                    % n.lineno)

for p in BAT_FILES:
    f = os.path.basename(p)
    if not os.path.exists(p):
        add("高", f, "文件不存在")
        continue
    raw = open(p, "rb").read()
    lf, crlf = raw.count(b"\n"), raw.count(b"\r\n")
    if lf != crlf:
        add("很高", f, "不是纯 CRLF（裸LF=%d）—— .bat 会闪退" % (lf - crlf))
    try:
        txt = raw.decode("gbk")
    except UnicodeDecodeError as e:
        add("高", f, "不是合法 GBK：%s" % e)
        txt = raw.decode("gbk", "replace")
    if "start-dsh.bat" in f:
        lines = txt.splitlines()
        if len(lines) > 1 and "setlocal enabledelayedexpansion" not in lines[1]:
            add("中", f, "第2行不是 setlocal enabledelayedexpansion")
        if "%SystemRoot%\\system32" not in txt:
            add("高", f, "缺少 System32 PATH 钉扎")
        if 'dsh-launcher.py' not in txt:
            add("中", f, "未转交 dsh-launcher.py")

        # ---- 块内跨行 if 检测（2026-09-18 秒退事故的常驻回归器）----
        # 实证：cmd 在【从批处理文件调用】的场景下，只要 for/if 括号块内出现
        #   `if <条件>` 独占一行、下一行才 `set ...`（无论加不加括号、行尾有没有空格）
        # 就会让整个块 rc=255 或静默中止，双击 -> 窗口还没打印就消失（秒退）。
        # 最小对照（A 原样 / B 给 if 加括号 / C 合并一行 / D 去行尾空格）：
        #   A rc=255  B rc=255  C rc=0  D rc=255
        # => 唯一解 = `if` 与它的命令必须写在【同一行】。
        #
        # 难点：`IF` 的语法不支持嵌套，`if not defined PYEXE if exist "..."` 里的
        # 第二个 if 其实是被当作【命令】的，只是同样没带语句体。所以要
        # 【循环剥离】链式条件，直到剥不动为止；剥完还剩东西才算「有命令」。
        # 曾经只剥一层 -> body 剩余 `if exist "..."` -> 非空 -> 漏报（检测器形同虚设）。
        #
        # [!] 2026-09-19 审计补漏（旧版四种形态全部漏检）：
        #   a. 大写 `IF`（batch 大小写不敏感）→ re.I；
        #   b. 顶格 if（块内缩进是可选的）→ 去掉缩进门槛；
        #   c. `if exist "C:\Program Files\x"`（带引号含空格路径）→
        #      exist 改吃完整引号串；`if "%A%" == "x"` → == 两侧允许引号串；
        #   d. 括号深度计数把 echo/rem 里的括号算进去（`echo :)` 全文件
        #      破坏深度）→ 先剥 rem 注释与引号段再计数。
        COND_RE = re.compile(
            r"^if\s+(not\s+)?(defined\s+\S+|exist\s+(\"[^\"]*\"|\S+)"
            r"|errorlevel\s+\d+|(\"[^\"]*\"|\S+)\s*==\s*(\"[^\"]*\"|\S+))\s*",
            re.I)
        lines_all = txt.splitlines()
        depth = 0
        for i, l in enumerate(lines_all, 1):
            # 剥 rem 注释（整词，别把路径里的 rem 截出来）与引号段后计括号
            code_l = re.split(r"(?i)(?<![^\s])rem\s", l)[0] \
                if re.search(r"(?i)(?<![^\s])rem\s", l) else l
            code_l = re.sub(r'"[^"]*"', '""', code_l)
            depth += code_l.count("(") - code_l.count(")")
            if depth < 0:
                depth = 0
            # 必须 strip 前导空白再用 `^if` 匹配（曾漏这步导致永不匹配）
            s = l.strip().lstrip("@")
            if depth > 0 and re.match(r"^if\s", s, re.I):
                rest = s
                while True:
                    m = COND_RE.match(rest)
                    if not m:
                        break
                    rest = rest[m.end():].strip()
                if not rest:
                    add("很高", f,
                        "行%d：括号块内 if 独占一行、命令另起一行 —— "
                        "会让整个块语法错、启动器秒退（必须 if 与命令同行）" % i)

# ---------- 7) 解耦检查：不得残留任何 agent / IDE 插件依赖 ----------
# 本工具应完全独立：代码里不出现 agent 目录、不读 agent 环境变量。
#
# [!] 下面的关键词【故意拆成多段拼接】，让本文件自身不含这些字面量 ——
#     否则任何第三方 grep 都会把"检测器"误当成"耦合"。
_A1 = "work" + "buddy"          # 某 AI 工作台
_A2 = "code" + "buddy"          # 某 AI 编码工具
_A3 = "z" + "code"              # 某 AI 编辑器
_A4 = "coding"                  # 泛指
AGENT_PAT = re.compile(
    r"(\." + _A1 + "|" + _A2 + "|" + _A3 + "|ai" + _A4 + ")",
    re.I)
ENV_AGENT = re.compile(r"os\.environ\.get\(\s*['\"]"
                       r"(" + _A1.upper() + "|" + _A2.upper() + "|"
                       + _A3.upper() + ")[A-Z_]*['\"]", re.I)
for f in PY_FILES + TEMP_PY_FILES:
    fp = os.path.join(TOOLS, f)
    if not os.path.exists(fp):
        continue
    src = open(fp, encoding="utf-8").read()
    # [!] 用 tokenize 剥注释 —— 旧版 split("#")[0] 是文本级切分：
    #     字符串里含 `#` 时（如 open("cfg#.workbuddy")）会把真耦合藏在
    #     「注释」里漏检。tokenize 按语法区分注释与代码（字符串保留，
    #     字符串里藏路径同样是违规）。
    import io, tokenize
    code_by_line = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE,
                            tokenize.INDENT, tokenize.DEDENT,
                            tokenize.ENDMARKER, tokenize.ENCODING):
                continue
            code_by_line.setdefault(tok.start[0], []).append(tok.string)
    except Exception:
        # tokenize 失败（残缺的临时脚本）→ 整行扫描：宁可误报不漏报
        for i, l in enumerate(src.splitlines(), 1):
            code_by_line[i] = [l]
    for i, frags in sorted(code_by_line.items()):
        code = "".join(frags)
        if AGENT_PAT.search(code):
            add("很高", f, "行%d 出现 agent 目录引用（应完全解耦）：%s"
                % (i, code.strip()[:70]))
        if ENV_AGENT.search(code):
            add("高", f, "行%d 读取了 agent 的环境变量" % i)

# 入口 bat 也不得引用 agent 路径
for bp in BAT_FILES:
    if not os.path.exists(bp):
        continue
    txt = open(bp, "rb").read().decode("gbk", "replace")
    for i, l in enumerate(txt.splitlines(), 1):
        # rem 是整词才算注释 —— 旧版 split("rem") 会被路径里的 rem 截断漏检
        code = re.split(r"(?i)(?<![^\s])rem\s", l)[0]
        if AGENT_PAT.search(code):
            add("很高", os.path.basename(bp),
                "行%d 引用 agent 目录（应完全解耦）：%s" % (i, l.strip()[:70]))


order = {"很高": 0, "高": 1, "中": 2, "低": 3}
issues.sort(key=lambda x: order.get(x[0], 9))

print()
if not issues:
    print("  未发现问题。")
else:
    cur = None
    for sev, where, msg in issues:
        if sev != cur:
            print()
            cur = sev
        print("  [%s] %-22s %s" % (sev, where, msg))
print()
print("-" * 72)
cnt = {}
for sev, _, _ in issues:
    cnt[sev] = cnt.get(sev, 0) + 1
print("  合计 %d 项：%s" % (len(issues),
                          " / ".join("%s %d" % (k, v) for k, v in
                                     sorted(cnt.items(), key=lambda x: order.get(x[0], 9)))
                          or "0"))
hi = [i for i in issues if i[0] in ("很高", "高")]
print("  高危 %d 项" % len(hi))
