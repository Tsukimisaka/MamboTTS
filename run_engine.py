import os
import sys
import subprocess

def main():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    # GPT-SoVITS 所在的子目录
    gsv_dir = os.path.join(current_dir, "GPT-SoVITS")
    
    # 使用 GPT-SoVITS 内部自带的 runtime python 启动 API，保证依赖正确
    python_exe = os.path.join(gsv_dir, "runtime", "python.exe")
    api_py = os.path.join(gsv_dir, "api.py")
    
    # 您的模型权重路径（使用绝对路径，防止路径识别错误）
    sovits_path = os.path.join(gsv_dir, "SoVITS_weights_v2Pro", "mambo_e8_s352.pth")
    gpt_path = os.path.join(gsv_dir, "GPT_weights_v2Pro", "mambo-e15.ckpt")
    
    # 选取的默认参考音频（包含中文和空格，Python 能够完美解析，不受 CMD 编码影响）
    ref_wav = os.path.join(gsv_dir, "output", "slicer_opt", "vocal_#煮包日常 #煮包 #曼波 #曼波配音讲故事 P1 #煮包日常 #煮包 #曼波 #曼波配音讲故事.mp3_10.wav_0000000000_0000129280.wav")
    ref_text = "最近看大家都在讲自己的经历，球波也是忍不住了。"
    
    # 构建启动命令
    cmd = [
        python_exe,
        api_py,
        "-a", "127.0.0.1",
        "-p", "9880",
        "-s", sovits_path,
        "-g", gpt_path,
        "-dr", ref_wav,
        "-dt", ref_text,
        "-dl", "zh"
    ]
    
    print("====================================================")
    print("         MamboTTS 本地 GPU 语音推理引擎启动程序")
    print("====================================================")
    print(f"[状态] 正在加载 SoVITS 模型: {os.path.basename(sovits_path)}")
    print(f"[状态] 正在加载 GPT 模型: {os.path.basename(gpt_path)}")
    print(f"[提示] 推理服务即将启动在: http://127.0.0.1:9880")
    print("====================================================\n")
    
    # 在 GPT-SoVITS 目录下运行 API，保证工作目录正确
    try:
        p = subprocess.Popen(cmd, cwd=gsv_dir)
        p.wait()
    except KeyboardInterrupt:
        print("\n[状态] 推理服务已关闭。")
    except Exception as e:
        print(f"\n[错误] 启动推理引擎失败: {str(e)}")

if __name__ == "__main__":
    main()
