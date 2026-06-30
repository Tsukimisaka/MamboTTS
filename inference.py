import os
import requests
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class TTSClient:
    def __init__(self, api_url="http://127.0.0.1:9880"):
        self.api_url = api_url

    def is_api_running(self):
        """检查本地 GPT-SoVITS API 服务是否在线"""
        try:
            # 尝试发送一个轻量级请求检查连接，请求 /control 接口以避免无参数的 500 错误
            response = requests.get(f"{self.api_url}/control", timeout=2)
            # 如果是 200 或者 404/405 均代表在线
            return response.status_code in [200, 404, 405]
        except requests.RequestException:
            return False

    def generate_speech(self, text, save_path, ref_audio="", prompt_text="", text_lang="zh", prompt_lang="zh", speed=1.0):
        """
        调用 GPT-SoVITS 本地 API 生成语音。
        参数：
            text: 需要合成的文本
            save_path: 导出的音频保存路径 (.wav)
            ref_audio: 参考音频文件路径 (留空则使用 API 默认配置)
            prompt_text: 参考音频对应的文本
            speed: 配音语速 (0.1 - 3.0)
        """
        if not self.is_api_running():
            logging.warning("GPT-SoVITS API 处于离线状态，进入 Mock（模拟生成）模式。")
            return self._mock_generate(text, save_path)

        # 标准 GPT-SoVITS API 参数格式
        params = {
            "text": text,
            "text_language": text_lang,
            "speed": speed,
        }
        
        # 如果提供了参考音频，则加入参数
        if ref_audio and prompt_text:
            params["ref_audio_path"] = ref_audio
            params["prompt_text"] = prompt_text
            params["prompt_language"] = prompt_lang

        try:
            logging.info(f"正在请求 GPT-SoVITS API 合成文本: {text[:20]}...")
            # 常见接口路径可能有 / 或者 /tts
            response = requests.get(f"{self.api_url}/", params=params, timeout=30)
            
            if response.status_code == 200:
                with open(save_path, "wb") as f:
                    f.write(response.content)
                logging.info(f"语音合成成功，已保存至: {save_path}")
                return True
            else:
                # 尝试备用路径 /tts
                response = requests.post(f"{self.api_url}/tts", json=params, timeout=30)
                if response.status_code == 200:
                    with open(save_path, "wb") as f:
                        f.write(response.content)
                    logging.info(f"语音合成成功，已保存至: {save_path}")
                    return True
                
                logging.error(f"API 响应失败，状态码: {response.status_code}, 内容: {response.text}")
                return False

        except Exception as e:
            logging.error(f"调用 API 异常: {str(e)}")
            return False

    def _mock_generate(self, text, save_path):
        """Mock 模式：模拟生成，在没有启动 API 服务时供 UI 调试使用"""
        # 生成一个超简易的提示音（例如静音或者简单的正弦波wav）
        # 这里为简便起见，生成一个 1 秒的极简静音 WAV 文件数据
        import struct
        import wave
        
        try:
            with wave.open(save_path, 'wb') as wav_file:
                # 单声道，2字节，16000采样率
                wav_file.setparams((1, 2, 16000, 16000, 'NONE', 'not compressed'))
                # 写入 1 秒的静音数据
                for _ in range(16000):
                    data = struct.pack('<h', 0)
                    wav_file.writeframesraw(data)
            logging.info(f"[Mock] 模拟语音文件已生成: {save_path}")
            return True
        except Exception as e:
            logging.error(f"[Mock] 模拟语音生成失败: {str(e)}")
            return False

if __name__ == "__main__":
    client = TTSClient()
    print("API 是否运行:", client.is_api_running())
    # 测试 Mock 生成
    client.generate_speech("测试一下模拟生成功能是否正常", "test.wav")
