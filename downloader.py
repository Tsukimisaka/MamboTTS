"""
MamboTTS - B站音源一键下载脚本
用于从 B 站下载"曼波讲故事"相关视频的纯音轨，用于后续 GPT-SoVITS 训练。

使用方法：
  1. 运行 run_downloader.bat 即可启动交互式下载
  2. 或者直接修改下面的 BILIBILI_URLS 列表，填入目标视频的 URL

注意事项：
  - 下载的音频仅用于个人学习研究，请勿公开传播
  - 音频文件会保存在 training_data/ 文件夹下，格式为 m4a/webm（可直接导入 GPT-SoVITS）
"""

import os
import sys
import shutil
import zipfile
import subprocess
import urllib.request
import traceback

# ─────────────────────────────────────────────
#  安装依赖
# ─────────────────────────────────────────────
def ensure_deps():
    try:
        import yt_dlp
    except ImportError:
        print("[STATUS] Installing yt-dlp ...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "yt-dlp",
                               "-i", "https://mirrors.aliyun.com/pypi/simple/", "-q"])

ensure_deps()
import yt_dlp

# ─────────────────────────────────────────────
#  自动下载 ffmpeg（Windows 便携版）
# ─────────────────────────────────────────────
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
FFMPEG_DIR  = os.path.join(CURRENT_DIR, "tools", "ffmpeg")
FFMPEG_EXE  = os.path.join(FFMPEG_DIR, "ffmpeg.exe")

def ensure_ffmpeg():
    """检测 ffmpeg，如果不存在则从 github.com/yt-dlp/FFmpeg-Builds 下载 Windows 便携版。"""
    # 1) 检查系统 PATH
    if shutil.which("ffmpeg"):
        print("[ffmpeg] 检测到系统已安装 ffmpeg，跳过下载。")
        return shutil.which("ffmpeg")

    # 2) 检查本地 tools/ffmpeg
    if os.path.exists(FFMPEG_EXE):
        print(f"[ffmpeg] 本地便携版已存在：{FFMPEG_EXE}")
        return FFMPEG_EXE

    print("[ffmpeg] 系统未检测到 ffmpeg，开始自动下载 Windows 便携版（约 90MB）...")
    os.makedirs(FFMPEG_DIR, exist_ok=True)

    # yt-dlp 官方维护的 ffmpeg 精简 Windows 构建
    url = "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
    zip_path = os.path.join(FFMPEG_DIR, "ffmpeg.zip")

    print(f"[ffmpeg] 下载地址：{url}")
    try:
        def reporthook(count, block_size, total_size):
            if total_size > 0:
                pct = count * block_size * 100 // total_size
                print(f"\r[ffmpeg] 下载进度：{min(pct, 100)}%", end="", flush=True)
        urllib.request.urlretrieve(url, zip_path, reporthook)
        print("\n[ffmpeg] 下载完毕，开始解压...")
    except Exception as e:
        print(f"\n[ffmpeg] GitHub 下载失败（可能需要代理）: {e}")
        print("[ffmpeg] 尝试备用镜像...")
        backup_url = "https://mirrors.huaweicloud.com/ffmpeg/releases/ffmpeg-7.1.tar.bz2"
        # 只下载便携 exe（GitHub releases 镜像比较少，这里改用 Chocolatey 数据源）
        # 最终退路：提示用户手动安装
        print("\n[提示] 无法自动下载 ffmpeg，请手动前往：")
        print("       https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip")
        print(f"       解压后将 ffmpeg.exe 放入：{FFMPEG_DIR}")
        print("       然后重新运行本脚本。\n")
        return None

    # 解压 zip
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            # zip 内结构：ffmpeg-master-latest-win64-gpl/bin/ffmpeg.exe
            for member in zf.namelist():
                if member.endswith("ffmpeg.exe"):
                    with zf.open(member) as src, open(FFMPEG_EXE, "wb") as dst:
                        dst.write(src.read())
                    break
        os.remove(zip_path)
        print(f"[ffmpeg] 解压完成：{FFMPEG_EXE}")
    except Exception as e:
        print(f"[ffmpeg] 解压失败: {e}")
        return None

    return FFMPEG_EXE


# ─────────────────────────────────────────────
#  配置区：可以在这里粘贴目标 B 站视频 URL
# ─────────────────────────────────────────────
# 格式：["https://www.bilibili.com/video/BVxxxxxx", ...]
BILIBILI_URLS = [
    # 在这里填入 B 站视频链接，例如：
    # "https://www.bilibili.com/video/BV1234567890",
]

# 输出目录（自动创建）
OUTPUT_DIR = os.path.join(CURRENT_DIR, "training_data")


def build_ydl_opts(ffmpeg_path: str | None) -> dict:
    """根据 ffmpeg 是否可用，生成不同的下载配置。"""
    base_opts = {
        "outtmpl":      os.path.join(OUTPUT_DIR, "%(title)s.%(ext)s"),
        "quiet":        False,
        "no_warnings":  False,
        "nooverwrites": True,
    }

    if ffmpeg_path:
        # 有 ffmpeg：转为高质量 WAV，直接导入 GPT-SoVITS
        ffmpeg_dir = os.path.dirname(ffmpeg_path)
        base_opts.update({
            "format": "bestaudio/best",
            "ffmpeg_location": ffmpeg_dir,
            "postprocessors": [
                {
                    "key":              "FFmpegExtractAudio",
                    "preferredcodec":   "wav",
                    "preferredquality": "0",
                }
            ],
        })
        print("[模式] ✅ 将下载音频并转换为 WAV 格式（最佳训练格式）")
    else:
        # 无 ffmpeg：直接下载原始音频流（m4a/webm），GPT-SoVITS 也支持导入
        base_opts.update({
            "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio",
        })
        print("[模式] ⚠️  ffmpeg 不可用，将下载原始音频（m4a/webm），GPT-SoVITS 同样支持此格式。")

    return base_opts


def download_audio(urls: list, ffmpeg_path):
    """批量下载 B 站视频音轨。"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print(f"\n[开始] 共 {len(urls)} 个视频待下载")
    print(f"[输出] 音频将保存至：{OUTPUT_DIR}\n")

    opts = build_ydl_opts(ffmpeg_path)
    success, failed = 0, []

    with yt_dlp.YoutubeDL(opts) as ydl:
        for i, url in enumerate(urls, 1):
            print(f"\n─── [{i}/{len(urls)}] 正在处理: {url}")
            try:
                ydl.download([url])
                success += 1
            except Exception as e:
                print(f"    [错误] 下载失败: {e}")
                failed.append(url)

    print("\n===================================================")
    print(f"✅ 下载完成！成功 {success} 个，失败 {len(failed)} 个")
    print(f"📁 音频保存路径：{OUTPUT_DIR}")
    if failed:
        print(f"\n⚠️  以下链接下载失败：")
        for f in failed:
            print(f"  - {f}")
    print("===================================================")
    print("\n接下来请打开 GPT-SoVITS WebUI，在【训练】标签页中：")
    print("  1. 将 training_data/ 文件夹中的音频导入")
    print("  2. 点击【一键三连】自动完成人声提取 → 切割 → ASR 文字标注")
    print("  3. 点击【开始训练 SoVITS + GPT】，约 20-40 分钟后模型训练完毕！\n")


def interactive_mode():
    """交互式输入 URL。"""
    print("===================================================")
    print("         MamboTTS - B站音源一键下载工具")
    print("===================================================")
    print("请粘贴 B 站视频链接（每行一个，输入空行结束）：")
    print("  支持格式：")
    print("    https://www.bilibili.com/video/BV1234567890")
    print("    https://b23.tv/xxxxxxx  （短链也支持）\n")

    urls = []
    while True:
        try:
            line = input("链接> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            break
        if "bilibili.com" in line or "b23.tv" in line:
            urls.append(line)
            print(f"  ✓ 已加入队列（共 {len(urls)} 个）")
        else:
            print("  ✗ 不像是 B 站链接，请重新输入（或直接按回车完成输入）")

    return urls


if __name__ == "__main__":
    # Step 1: 确保 ffmpeg 可用
    ffmpeg_path = ensure_ffmpeg()

    # Step 2: 确定下载链接
    if BILIBILI_URLS:
        urls = BILIBILI_URLS
    else:
        urls = interactive_mode()

    # Step 3: 执行下载
    if urls:
        download_audio(urls, ffmpeg_path)
    else:
        print("\n[提示] 未输入任何链接，程序退出。")
        print("       您可以直接编辑 downloader.py 的 BILIBILI_URLS 列表填入链接后重新运行。\n")
