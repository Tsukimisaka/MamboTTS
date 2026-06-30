import os
import shutil
import glob

def main():
    # 获取当前用户的系统下载目录
    user_profile = os.environ.get("USERPROFILE", "C:\\Users\\admin")
    downloads_dir = os.path.join(user_profile, "Downloads")
    
    current_dir = os.path.dirname(os.path.abspath(__file__))
    gpt_weights_dir = os.path.join(current_dir, "GPT-SoVITS", "GPT_weights_v2")
    sovits_weights_dir = os.path.join(current_dir, "GPT-SoVITS", "SoVITS_weights_v2")

    # 创建目标目录以防不存在
    os.makedirs(gpt_weights_dir, exist_ok=True)
    os.makedirs(sovits_weights_dir, exist_ok=True)

    print("===================================================")
    print("             MamboTTS 音色模型自动导入程序")
    print("===================================================")
    print(f"[状态] 正在扫描您的系统下载夹: {downloads_dir} ...")

    # 模糊搜索包含 "mambo" 或 "曼波" 的文件
    pth_files = []
    ckpt_files = []

    # 扫描下载目录下的文件
    for file_name in os.listdir(downloads_dir):
        lower_name = file_name.lower()
        full_path = os.path.join(downloads_dir, file_name)
        if not os.path.isfile(full_path):
            continue
            
        # 如果文件名包含 "mambo"、"mabo" 或 "曼波"
        if any(keyword in lower_name for keyword in ["mambo", "mabo", "曼波"]):
            if file_name.endswith(".pth"):
                pth_files.append(full_path)
            elif file_name.endswith(".ckpt"):
                ckpt_files.append(full_path)

    # 打印扫描结果
    if not pth_files and not ckpt_files:
        print("[提示] 未在您的系统“下载”文件夹中找到包含“曼波”或“mambo”命名的模型文件。")
        print("\n💡 建议步骤：")
        print("1. 请先在网盘或浏览器下载“曼波音色包”。")
        print("2. 确保下载的文件在您的【下载】文件夹中，且文件名包含“曼波”或“mambo”字样。")
        print("3. 重新运行本导入程序。")
        print("===================================================\n")
        return

    print(f"[发现] 识别到待导入的音频参数 (.pth) 文件: {[os.path.basename(f) for f in pth_files]}")
    print(f"[发现] 识别到待导入的文本参数 (.ckpt) 文件: {[os.path.basename(f) for f in ckpt_files]}")
    
    # 执行移动导入操作
    imported_pth = 0
    imported_ckpt = 0

    for pth in pth_files:
        dest = os.path.join(gpt_weights_dir, os.path.basename(pth))
        try:
            shutil.move(pth, dest)
            print(f"[成功] 已将 {os.path.basename(pth)} 移动至 GPT_weights_v2 目录")
            imported_pth += 1
        except Exception as e:
            print(f"[错误] 移动 {os.path.basename(pth)} 失败: {str(e)}")

    for ckpt in ckpt_files:
        dest = os.path.join(sovits_weights_dir, os.path.basename(ckpt))
        try:
            shutil.move(ckpt, dest)
            print(f"[成功] 已将 {os.path.basename(ckpt)} 移动至 SoVITS_weights_v2 目录")
            imported_ckpt += 1
        except Exception as e:
            print(f"[错误] 移动 {os.path.basename(ckpt)} 失败: {str(e)}")

    print("\n===================================================")
    print(f"🎉 模型导入完成！成功导入 {imported_pth} 个 .pth 文件，{imported_ckpt} 个 .ckpt 文件。")
    print("===================================================")
    print("您现在可以直接双击运行项目目录下的 [run_local_engine.bat] 启动语音引擎服务了！")
    print("===================================================\n")

if __name__ == "__main__":
    main()
