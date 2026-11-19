"""新编号体系：客户端别名 / 客户端编号 / 启动编号。

- **客户端别名**：用户自己起的名字。开了网站检测却没设，UI 会拦下来要求先设置。
- **客户端编号**：5 位字母+数字，首次运行随机生成后写进 config，之后永不变。
  生成时先去服务端查重，撞了就重新随机，直到不重复（离线时先用着，联上再查）。
- **启动编号**：``yyyy-mm-dd_hh-mm_XXXXXXX``，**每个窗口一个**，一行表格 = 一个窗口。
  带上时分是有用的：它的**字典序就是时间序**，服务端直接拿它排序，不用再存时间戳。

字母表去掉了 I/O/0/1：这些编号要靠人念、靠人抄，认错一位就查不到记录。
"""

from __future__ import annotations

import os
import re
import secrets
from datetime import datetime

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CLIENT_NUMBER_LENGTH = 5
LAUNCH_SUFFIX_LENGTH = 7
MAX_ALIAS_LENGTH = 40
MAX_LOOKUP_TRIES = 20

CLIENT_NUMBER_RE = re.compile(r"^[A-Z0-9]{%d}$" % CLIENT_NUMBER_LENGTH)
# 新格式 yyyy-mm-dd_hh-mm_XXXXXXX；旧格式 yyyy-mm-dd-XXXXXXX 仍然认——
# 盘上和服务端已有的存档目录都用旧格式命名，正则一收紧它们就全查不到了。
LAUNCH_ID_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}(?:_\d{2}-\d{2}_|-)[A-Z0-9]{%d}$" % LAUNCH_SUFFIX_LENGTH)
FOLDER_NAME_RE = re.compile(r"^[^\\/:*?\"<>|]{1,%d}-[A-Z0-9]{%d}$"
                            % (MAX_ALIAS_LENGTH, CLIENT_NUMBER_LENGTH))

_BAD_ALIAS_CHARS = re.compile(r"[\\/:*?\"<>|\x00-\x1f]+")


def _random(length: int) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def new_client_number() -> str:
    return _random(CLIENT_NUMBER_LENGTH)


def new_launch_id(now: datetime | None = None) -> str:
    """``yyyy-mm-dd_hh-mm_XXXXXXX``：带上时分，一眼能看出是哪一次跑的。"""
    stamp = (now or datetime.now()).strftime("%Y-%m-%d_%H-%M")
    return f"{stamp}_{_random(LAUNCH_SUFFIX_LENGTH)}"


def is_client_number(value: str) -> bool:
    return bool(CLIENT_NUMBER_RE.match(str(value or "")))


def is_launch_id(value: str) -> bool:
    return bool(LAUNCH_ID_RE.match(str(value or "")))


def normalize_alias(alias: str) -> str:
    """别名要当目录名用：去掉路径元字符，留下中文/字母/数字/空格。

    连续的点也一并压掉——单看它挡不住什么（分隔符已经没了），但目录名里留着
    ``..`` 只会让人以后读代码时怀疑这里到底安不安全。
    """
    text = _BAD_ALIAS_CHARS.sub("_", str(alias or ""))
    text = re.sub(r"[.]{2,}", "_", text).strip().strip(".")
    return text[:MAX_ALIAS_LENGTH]


def folder_name(alias: str, number: str) -> str:
    """存档第一层目录 ``<客户端别名>-<客户端编号>``，双端一致。"""
    clean = normalize_alias(alias) or "未命名"
    code = str(number or "").upper()
    if not is_client_number(code):
        code = "XXXXX"
    return f"{clean}-{code}"


def split_folder_name(name: str) -> tuple[str, str]:
    """``别名-ABC23`` → ``("别名", "ABC23")``；别名里带 ``-`` 也能拆对。"""
    text = str(name or "")
    if len(text) > CLIENT_NUMBER_LENGTH + 1 and text[-CLIENT_NUMBER_LENGTH - 1] == "-":
        code = text[-CLIENT_NUMBER_LENGTH:].upper()
        if is_client_number(code):
            return text[:-CLIENT_NUMBER_LENGTH - 1], code
    return text, ""


def safe_join(root: str, *parts: str) -> str:
    """拼存档路径时挡住 ``..`` 和绝对路径，越界直接报错。"""
    base = os.path.abspath(root)
    target = os.path.abspath(os.path.join(base, *[str(part) for part in parts]))
    if target != base and not target.startswith(base + os.sep):
        raise ValueError("路径越界")
    return target


def ensure_client_number(config: dict, exists=None) -> dict:
    """确保 config 里有一个不与服务端冲突的客户端编号。

    ``exists(number) -> bool`` 由调用方提供（一般是查服务端）。查不通就当离线：
    先用着，等联上服务端再查一次——**不能静默改号**，否则历史记录会断成两半。

    返回 ``{"number", "changed", "checked", "warning"}``。
    """
    number = str(config.get("client_number", "") or "").upper()
    changed = False
    if not is_client_number(number):
        number = new_client_number()
        changed = True

    checked = False
    warning = ""
    if exists is not None:
        existing = str(config.get("client_number", "") or "").upper()
        for _ in range(MAX_LOOKUP_TRIES):
            try:
                taken = bool(exists(number))
            except Exception:
                break  # 离线：先用着，下次启动再查
            checked = True
            if not taken:
                break
            if not changed and is_client_number(existing):
                # 号是以前就在用的，服务端已有同号记录：只提示，绝不静默换
                warning = f"客户端编号 {number} 在服务端已被占用，建议手动改号后再采集"
                break
            number = new_client_number()
            changed = True
        else:
            warning = f"连试 {MAX_LOOKUP_TRIES} 个客户端编号都重复，请手动指定"

    if changed:
        config["client_number"] = number
    return {"number": number, "changed": changed, "checked": checked, "warning": warning}
