"""macOS 密钥提取 — 通过 C 二进制扫描微信进程内存

来源：wechat-cli (Apache 2.0)
"""

import json
import os
import platform
import plistlib
import subprocess
import sys
import tempfile

from .common import collect_db_files, cross_verify_keys, save_results, scan_memory_for_keys


def _find_binary():
    """查找对应架构的 C 二进制。"""
    machine = platform.machine()
    if machine == "arm64":
        name = "find_all_keys_macos.arm64"
    elif machine == "x86_64":
        name = "find_all_keys_macos.x86_64"
    else:
        raise RuntimeError(f"不支持的 macOS 架构: {machine}")

    # 在项目目录内查找
    base = os.path.dirname(os.path.abspath(__file__))
    bin_path = os.path.join(base, "bin", name)
    if os.path.isfile(bin_path):
        return bin_path

    raise RuntimeError(
        f"找不到密钥提取二进制: {bin_path}\n"
        "请确认项目文件完整"
    )


def _build_entitlements_xml(app_path):
    """构建 entitlements：保留原有权限 + 添加 get-task-allow。"""
    try:
        result = subprocess.run(
            ["codesign", "-d", "--entitlements", ":-", app_path],
            capture_output=True, timeout=15,
        )
        if result.returncode == 0 and result.stdout:
            entitlements = plistlib.loads(result.stdout)
        else:
            entitlements = {}
    except Exception:
        entitlements = {}

    entitlements["com.apple.security.get-task-allow"] = True
    return plistlib.dumps(entitlements, fmt=plistlib.FMT_XML)


def _resign_wechat():
    """Re-sign WeChat: 保留原有 entitlements，仅添加 get-task-allow。"""
    wechat_paths = [
        "/Applications/WeChat.app",
        os.path.expanduser("~/Applications/WeChat.app"),
    ]
    wechat_app = None
    for p in wechat_paths:
        if os.path.isdir(p):
            wechat_app = p
            break

    if wechat_app is None:
        return False, "未找到 WeChat.app（已搜索 /Applications 和 ~/Applications）"

    print(f"\n[*] 检测到 task_for_pid 权限不足，正在对微信重新签名...")
    print(f"    目标: {wechat_app}")

    try:
        ent_data = _build_entitlements_xml(wechat_app)
    except Exception as e:
        return False, f"提取微信原始权限失败: {e}"

    ent_fd, ent_path = tempfile.mkstemp(suffix=".plist")
    try:
        with os.fdopen(ent_fd, "wb") as f:
            f.write(ent_data)

        result = subprocess.run(
            ["codesign", "--force", "--sign", "-", "--entitlements", ent_path, wechat_app],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        os.unlink(ent_path)

    if result.returncode != 0:
        return False, f"codesign 失败: {result.stderr.strip()}"

    print("[+] 签名完成（已保留微信原有权限，仅添加调试访问权限）。")
    print("[+] 请重新启动微信后再执行 init-keys.py。")
    return True, None


def extract_keys(db_dir, output_path, pid=None):
    """通过 C 二进制提取 macOS 微信数据库密钥。"""
    binary = _find_binary()

    work_dir = os.path.dirname(db_dir)
    if not os.path.isdir(work_dir):
        raise RuntimeError(f"微信数据目录不存在: {work_dir}")

    print(f"[+] 使用 C 二进制提取密钥: {binary}")
    print(f"[+] 工作目录: {work_dir}")

    try:
        result = subprocess.run(
            [binary], cwd=work_dir,
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("密钥提取超时（120s）")
    except PermissionError:
        raise RuntimeError(
            f"无法执行 {binary}\n"
            "请确保文件有执行权限: chmod +x " + binary
        )

    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)

    # 检测 task_for_pid 失败 → 尝试 re-sign
    combined_output = (result.stdout or "") + (result.stderr or "")
    if "task_for_pid" in combined_output:
        print("\n[!] task_for_pid 失败：macOS 安全策略阻止了进程内存访问。")
        ok, err = _resign_wechat()
        if ok:
            raise RuntimeError(
                "已对微信重新签名。请执行以下步骤后重试：\n"
                "  1. 完全退出微信\n"
                "  2. 重新打开微信并登录\n"
                "  3. 再次执行: sudo python3 init-keys.py"
            )
        else:
            raise RuntimeError(
                f"自动签名失败: {err}\n"
                "请手动执行：\n"
                "  codesign -d --entitlements wechat_ent.plist /Applications/WeChat.app\n"
                '  /usr/libexec/PlistBuddy -c "Add :com.apple.security.get-task-allow bool true" wechat_ent.plist\n'
                "  codesign --force --sign - --entitlements wechat_ent.plist /Applications/WeChat.app\n"
                "  rm wechat_ent.plist\n"
                "然后重启微信，再执行: sudo python3 init-keys.py"
            )

    # C 二进制输出 all_keys.json 到 work_dir
    c_output = os.path.join(work_dir, "all_keys.json")
    if not os.path.exists(c_output):
        raise RuntimeError(
            "C 二进制未能生成密钥文件。\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    with open(c_output, encoding="utf-8") as f:
        keys_data = json.load(f)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(keys_data, f, indent=2, ensure_ascii=False)
    os.chmod(output_path, 0o600)  # 仅当前用户可读写，保护密钥安全

    if os.path.abspath(c_output) != os.path.abspath(output_path):
        os.remove(c_output)

    key_map = {}
    for rel, info in keys_data.items():
        if isinstance(info, dict) and "enc_key" in info and "salt" in info:
            key_map[info["salt"]] = info["enc_key"]

    print(f"\n[+] 提取到 {len(key_map)} 个密钥，保存到: {output_path}")
    return key_map
