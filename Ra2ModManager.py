# -*- coding: utf-8 -*-
"""
红警2 MOD 管理器 v1.3.1
- MOD 内置 ID
- 多选 MOD 同时应用
- 部署前文件冲突检测
- 冲突自动合并（不修改原始 MOD，生成新 MOD）
- 合并来源预览（大小 / 修改时间）
- 合并后自检
- 生成 merge_report.txt 合并报告
- 备份 / 部署 / 还原 / 崩溃恢复全面支持多 MOD
"""

import os
import sys
import json
import uuid
import shutil
import subprocess
import threading
import zipfile
import datetime
import traceback
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

# ==================== 常量定义 ====================

BASE_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))
MODS_DIR = os.path.join(BASE_DIR, "mods")
BACKUPS_DIR = os.path.join(BASE_DIR, "backups")
CONFIG_DIR = os.path.join(BASE_DIR, "config")
RECOVERY_DIR = os.path.join(BASE_DIR, "recovery")
LOGS_DIR = os.path.join(BASE_DIR, "logs")
TEMP_DIR = os.path.join(BASE_DIR, "temp")

SESSION_FILE = os.path.join(RECOVERY_DIR, "session.json")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")
HISTORY_FILE = os.path.join(CONFIG_DIR, "launch_history.json")

ICON_FILENAME = "RA2.ICO"

DEFAULT_PARAMS = [
    {"name": "窗口化模式", "value": "-win", "desc": "以窗口模式运行游戏，避免全屏切换导致的崩溃"},
    {"name": "跳过EA标志", "value": "-nologo", "desc": "不加载EA/Westwood标志动画，加快启动速度"},
    {"name": "免CD模式", "value": "-cd", "desc": "允许在没有插入尤里的复仇光盘的情况下运行游戏"},
    {"name": "生成调试日志", "value": "-log", "desc": "在游戏目录下生成debug.log日志文件，用于排查问题"},
    {"name": "CSF缺失日志", "value": "-log-csf", "desc": "将缺失的语言文件标签输出到debug.log"},
    {"name": "严格模式", "value": "-strict", "desc": "检测到非致命错误时也抛出内部错误并终止游戏"},
    {"name": "AI控制", "value": "-ai-control", "desc": "启用AI控制功能，允许玩家通过快捷键将基地交给AI管理"},
    {"name": "速度控制", "value": "-speedcontrol", "desc": "允许在战役中调整游戏速度（原版有效，Ares下可能无效）"},
    {"name": "禁用鼠标摇杆", "value": "-nojoy", "desc": "禁用游戏对摇杆设备的检测，解决部分手柄冲突问题"},
]

DEFAULT_YR_EXE = "gamemd.exe"
DEFAULT_RA2_EXE = "ra2.exe"


# ==================== 自定义异常 ====================

class ModConflictError(Exception):
    def __init__(self, conflicts, mod_names):
        self.conflicts = conflicts
        self.mod_names = mod_names
        super().__init__(f"已选中的 {len(mod_names)} 个 MOD 之间存在 {len(conflicts)} 个同名冲突文件。")


# ==================== 日志系统 ====================

class LogManager:
    def __init__(self, gui_callback=None):
        self.gui_callback = gui_callback
        self._lock = threading.Lock()
        os.makedirs(LOGS_DIR, exist_ok=True)
        self._log_file = os.path.join(
            LOGS_DIR, f"manager_{datetime.date.today().strftime('%Y%m%d')}.log"
        )

    def _write(self, level, msg):
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] [{level}] {msg}"
        with self._lock:
            try:
                with open(self._log_file, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                pass
        if self.gui_callback:
            try:
                self.gui_callback(line)
            except Exception:
                pass

    def info(self, msg): self._write("信息", msg)
    def warn(self, msg): self._write("警告", msg)
    def error(self, msg): self._write("错误", msg)
    def success(self, msg): self._write("成功", msg)
    def debug(self, msg): self._write("调试", msg)


# ==================== 工具函数 ====================

def ensure_dirs():
    for d in [MODS_DIR, BACKUPS_DIR, CONFIG_DIR, RECOVERY_DIR, LOGS_DIR, TEMP_DIR]:
        os.makedirs(d, exist_ok=True)


def load_json(path, default=None):
    if default is None:
        default = {}
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_relative_files(root_dir):
    result = []
    for dirpath, _, filenames in os.walk(root_dir):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root_dir)
            result.append(rel)
    return result


def copy_with_dirs(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)


def generate_mod_id():
    return "mod-" + uuid.uuid4().hex[:12]


def is_valid_mod_name(name):
    if not name or not name.strip():
        return False, "名称不能为空。"
    for c in r'\/:*?"<>|':
        if c in name:
            return False, f"名称中包含非法字符：{c}"
    reserved = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
    if name.upper() in reserved:
        return False, f"'{name}' 是系统保留名称，请换一个。"
    return True, ""


def format_file_size(size_bytes):
    if size_bytes is None or size_bytes < 0:
        return "N/A"
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / 1024 / 1024:.2f} MB"
    return f"{size_bytes / 1024 / 1024 / 1024:.2f} GB"


def format_mtime(ts):
    try:
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "未知"


def format_import_time(value):
    """将 ISO 时间字符串转为人类可读格式：2026-10-12 15:30:00"""
    if not value or value == "未知":
        return value or "未知"
    # 已经是人类可读格式
    if "T" not in value:
        return value
    try:
        dt = datetime.datetime.fromisoformat(value)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        # 兜底：直接替换 T 为空格
        return value.replace("T", " ").split(".")[0]


def get_resource_path(filename):
    """获取打包后或开发环境中的资源路径（PyInstaller onefile 兼容）"""
    if hasattr(sys, "_MEIPASS"):
        candidate = os.path.join(sys._MEIPASS, filename)
        if os.path.isfile(candidate):
            return candidate
    # 开发环境或 exe 同级
    candidate = os.path.join(BASE_DIR, filename)
    if os.path.isfile(candidate):
        return candidate
    return None


# ==================== 崩溃恢复管理器 ====================

class RecoveryManager:
    def __init__(self, log: LogManager):
        self.log = log

    def load_session(self):
        return load_json(SESSION_FILE, None)

    def save_session(self, session_data):
        save_json(SESSION_FILE, session_data)

    def clear_session(self):
        if os.path.exists(SESSION_FILE):
            os.remove(SESSION_FILE)

    def has_pending_recovery(self):
        session = self.load_session()
        return bool(session and session.get("active"))

    def get_session_mods(self, session):
        """直接读取 session 中的 mods 列表（v1.3+ 格式）"""
        if not session:
            return []
        return session.get("mods", [])

    def perform_recovery(self, game_dir):
        session = self.load_session()
        if not session or not session.get("active"):
            self.log.info("没有待恢复的状态，跳过崩溃恢复。")
            return False

        mods = self.get_session_mods(session)
        mod_names = [m["name"] for m in mods]
        self.log.warn(
            f"检测到上次会话异常退出！涉及的 MOD：{'、'.join(mod_names) if mod_names else '未知'}，"
            f"正在执行崩溃恢复..."
        )

        total_deleted = 0
        total_restored = 0

        for mod_info in mods:
            mod_name = mod_info["name"]
            copied = mod_info.get("copied_files", [])
            backed = mod_info.get("backed_up_files", {})
            backup_root = os.path.join(BACKUPS_DIR, mod_name)

            for rel in copied:
                target = os.path.join(game_dir, rel)
                if os.path.exists(target):
                    try:
                        os.remove(target)
                        total_deleted += 1
                    except Exception as e:
                        self.log.error(f"[{mod_name}] 恢复删除失败：{rel} — {e}")

            for rel in backed:
                backup_path = os.path.join(backup_root, rel)
                target = os.path.join(game_dir, rel)
                if os.path.exists(backup_path):
                    try:
                        copy_with_dirs(backup_path, target)
                        total_restored += 1
                    except Exception as e:
                        self.log.error(f"[{mod_name}] 恢复备份失败：{rel} — {e}")

        self.clear_session()
        self.log.success(
            f"崩溃恢复完成：删除 {total_deleted} 个 MOD 文件，还原 {total_restored} 个原始文件。"
        )
        return True


# ==================== MOD 管理器核心 ====================

class ModManagerCore:
    def __init__(self, log: LogManager, recovery: RecoveryManager):
        self.log = log
        self.recovery = recovery

    # ---------- 导入 ----------

    def import_mod_from_folder(self, source_dir, mod_name=None):
        if not os.path.isdir(source_dir):
            raise ValueError(f"源路径不是有效目录：{source_dir}")

        if mod_name is None:
            mod_name = os.path.basename(os.path.abspath(source_dir))

        ok, msg = is_valid_mod_name(mod_name)
        if not ok:
            raise ValueError(msg)

        mod_dir = os.path.join(MODS_DIR, mod_name)
        if os.path.exists(mod_dir):
            raise FileExistsError(f"MOD '{mod_name}' 已存在，请先删除或使用其他名称。")

        files_dst = os.path.join(mod_dir, "files")
        os.makedirs(files_dst, exist_ok=True)

        mod_id = generate_mod_id()
        self.log.info(f"开始导入 MOD：{mod_name}（ID：{mod_id}），来源：{source_dir}")

        count = 0
        for dirpath, _, filenames in os.walk(source_dir):
            for fn in filenames:
                src = os.path.join(dirpath, fn)
                rel = os.path.relpath(src, source_dir)
                dst = os.path.join(files_dst, rel)
                copy_with_dirs(src, dst)
                count += 1

        meta = {
            "name": mod_name,
            "mod_id": mod_id,
            "import_time": datetime.datetime.now().isoformat(),
            "source": source_dir,
            "file_count": count,
        }
        save_json(os.path.join(mod_dir, "mod.json"), meta)

        self.log.success(f"MOD '{mod_name}' 导入完成（ID：{mod_id}），共 {count} 个文件。")
        return mod_name, mod_id

    def import_mod_from_zip(self, zip_path, mod_name=None):
        if not os.path.isfile(zip_path):
            raise ValueError(f"文件不存在：{zip_path}")

        if mod_name is None:
            mod_name = os.path.splitext(os.path.basename(zip_path))[0]

        ok, msg = is_valid_mod_name(mod_name)
        if not ok:
            raise ValueError(msg)

        mod_dir = os.path.join(MODS_DIR, mod_name)
        if os.path.exists(mod_dir):
            raise FileExistsError(f"MOD '{mod_name}' 已存在。")

        files_dst = os.path.join(mod_dir, "files")
        os.makedirs(files_dst, exist_ok=True)

        mod_id = generate_mod_id()
        self.log.info(f"开始从 ZIP 导入 MOD：{mod_name}（ID：{mod_id}）")

        count = 0
        with zipfile.ZipFile(zip_path, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = info.filename
                parts = name.replace("\\", "/").split("/")
                rel = "/".join(parts[1:]) if len(parts) > 1 else parts[0]
                if not rel:
                    continue
                dst = os.path.join(files_dst, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with zf.open(info) as src_f, open(dst, "wb") as dst_f:
                    shutil.copyfileobj(src_f, dst_f)
                count += 1

        meta = {
            "name": mod_name,
            "mod_id": mod_id,
            "import_time": datetime.datetime.now().isoformat(),
            "source": zip_path,
            "file_count": count,
        }
        save_json(os.path.join(mod_dir, "mod.json"), meta)

        self.log.success(f"MOD '{mod_name}' 从 ZIP 导入完成（ID：{mod_id}），共 {count} 个文件。")
        return mod_name, mod_id

    # ---------- 查询 ----------

    def list_mods(self):
        mods = []
        if not os.path.isdir(MODS_DIR):
            return mods
        for name in sorted(os.listdir(MODS_DIR)):
            mod_dir = os.path.join(MODS_DIR, name)
            if not os.path.isdir(mod_dir):
                continue
            meta_path = os.path.join(mod_dir, "mod.json")
            meta = load_json(meta_path, {})

            mod_id = meta.get("mod_id", "")
            if not mod_id:
                mod_id = generate_mod_id()
                meta["mod_id"] = mod_id
                save_json(meta_path, meta)

            files_dir = os.path.join(mod_dir, "files")
            file_count = len(get_relative_files(files_dir)) if os.path.isdir(files_dir) else 0
            mods.append({
                "name": name,
                "mod_id": mod_id,
                "import_time": format_import_time(meta.get("import_time", "未知")),
                "file_count": file_count,
                "source": meta.get("source", ""),
                "path": mod_dir,
            })
        return mods

    def _collect_file_owners(self, mod_names):
        file_owners = {}
        for mod_name in mod_names:
            files_dir = os.path.join(MODS_DIR, mod_name, "files")
            if not os.path.isdir(files_dir):
                continue
            for rel in get_relative_files(files_dir):
                file_owners.setdefault(rel, []).append(mod_name)
        return file_owners

    # ---------- 冲突检测 ----------

    def get_conflict_details(self, mod_names):
        owners = self._collect_file_owners(mod_names)
        conflicts = []
        for rel, ms in owners.items():
            if len(ms) <= 1:
                continue
            candidates = []
            for mn in ms:
                fpath = os.path.join(MODS_DIR, mn, "files", rel)
                try:
                    st = os.stat(fpath)
                    candidates.append({
                        "mod": mn,
                        "size": st.st_size,
                        "mtime": st.st_mtime,
                    })
                except Exception:
                    candidates.append({"mod": mn, "size": -1, "mtime": 0})
            conflicts.append({"file": rel, "candidates": candidates})
        conflicts.sort(key=lambda x: x["file"])
        return conflicts

    def check_conflicts(self, mod_names):
        details = self.get_conflict_details(mod_names)
        return [
            {"file": d["file"], "mods": [c["mod"] for c in d["candidates"]]}
            for d in details
        ]

    # ---------- 合并 MOD ----------

    def merge_mods(self, mod_names, new_mod_name, conflict_resolutions):
        if not mod_names:
            raise ValueError("未指定要合并的 MOD。")
        ok, msg = is_valid_mod_name(new_mod_name)
        if not ok:
            raise ValueError(msg)

        new_dir = os.path.join(MODS_DIR, new_mod_name)
        if os.path.exists(new_dir):
            raise FileExistsError(f"名称 '{new_mod_name}' 已被占用。")

        for mn in mod_names:
            if not os.path.isdir(os.path.join(MODS_DIR, mn, "files")):
                raise FileNotFoundError(f"MOD '{mn}' 的文件目录不存在。")

        file_owners = self._collect_file_owners(mod_names)
        conflict_details = self.get_conflict_details(mod_names)
        conflict_map = {c["file"]: c["candidates"] for c in conflict_details}
        conflict_files = set(conflict_map.keys())

        files_dst = os.path.join(new_dir, "files")
        os.makedirs(files_dst, exist_ok=True)

        mod_id = generate_mod_id()
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.log.info(
            f"开始合并 {len(mod_names)} 个 MOD 为新 MOD '{new_mod_name}'"
            f"（ID：{mod_id}），来源：{'、'.join(mod_names)}"
        )

        file_sources = {}
        count = 0
        for rel, owners in file_owners.items():
            if len(owners) == 1:
                src_mod = owners[0]
            else:
                src_mod = conflict_resolutions.get(rel)
                if not src_mod or src_mod not in owners:
                    src_mod = owners[0]
                    self.log.warn(f"冲突文件 {rel} 未指定有效来源，默认使用 {src_mod}。")
            file_sources[rel] = src_mod
            src = os.path.join(MODS_DIR, src_mod, "files", rel)
            dst = os.path.join(files_dst, rel)
            copy_with_dirs(src, dst)
            count += 1

        conflict_count = len(conflict_files)

        # 合并后自检
        validation_ok = True
        validation_msgs = []
        try:
            actual_files = set(get_relative_files(files_dst))
            expected_files = set(file_owners.keys())
            missing = expected_files - actual_files
            extra = actual_files - expected_files
            if missing:
                validation_ok = False
                validation_msgs.append(f"[失败] 缺失 {len(missing)} 个文件：{', '.join(list(missing)[:5])}...")
                self.log.error(f"合并后自检失败：缺失 {len(missing)} 个文件。")
            if extra:
                validation_ok = False
                validation_msgs.append(f"[失败] 出现 {len(extra)} 个未预期的文件。")
                self.log.error(f"合并后自检失败：出现 {len(extra)} 个未预期文件。")
            if len(actual_files) != count:
                validation_ok = False
                validation_msgs.append(f"[失败] 文件数量不一致：预期 {count}，实际 {len(actual_files)}")
            if validation_ok:
                validation_msgs.append(f"[通过] 文件总数与预期一致：{count}")
                validation_msgs.append(f"[通过] 所有预期文件均存在")
                self.log.success(f"合并后自检通过：共 {count} 个文件，无缺失无多余。")
            else:
                self.log.warn("合并后自检发现问题，请查看合并报告了解详情。")
        except Exception as e:
            validation_ok = False
            validation_msgs.append(f"[异常] 自检过程出错：{e}")
            self.log.error(f"合并后自检出现异常：{e}")

        # 生成合并报告
        report_path = os.path.join(new_dir, "merge_report.txt")
        try:
            self._write_merge_report(
                report_path=report_path,
                new_mod_name=new_mod_name,
                mod_id=mod_id,
                now_str=now_str,
                mod_names=mod_names,
                file_sources=file_sources,
                conflict_map=conflict_map,
                conflict_resolutions=conflict_resolutions,
                total_count=count,
                conflict_count=conflict_count,
                validation_msgs=validation_msgs,
                validation_ok=validation_ok,
            )
            self.log.success(f"合并报告已生成：{report_path}")
        except Exception as e:
            self.log.error(f"生成合并报告失败：{e}")

        meta = {
            "name": new_mod_name,
            "mod_id": mod_id,
            "import_time": datetime.datetime.now().isoformat(),
            "source": "merged",
            "merged_from": list(mod_names),
            "conflict_count": conflict_count,
            "file_count": count,
            "merge_validation_ok": validation_ok,
        }
        save_json(os.path.join(new_dir, "mod.json"), meta)

        self.log.success(
            f"合并完成：新 MOD '{new_mod_name}'（ID：{mod_id}），"
            f"共 {count} 个文件，处理了 {conflict_count} 个冲突文件，"
            f"自检{'通过' if validation_ok else '未通过'}。"
        )
        return new_mod_name, mod_id, count, conflict_count, validation_ok

    def _write_merge_report(self, report_path, new_mod_name, mod_id, now_str,
                            mod_names, file_sources, conflict_map,
                            conflict_resolutions, total_count, conflict_count,
                            validation_msgs, validation_ok):
        line = "=" * 72
        sub = "-" * 72

        with open(report_path, "w", encoding="utf-8") as f:
            f.write(line + "\n")
            f.write("MOD 合并报告\n")
            f.write(line + "\n\n")

            f.write(f"生成时间：{now_str}\n")
            f.write(f"新 MOD 名称：{new_mod_name}\n")
            f.write(f"新 MOD ID：{mod_id}\n")
            f.write(f"来源 MOD：{'、'.join(mod_names)}\n")
            f.write(f"合并后文件总数：{total_count}\n")
            f.write(f"冲突文件数：{conflict_count}\n")
            f.write(f"非冲突文件数：{total_count - conflict_count}\n\n")

            f.write(sub + "\n")
            f.write("一、冲突文件处理明细\n")
            f.write(sub + "\n")
            if not conflict_map:
                f.write("（本次合并没有冲突文件。）\n\n")
            else:
                for idx, (rel, candidates) in enumerate(sorted(conflict_map.items()), start=1):
                    f.write(f"\n[冲突 {idx}/{conflict_count}] {rel}\n")
                    f.write("  候选来源：\n")
                    for c in candidates:
                        size_str = format_file_size(c["size"]) if c["size"] >= 0 else "N/A"
                        time_str = format_mtime(c["mtime"]) if c["mtime"] > 0 else "未知"
                        marker = " ★" if conflict_resolutions.get(rel) == c["mod"] else "  "
                        f.write(f"   {marker} {c['mod']:<24} 大小：{size_str:<10} 修改时间：{time_str}\n")
                    chosen = conflict_resolutions.get(rel, candidates[0]["mod"])
                    f.write(f"  最终保留：{chosen}\n")

            f.write("\n" + sub + "\n")
            f.write("二、合并后自检结果\n")
            f.write(sub + "\n")
            if validation_msgs:
                for m in validation_msgs:
                    f.write(m + "\n")
            f.write(f"\n总体结论：{'通过 ✔' if validation_ok else '未通过 ✘'}\n")

            f.write("\n" + sub + "\n")
            f.write("三、完整文件清单\n")
            f.write(sub + "\n")
            f.write(f"{'序号':<6}{'相对路径':<48}{'来源 MOD':<20}备注\n")
            f.write("-" * 72 + "\n")
            sorted_files = sorted(file_sources.keys())
            for idx, rel in enumerate(sorted_files, start=1):
                src = file_sources[rel]
                tag = "[冲突]" if rel in conflict_map else ""
                path_disp = rel if len(rel) <= 46 else "..." + rel[-43:]
                f.write(f"{idx:<6}{path_disp:<48}{src:<20}{tag}\n")

            f.write("\n" + line + "\n")
            f.write("报告结束\n")
            f.write(line + "\n")

    # ---------- 删除 ----------

    def delete_mod(self, mod_name):
        mod_dir = os.path.join(MODS_DIR, mod_name)
        if not os.path.isdir(mod_dir):
            raise FileNotFoundError(f"MOD 不存在：{mod_name}")

        shutil.rmtree(mod_dir, ignore_errors=True)
        self.log.success(f"已删除 MOD：{mod_name}")

        backup_dir = os.path.join(BACKUPS_DIR, mod_name)
        if os.path.isdir(backup_dir):
            shutil.rmtree(backup_dir, ignore_errors=True)
            self.log.info(f"已删除 MOD '{mod_name}' 的备份文件。")

    # ---------- 重命名 ----------

    def rename_mod(self, old_name, new_name):
        if not new_name or old_name == new_name:
            return
        ok, msg = is_valid_mod_name(new_name)
        if not ok:
            raise ValueError(msg)

        old_dir = os.path.join(MODS_DIR, old_name)
        new_dir = os.path.join(MODS_DIR, new_name)

        if not os.path.isdir(old_dir):
            raise FileNotFoundError(f"MOD 不存在：{old_name}")
        if os.path.exists(new_dir):
            raise FileExistsError(f"名称 '{new_name}' 已被占用。")

        os.rename(old_dir, new_dir)

        meta_path = os.path.join(new_dir, "mod.json")
        meta = load_json(meta_path, {})
        meta["name"] = new_name
        save_json(meta_path, meta)

        old_backup = os.path.join(BACKUPS_DIR, old_name)
        new_backup = os.path.join(BACKUPS_DIR, new_name)
        if os.path.isdir(old_backup):
            os.rename(old_backup, new_backup)

        self.log.success(
            f"MOD 已重命名：'{old_name}' → '{new_name}'（ID：{meta.get('mod_id', '未知')} 保持不变）"
        )

    # ---------- 导出 ----------

    def export_mod(self, mod_name, dest_zip_path):
        mod_dir = os.path.join(MODS_DIR, mod_name)
        files_dir = os.path.join(mod_dir, "files")
        if not os.path.isdir(files_dir):
            raise FileNotFoundError(f"MOD '{mod_name}' 的文件目录不存在。")

        with zipfile.ZipFile(dest_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for dirpath, _, filenames in os.walk(files_dir):
                for fn in filenames:
                    full = os.path.join(dirpath, fn)
                    rel = os.path.relpath(full, files_dir)
                    zf.write(full, rel)

        self.log.success(f"MOD '{mod_name}' 已导出到：{dest_zip_path}")

    # ---------- 部署 + 启动 ----------

    def apply_mods_and_launch(self, mod_names, game_dir, exe_name, params, launcher_path=None):
        if not mod_names:
            raise ValueError("未选择任何 MOD。")
        if not os.path.isdir(game_dir):
            raise NotADirectoryError(f"游戏目录不存在：{game_dir}")

        session_mods = []
        total_copied = 0
        total_backed = 0

        for mod_name in mod_names:
            mod_files_dir = os.path.join(MODS_DIR, mod_name, "files")
            if not os.path.isdir(mod_files_dir):
                raise FileNotFoundError(f"MOD '{mod_name}' 的文件目录不存在。")

            backup_root = os.path.join(BACKUPS_DIR, mod_name)
            os.makedirs(backup_root, exist_ok=True)

            rel_files = get_relative_files(mod_files_dir)
            copied = []
            backed = {}

            self.log.info(f"部署 MOD [{mod_name}]，共 {len(rel_files)} 个文件...")

            for rel in rel_files:
                src = os.path.join(mod_files_dir, rel)
                dst = os.path.join(game_dir, rel)

                if os.path.exists(dst):
                    backup_dst = os.path.join(backup_root, rel)
                    if not os.path.exists(backup_dst):
                        copy_with_dirs(dst, backup_dst)
                        self.log.debug(f"[{mod_name}] 备份原始文件：{rel}")
                    backed[rel] = rel

                copy_with_dirs(src, dst)
                copied.append(rel)

            session_mods.append({
                "name": mod_name,
                "copied_files": copied,
                "backed_up_files": backed,
            })
            total_copied += len(copied)
            total_backed += len(backed)

        session = {
            "active": True,
            "mods": session_mods,
            "game_dir": game_dir,
            "start_time": datetime.datetime.now().isoformat(),
        }
        self.recovery.save_session(session)
        self.log.success(
            f"已部署 {len(mod_names)} 个 MOD，共复制 {total_copied} 个文件，备份 {total_backed} 个原始文件。"
        )

        return self._launch_and_wait(game_dir, exe_name, params, launcher_path)

    def _launch_and_wait(self, game_dir, exe_name, params, launcher_path=None):
        if launcher_path and os.path.isfile(launcher_path):
            cmd = [launcher_path, exe_name] + params
        else:
            exe_path = os.path.join(game_dir, exe_name)
            if not os.path.isfile(exe_path):
                raise FileNotFoundError(f"游戏可执行文件不存在：{exe_path}")
            cmd = [exe_path] + params

        self.log.info(f"启动命令：{' '.join(cmd)}")
        self.log.info("游戏运行中，请勿关闭管理器。游戏退出后将自动还原文件。")

        try:
            process = subprocess.Popen(
                cmd,
                cwd=game_dir,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
        except Exception as e:
            self.log.error(f"启动游戏失败：{e}")
            self._restore_after_game(game_dir)
            raise

        process.wait()
        self.log.info(f"游戏进程已结束（退出码：{process.returncode}）。")

        self._restore_after_game(game_dir)

    def _restore_after_game(self, game_dir):
        self.log.info("正在还原原始文件...")
        session = self.recovery.load_session()
        if not session:
            self.log.warn("恢复状态文件不存在，跳过还原。")
            return

        mods = self.recovery.get_session_mods(session)
        total_deleted = 0
        total_restored = 0

        for mod_info in mods:
            mod_name = mod_info["name"]
            copied = mod_info.get("copied_files", [])
            backed = mod_info.get("backed_up_files", {})
            backup_root = os.path.join(BACKUPS_DIR, mod_name)

            for rel in copied:
                target = os.path.join(game_dir, rel)
                if os.path.exists(target):
                    try:
                        os.remove(target)
                        total_deleted += 1
                    except Exception as e:
                        self.log.error(f"[{mod_name}] 删除失败：{rel} — {e}")

            for rel in backed:
                backup_path = os.path.join(backup_root, rel)
                target = os.path.join(game_dir, rel)
                if os.path.exists(backup_path):
                    try:
                        copy_with_dirs(backup_path, target)
                        total_restored += 1
                    except Exception as e:
                        self.log.error(f"[{mod_name}] 还原失败：{rel} — {e}")

        self.recovery.clear_session()
        self.log.success(
            f"还原完成：删除 {total_deleted} 个 MOD 文件，恢复 {total_restored} 个原始文件。"
        )


# ==================== 冲突合并对话框 ====================

class ConflictMergeDialog(tk.Toplevel):
    def __init__(self, parent, conflict_details, mod_names, on_confirm):
        super().__init__(parent)
        self.title("MOD 冲突合并")
        self.geometry("1000x680")
        self.minsize(880, 560)
        self.transient(parent)
        self.grab_set()

        self.conflict_details = conflict_details
        self.mod_names = mod_names
        self.on_confirm = on_confirm

        self.candidates_map = {c["file"]: c["candidates"] for c in conflict_details}
        self.resolutions = {
            c["file"]: c["candidates"][0]["mod"] for c in conflict_details
        }

        self._build_ui()

    def _build_ui(self):
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")
        ttk.Label(
            top,
            text=f"检测到 {len(self.conflict_details)} 个冲突文件，请为每个文件选择保留哪个 MOD 的版本：",
            font=("微软雅黑", 10, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            top,
            text=f"参与合并的 MOD：{'、'.join(self.mod_names)}",
            foreground="gray",
        ).pack(anchor="w", pady=(4, 0))
        ttk.Label(
            top,
            text="操作提示：双击某一行可查看各候选 MOD 的文件大小与修改时间，并切换来源。",
            foreground="gray",
        ).pack(anchor="w", pady=(2, 0))

        list_frame = ttk.LabelFrame(self, text="冲突文件列表", padding=4)
        list_frame.pack(fill="both", expand=True, padx=10, pady=(4, 8))

        columns = ("file", "chosen", "size", "time")
        self.tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=15)
        self.tree.heading("file", text="文件相对路径")
        self.tree.heading("chosen", text="当前保留来源")
        self.tree.heading("size", text="大小")
        self.tree.heading("time", text="修改时间")
        self.tree.column("file", width=460, anchor="w")
        self.tree.column("chosen", width=170, anchor="center")
        self.tree.column("size", width=110, anchor="center")
        self.tree.column("time", width=170, anchor="center")
        self.tree.pack(fill="both", expand=True, side="left")

        sb = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        sb.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=sb.set)

        self.tree.bind("<Double-1>", self._on_double_click)

        for c in self.conflict_details:
            rel = c["file"]
            self._insert_row(rel)

        quick = ttk.Frame(self, padding=(10, 0))
        quick.pack(fill="x")
        ttk.Label(quick, text="批量快捷：").pack(side="left")
        for mn in self.mod_names:
            ttk.Button(
                quick,
                text=f"全部选「{mn}」",
                command=lambda m=mn: self._set_all_to(m),
            ).pack(side="left", padx=3)
        ttk.Button(quick, text="重置为默认", command=self._reset_default).pack(side="left", padx=(12, 3))
        ttk.Button(quick, text="更改选中行来源", command=self._change_selected_row).pack(side="left", padx=(12, 3))

        bottom = ttk.Frame(self, padding=10)
        bottom.pack(fill="x")
        ttk.Label(bottom, text="新合并 MOD 名称：").pack(side="left")
        default_name = f"合并MOD_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.name_var = tk.StringVar(value=default_name)
        entry = ttk.Entry(bottom, textvariable=self.name_var, width=42)
        entry.pack(side="left", padx=4)
        entry.select_range(0, "end")

        btn_row = ttk.Frame(self, padding=(10, 0, 10, 10))
        btn_row.pack(fill="x")
        ttk.Button(btn_row, text="生成合并 MOD", command=self._on_ok).pack(side="right", padx=3)
        ttk.Button(btn_row, text="取消", command=self.destroy).pack(side="right", padx=3)

    def _insert_row(self, rel):
        chosen = self.resolutions[rel]
        candidates = self.candidates_map.get(rel, [])
        cand = next((x for x in candidates if x["mod"] == chosen), None)
        size_str = format_file_size(cand["size"]) if cand and cand["size"] >= 0 else "N/A"
        time_str = format_mtime(cand["mtime"]) if cand and cand["mtime"] > 0 else "未知"
        self.tree.insert("", "end", iid=rel, values=(rel, chosen, size_str, time_str))

    def _update_row(self, rel):
        chosen = self.resolutions[rel]
        candidates = self.candidates_map.get(rel, [])
        cand = next((x for x in candidates if x["mod"] == chosen), None)
        size_str = format_file_size(cand["size"]) if cand and cand["size"] >= 0 else "N/A"
        time_str = format_mtime(cand["mtime"]) if cand and cand["mtime"] > 0 else "未知"
        self.tree.item(rel, values=(rel, chosen, size_str, time_str))

    def _change_selected_row(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先在列表中选中一行。", parent=self)
            return
        self._open_choice_dialog(sel[0])

    def _on_double_click(self, event):
        item = self.tree.identify_row(event.y)
        if not item:
            return
        self._open_choice_dialog(item)

    def _open_choice_dialog(self, rel):
        candidates = self.candidates_map.get(rel, [])
        if len(candidates) <= 1:
            messagebox.showinfo("提示", "该文件只有一个候选来源。", parent=self)
            return

        dlg = tk.Toplevel(self)
        dlg.title("选择来源 MOD")
        dlg.geometry("620x360")
        dlg.transient(self)
        dlg.grab_set()

        ttk.Label(dlg, text="文件：", font=("微软雅黑", 9, "bold")).pack(pady=(12, 2), padx=14, anchor="w")
        ttk.Label(dlg, text=rel, wraplength=580, foreground="#333").pack(padx=14, anchor="w")
        ttk.Separator(dlg, orient="horizontal").pack(fill="x", pady=8, padx=14)
        ttk.Label(dlg, text="请选择保留哪个 MOD 的版本（含大小、修改时间）：").pack(padx=14, anchor="w")

        cand_frame = ttk.Frame(dlg, padding=(20, 6, 14, 6))
        cand_frame.pack(fill="x")

        ttk.Label(cand_frame, text="").grid(row=0, column=0, padx=6)
        ttk.Label(cand_frame, text="MOD 名称", width=24, anchor="w").grid(row=0, column=1, padx=4, sticky="w")
        ttk.Label(cand_frame, text="大小", width=12, anchor="w").grid(row=0, column=2, padx=4, sticky="w")
        ttk.Label(cand_frame, text="修改时间", width=22, anchor="w").grid(row=0, column=3, padx=4, sticky="w")

        var = tk.StringVar(value=self.resolutions[rel])
        for i, c in enumerate(candidates, start=1):
            size_str = format_file_size(c["size"]) if c["size"] >= 0 else "N/A"
            time_str = format_mtime(c["mtime"]) if c["mtime"] > 0 else "未知"
            ttk.Radiobutton(cand_frame, variable=var, value=c["mod"]).grid(row=i, column=0, padx=6)
            ttk.Label(cand_frame, text=c["mod"], width=24, anchor="w").grid(row=i, column=1, padx=4, sticky="w")
            ttk.Label(cand_frame, text=size_str, width=12, anchor="w").grid(row=i, column=2, padx=4, sticky="w")
            ttk.Label(cand_frame, text=time_str, width=22, anchor="w").grid(row=i, column=3, padx=4, sticky="w")

        def do_ok():
            self.resolutions[rel] = var.get()
            self._update_row(rel)
            dlg.destroy()

        btn_row = ttk.Frame(dlg)
        btn_row.pack(pady=14)
        ttk.Button(btn_row, text="确定", command=do_ok).pack(side="left", padx=6)
        ttk.Button(btn_row, text="取消", command=dlg.destroy).pack(side="left", padx=6)

    def _set_all_to(self, mod_name):
        for rel, candidates in self.candidates_map.items():
            cand_mods = [c["mod"] for c in candidates]
            if mod_name in cand_mods:
                self.resolutions[rel] = mod_name
                self._update_row(rel)

    def _reset_default(self):
        for rel, candidates in self.candidates_map.items():
            self.resolutions[rel] = candidates[0]["mod"]
            self._update_row(rel)

    def _on_ok(self):
        new_name = self.name_var.get().strip()
        ok, msg = is_valid_mod_name(new_name)
        if not ok:
            messagebox.showwarning("提示", msg, parent=self)
            return
        self.on_confirm(dict(self.resolutions), new_name)
        self.destroy()


# ==================== GUI 主界面 ====================

class RA2ModManagerGUI:
    def __init__(self):
        ensure_dirs()

        self.log = LogManager(gui_callback=self._append_log)
        self.recovery = RecoveryManager(self.log)
        self.core = ModManagerCore(self.log, self.recovery)

        self.settings = load_json(SETTINGS_FILE, {
            "game_dir": "",
            "yr_exe": DEFAULT_YR_EXE,
            "ra2_exe": DEFAULT_RA2_EXE,
            "launcher_path": "",
        })
        self.history = load_json(HISTORY_FILE, {"params": [], "last": ""})

        self.root = tk.Tk()
        self.root.title("红警2 MOD 管理器 v1.3.1")
        self.root.geometry("1060x800")
        self.root.minsize(940, 680)

        self._set_window_icon()
        self._build_ui()
        self._refresh_mod_list()
        self._load_settings_to_ui()
        self._load_history_to_ui()

        self.log.info("=" * 55)
        self.log.info("红警2 MOD 管理器 v1.3.1 启动。")

        self.root.after(200, self._check_recovery)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _set_window_icon(self):
        """设置窗口图标（兼容 PyInstaller onefile）"""
        icon_path = get_resource_path(ICON_FILENAME)
        if not icon_path:
            self.root.after(100, lambda: self.log.warn(
                f"未找到图标文件 {ICON_FILENAME}，窗口将使用默认图标。"
                f"如需自定义图标，请将 {ICON_FILENAME} 放在程序同级目录，"
                f"并在打包时使用 --add-data 参数。"
            ))
            return
        try:
            self.root.iconbitmap(default=icon_path)
            self.root.after(100, lambda: self.log.info(f"窗口图标已加载：{icon_path}"))
        except Exception as e:
            self.root.after(100, lambda: self.log.warn(f"设置窗口图标失败：{e}"))

    # ---------- 界面构建 ----------

    def _build_ui(self):
        top_frame = ttk.LabelFrame(self.root, text="游戏设置", padding=8)
        top_frame.pack(fill="x", padx=8, pady=(8, 4))

        ttk.Label(top_frame, text="游戏目录：").grid(row=0, column=0, sticky="w")
        self.game_dir_var = tk.StringVar()
        ttk.Entry(top_frame, textvariable=self.game_dir_var, width=60).grid(
            row=0, column=1, padx=4, sticky="ew"
        )
        ttk.Button(top_frame, text="浏览...", command=self._browse_game_dir).grid(
            row=0, column=2, padx=2
        )

        ttk.Label(top_frame, text="尤里EXE：").grid(row=1, column=0, sticky="w", pady=2)
        self.yr_exe_var = tk.StringVar(value=DEFAULT_YR_EXE)
        ttk.Entry(top_frame, textvariable=self.yr_exe_var, width=25).grid(
            row=1, column=1, padx=4, sticky="w"
        )

        ttk.Label(top_frame, text="原版EXE：").grid(row=1, column=1, sticky="e", padx=(240, 0))
        self.ra2_exe_var = tk.StringVar(value=DEFAULT_RA2_EXE)
        ttk.Entry(top_frame, textvariable=self.ra2_exe_var, width=20).grid(
            row=1, column=2, padx=4, sticky="w"
        )

        top_frame.columnconfigure(1, weight=1)

        mid_frame = ttk.Frame(self.root)
        mid_frame.pack(fill="both", expand=True, padx=8, pady=4)

        list_frame = ttk.LabelFrame(mid_frame, text="已导入的 MOD（按住 Ctrl / Shift 可多选）", padding=4)
        list_frame.pack(side="left", fill="both", expand=True)

        columns = ("name", "files", "time", "mod_id", "source")
        self.tree = ttk.Treeview(
            list_frame, columns=columns, show="headings",
            height=10, selectmode="extended"
        )
        self.tree.heading("name", text="MOD 名称")
        self.tree.heading("files", text="文件数")
        self.tree.heading("time", text="导入时间")
        self.tree.heading("mod_id", text="内置 ID")
        self.tree.heading("source", text="来源")
        self.tree.column("name", width=170)
        self.tree.column("files", width=60, anchor="center")
        self.tree.column("time", width=160, anchor="center")
        self.tree.column("mod_id", width=140, anchor="center")
        self.tree.column("source", width=90, anchor="center")
        self.tree.pack(fill="both", expand=True)

        btn_frame = ttk.LabelFrame(mid_frame, text="MOD 操作", padding=6)
        btn_frame.pack(side="right", fill="y", padx=(6, 0))

        ttk.Button(btn_frame, text="导入文件夹", command=self._import_folder, width=18).pack(pady=2)
        ttk.Button(btn_frame, text="导入 ZIP", command=self._import_zip, width=18).pack(pady=2)
        ttk.Button(btn_frame, text="导出选中 MOD", command=self._export_mod, width=18).pack(pady=2)
        ttk.Button(btn_frame, text="重命名选中 MOD", command=self._rename_mod, width=18).pack(pady=2)
        ttk.Button(btn_frame, text="删除选中 MOD", command=self._delete_mod, width=18).pack(pady=2)
        ttk.Separator(btn_frame, orient="horizontal").pack(fill="x", pady=6)

        ttk.Button(btn_frame, text="带MOD启动尤里", command=self._launch_yr, width=18).pack(pady=2)
        ttk.Button(btn_frame, text="带MOD启动原版", command=self._launch_ra2, width=18).pack(pady=2)
        ttk.Separator(btn_frame, orient="horizontal").pack(fill="x", pady=6)

        ttk.Button(btn_frame, text="检查选中MOD冲突", command=self._manual_check_conflicts, width=18).pack(pady=2)
        ttk.Button(btn_frame, text="合并选中 MOD", command=self._merge_selected, width=18).pack(pady=2)
        ttk.Button(btn_frame, text="刷新列表", command=self._refresh_mod_list, width=18).pack(pady=2)
        ttk.Button(btn_frame, text="打开数据目录", command=self._open_data_dir, width=18).pack(pady=2)

        param_frame = ttk.LabelFrame(self.root, text="启动参数设置", padding=6)
        param_frame.pack(fill="x", padx=8, pady=4)

        param_row = ttk.Frame(param_frame)
        param_row.pack(fill="x")
        ttk.Label(param_row, text="自定义参数：").pack(side="left")
        self.param_var = tk.StringVar()
        ttk.Entry(param_row, textvariable=self.param_var, width=50).pack(
            side="left", padx=4, fill="x", expand=True
        )
        ttk.Button(param_row, text="应用", command=self._apply_param).pack(side="left", padx=2)

        default_row = ttk.Frame(param_frame)
        default_row.pack(fill="x", pady=(4, 0))
        ttk.Label(default_row, text="常用参数：").pack(side="left", anchor="n")

        self.default_listbox = tk.Listbox(default_row, height=5, width=60)
        self.default_listbox.pack(side="left", padx=4, fill="x", expand=True)

        for p in DEFAULT_PARAMS:
            self.default_listbox.insert("end", f"{p['value']}  —  {p['name']}")

        btn_col = ttk.Frame(default_row)
        btn_col.pack(side="left", padx=2)
        ttk.Button(btn_col, text="添加选中", command=self._add_selected_param, width=10).pack(pady=1)
        ttk.Button(btn_col, text="清空参数", command=lambda: self.param_var.set(""), width=10).pack(pady=1)

        hist_row = ttk.Frame(param_frame)
        hist_row.pack(fill="x", pady=(4, 0))
        ttk.Label(hist_row, text="历史参数：").pack(side="left")
        self.history_var = tk.StringVar()
        self.history_combo = ttk.Combobox(hist_row, textvariable=self.history_var, width=60)
        self.history_combo.pack(side="left", padx=4, fill="x", expand=True)
        self.history_combo.bind("<<ComboboxSelected>>", self._on_history_selected)

        log_frame = ttk.LabelFrame(self.root, text="运行日志", padding=4)
        log_frame.pack(fill="both", expand=True, padx=8, pady=(4, 8))

        self.log_text = scrolledtext.ScrolledText(
            log_frame, height=8, state="disabled", wrap="word", font=("Consolas", 9)
        )
        self.log_text.pack(fill="both", expand=True)

        log_btn_row = ttk.Frame(log_frame)
        log_btn_row.pack(fill="x", pady=(2, 0))
        ttk.Button(log_btn_row, text="导出日志", command=self._export_log).pack(side="right")
        ttk.Button(log_btn_row, text="清空显示", command=self._clear_log_display).pack(side="right", padx=4)

    # ---------- 设置 ----------

    def _load_settings_to_ui(self):
        self.game_dir_var.set(self.settings.get("game_dir", ""))
        self.yr_exe_var.set(self.settings.get("yr_exe", DEFAULT_YR_EXE))
        self.ra2_exe_var.set(self.settings.get("ra2_exe", DEFAULT_RA2_EXE))

    def _save_settings_from_ui(self):
        self.settings["game_dir"] = self.game_dir_var.get().strip()
        self.settings["yr_exe"] = self.yr_exe_var.get().strip() or DEFAULT_YR_EXE
        self.settings["ra2_exe"] = self.ra2_exe_var.get().strip() or DEFAULT_RA2_EXE
        save_json(SETTINGS_FILE, self.settings)

    def _load_history_to_ui(self):
        history = self.history.get("params", [])
        self.history_combo["values"] = history
        last = self.history.get("last", "")
        if last:
            self.param_var.set(last)

    def _save_history(self):
        current = self.param_var.get().strip()
        if current:
            params = self.history.get("params", [])
            if current not in params:
                params.insert(0, current)
                params = params[:20]
            self.history["params"] = params
            self.history["last"] = current
            save_json(HISTORY_FILE, self.history)
            self.history_combo["values"] = params

    # ---------- 日志 ----------

    def _append_log(self, line):
        def _do():
            self.log_text.configure(state="normal")
            self.log_text.insert("end", line + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        try:
            self.root.after(0, _do)
        except Exception:
            pass

    def _clear_log_display(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _export_log(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".log",
            filetypes=[("日志文件", "*.log"), ("文本文件", "*.txt"), ("所有文件", "*.*")],
            initialfile=f"RA2ModManager_log_{datetime.date.today().strftime('%Y%m%d')}.log",
        )
        if not path:
            return
        try:
            content = self.log_text.get("1.0", "end")
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            self.log.success(f"日志已导出到：{path}")
        except Exception as e:
            self.log.error(f"导出日志失败：{e}")

    # ---------- MOD 列表 ----------

    def _refresh_mod_list(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        mods = self.core.list_mods()
        for m in mods:
            src = m.get("source", "")
            if src == "merged":
                src_tag = "合并"
            elif src:
                src_tag = "本地"
            else:
                src_tag = "未知"
            self.tree.insert(
                "", "end",
                values=(m["name"], m["file_count"], m["import_time"], m["mod_id"], src_tag)
            )
        self.log.info(f"MOD 列表已刷新，共 {len(mods)} 个 MOD。")

    def _get_selected_mods(self):
        sel = self.tree.selection()
        if not sel:
            return []
        return [self.tree.item(item, "values")[0] for item in sel]

    def _get_single_selected_mod(self):
        mods = self._get_selected_mods()
        if not mods:
            messagebox.showwarning("提示", "请先选中一个 MOD。")
            return None
        if len(mods) > 1:
            messagebox.showinfo("提示", "此操作只能选择一个 MOD，请取消多余选择。")
            return None
        return mods[0]

    # ---------- 导入/导出/重命名/删除 ----------

    def _import_folder(self):
        path = filedialog.askdirectory(title="选择 MOD 文件夹")
        if not path:
            return
        try:
            self.core.import_mod_from_folder(path)
            self._refresh_mod_list()
        except Exception as e:
            self.log.error(f"导入文件夹失败：{e}")
            messagebox.showerror("导入失败", str(e))

    def _import_zip(self):
        path = filedialog.askopenfilename(
            title="选择 MOD ZIP 压缩包",
            filetypes=[("ZIP 文件", "*.zip"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            self.core.import_mod_from_zip(path)
            self._refresh_mod_list()
        except Exception as e:
            self.log.error(f"导入 ZIP 失败：{e}")
            messagebox.showerror("导入失败", str(e))

    def _export_mod(self):
        mod_name = self._get_single_selected_mod()
        if not mod_name:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".zip",
            filetypes=[("ZIP 文件", "*.zip")],
            initialfile=f"{mod_name}.zip",
        )
        if not path:
            return
        try:
            self.core.export_mod(mod_name, path)
        except Exception as e:
            self.log.error(f"导出失败：{e}")
            messagebox.showerror("导出失败", str(e))

    def _rename_mod(self):
        mod_name = self._get_single_selected_mod()
        if not mod_name:
            return

        dialog = tk.Toplevel(self.root)
        dialog.title("重命名 MOD")
        dialog.geometry("380x130")
        dialog.transient(self.root)
        dialog.grab_set()

        ttk.Label(dialog, text="新名称：").pack(pady=(14, 4))
        var = tk.StringVar(value=mod_name)
        entry = ttk.Entry(dialog, textvariable=var, width=40)
        entry.pack(pady=2)
        entry.select_range(0, "end")
        entry.focus()

        def do_rename():
            new_name = var.get().strip()
            if not new_name or new_name == mod_name:
                dialog.destroy()
                return
            try:
                self.core.rename_mod(mod_name, new_name)
                self._refresh_mod_list()
                dialog.destroy()
            except Exception as e:
                self.log.error(f"重命名失败：{e}")
                messagebox.showerror("重命名失败", str(e), parent=dialog)

        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(pady=8)
        ttk.Button(btn_frame, text="确定", command=do_rename).pack(side="left", padx=8)
        ttk.Button(btn_frame, text="取消", command=dialog.destroy).pack(side="left", padx=8)

    def _delete_mod(self):
        mods = self._get_selected_mods()
        if not mods:
            messagebox.showwarning("提示", "请先选中至少一个 MOD。")
            return
        if len(mods) == 1:
            msg = f"确定要删除 MOD '{mods[0]}' 及其备份吗？\n此操作不可撤销。"
        else:
            listing = "\n".join(f"  • {n}" for n in mods)
            msg = f"确定要删除以下 {len(mods)} 个 MOD 及其备份吗？\n\n{listing}\n\n此操作不可撤销。"
        if not messagebox.askyesno("确认删除", msg):
            return
        for mod_name in mods:
            try:
                self.core.delete_mod(mod_name)
            except Exception as e:
                self.log.error(f"删除 '{mod_name}' 失败：{e}")
        self._refresh_mod_list()

    # ---------- 冲突检测与合并 ----------

    def _format_conflicts(self, conflicts, mod_names, limit=15):
        lines = []
        for c in conflicts[:limit]:
            owners = c.get("mods") or c.get("candidates") or []
            if owners and isinstance(owners[0], dict):
                owners = [x["mod"] for x in owners]
            lines.append(f"  • {c['file']}  ←  {' / '.join(owners)}")
        if len(conflicts) > limit:
            lines.append(f"  ...（共 {len(conflicts)} 处冲突，仅显示前 {limit} 处）")
        return (
            f"已选中的 {len(mod_names)} 个 MOD 之间存在 {len(conflicts)} 个同名冲突文件。\n\n"
            f"冲突文件列表：\n" + "\n".join(lines)
        )

    def _manual_check_conflicts(self):
        mods = self._get_selected_mods()
        if len(mods) < 2:
            messagebox.showinfo("提示", "请至少选中 2 个 MOD 才能检查冲突。")
            return
        self.log.info(f"手动检查冲突：{'、'.join(mods)}")
        conflicts = self.core.check_conflicts(mods)
        if not conflicts:
            self.log.success(f"检查完成：选中的 {len(mods)} 个 MOD 之间没有冲突文件。")
            messagebox.showinfo("检查结果", f"选中的 {len(mods)} 个 MOD 之间没有冲突文件，可以同时应用。")
        else:
            self.log.warn(f"检查发现 {len(conflicts)} 个冲突文件。")
            if messagebox.askyesno(
                "存在冲突",
                self._format_conflicts(conflicts, mods)
                + "\n\n是否立即打开冲突合并界面，生成一个新的合并 MOD？",
            ):
                self._open_merge_dialog(mods)

    def _merge_selected(self):
        mods = self._get_selected_mods()
        if len(mods) < 2:
            messagebox.showinfo("提示", "请至少选中 2 个 MOD 才能合并。")
            return
        self.log.info(f"用户主动请求合并：{'、'.join(mods)}")
        self._open_merge_dialog(mods)

    def _open_merge_dialog(self, mod_names):
        conflict_details = self.core.get_conflict_details(mod_names)
        if not conflict_details:
            if not messagebox.askyesno(
                "无冲突文件",
                f"选中的 {len(mod_names)} 个 MOD 之间没有同名文件。\n"
                f"是否仍然将它们合并为一个新 MOD？",
            ):
                return

        def on_confirm(resolutions, new_name):
            try:
                name, mod_id, count, conflict_count, validation_ok = self.core.merge_mods(
                    mod_names, new_name, resolutions
                )
                self._refresh_mod_list()
                report_path = os.path.join("mods", name, "merge_report.txt")
                msg = (
                    f"合并 MOD 已生成：\n\n"
                    f"名称：{name}\n"
                    f"ID：{mod_id}\n"
                    f"文件总数：{count}\n"
                    f"处理冲突：{conflict_count} 个\n"
                    f"合并后自检：{'通过 ✔' if validation_ok else '未通过 ✘'}\n\n"
                    f"合并报告已保存到：\n{report_path}"
                )
                messagebox.showinfo("合并完成", msg)
            except Exception as e:
                self.log.error(f"合并失败：{e}")
                self.log.error(traceback.format_exc())
                messagebox.showerror("合并失败", str(e))

        ConflictMergeDialog(self.root, conflict_details, mod_names, on_confirm)

    # ---------- 启动游戏 ----------

    def _validate_launch(self):
        game_dir = self.game_dir_var.get().strip()
        if not game_dir or not os.path.isdir(game_dir):
            messagebox.showwarning("提示", "请先设置有效的游戏目录。")
            return None
        self._save_settings_from_ui()
        return game_dir

    def _launch_yr(self):
        mods = self._get_selected_mods()
        if not mods:
            messagebox.showwarning("提示", "请先选中至少一个 MOD。")
            return
        game_dir = self._validate_launch()
        if not game_dir:
            return
        self._do_launch(mods, game_dir, self.yr_exe_var.get().strip() or DEFAULT_YR_EXE)

    def _launch_ra2(self):
        mods = self._get_selected_mods()
        if not mods:
            messagebox.showwarning("提示", "请先选中至少一个 MOD。")
            return
        game_dir = self._validate_launch()
        if not game_dir:
            return
        self._do_launch(mods, game_dir, self.ra2_exe_var.get().strip() or DEFAULT_RA2_EXE)

    def _do_launch(self, mod_names, game_dir, exe_name):
        if len(mod_names) > 1:
            self.log.info(f"正在检查 {len(mod_names)} 个 MOD 之间的文件冲突...")
            conflicts = self.core.check_conflicts(mod_names)
            if conflicts:
                self.log.error(f"启动被阻止：检测到 {len(conflicts)} 个冲突文件。")
                if messagebox.askyesno(
                    "MOD 冲突",
                    self._format_conflicts(conflicts, mod_names)
                    + "\n\n是否立即打开冲突合并界面，生成一个新的合并 MOD？",
                ):
                    self._open_merge_dialog(mod_names)
                return
            self.log.success(f"冲突检查通过：{len(mod_names)} 个 MOD 之间没有同名文件。")

        params_str = self.param_var.get().strip()
        params = params_str.split() if params_str else []
        self._save_history()

        def run():
            try:
                launcher = self.settings.get("launcher_path", "").strip()
                launcher = launcher if launcher and os.path.isfile(launcher) else None
                self.core.apply_mods_and_launch(
                    mod_names, game_dir, exe_name, params, launcher
                )
            except Exception as e:
                self.log.error(f"启动过程出错：{e}")
                self.log.error(traceback.format_exc())

        threading.Thread(target=run, daemon=True).start()

    # ---------- 参数 ----------

    def _apply_param(self):
        self._save_history()
        self.log.info(f"参数已应用：{self.param_var.get().strip() or '（无）'}")

    def _add_selected_param(self):
        sel = self.default_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        if idx < len(DEFAULT_PARAMS):
            p = DEFAULT_PARAMS[idx]["value"]
            current = self.param_var.get().strip()
            if current:
                if p not in current.split():
                    self.param_var.set(current + " " + p)
            else:
                self.param_var.set(p)
            self.log.info(f"已添加参数：{p}")

    def _on_history_selected(self, event=None):
        selected = self.history_var.get()
        if selected:
            self.param_var.set(selected)

    # ---------- 其他 ----------

    def _browse_game_dir(self):
        path = filedialog.askdirectory(title="选择红警2游戏根目录")
        if path:
            self.game_dir_var.set(path)
            self._save_settings_from_ui()
            self.log.info(f"游戏目录已设置为：{path}")

    def _open_data_dir(self):
        os.startfile(BASE_DIR)

    def _check_recovery(self):
        if self.recovery.has_pending_recovery():
            session = self.recovery.load_session()
            mods = self.recovery.get_session_mods(session)
            names = "、".join(m["name"] for m in mods) if mods else "未知"
            game_dir = self.game_dir_var.get().strip()
            if game_dir and os.path.isdir(game_dir):
                if messagebox.askyesno(
                    "崩溃恢复",
                    f"检测到上次程序异常退出，涉及的 MOD：{names}\n"
                    f"可能残留了未还原的游戏文件。\n是否立即恢复？",
                ):
                    try:
                        self.recovery.perform_recovery(game_dir)
                    except Exception as e:
                        self.log.error(f"崩溃恢复失败：{e}")
            else:
                self.log.warn(
                    "检测到待恢复状态，但游戏目录未设置或无效。"
                    "请设置游戏目录后重启程序以执行恢复。"
                )
                messagebox.showwarning(
                    "需要恢复",
                    f"检测到上次异常退出，涉及的 MOD：{names}\n"
                    f"请先设置游戏目录，然后重启程序以自动恢复。",
                )
        else:
            self.log.info("崩溃恢复检查：无待恢复状态。")

    def _on_close(self):
        self._save_settings_from_ui()
        self._save_history()
        if self.recovery.has_pending_recovery():
            if not messagebox.askyesno(
                "确认退出",
                "当前有未完成的 MOD 应用会话，直接退出可能导致游戏文件未还原。\n"
                "下次启动时将自动尝试恢复。确定退出吗？",
            ):
                return
        self.log.info("管理器退出。")
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# ==================== 入口 ====================

def main():
    try:
        app = RA2ModManagerGUI()
        app.run()
    except Exception as e:
        ensure_dirs()
        err_path = os.path.join(LOGS_DIR, "startup_error.log")
        with open(err_path, "w", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now()}\n{traceback.format_exc()}")
        try:
            messagebox.showerror("致命错误", f"程序启动失败：{e}\n详情见：{err_path}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
