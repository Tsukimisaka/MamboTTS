import os
import sys
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QTextEdit, QPushButton, QLabel, 
                             QLineEdit, QFileDialog, QFrame, QGraphicsDropShadowEffect,
                             QSlider)
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QFont, QColor, QIcon
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput

# 导入推理客户端
from inference import TTSClient

class MamboTTSApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.tts_client = TTSClient()
        self.output_file = os.path.join(os.path.expanduser("~"), "Desktop", "mambo_output.wav")
        
        # 音频播放器初始化
        self.player = QMediaPlayer()
        self.audio_output = QAudioOutput()
        self.player.setAudioOutput(self.audio_output)
        
        self.init_ui()
        self.check_api_status()

    def init_ui(self):
        self.setWindowTitle("MamboTTS - 曼波配音工具")
        self.resize(750, 550)
        self.setMinimumSize(600, 450)
        
        # 全局深色猫咪肤色风格 (Catppuccin Mocha Inspired)
        self.setStyleSheet("""
            QMainWindow {
                background-color: #1e1e2e;
            }
            QWidget {
                color: #cdd6f4;
                font-family: 'Segoe UI', 'Microsoft YaHei', sans-serif;
            }
            QLabel {
                font-size: 13px;
            }
            QTextEdit {
                background-color: #11111b;
                border: 1px solid #313244;
                border-radius: 8px;
                padding: 10px;
                color: #a6adc8;
                font-size: 14px;
            }
            QTextEdit:focus {
                border: 1px solid #89b4fa;
            }
            QLineEdit {
                background-color: #11111b;
                border: 1px solid #313244;
                border-radius: 6px;
                padding: 6px 10px;
                color: #cdd6f4;
            }
            QLineEdit:focus {
                border: 1px solid #89b4fa;
            }
            QPushButton {
                background-color: #89b4fa;
                color: #11111b;
                border: none;
                border-radius: 6px;
                padding: 8px 16px;
                font-weight: bold;
                font-size: 13px;
            }
            QPushButton:hover {
                background-color: #b4befe;
            }
            QPushButton:pressed {
                background-color: #74c7ec;
            }
            QPushButton:disabled {
                background-color: #45475a;
                color: #7f849c;
            }
            QFrame#HeaderCard {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #89b4fa, stop:1 #b4befe);
                border-radius: 10px;
            }
            QFrame#MainCard {
                background-color: #181825;
                border-radius: 12px;
                border: 1px solid #313244;
            }
            QSlider::groove:horizontal {
                border: 1px solid #313244;
                height: 6px;
                background: #11111b;
                border-radius: 3px;
            }
            QSlider::handle:horizontal {
                background: #89b4fa;
                border: none;
                width: 14px;
                height: 14px;
                margin: -4px 0;
                border-radius: 7px;
            }
            QSlider::handle:horizontal:hover {
                background: #b4befe;
            }
        """)

        # 主布局
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(15)

        # 1. 顶部 Header 栏
        header_card = QFrame()
        header_card.setObjectName("HeaderCard")
        header_card.setFixedHeight(70)
        
        # 添加阴影效果
        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(15)
        shadow.setColor(QColor(0, 0, 0, 80))
        shadow.setOffset(0, 4)
        header_card.setGraphicsEffect(shadow)

        header_layout = QVBoxLayout(header_card)
        header_layout.setContentsMargins(15, 10, 15, 10)
        
        header_title = QLabel("MamboTTS")
        header_title.setStyleSheet("color: #11111b; font-size: 20px; font-weight: bold;")
        header_subtitle = QLabel("本地专属“曼波”文字转语音配音助手")
        header_subtitle.setStyleSheet("color: #313244; font-size: 12px;")
        
        header_layout.addWidget(header_title)
        header_layout.addWidget(header_subtitle)
        main_layout.addWidget(header_card)

        # 2. 中间主要工作区 Card
        main_card = QFrame()
        main_card.setObjectName("MainCard")
        main_card_layout = QVBoxLayout(main_card)
        main_card_layout.setContentsMargins(15, 15, 15, 15)
        main_card_layout.setSpacing(12)

        # 2.1 API 设置与状态
        api_layout = QHBoxLayout()
        api_label = QLabel("引擎接口:")
        self.api_input = QLineEdit("http://127.0.0.1:9880")
        self.api_input.textChanged.connect(self.on_api_url_changed)
        
        self.status_label = QLabel("正在检测服务...")
        self.status_label.setStyleSheet("color: #f9e2af; font-weight: bold;")
        
        api_layout.addWidget(api_label)
        api_layout.addWidget(self.api_input)
        api_layout.addWidget(self.status_label)
        main_card_layout.addLayout(api_layout)

        # 2.2 文本输入区域
        text_label = QLabel("配音文案内容:")
        main_card_layout.addWidget(text_label)
        
        self.text_input = QTextEdit()
        self.text_input.setPlaceholderText("在此输入想要配音的文字。例如：“大家好，我是曼波，今天给大家讲一个非常炸裂的故事……”")
        self.text_input.setText("大家好，我是曼波。今天给大家分享一个非常有意思的技术方案。")
        main_card_layout.addWidget(self.text_input)

        # 2.2.5 语速控制区域
        speed_layout = QHBoxLayout()
        speed_label = QLabel("语速调节:")
        
        self.speed_slider = QSlider(Qt.Horizontal)
        self.speed_slider.setMinimum(10)      # 0.1x
        self.speed_slider.setMaximum(300)     # 3.0x
        self.speed_slider.setValue(100)       # 1.0x (默认)
        self.speed_slider.setTickInterval(10)
        self.speed_slider.setTickPosition(QSlider.TicksBelow)
        
        self.speed_value_label = QLabel("1.00x")
        self.speed_value_label.setFixedWidth(50)
        self.speed_value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.speed_value_label.setStyleSheet("font-weight: bold; color: #89b4fa;")
        
        self.speed_slider.valueChanged.connect(self.on_speed_changed)
        
        speed_layout.addWidget(speed_label)
        speed_layout.addWidget(self.speed_slider)
        speed_layout.addWidget(self.speed_value_label)
        main_card_layout.addLayout(speed_layout)

        # 2.3 导出输出路径设置
        path_layout = QHBoxLayout()
        path_label = QLabel("保存路径:")
        self.path_input = QLineEdit(self.output_file)
        btn_browse = QPushButton("浏览...")
        btn_browse.setStyleSheet("background-color: #45475a; color: #cdd6f4;")
        btn_browse.clicked.connect(self.browse_path)
        
        path_layout.addWidget(path_label)
        path_layout.addWidget(self.path_input)
        path_layout.addWidget(btn_browse)
        main_card_layout.addLayout(path_layout)

        main_layout.addWidget(main_card)

        # 3. 底部操作按钮栏
        actions_layout = QHBoxLayout()
        
        self.btn_generate = QPushButton("⚡ 合成配音")
        self.btn_generate.setFixedHeight(40)
        self.btn_generate.clicked.connect(self.generate_voice)
        
        self.btn_play = QPushButton("▶ 播放音频")
        self.btn_play.setStyleSheet("background-color: #a6e3a1; color: #11111b;")
        self.btn_play.setFixedHeight(40)
        self.btn_play.setEnabled(False)
        self.btn_play.clicked.connect(self.play_voice)
        
        btn_open_folder = QPushButton("📁 打开输出目录")
        btn_open_folder.setStyleSheet("background-color: #f5c2e7; color: #11111b;")
        btn_open_folder.setFixedHeight(40)
        btn_open_folder.clicked.connect(self.open_output_folder)
        
        actions_layout.addWidget(self.btn_generate, 2)
        actions_layout.addWidget(self.btn_play, 1)
        actions_layout.addWidget(btn_open_folder, 1)
        main_layout.addLayout(actions_layout)

    def check_api_status(self):
        """异步/定时检查本地服务状态"""
        self.tts_client.api_url = self.api_input.text().strip()
        if self.tts_client.is_api_running():
            self.status_label.setText("🟢 本地 GPU 引擎在线")
            self.status_label.setStyleSheet("color: #a6e3a1; font-weight: bold;")
        else:
            self.status_label.setText("🟡 离线模式 (Mock 生成)")
            self.status_label.setStyleSheet("color: #f9e2af; font-weight: bold;")

    def on_api_url_changed(self):
        self.check_api_status()

    def browse_path(self):
        file_path, _ = QFileDialog.getSaveFileName(
            self, "选择保存路径", self.output_file, "WAV Files (*.wav)"
        )
        if file_path:
            self.output_file = file_path
            self.path_input.setText(file_path)

    def generate_voice(self):
        text = self.text_input.toPlainText().strip()
        if not text:
            self.status_label.setText("❌ 请输入配音文案")
            self.status_label.setStyleSheet("color: #f38ba8; font-weight: bold;")
            return

        self.btn_generate.setEnabled(False)
        self.btn_generate.setText("⏳ 正在合成中...")
        QApplication.processEvents()

        save_path = self.path_input.text().strip()
        speed_val = self.speed_slider.value() / 100.0
        
        # 调用后端合成语音，传入用户设置的语速
        success = self.tts_client.generate_speech(text, save_path, speed=speed_val)
        
        self.btn_generate.setEnabled(True)
        self.btn_generate.setText("⚡ 合成配音")

        if success:
            self.btn_play.setEnabled(True)
            self.status_label.setText("✅ 合成成功！")
            self.status_label.setStyleSheet("color: #a6e3a1; font-weight: bold;")
        else:
            self.status_label.setText("❌ 合成失败，请检查服务日志")
            self.status_label.setStyleSheet("color: #f38ba8; font-weight: bold;")

    def on_speed_changed(self, value):
        speed_float = value / 100.0
        self.speed_value_label.setText(f"{speed_float:.2f}x")

    def play_voice(self):
        save_path = self.path_input.text().strip()
        if os.path.exists(save_path):
            self.player.setSource(QUrl.fromLocalFile(save_path))
            self.player.play()
            self.status_label.setText("🔊 正在播放音频...")
            self.status_label.setStyleSheet("color: #a6e3a1; font-weight: bold;")
        else:
            self.status_label.setText("❌ 未找到音频文件")
            self.status_label.setStyleSheet("color: #f38ba8; font-weight: bold;")

    def open_output_folder(self):
        save_path = self.path_input.text().strip()
        folder_path = os.path.dirname(save_path)
        if os.path.exists(folder_path):
            os.startfile(folder_path)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MamboTTSApp()
    window.show()
    sys.exit(app.exec())
