#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
这是用于快速将 HAP 包签名、打包并通过无线安装到 Harmony Next 系统设备的脚本

基于 https://github.com/likuai2010/auto-installer.git 实现

具体用法，请运行以下命令查看。

$ python auto-installer.py -h
"""

import argparse
import http.server
import json
import os
import re
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import urllib.parse

try:
    import requests
except ImportError:
    print("[ERROR] 未安装 requests 模块，请执行: pip3 install requests")
    sys.exit(1)

# ============ 全局调试开关 ============
DEBUG = False  # 若要打印 debug 日志，请将其置为 True

# ============ 全局常量/变量 ============
HOME_DIR = os.path.expanduser("~")
CONFIG_DIR = os.path.join(HOME_DIR, ".autoPublisher", "config")
STORE_DIR = os.path.join(CONFIG_DIR, "store")  # 用于存储 .p12 keystore / csr
ECO_CONFIG_FILE = os.path.join(CONFIG_DIR, "eco_config.json")

# JDK目录，将在前置操作中被赋值
JAVA_HOME = None

# Harmony命令行工具路径, 将在前置操作中被赋值
HDC_COMMAND = None
SIGN_JAR = None
APP_UNPACK_TOOL = None
APP_PACK_TOOL = None

# 设备IP：端口，通过命令行读入
DEVICE_IP = None

CERT_NAME = "xiaobai-debug"
PROFILE_NAME = "xiaobai-debug"

DEFAULT_STOREPASS = "xiaobai123"
DEFAULT_KEYALIAS = "xiaobai"


# ============ 工具函数：日志输出 ============
def debug_print(message: str):
    """在 DEBUG = True 时，才打印调试信息"""
    if DEBUG:
        print(f"[DEBUG] {message}")

def info_print(message: str):
    """正常信息输出"""
    print(f"[INFO] {message}")

def error_print(message: str):
    """错误信息输出"""
    print(f"[ERROR] {message}", file=sys.stderr)

# 全局变量，用于跟踪步骤数
step_counter = -1
def separator_print(title: str):
    """打印一个明显的分隔符，带标题和步骤数"""
    global step_counter
    step_counter += 1
    
    print("\n" + "=" * 60)
    print(f"=== 步骤{step_counter}: {title}")
    print("=" * 60 + "\n")

# ============ 前置检查相关 ============
def ensure_jdk17():
    """
    确保 JDK17+ 环境：
      - 若系统已设置 JAVA_HOME，则使用 JAVA_HOME/bin/java 检查版本
      - 否则用 which java -> readlink -f 推断 JAVA_HOME
      - 若版本 <17 或无 keytool，则报错并给出官方文档链接和持久化设置方法
    """
    java_install_doc = (
        "https://developer.huawei.com/consumer/cn/doc/harmonyos-guides-V5/"
        "ide-command-line-building-app-V5#section195447475220"
    )

    # 获取用户设置的 JAVA_HOME
    user_java_home = os.environ.get("JAVA_HOME", "").strip()
    if user_java_home:
        java_bin = os.path.join(user_java_home, "bin", "java")
        if not (os.path.isfile(java_bin) and os.access(java_bin, os.X_OK)):
            error_print(
                f"检测到 JAVA_HOME={user_java_home}，但其中未找到可执行文件：{java_bin}"
            )
            error_print("请确认 JDK 安装完整并且该路径下包含 bin/java。")
            error_print("参考文档：\n" + java_install_doc)
            error_print("设置示例（Linux/macOS）：")
            error_print("  export JAVA_HOME=/path/to/jdk17")
            error_print("  export PATH=$JAVA_HOME/bin:$PATH")
            sys.exit(1)
    else:
        # 未设置 JAVA_HOME，尝试 which java
        try:
            java_path = subprocess.check_output(
                ["which", "java"], stderr=subprocess.STDOUT
            ).decode().strip()
            if not java_path:
                error_print("未检测到 java 命令，也未设置 JAVA_HOME，需要 JDK17+。")
                error_print("请按以下文档安装并配置 JDK17：\n" + java_install_doc)
                sys.exit(1)
            java_bin = java_path
        except subprocess.CalledProcessError as e:
            error_print(
                f"无法定位 java 命令，请先安装 JDK17并配置环境变量。异常信息: {e.output.decode().strip()}"
            )
            error_print("安装与配置参考: \n" + java_install_doc)
            sys.exit(1)

    # 执行 java -version 检测版本
    try:
        version_output = subprocess.check_output(
            [java_bin, "-version"], stderr=subprocess.STDOUT
        ).decode("utf-8", "ignore")
    except subprocess.CalledProcessError as e:
        error_print(f"执行 {java_bin} -version 出错:\n{e.output.decode()}")
        error_print("请确认 JDK17 安装无误并在 PATH 或 JAVA_HOME 中配置。")
        sys.exit(1)

    match = re.search(r'version\s+"?(\d+(\.\d+)*)', version_output)
    if not match:
        error_print("无法解析 Java 版本，请确认已安装 JDK17+。")
        error_print("参考文档：\n" + java_install_doc)
        sys.exit(1)

    major_version = int(match.group(1).split(".")[0])
    if major_version < 17:
        error_print(f"检测到 Java 版本({match.group(1)})过低，需要 JDK17+。")
        error_print("当前 java -version 输出：\n" + version_output)
        error_print(
            "请升级或安装 JDK17并配置 JAVA_HOME 或 PATH。参考：\n" + java_install_doc
        )
        sys.exit(1)

    # 若用户没有显式设置 JAVA_HOME，则尝试自动推断并写入
    if not user_java_home:
        try:
            real_path = subprocess.check_output(
                ["readlink", "-f", java_bin], stderr=subprocess.STDOUT
            ).decode().strip()
            java_bin_dir = os.path.dirname(real_path)
            user_java_home = os.path.dirname(java_bin_dir)
            os.environ["JAVA_HOME"] = user_java_home
            info_print(f"系统未设置 JAVA_HOME，已自动推断并设置：{user_java_home}")
        except subprocess.CalledProcessError as e:
            error_print("无法自动推断 JAVA_HOME，请手动设置后重试。")
            error_print("例如在 ~/.bashrc 或 ~/.zshrc 中添加：")
            error_print("  export JAVA_HOME=/path/to/jdk17")
            error_print("  export PATH=$JAVA_HOME/bin:$PATH")
            sys.exit(1)

    # 检查 keytool
    keytool_path = os.path.join(user_java_home, "bin", "keytool")
    if not (os.path.isfile(keytool_path) and os.access(keytool_path, os.X_OK)):
        error_print(f"在 JAVA_HOME={user_java_home} 下未找到可执行的 keytool。")
        error_print("请确认 JDK 安装目录完整，bin/ 下应包含 keytool。")
        error_print("参考文档：\n" + java_install_doc)
        sys.exit(1)

    global JAVA_HOME
    JAVA_HOME = user_java_home
    info_print(f"已检测到 JDK17+，JAVA_HOME={JAVA_HOME}")

def test_connect_device():
    """
    使用 hdc tconn device_ip 测试是否能与设备建立连接。
    若失败则退出并提示相应帮助信息。
    """
    global HDC_COMMAND
    global DEVICE_IP

    assert HDC_COMMAND is not None, "HDC_COMMAND未初始化？？？"
    cmd = [HDC_COMMAND, "tconn", DEVICE_IP]
    debug_print(f"执行命令: {cmd}")

    try:
        output = subprocess.check_output(
            cmd, stderr=subprocess.STDOUT, encoding="utf-8", timeout=10
        )
        debug_print(f"[DEBUG] hdc tconn 输出：\n{output}")
        if "Connect failed" in output:
            error_print(f"连接设备失败: {DEVICE_IP}。")
            error_print("1. 请确保设备和电脑在同一网段(且没有使用代理)")
            error_print(
                "2. 请打开设备上的无线调试： 系统 -> 开发者选项 -> 无线调试，并使用其中的IP和端口，作为参数 -d ip:port 来调用本脚本"
            )
            sys.exit(1)
    except subprocess.CalledProcessError as e:
        error_print(f"连接设备失败: {DEVICE_IP}。命令输出：\n{e.output}")
        error_print("请检查 HDC_HOME、网络环境、设备设置等是否正确。")
        sys.exit(1)
    except subprocess.TimeoutExpired:
        error_print(f"连接设备命令超时: {DEVICE_IP}")
        sys.exit(1)

    info_print("测试设备连接成功。")

def ensure_hdc_tools():
    """
    检查 Harmony 开发命令行工具是否安装。
    若不存在或不可执行则退出，并给出对应文档链接。

    参考文档：
    https://developer.huawei.com/consumer/cn/doc/harmonyos-guides-V5/ide-command-line-building-app-V5#section6767112163710
    """
    hdc_install_doc = (
        "https://developer.huawei.com/consumer/cn/doc/harmonyos-guides-V5/"
        "ide-command-line-building-app-V5#section6767112163710"
    )
    hdc_home = os.environ.get("HDC_HOME", "").strip()
    if not hdc_home:
        error_print("未设置环境变量 HDC_HOME，无法使用 hdc 工具。")
        error_print("请参考官方文档安装并配置 HDC：\n" + hdc_install_doc)
        error_print("示例：")
        error_print("  export HDC_HOME=/path/to/hdc_folder")
        error_print("  export PATH=$HDC_HOME:$PATH")
        sys.exit(1)

    hdc_cmd = os.path.join(hdc_home, "hdc")
    if not (os.path.isfile(hdc_cmd) and os.access(hdc_cmd, os.X_OK)):
        error_print(f"在 HDC_HOME={hdc_home} 下未找到可执行的 hdc：{hdc_cmd}")
        error_print("请确认 hdc 安装与解压是否正确，或重新下载并配置 HDC_HOME。")
        error_print("参考文档：\n" + hdc_install_doc)
        sys.exit(1)

    global HDC_COMMAND
    HDC_COMMAND = hdc_cmd
    info_print(f"已检测到 hdc 工具：{HDC_COMMAND}")

    global SIGN_JAR
    SIGN_JAR = os.path.join(hdc_home, "lib", "hap-sign-tool.jar")
    if not os.path.isfile(SIGN_JAR):
        error_print(f"签名 jar 不存在: {SIGN_JAR}")
        sys.exit(1)
    info_print(f"已检测到 hap 签名工具：{SIGN_JAR}")

    global APP_UNPACK_TOOL
    APP_UNPACK_TOOL = os.path.join(hdc_home, "lib", "app_unpacking_tool.jar")
    if not os.path.isfile(APP_UNPACK_TOOL):
        error_print(f"解包工具 jar 不存在: {APP_UNPACK_TOOL}")
        sys.exit(1)
    info_print(f"已检测到 hap 解包工具：{APP_UNPACK_TOOL}")

    global APP_PACK_TOOL
    APP_PACK_TOOL = os.path.join(hdc_home, "lib", "app_packing_tool.jar")
    if not os.path.isfile(APP_PACK_TOOL):
        error_print(f"打包工具 jar 不存在: {APP_PACK_TOOL}")
        sys.exit(1)
    info_print(f"已检测到 hap 打包工具：{APP_PACK_TOOL}")

def check_create_config_dir():
    """检查并创建配置目录"""
    if not os.path.isdir(CONFIG_DIR):
        os.makedirs(CONFIG_DIR)
        info_print(f"已创建配置目录 CONFIG_DIR: {CONFIG_DIR}")
    else:
        info_print(f"配置目录 CONFIG_DIR: {CONFIG_DIR}已存在")

def check_copy_store_files():
    """
    检查并复制 store/ 目录下的 xiaobai.csr 和 xiaobai.p12 文件到 STORE_DIR。
    如果不存在则退出并提示。
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    store_dir = os.path.join(script_dir, "store")

    # 检查 store/ 目录是否存在
    if not os.path.isdir(store_dir):
        error_print(f"未找到 store/ 目录: {store_dir}")
        error_print(
            "请将 store/ 目录与脚本放在同级，并放入 xiaobai.csr、xiaobai.p12 等必要文件。"
        )
        sys.exit(1)

    # 检查 xiaobai.csr 文件
    csr_file = os.path.join(store_dir, "xiaobai.csr")
    if not (os.path.isfile(csr_file) and os.access(csr_file, os.R_OK)):
        error_print(f"{csr_file} 不存在或不可读，请检查。")
        sys.exit(1)

    # 检查 xiaobai.p12 文件
    p12_file = os.path.join(store_dir, "xiaobai.p12")
    if not (os.path.isfile(p12_file) and os.access(p12_file, os.R_OK)):
        error_print(f"{p12_file} 不存在或不可读，请检查。")
        sys.exit(1)

    # 确保 STORE_DIR 存在
    if not os.path.isdir(STORE_DIR):
        try:
            os.makedirs(STORE_DIR)
            info_print(f"已创建 STORE_DIR 目录: {STORE_DIR}")
        except Exception as e:
            error_print(f"无法创建 STORE_DIR 目录: {STORE_DIR}，错误: {e}")
            sys.exit(1)

    # 复制 xiaobai.csr 到 STORE_DIR，覆盖已有文件
    try:
        shutil.copy2(csr_file, STORE_DIR)
        update_config(csr_file=os.path.join(STORE_DIR, os.path.basename(csr_file)))
        debug_print(f"已复制 {csr_file} 到 {STORE_DIR}, 并更新配置文件")
    except Exception as e:
        error_print(f"复制 {csr_file} 到 {STORE_DIR} 失败，错误: {e}")
        sys.exit(1)

    # 复制 xiaobai.p12 到 STORE_DIR，覆盖已有文件
    try:
        shutil.copy2(p12_file, STORE_DIR)
        update_config(keystore_file=os.path.join(STORE_DIR, os.path.basename(p12_file)))
        debug_print(f"已复制 {p12_file} 到 {STORE_DIR}, 并更新配置文件")
    except Exception as e:
        error_print(f"复制 {p12_file} 到 {STORE_DIR} 失败，错误: {e}")
        sys.exit(1)

    # 打印成功日志
    info_print(f"已将 xiaobai.csr 和 xiaobai.p12 复制到 STORE_DIR 目录下:  {STORE_DIR}。")

def clear_eco_config_file():
    """清空 ECO_CONFIG_FILE 文件"""
    try:
        with open(ECO_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f, ensure_ascii=False, indent=2)
        info_print(f"ECO_CONFIG_FILE 已被清空: {ECO_CONFIG_FILE}")
    except Exception as e:
        error_print(f"清空 ECO_CONFIG_FILE 失败: {e}")
        sys.exit(1)

def initialize_eco_config():
    """
    初始化 eco_config.json 文件：
    1. 如果文件不存在，创建并写入默认值。
    2. 如果文件存在，确保 'storepass' 和 'keyAlias' 存在，否则设置默认值。
    """
    if DEBUG:
        clear_eco_config_file()

    if not os.path.isfile(ECO_CONFIG_FILE):
        info_print("创建新的 eco_config.json 并写入默认值。")
        default_config = {
            "storepass": DEFAULT_STOREPASS,
            "keyalias": DEFAULT_KEYALIAS,
        }
        write_eco_config(default_config)
    else:
        # 确保 'storepass' 存在
        storepass = get_config_value("storepass")
        if not storepass:
            info_print(f"缺少 'storepass'，使用默认值 {DEFAULT_STOREPASS} 进行更新。")
            update_config(storepass=DEFAULT_STOREPASS)
        
        # 确保 'keyAlias' 存在
        keyalias = get_config_value("keyAlias")
        if not keyalias:
            info_print(f"缺少 'keyalias'，使用默认值 {DEFAULT_KEYALIAS} 进行更新。")
            update_config(keyalias=DEFAULT_KEYALIAS)
    
    info_print("eco_config.json 初始化完成。")

def check_prerequisite():
    """
    最终对外提供的前提检查函数：
      1. 确保 JDK17 环境可用
      2. 确保 HDC_HOME 设置正确并具备可执行 hdc
      3. 检查 store/ 下 xiaobai.csr/p12 文件并复制到 STORE_DIR
      4. 清空 ECO_CONFIG_FILE
    """
    separator_print("步骤0: 前提检查及操作")

    # 1. Java (JDK17)
    ensure_jdk17()

    # 32. Harmony 开发命令行工具(hdc)
    ensure_hdc_tools()

    # 3. 有了hdc，测试连接设备
    test_connect_device()

    # 4. 创建 CONFIG_DIR
    check_create_config_dir()

    # 5. 初始化 ECO_CONFIG_FILE
    initialize_eco_config()

    # 6. 复制 store 文件到 STORE_DIR
    check_copy_store_files()

    info_print("===== 前提检查及操作全部通过! =====\n")

# ============ 配置文件读取/写入 ============
def read_eco_config() -> dict:
    """
    读取 eco_config.json 并返回字典。如果不存在则返回空字典。
    """
    if not os.path.isfile(ECO_CONFIG_FILE):
        return {}
    try:
        with open(ECO_CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        error_print(f"读取配置文件失败: {ECO_CONFIG_FILE}, error: {e}")
        return {}

def write_eco_config(data: dict):
    """
    将 data 写入到 eco_config.json
    """
    try:
        with open(ECO_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        info_print(f"配置已更新至: {ECO_CONFIG_FILE}")
    except Exception as e:
        error_print(f"写入配置文件失败: {ECO_CONFIG_FILE}, error: {e}")

def get_config_value(key: str) -> str:
    """
    获取 eco_config.json 中的某个 key 的值，如果不存在返回空字符串
    """
    config = read_eco_config()
    return config.get(key, "")

def update_config(**kwargs):
    """
    批量更新 eco_config.json 的字段
    """
    config = read_eco_config()
    for k, v in kwargs.items():
        config[k] = v
    write_eco_config(config)

# ============ HTTP 请求相关 ============
def http_request(method: str, url: str, data=None):
    """
    发送 HTTP 请求并返回响应内容和状态码。

    参数：
        method (str): HTTP 方法，如 'GET' 或 'POST'
        url (str): 请求的 URL
        data: 发送的数据（仅在 POST 时使用）

    返回：
        tuple: (response_body, http_code)
    """
    config = read_eco_config()
    oauth2_token = config.get("oauth2_token", "")
    team_id = config.get("team_id", "")
    uid = config.get("uid", "")

    headers = {
        "Content-Type": "application/json",
        "oauth2Token": oauth2_token,
        "teamId": team_id,
        "uid": uid
    }

    debug_print(f"HTTP {method} -> {url}, data={data}, headers={headers}")

    try:
        if method.upper() == "GET":
            response = requests.get(url, headers=headers, timeout=15)
        elif method.upper() == "POST":
            response = requests.post(url, headers=headers, json=data, timeout=15)
        else:
            error_print(f"不支持的 HTTP method: {method}")
            return None, 0
        return response.text, response.status_code
    except requests.RequestException as e:
        error_print(f"请求失败: {e}")
        return None, 0

# ============ 文件下载 ============
def download_file(file_url: str, file_name: str) -> str:
    """
    下载文件到 CONFIG_DIR/file_name，返回本地路径

    参数：
        file_url (str): 文件下载 URL
        file_name (str): 本地保存的文件名

    返回：
        str: 本地文件路径
    """
    local_path = os.path.join(CONFIG_DIR, file_name)
    info_print(f"正在下载 {file_url} -> {local_path}")
    try:
        response = requests.get(file_url, stream=True, timeout=30)
        response.raise_for_status()
        with open(local_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        info_print(f"下载完成: {local_path}")
    except requests.RequestException as e:
        error_print(f"下载失败: {e}")
        sys.exit(1)
    return local_path

# ============ HAP 解包 / 打包 ============
def unpack_hap(hap_path: str, unpack_dir: str):
    """
    调用 app_unpacking_tool.jar 进行解包

    参数：
        hap_path (str): 待解包的 HAP 文件路径
        unpack_dir (str): 解包后的目录路径
    """
    debug_print(f"解包 .hap 文件: {hap_path} -> {unpack_dir}")
    cmd = [
        os.path.join(JAVA_HOME, "bin", "java"),
        "-jar", APP_UNPACK_TOOL,
        "--mode", "hap",
        "--hap-path", hap_path,
        "--out-path", unpack_dir,
        "--force", "true"
    ]
    debug_print(" ".join(cmd))
    try:
        subprocess.check_call(cmd)
        info_print(f"解包成功: {hap_path} -> {unpack_dir}")
    except subprocess.CalledProcessError as e:
        error_print(f"解包失败: {e}")
        sys.exit(1)

def pack_hap(output_hap_path: str, unpack_dir: str):
    """
    调用 app_packing_tool.jar 进行打包

    参数：
        output_hap_path (str): 输出的 HAP 文件路径
        unpack_dir (str): 解包的目录路径
    """
    debug_print(f"重新打包 .hap 文件: {output_hap_path}")
    cmd = [
        os.path.join(JAVA_HOME, "bin", "java"),
        "-jar", APP_PACK_TOOL,
        "--mode", "hap",
        "--json-path", os.path.join(unpack_dir, "module.json"),
        "--lib-path", os.path.join(unpack_dir, "libs"),
        "--resources-path", os.path.join(unpack_dir, "resources"),
        "--index-path", os.path.join(unpack_dir, "resources.index"),
        "--ets-path", os.path.join(unpack_dir, "ets"),
        "--pack-info-path", os.path.join(unpack_dir, "pack.info"),
        "--force", "true",
        "--out-path", output_hap_path
    ]
    debug_print(" ".join(cmd))
    try:
        subprocess.check_call(cmd)
        info_print(f"打包成功: {output_hap_path}")
    except subprocess.CalledProcessError as e:
        error_print(f"打包失败: {e}")
        sys.exit(1)

def update_get_hap_info(hap_path: str):
    """
    解包 HAP，修改 module.json 里的 app.debug = true，获取 bundleName，打包新文件
    返回 (bundle_name, new_hap_path)

    参数：
        hap_path (str): 原始 HAP 文件路径

    返回：
        tuple: (bundle_name, new_hap_path)
    """
    info_print(f"开始处理 HAP 文件: {hap_path}")
    unpack_dir = tempfile.mkdtemp(prefix="hap_unpack_")
    debug_print(f"临时解包目录: {unpack_dir}")

    # 解包
    unpack_hap(hap_path, unpack_dir)

    # module.json
    module_json_path = os.path.join(unpack_dir, "module.json")
    if not os.path.isfile(module_json_path):
        error_print(f"未找到 module.json, 解包目录: {unpack_dir}")
        return None, None

    with open(module_json_path, "r", encoding="utf-8") as f:
        try:
            module_data = json.load(f)
        except json.JSONDecodeError as e:
            error_print(f"解析 module.json 失败: {e}")
            return None, None

    bundle_name = module_data.get("app", {}).get("bundleName", "")
    info_print(f"获取到包名: {bundle_name}")

    # 修改 debug = true
    if "app" in module_data:
        module_data["app"]["debug"] = True
    else:
        module_data["app"] = {"debug": True}

    # 写回 module.json
    with open(module_json_path, "w", encoding="utf-8") as f:
        json.dump(module_data, f, ensure_ascii=False, indent=2)
    info_print(f"已修改 module.json 中的 app.debug 为 True")

    # 生成新的 .hap
    hap_basename = os.path.splitext(os.path.basename(hap_path))[0]
    new_hap_path = os.path.join(
        os.path.dirname(hap_path),
        f"{hap_basename}_updated.hap"
    )

    pack_hap(new_hap_path, unpack_dir)

    # 清理临时目录
    shutil.rmtree(unpack_dir, ignore_errors=True)
    debug_print(f"已清理临时解包目录: {unpack_dir}")

    info_print(f"处理完成，新的 HAP 文件: {new_hap_path}")
    return bundle_name, new_hap_path

# ============ 账户登录相关 ============
class SingleFileLogger:
    """
    简易单文件日志记录器：
      - 构造时以追加模式打开文件
      - info()/error() 写日志并 flush
      - close() 在结束时调用，以释放文件句柄
    """

    def __init__(self, log_path: str):
        self.log_path = log_path
        self.file = open(log_path, "a", encoding="utf-8")

    def info(self, message: str):
        """记录信息日志"""
        self.file.write(f"[INFO] {message}\n")
        self.file.flush()

    def error(self, message: str):
        """记录错误日志"""
        self.file.write(f"[ERROR] {message}\n")
        self.file.flush()

    def close(self):
        """关闭日志文件"""
        if not self.file.closed:
            self.file.close()

# 定义用于处理浏览器登录回调的 Handler
class OAuthHandler(http.server.SimpleHTTPRequestHandler):
    """
    处理 OAuth 回调请求的 HTTP 处理器。
    """

    def __init__(self, *args, token_file=None, logger=None, **kwargs):
        """
        初始化 OAuthHandler。

        参数：
            token_file (str): 存储 token 的文件路径
            logger (SingleFileLogger): 日志记录器
        """
        self.token_file = token_file
        self.logger = logger
        super().__init__(*args, **kwargs)

    def log_message(self, format, *args):
        """
        覆盖默认的 log_message 方法，隐藏访问日志。
        """
        pass  # 不执行任何操作，隐藏日志，我们有自己的日志打印

    def do_POST(self):
        """
        处理 POST 请求，完成 OAuth 流程。
        """
        parsed_path = urllib.parse.urlparse(self.path)
        if parsed_path.path != "/callback":
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'Not Found')
            self.logger.error(f"收到未知路径的 POST 请求: {parsed_path.path}")
            return

        # 1. 从回调数据中提取 tempToken
        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length).decode('utf-8')
        self.logger.info(f"收到回调 post_data: {post_data}")

        params = urllib.parse.parse_qs(post_data)
        temp_token_list = params.get('tempToken', [])
        if not temp_token_list:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'Missing tempToken')
            self.logger.error("回调中缺少 tempToken")
            return

        temp_token = temp_token_list[0]
        self.logger.info(f"获取到 tempToken: {temp_token}")

        # 2. 第一次调用: 用 tempToken 获取 jwtToken
        temptoken_url = (
            "https://cn.devecostudio.huawei.com/authrouter/auth/api/"
            f"temptoken/check?site=CN&tempToken={temp_token}&appid=1007&version=0.0.0"
        )
        session = requests.Session()
        try:
            self.logger.info("用 tempToken 获取 jwtToken...")
            r1 = session.get(temptoken_url, timeout=10)
            r1.raise_for_status()
        except requests.RequestException as e:
            self.logger.error(f"temptoken/check 接口调用失败: {e}")
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'Failed to verify tempToken')
            return

        jwt_token = r1.text.strip()
        self.logger.info(f"获取到 jwtToken: {jwt_token}")

        # 3. 第二次调用: 用 jwtToken 换 userInfo
        jwtoken_url = "https://cn.devecostudio.huawei.com/authrouter/auth/api/jwToken/check"
        headers = {"jwtToken": jwt_token, "refresh": "false"}
        try:
            self.logger.info("用 jwtToken 获取 userInfo...")
            r2 = session.get(jwtoken_url, headers=headers, timeout=10)
            r2.raise_for_status()
            resp_json = r2.json()
        except requests.RequestException as e:
            self.logger.error(f"jwToken/check 接口调用失败: {e}")
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'Failed to verify jwtToken')
            return
        except json.JSONDecodeError as e:
            self.logger.error(f"解析 jwToken/check 响应失败: {e}")
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'Invalid response format')
            return

        user_info = resp_json.get("userInfo", {})
        self.logger.info(f"userInfo: {user_info}")

        if not user_info:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'userInfo not found')
            self.logger.error("获取 userInfo 失败")
            return

        # 4. 写入 token_file
        try:
            with open(self.token_file, "w", encoding="utf-8") as f:
                json.dump(user_info, f)
            self.logger.info(f"把 userInfo 成功写入 token_file: {self.token_file}")
        except Exception as e:
            self.logger.error(f"写 token_file 出错: {e}")

        # 5. 返回给浏览器
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("登录成功，可关掉此页面。".encode("utf-8"))
        self.logger.info("回调处理完成: 登录成功页面已返回给用户")

        # 6. 关闭服务器
        threading.Thread(target=self.server.shutdown).start()
        self.logger.info("关闭服务器(server.shutdown)")

def run_login_callback_handler_server_in_background(token_file: str, port: int = 3333):
    """
    后台启动登录回调处理服务器，并只打开一次日志文件。
    发生异常或退出时在 finally 中关闭日志文件。
    返回线程对象和日志文件路径，方便上层提示用户排查问题。

    参数：
        token_file (str): 存储 token 的文件路径
        port (int): 服务器监听的端口号

    返回：
        tuple: (threading.Thread, str)
    """
    server_log_path = tempfile.mktemp(prefix="login_callback_handler_log_")
    logger = SingleFileLogger(server_log_path)

    def handler_with_token(*args, **kwargs):
        return OAuthHandler(*args, token_file=token_file, logger=logger, **kwargs)

    try:
        httpd = socketserver.TCPServer(("", port), handler_with_token)
    except OSError as e:
        error_print(f"启动服务器失败，可能端口 {port} 被占用。错误信息: {e}")
        logger.error(f"启动服务器失败: {e}")
        logger.close()
        sys.exit(1)

    def serve():
        try:
            logger.info(f"服务器启动, port={port}, log={server_log_path}")
            httpd.serve_forever()
        except KeyboardInterrupt:
            logger.error("KeyboardInterrupt, shutting down server.")
        except Exception as e:
            logger.error(f"Server异常: {e}")
        finally:
            httpd.server_close()
            logger.info("服务器已关闭，关闭日志文件。")
            logger.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    return thread, server_log_path

def check_login(url: str):
    """
    基于操作系统自动尝试打开登录 URL，如果失败，则提示手动复制。

    参数：
        url (str): 登录 URL
    """
    if sys.platform.startswith("darwin"):
        # macOS
        try:
            subprocess.Popen(["open", url])
            info_print("已尝试通过 'open' 命令在浏览器打开登录链接。")
        except Exception as e:
            error_print(f"自动打开浏览器失败: {e}")
            info_print(f"请手动复制链接到浏览器: {url}")
    elif sys.platform.startswith("linux"):
        # Linux
        try:
            subprocess.Popen(["xdg-open", url])
            info_print("已尝试通过 'xdg-open' 命令在浏览器打开登录链接。")
        except Exception as e:
            error_print(f"自动打开浏览器失败: {e}")
            info_print(f"请手动复制登录链接到浏览器: {url}")
    elif sys.platform.startswith("win") or sys.platform.startswith("cygwin"):
        # Windows
        try:
            # Windows 下使用 'start' 命令，需要 shell=True
            subprocess.Popen(["start", url], shell=True)
            info_print("已尝试通过 'start' 命令在浏览器打开链接。")
        except Exception as e:
            error_print(f"自动打开浏览器失败: {e}")
            info_print(f"请手动复制登录链接到浏览器: {url}")
    else:
        # 识别不到, 手动
        info_print(f"请自行打开登录链接: {url}")

def login_eco():
    """
    华为帐号登录流程：
      1. 启动登录回调处理服务器于后台线程
      2. 打开浏览器进行登录
      3. 等待服务器回调处理完成
      4. 读取 token_file 并更新配置
    """
    info_print("未授权或 Token 过期，开始华为帐号登录流程")
    token_file = tempfile.mktemp(prefix="eco_token_")
    port = 3333

    login_url = (
        "https://cn.devecostudio.huawei.com/console/DevEcoIDE/apply"
        f"?port={port}&appid=1007&code=20698961dd4f420c8b44f49010c6f0cc"
    )
    info_print(f"登录地址: {login_url}")

    # 1. 启动登录回调处理服务器于后台线程
    thread, server_log_path = run_login_callback_handler_server_in_background(
        token_file, port=port
    )

    # 2. 打开浏览器进行登录
    check_login(login_url)
    info_print(f"登录中... 等待获取登录后的 token_file: {token_file}")

    # 3. 等待登录回调处理服务器退出，获取 token_file
    thread.join()

    # 4. 读取 token_file
    if not os.path.exists(token_file):
        error_print("未生成 token_file, 可能登录流程出现异常.")
        error_print(f"请查看日志文件排查: {server_log_path}")
        sys.exit(1)

    try:
        with open(token_file, "r", encoding="utf-8") as f:
            tokens = json.load(f)
    except Exception as e:
        error_print(f"读取 token_file 出错: {e}")
        error_print(f"请查看日志文件: {server_log_path}")
        sys.exit(1)
    finally:
        os.remove(token_file)  # 如果您不想保留 token 文件，可删除

    # 提取 token 并更新配置
    oauth2_token = tokens.get("accessToken", "")
    user_id = tokens.get("userId", "")
    nick_name = tokens.get("nickName", "")

    if not oauth2_token or not user_id:
        error_print("获取 token 失败, 请查看服务器日志。")
        error_print(f"登录日志文件: {server_log_path}")
        sys.exit(1)

    update_config(
        oauth2_token=oauth2_token,
        team_id=user_id,
        uid=user_id,
        nick_name=nick_name
    )
    info_print(f"华为帐号登录成功, 配置已写入 {ECO_CONFIG_FILE}。")
    info_print(f"登录日志记录在: {server_log_path}")

# ============ 证书 / 设备 / Profile 相关函数 ============
def get_cert_list():
    """
    获取证书列表
    """
    url = "https://connect-api.cloud.huawei.com/api/cps/harmony-cert-manage/v1/cert/list"
    return http_request("GET", url)

def create_cert(name: str, cert_type: int, csr: str):
    """
    创建证书，cert_type: 1=debug, 2=prod

    参数：
        name (str): 证书名称
        cert_type (int): 证书类型（1=debug, 2=prod）
        csr (str): CSR 内容

    返回：
        tuple: (response_body, http_code)
    """
    url = "https://connect-api.cloud.huawei.com/api/cps/harmony-cert-manage/v1/cert/add"
    data = {
        "csr": csr,
        "certName": name,
        "certType": cert_type
    }
    return http_request("POST", url, data)

def delete_certs(cert_ids):
    """
    删除证书

    参数：
        cert_ids (list): 需要删除的证书 ID 列表

    返回：
        tuple: (response_body, http_code)
    """
    url = "https://connect-api.cloud.huawei.com/api/cps/harmony-cert-manage/v1/cert/delete"
    data = {
        "certIds": cert_ids
    }
    return http_request("POST", url, data)

def download_cert(cert_object_id: str, file_name: str) -> str:
    """
    下载证书
    先用cert_object_id 通过reapply 接口获取文件下载链接，然后下载

    参数：
        cert_object_id (str): 证书对象 ID
        file_name (str): 下载后存储的文件名

    返回：
        str: 下载后的本地文件路径
    """
    url = "https://connect-api.cloud.huawei.com/api/amis/app-manage/v1/objects/url/reapply"
    data = {
        "sourceUrls": [cert_object_id]
    }
    resp_body, http_code = http_request("POST", url, data)
    if http_code != 200:
        error_print(f"reapply 接口返回码非 200: {http_code}")
        error_print(resp_body)
        sys.exit(1)

    try:
        resp_json = json.loads(resp_body)
        download_url = resp_json["urlsInfo"][0]["newUrl"]
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        error_print(f"解析 reapply 接口响应失败: {e}")
        error_print(f"响应内容: {resp_body}")
        sys.exit(1)

    if not download_url:
        error_print("newUrl 为空")
        sys.exit(1)

    local_cert = download_file(download_url, file_name)
    return local_cert

def create_keystore(keystore_path: str, storepass: str, alias: str, common_name: str):
    """
    创建 keystore (p12)，若已存在则跳过

    参数：
        keystore_path (str): keystore 文件路径
        storepass (str): keystore 密码
        alias (str): key alias
        common_name (str): 证书的 Common Name (CN)
    """
    if os.path.isfile(keystore_path):
        debug_print(f"keystore 已存在: {keystore_path}")
        return

    info_print(f"创建 keystore: {keystore_path}")
    cmd = [
        os.path.join(JAVA_HOME, "bin", "keytool"),
        "-genkeypair",
        "-alias", alias,
        "-keyalg", "EC",
        "-sigalg", "SHA256withECDSA",
        "-dname", f"C=CN,O=HUAWEI,OU=HUAWEI IDE,CN={common_name}",
        "-keystore", keystore_path,
        "-storetype", "pkcs12",
        "-validity", "9125",
        "-storepass", storepass,
        "-keypass", storepass
    ]
    debug_print(" ".join(cmd))
    try:
        subprocess.check_call(cmd)
        info_print(f"keystore 创建成功: {keystore_path}")
    except subprocess.CalledProcessError as e:
        error_print(f"创建 keystore 失败: {e}")
        sys.exit(1)

def create_csr(keystore_path: str, csr_path: str, alias: str, storepass: str):
    """
    创建 CSR 文件

    参数：
        keystore_path (str): keystore 文件路径
        csr_path (str): CSR 文件路径
        alias (str): key alias
        storepass (str): keystore 密码
    """
    if os.path.isfile(csr_path):
        debug_print(f"CSR 已存在: {csr_path}")
        return

    info_print(f"创建 CSR: {csr_path}")
    cmd = [
        os.path.join(JAVA_HOME, "bin", "keytool"),
        "-certreq",
        "-alias", alias,
        "-keystore", keystore_path,
        "-storetype", "pkcs12",
        "-file", csr_path,
        "-storepass", storepass
    ]
    debug_print(" ".join(cmd))
    try:
        subprocess.check_call(cmd)
        info_print(f"CSR 创建成功: {csr_path}")
    except subprocess.CalledProcessError as e:
        error_print(f"创建 CSR 失败: {e}")
        sys.exit(1)

def read_csr(csr_path: str) -> str:
    """
    读取 CSR 文件内容

    参数：
        csr_path (str): CSR 文件路径

    返回：
        str: CSR 内容
    """
    if not os.path.isfile(csr_path):
        error_print(f"CSR 文件不存在: {csr_path}")
        sys.exit(1)
    with open(csr_path, "r", encoding="utf-8") as f:
        return f.read()

def create_and_download_debug_cert(name: str, existing_cert_id: str):
    """
    处理 Debug 证书，若已经存在同名证书则直接下载或跳过，否则创建新证书

    参数：
        name (str): 证书名称
        existing_cert_id (str): 已存在的证书 ID

    返回：
        dict: 证书信息
    """
    cert_file = f"{name}.cer"

    # 获取证书列表
    resp_body, http_code = get_cert_list()
    if http_code == 401:
        info_print("未授权，需要登录 ...")
        login_eco()
        resp_body, http_code = get_cert_list()
        if http_code != 200:
            error_print("登录后获取证书列表失败")
            sys.exit(1)
    elif http_code != 200:
        error_print("get_cert_list 请求失败")
        error_print(resp_body)
        sys.exit(1)

    try:
        resp_json = json.loads(resp_body)
    except json.JSONDecodeError as e:
        error_print(f"证书列表返回非 JSON: {e}")
        error_print(f"响应内容: {resp_body}")
        sys.exit(1)

    # certType == 1 表示 debug， 2 表示 prod
    debug_certs = [
        c for c in resp_json.get("certList", []) if c.get("certType") == 1
    ]

    # 在 debug_certs 中查找同名证书
    same_name_certs = [c for c in debug_certs if c.get("certName") == name]

    if same_name_certs:
        # 已存在同名证书
        c0 = same_name_certs[0]
        cert_id = c0.get("id")
        cert_object_id = c0.get("certObjectId")
        if cert_id and cert_object_id:
            info_print(f"Debug 证书 {name} 已存在，ID: {cert_id}")
            local_cert_path = os.path.join(CONFIG_DIR, f"{name}.cer")
            if os.path.isfile(local_cert_path) and cert_id == existing_cert_id:
                info_print("本地证书文件已存在且 ID 相符，跳过下载。")
                return {
                    "new_cert": False,
                    "id": cert_id,
                    "name": cert_file,
                    "path": local_cert_path
                }

            info_print("开始下载已存在的证书 ...")
            cert_file_path = download_cert(cert_object_id, cert_file)

            # 更新配置再返回
            update_config(
                debug_cert_new_cert="false",
                debug_cert_id=cert_id,
                debug_cert_name=os.path.basename(cert_file_path),
                debug_cert_path=cert_file_path
            )
            return {
                "new_cert": False,
                "id": cert_id,
                "name": os.path.basename(cert_file_path),
                "path": cert_file_path
            }

    #
    #  不存在同名证书，进入创建流程
    #
    info_print("创建新的 Debug 证书 ...")

    # 若已有同名证书，先删除
    same_ids = [
        c.get("id") for c in debug_certs
        if c.get("certName") == name and c.get("id")
    ]
    if same_ids:
        info_print(f"删除同名证书: {same_ids}")
        delete_certs(same_ids)

    # 创建用于存储私钥和获得的证书的keystore（相当于保险柜）
    keystore_path = get_config_value("keystore_file") or os.path.join(STORE_DIR, 'xiaobai.p12')
    storepass = get_config_value("storepass") or DEFAULT_STOREPASS
    alias = get_config_value("keyalias") or DEFAULT_KEYALIAS
    create_keystore(keystore_path, storepass, alias, "xiaobai")

    # 创建CSR (Certificate Signing Request)
    csr_path = get_config_value("csr_file") or os.path.join(STORE_DIR, "xiaobai.csr")
    create_csr(keystore_path, csr_path, alias, storepass)
    csr_content = read_csr(csr_path)

    # 提交 CSR，以请求证书
    info_print(f"向华为提交 CSR，以请求证书...")
    resp_body, http_code = create_cert(name, 1, csr_content)
    if http_code != 200:
        error_print("create_cert 请求失败")
        error_print(resp_body)
        sys.exit(1)

    try:
        resp_json = json.loads(resp_body)
    except json.JSONDecodeError as e:
        error_print(f"create_cert 返回非 JSON: {e}")
        error_print(f"响应内容: {resp_body}")
        sys.exit(1)

    ret_code = resp_json.get("ret", {}).get("code", 0)
    ret_msg = resp_json.get("ret", {}).get("msg", "")
    if ret_code != 0:
        error_print(f"请求证书失败: {ret_msg}")
        sys.exit(1)

    new_cert_object_id = resp_json.get("harmonyCert", {}).get("certObjectId")
    new_cert_id = resp_json.get("harmonyCert", {}).get("id")

    if not new_cert_object_id or not new_cert_id:
        error_print(f"请求证书返回异常: {resp_body}")
        sys.exit(1)

    # 下载证书
    info_print(f"请求证书成功，证书ID：{new_cert_object_id}, 开始下载证书...")
    cert_file_path = download_cert(new_cert_object_id, cert_file)

    # 更新配置再返回
    update_config(
        debug_cert_new_cert="true",
        debug_cert_id=new_cert_id,
        debug_cert_name=os.path.basename(cert_file_path),
        debug_cert_path=cert_file_path
    )
    return {
        "new_cert": True,
        "id": new_cert_id,
        "name": os.path.basename(cert_file_path),
        "path": cert_file_path
    }

def eco_device_list():
    """
    获取已注册设备列表

    返回：
        tuple: (response_body, http_code)
    """
    url = (
        "https://connect-api.cloud.huawei.com/api/cps/device-manage/v1/device/list"
        "?start=1&pageSize=100&encodeFlag=0"
    )
    return http_request("GET", url)

def create_device(device_name: str, uuid: str):
    """
    注册设备

    参数：
        device_name (str): 设备名称
        uuid (str): 设备的 UUID

    返回：
        tuple: (response_body, http_code)
    """
    url = "https://connect-api.cloud.huawei.com/api/cps/device-manage/v1/device/add"
    data = {
        "deviceName": device_name,
        "udid": uuid,
        "deviceType": 4
    }
    return http_request("POST", url, data)

def create_profile(name: str, cert_id: str, package_name: str, device_ids: list):
    """
    创建调试 profile

    参数：
        name (str): profile 名称
        cert_id (str): 证书 ID
        package_name (str): 应用包名
        device_ids (list): 设备 ID 列表

    返回：
        tuple: (response_body, http_code)
    """
    url = "https://connect-api.cloud.huawei.com/api/cps/provision-manage/v1/ide/test/provision/add"
    data = {
        "provisionName": name,
        "deviceList": device_ids,
        "certList": [cert_id],
        "packageName": package_name
    }
    return http_request("POST", url, data)

def create_and_download_debug_profile(profile_name: str, package_name: str):
    """
    创建并下载调试 profile

    参数：
        profile_name (str): profile 名称
        package_name (str): 应用包名

    返回：
        dict: profile 信息
    """
    profile_filename = f"{profile_name}_{package_name.replace('.', '_')}.p7b"
    profile_path = os.path.join(CONFIG_DIR, profile_filename)

    if os.path.isfile(profile_path):
        info_print(f"调试 Profile 已存在: {profile_filename}")
        # 更新配置再返回
        update_config(
            debug_profile_name=profile_filename,
            debug_profile_path=profile_path
        )
        return {"name": profile_filename, "path": profile_path}

    info_print("准备创建 Debug Profile ...")

    # 确保已连接设备
    device_key = DEVICE_IP.strip()
    if not device_key:
        error_print("没有指定 DEVICE_IP，请先连接或在命令行指定 -d")
        sys.exit(1)

    connect_device(device_key)
    udid = get_udid(device_key)
    if not udid:
        error_print("获取设备 UDID 失败。")
        sys.exit(1)
    info_print(f"设备 UDID: {udid}")

    # 查询设备列表
    resp_body, http_code = eco_device_list()
    if http_code == 401:
        info_print("未授权，需要登录 ...")
        login_eco()
        resp_body, http_code = eco_device_list()
        if http_code != 200:
            error_print("登录后获取设备列表失败")
            sys.exit(1)
    if http_code != 200:
        error_print(f"获取设备列表失败: {resp_body}")
        sys.exit(1)

    try:
        resp_json = json.loads(resp_body)
    except json.JSONDecodeError as e:
        error_print(f"设备列表响应非 JSON: {e}")
        error_print(f"响应内容: {resp_body}")
        sys.exit(1)

    devices = resp_json.get("list", [])
    found = None
    for device in devices:
        if device.get("udid") == udid:
            found = device
            break

    if not found:
        info_print("当前设备未注册，正在注册...")
        create_device(f"xiaobai-device-{udid[:10]}", udid)
        # 再次获取
        resp_body, http_code = eco_device_list()
        if http_code != 200:
            error_print("注册后获取设备列表失败")
            sys.exit(1)
        try:
            resp_json = json.loads(resp_body)
        except json.JSONDecodeError as e:
            error_print(f"设备列表响应非 JSON: {e}")
            error_print(f"响应内容: {resp_body}")
            sys.exit(1)
        devices = resp_json.get("list", [])
        for device in devices:
            if device.get("udid") == udid:
                found = device
                break
        if not found:
            error_print("注册失败，未在列表找到")
            sys.exit(1)
        info_print("设备注册成功。")

    dev_id = found.get("id")
    if not dev_id:
        error_print(f"设备 ID 不存在: {found}")
        sys.exit(1)

    cert_id = get_config_value("debug_cert_id")
    if not cert_id:
        error_print("未找到 debug_cert_id，请先创建 Debug 证书")
        sys.exit(1)

    # 创建 Debug Profile
    resp_body, http_code = create_profile(
        profile_name, cert_id, package_name, [dev_id]
    )
    if http_code != 200:
        error_print(f"create_profile 请求失败: {resp_body}")
        sys.exit(1)

    try:
        resp_json = json.loads(resp_body)
    except json.JSONDecodeError as e:
        error_print(f"create_profile 返回非 JSON: {e}")
        error_print(f"响应内容: {resp_body}")
        sys.exit(1)

    ret_code = resp_json.get("ret", {}).get("code", 0)
    ret_msg = resp_json.get("ret", {}).get("msg", "")
    if ret_code != 0:
        error_print(f"创建 profile 失败: {ret_msg}")
        sys.exit(1)

    profile_url = resp_json.get("provisionFileUrl")
    if not profile_url:
        error_print(f"provisionFileUrl 为空: {resp_body}")
        sys.exit(1)

    downloaded_profile_path = download_file(profile_url, profile_filename)
    info_print(f"下载的 Debug Profile：{downloaded_profile_path}")

    # 更新配置再返回
    update_config(
        debug_profile_name=profile_filename,
        debug_profile_path=downloaded_profile_path
    )
    return {"name": profile_filename, "path": downloaded_profile_path}

# ============ 设备连接相关 ============
def connect_device(device_ip: str):
    """
    使用 hdc tconn 命令连接设备

    参数：
        device_ip (str): 设备 IP 地址
    """
    global HDC_COMMAND
    assert HDC_COMMAND is not None, "HDC_COMMAND为空"

    debug_print(f"连接设备: {device_ip}")
    cmd = [HDC_COMMAND, "tconn", device_ip]
    try:
        output = subprocess.check_output(
            cmd, stderr=subprocess.STDOUT, encoding="utf-8", timeout=10
        )
        debug_print(output)
        if "Connect failed" in output:
            error_print(f"连接设备失败: {device_ip}")
            sys.exit(1)
        info_print(f"成功连接设备: {device_ip}")
    except subprocess.CalledProcessError as e:
        error_print(f"连接设备失败: {e.output}")
        sys.exit(1)
    except subprocess.TimeoutExpired:
        error_print(f"连接设备命令超时: {device_ip}")
        sys.exit(1)

def get_udid(device_ip: str) -> str:
    """
    获取设备的 UDID

    参数：
        device_ip (str): 设备 IP 地址

    返回：
        str: 设备的 UDID
    """
    cmd = [HDC_COMMAND, "-t", device_ip, "shell", "bm", "get", "--udid"]
    try:
        output = subprocess.check_output(
            cmd, stderr=subprocess.STDOUT, encoding="utf-8", timeout=10
        )
        debug_print(output)
        lines = output.strip().split("\n")
        if len(lines) >= 2:
            udid = lines[1].strip()
            return udid
        else:
            error_print(f"获取 UDID 失败，输出内容不足: {output}")
            sys.exit(1)
    except subprocess.CalledProcessError as e:
        error_print(f"获取 UDID 失败: {e.output}")
        sys.exit(1)
    except subprocess.TimeoutExpired:
        error_print(f"获取 UDID 命令超时: {device_ip}")
        sys.exit(1)

# ============ 签名与安装 ============
def sign_hap(input_hap: str, output_hap: str):
    """
    使用 hap-sign-tool.jar 进行签名

    参数：
        input_hap (str): 输入的 HAP 文件路径
        output_hap (str): 输出的已签名 HAP 文件路径
    """
    separator_print("对 HAP 文件进行签名")

    if not os.path.isfile(input_hap):
        error_print(f"待签名文件不存在: {input_hap}")
        sys.exit(1)

    keystore_file = get_config_value("keystore_file")
    storepass = get_config_value("storepass")
    keyalias = get_config_value("keyalias")
    cert_file = get_config_value("debug_cert_path")
    profile_file = get_config_value("debug_profile_path")

    # 检查必要文件
    if not (keystore_file and os.path.isfile(keystore_file)):
        error_print(f"Keystore 文件无效: {keystore_file}")
        sys.exit(1)
    if not (cert_file and os.path.isfile(cert_file)):
        error_print(f"证书文件无效: {cert_file}")
        sys.exit(1)
    if not (profile_file and os.path.isfile(profile_file)):
        error_print(f"Profile 文件无效: {profile_file}")
        sys.exit(1)
    if not os.path.isfile(SIGN_JAR):
        error_print(f"签名 jar 不存在: {SIGN_JAR}")
        sys.exit(1)

    info_print(f"开始签名 {input_hap} -> {output_hap}")

    # 构造命令行
    cmd = [
        os.path.join(JAVA_HOME, "bin", "java"),
        "-jar", SIGN_JAR, "sign-app",
        "-mode", "localSign",
        "-keyAlias", keyalias,
        "-appCertFile", cert_file,
        "-profileFile", profile_file,
        "-inFile", input_hap,
        "-signAlg", "SHA256withECDSA",
        "-keystoreFile", keystore_file,
        "-keystorePwd", storepass,
        "-keyPwd", storepass,
        "-outFile", output_hap,
        "-signCode", "1"
    ]

    debug_print(" ".join(cmd))

    try:
        output = subprocess.check_output(
            cmd, stderr=subprocess.STDOUT, encoding="utf-8", timeout=30
        )
        debug_print(f"签名输出: {output}")
        info_print("签名成功！")
    except subprocess.CalledProcessError as e:
        error_print(f"签名失败: {e.output}")
        sys.exit(1)
    except subprocess.TimeoutExpired:
        error_print("签名命令超时")
        sys.exit(1)

def send_and_install(hap_file: str):
    """
    发送 .hap 到设备并安装

    参数：
        hap_file (str): 待安装的 HAP 文件路径
    """
    separator_print("发送并安装 HAP")

    if not os.path.isfile(hap_file):
        error_print(f"待安装文件不存在: {hap_file}")
        sys.exit(1)

    connect_device(DEVICE_IP)
    udid = get_udid(DEVICE_IP)
    info_print(f"准备推送到设备 UDID: {udid}")

    # 创建目录
    try:
        subprocess.run(
            [HDC_COMMAND, "-t", DEVICE_IP, "shell", "mkdir", "-p", "data/local/tmp/hap"],
            check=True
        )
        info_print("设备上目录已创建或已存在: data/local/tmp/hap")
    except subprocess.CalledProcessError as e:
        error_print(f"在设备上创建目录失败: {e}")
        sys.exit(1)

    # 发送
    cmd_send = [
        HDC_COMMAND,
        "-t",
        DEVICE_IP,
        "file",
        "send",
        hap_file,
        "data/local/tmp/hap/"
    ]
    debug_print(" ".join(cmd_send))
    try:
        output = subprocess.check_output(
            cmd_send, stderr=subprocess.STDOUT, encoding="utf-8", timeout=30
        )
        debug_print(output)
        if "finish" not in output.lower():
            error_print(f"发送失败: {output}")
            sys.exit(1)
        info_print("HAP 文件发送成功。")
    except subprocess.CalledProcessError as e:
        error_print(f"发送 hap 文件失败: {e.output}")
        sys.exit(1)
    except subprocess.TimeoutExpired:
        error_print("发送 hap 文件命令超时")
        sys.exit(1)

    info_print("发送成功，开始安装 ...")
    cmd_install = [
        HDC_COMMAND,
        "-t",
        DEVICE_IP,
        "shell",
        "bm",
        "install",
        "-p",
        f"data/local/tmp/hap/{os.path.basename(hap_file)}"
    ]
    debug_print(" ".join(cmd_install))
    try:
        output = subprocess.check_output(
            cmd_install, stderr=subprocess.STDOUT, encoding="utf-8", timeout=60
        )
        debug_print(output)
        if "successfully" in output.lower():
            info_print("安装成功！")
        else:
            error_print(f"安装失败: {output}")
            sys.exit(1)
    except subprocess.CalledProcessError as e:
        error_print(f"安装指令执行失败: {e.output}")
        sys.exit(1)
    except subprocess.TimeoutExpired:
        error_print("安装命令超时")
        sys.exit(1)

# ============ 检查或登录华为账号 ============
def check_or_login_huawei_eco():
    """
    步骤1: 检查华为账号登录情况，如未登录则调用 login_eco()
    """
    separator_print("检查华为账号登录")
    url = (
        "https://connect-api.cloud.huawei.com/api/ups/user-permission-service/v1/user-team-list"
    )
    resp_body, http_code = http_request("GET", url)
    if http_code == 401:
        info_print("未授权，需要登录 ...")
        login_eco()
        resp_body, http_code = http_request("GET", url)
        if http_code != 200:
            error_print("登录后仍无法获取用户团队列表。")
            sys.exit(1)
    elif http_code != 200:
        error_print(f"获取用户团队列表失败: {resp_body}")
        sys.exit(1)

    try:
        resp_json = json.loads(resp_body)
    except json.JSONDecodeError as e:
        error_print(f"返回非 JSON: {e}")
        error_print(f"响应内容: {resp_body}")
        sys.exit(1)

    # 简单取其中一个团队名称或昵称
    teams = resp_json.get("teams", [])
    if not teams:
        info_print("未获取到团队信息，可能尚未登录？尝试再次登录 ...")
        login_eco()
        resp_body, http_code = http_request("GET", url)
        if http_code != 200:
            error_print("登录后仍无法获取用户团队列表。")
            sys.exit(1)
        try:
            resp_json = json.loads(resp_body)
        except json.JSONDecodeError as e:
            error_print(f"返回非 JSON: {e}")
            error_print(f"响应内容: {resp_body}")
            sys.exit(1)
        teams = resp_json.get("teams", [])
        if not teams:
            error_print("未获取到团队信息，即使登录后。")
            sys.exit(1)

    user_team_name = teams[0].get("name") or resp_json.get("nickName", "")
    info_print(f"已登录账号: {user_team_name}")

# ============ 处理输入的 HAP ============
def prepare_hap(input_hap: str):
    """
    处理输入的 HAP，为后续的签名作准备

    参数：
        input_hap (str): 输入的未签名 HAP 文件路径

    返回：
        tuple: (bundle_name, new_hap_path)
    """
    separator_print("处理输入的 HAP，为后续的签名作准备")
    bundle_name, new_hap_path = update_get_hap_info(input_hap)
    if not bundle_name or not new_hap_path:
        error_print("处理 HAP 文件失败，退出。")
        sys.exit(1)

    info_print(f"获得新的 HAP: {new_hap_path}，包名: {bundle_name}")
    return bundle_name, new_hap_path

# ============ 处理 Debug 证书 ============
def process_debug_cert():
    """
    步骤3: 创建/下载 Debug 证书
    """
    separator_print("步骤3: 创建/下载 Debug 证书")
    existing_cert_id = get_config_value("debug_cert_id")
    result = create_and_download_debug_cert(CERT_NAME, existing_cert_id)
    info_print(f"证书路径: {result['path']}")

# ============ 处理 Debug Profile ============
def process_debug_profile(bundle_name: str):
    """
    步骤4: 创建/下载 Debug Profile

    参数：
        bundle_name (str): 应用的 bundle 名称
    """
    separator_print("步骤4: 创建/下载 Debug Profile")
    result = create_and_download_debug_profile(PROFILE_NAME, bundle_name)
    info_print(f"Profile 路径: {result['path']}")

# ============ 主函数 ============
def main():
    """
    主入口函数，负责解析命令行参数并执行各步骤
    """
    # 命令行解析
    parser = argparse.ArgumentParser(
        description="自动打包并安装 .hap 到设备的脚本"
    )
    parser.add_argument(
        "-i",
        "--input_hap",
        required=True,
        help="输入的未签名 HAP 文件路径"
    )
    parser.add_argument(
        "-o",
        "--output_hap",
        default="",
        help="输出的已签名 HAP 文件路径，默认为 ${INPUT_HAP}_signed.hap"
    )
    parser.add_argument(
        "-d",
        "--device_ip",
        required=True,
        help="要安装的目标设备，格式： 'IP:Port'",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="是否开启 debug 模式"
    )
    args = parser.parse_args()

    global DEBUG
    DEBUG = args.debug

    # 设置全局 DEVICE_IP (格式：ip:port)
    if args.device_ip.strip():
        global DEVICE_IP
        DEVICE_IP = args.device_ip.strip()

    input_hap = args.input_hap
    if not args.output_hap:
        output_hap = f"{os.path.splitext(input_hap)[0]}_signed.hap"
    else:
        output_hap = args.output_hap

    # 0. 前置要求检查
    check_prerequisite()

    # 1. 检查/登录华为账号
    check_or_login_huawei_eco()

    # 2. 处理输入的 HAP，为后续的签名作准备
    bundle_name, input_hap = prepare_hap(input_hap)

    # 3. 创建/下载 debug 证书
    process_debug_cert()

    # 4. 创建/下载 debug profile
    process_debug_profile(bundle_name)

    # 5. 签名
    sign_hap(input_hap, output_hap)

    # 6. 发送至设备并安装
    # send_and_install(output_hap)

    separator_print("全部操作完成")

if __name__ == "__main__":
    main()