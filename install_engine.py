import os
import sys
import shutil
import subprocess
import requests

def install_and_import(package):
    try:
        __import__(package)
    except ImportError:
        print(f"[STATUS] Installing {package}...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", package, "-i", "https://mirrors.aliyun.com/pypi/simple/"])

# 自动确保 modelscope 已经安装
install_and_import('modelscope')

from modelscope import snapshot_download

def download_7z_tool(target_path):
    url = "https://www.7-zip.org/a/7zr.exe"
    print("[状态] 正在获取 7z 解压工具 (约 500KB)...")
    try:
        r = requests.get(url, stream=True, timeout=15)
        r.raise_for_status()
        with open(target_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        print("[状态] 解压工具获取成功！")
    except Exception as e:
        print(f"[错误] 无法从官方下载 7z 解压工具: {str(e)}")
        raise

def main():
    repo_id = 'FlowerCry/gpt-sovits-7z-pacakges'
    # 针对 5070 显卡，下载 50 系列专用的稳定 V2pro 整合包
    target_file = 'GPT-SoVITS-v2pro-20250604-nvidia50.7z'
    
    current_dir = os.path.dirname(os.path.abspath(__file__))
    target_dir = os.path.join(current_dir, "engine_temp")
    extract_dir = os.path.join(current_dir, "GPT-SoVITS")
    tool_path = os.path.join(current_dir, "7zr.exe")

    print("===================================================")
    # 打印中文信息以方便用户看，因为这是个独立脚本，用户在终端运行
    print("               MamboTTS 引擎一键安装程序")
    print("===================================================")
    print(f"[状态] 开始通过魔搭社区极速通道下载 GPT-SoVITS 50系列专用整合包...")
    print(f"[提示] 文件较大 (约 3-4GB)，由于是国内镜像，速度通常非常快，请耐心等待...")

    try:
        # 只下载需要的 7z 整合包文件，避免下载整库
        download_path = snapshot_download(
            repo_id, 
            allow_file_pattern=target_file,
            cache_dir=target_dir
        )
        
        # 寻找到下载 of 7z 文件路径
        src_7z = None
        for root, dirs, files in os.walk(download_path):
            if target_file in files:
                src_7z = os.path.join(root, target_file)
                break

        if not src_7z or not os.path.exists(src_7z):
            print("[错误] 未找到下载的整合包文件，请重试！")
            return

        # 确保下载 7zr.exe 工具
        if not os.path.exists(tool_path):
            download_7z_tool(tool_path)

        print(f"\n[状态] 下载完成！正在使用 7-Zip 解压整合包到: {extract_dir} ...")
        print("[提示] 解压大文件约需要 1-2 分钟，请不要关闭窗口...")

        # 使用 7zr.exe 命令行进行解压，绕过 python 库不支持 BCJ2 算法的问题
        # x: 解压并保持目录结构, -y: 自动覆盖
        subprocess.check_call([tool_path, 'x', src_7z, f'-o{extract_dir}', '-y'])

        print("[状态] 解压完成！正在清理临时文件...")
        # 清理临时下载文件夹和工具
        if os.path.exists(target_dir):
            shutil.rmtree(target_dir)
        if os.path.exists(tool_path):
            os.remove(tool_path)

        # 检查解压后的目录结构是否有多嵌套一层文件夹
        # 整合包解压后里面通常有一个 GPT-SoVITS-v2pro-... 文件夹，我们把它移动到根部的 GPT-SoVITS 文件夹下
        subdirs = [d for d in os.listdir(extract_dir) if os.path.isdir(os.path.join(extract_dir, d))]
        if len(subdirs) == 1:
            nested_dir = os.path.join(extract_dir, subdirs[0])
            print(f"[状态] 优化文件目录结构...")
            # 把嵌套目录里的所有文件往上移动一层
            for item in os.listdir(nested_dir):
                shutil.move(os.path.join(nested_dir, item), extract_dir)
            os.rmdir(nested_dir)

        print("[状态] 正在为您生成本地引擎启动脚本...")
        # 生成一个方便的运行脚本 go-api.bat
        api_bat_content = (
            "@echo off\n"
            "title GPT-SoVITS Local API Server\n"
            "cd /d \"%~dp0\"\n"
            "echo Starting GPT-SoVITS API on http://127.0.0.1:9880 ...\n"
            "runtime\\python.exe api.py -a 127.0.0.1 -p 9880\n"
            "pause\n"
        )
        with open(os.path.join(extract_dir, "go-api-mambo.bat"), "w", encoding="utf-8") as f:
            f.write(api_bat_content)

        print("\n===================================================")
        print("🎉 恭喜！GPT-SoVITS 本地 GPU 推理引擎一键安装成功！")
        print("===================================================")
        print(f"安装路径: {extract_dir}")
        print("\n因为您下载的是完整整合包，音色模型已默认配置完毕，无需手动放置！")
        print("\n接下来您只需要两步：")
        print("1. 双击项目主目录下的 [run_local_engine.bat] 启动本地 GPU 语音引擎服务。")
        print("2. 双击项目主目录下的 [run.bat] 启动 MamboTTS 配音客户端，即可开始超速配音！")
        print("===================================================\n")

    except Exception as e:
        print(f"\n[错误] 安装过程中发生异常: {str(e)}")

if __name__ == "__main__":
    main()
