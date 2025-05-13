import sys
import os
import json
import re
import random
import subprocess
import requests
import cv2

from PyQt6.QtWidgets import (
    QWidget, QApplication, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QFormLayout, QSpinBox, QTextEdit,
    QLineEdit, QCheckBox, QFileDialog, QProgressBar
)
from PyQt6.QtGui import QPixmap, QFont, QImage
from PyQt6.QtCore import Qt, QThread, pyqtSignal

from instagrapi import Client
from yt_dlp import YoutubeDL

CONFIG_FILE = "settings.json"
TEMP_VIDEO_PREFIX = "temp_video"

# ——— Yardımcı Fonksiyonlar —————————————————————————————————————————

def clean_caption(caption: str) -> str:
    caption = re.sub(r'@[\w]+', '', caption)
    caption = re.sub(r'http\S+', '', caption)
    caption = re.sub(r'#[\w]+', '', caption)
    return caption.strip()

def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', '_', name)
    name = re.sub(r'\s+', '_', name)
    return name.strip('_')

def create_thumbnail(video_path: str, thumb_path: str):
    cap = cv2.VideoCapture(video_path)
    best_score, best_frame = -1, None
    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    if fps and total_frames:
        samples = 5
        duration = total_frames / fps
        for i in range(samples):
            cap.set(cv2.CAP_PROP_POS_MSEC, (i+1)*duration/(samples+1)*1000)
            ret, frame = cap.read()
            if not ret:
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            score = float(gray.mean())
            if score > best_score:
                best_score = score
                best_frame = frame
    if best_frame is not None:
        cv2.imwrite(thumb_path, best_frame)
    else:
        subprocess.run([
            "ffmpeg", "-y", "-i", video_path,
            "-ss", "00:00:01.000", "-vframes", "1", thumb_path
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

# ——— Video List Item Widget ———————————————————————————————————————

class VideoListItemWidget(QWidget):
    def __init__(self, url: str, download_dir: str):
        super().__init__()
        self.url = url
        self.download_dir = download_dir

        self.checkbox = QCheckBox()
        self.thumb_label = QLabel()
        self.thumb_label.setFixedSize(100, 56)
        self.title_edit = QLineEdit("Başlık yükleniyor...")
        self.title_edit.setFont(QFont("Arial", 10))
        self.remove_btn = QPushButton("❌")

        layout = QHBoxLayout(self)
        layout.addWidget(self.checkbox)
        layout.addWidget(self.thumb_label)
        layout.addWidget(self.title_edit)
        layout.addWidget(self.remove_btn)

    def set_preview(self, pixmap: QPixmap, title: str):
        if not pixmap.isNull():
            scaled = pixmap.scaled(
                self.thumb_label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            )
            self.thumb_label.setPixmap(scaled)
        self.title_edit.setText(title)

    def set_progress(self, val: int):
        if not hasattr(self, 'progress_bar'):
            self.progress_bar = QProgressBar()
            self.layout().insertWidget(2, self.progress_bar)
        self.progress_bar.setValue(val)

# ——— Önizleme Thread ————————————————————————————————————————————

class PreviewThread(QThread):
    preview_ready = pyqtSignal(str, QPixmap, str)
    log_signal = pyqtSignal(str)

    def __init__(self, url: str):
        super().__init__()
        self.url = url

    def run(self):
        try:
            opts = {
                'skip_download': True,
                'quiet': True,
                'no_warnings': True,
                'noplaylist': True,
                'geo_bypass': True,
                'nocheckcertificate': True,
                'user_agent': 'Mozilla/5.0'
            }
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(self.url, download=False)
            title = info.get("title", "")
            thumb_url = info.get("thumbnail", "")
            pixmap = QPixmap()
            if thumb_url:
                resp = requests.get(thumb_url, timeout=10)
                pixmap.loadFromData(resp.content)
            self.preview_ready.emit(self.url, pixmap, title)
        except Exception as e:
            self.log_signal.emit(f"⚠️ Önizleme hatası [{self.url}]: {e}")

# ——— İndirme Handler ————————————————————————————————————————————

class DownloadHandler(QThread):
    log_signal = pyqtSignal(str)
    finished_signal = pyqtSignal(str)

    def __init__(self, widget: VideoListItemWidget):
        super().__init__()
        self.widget = widget
        self.url = widget.url

    def run(self):
        title = self.widget.title_edit.text()
        safe_name = sanitize_filename(title)
        outtmpl = os.path.join(self.widget.download_dir, f"{safe_name}.%(ext)s")
        opts = {
            'format': 'bestvideo*+bestaudio/best',
            'merge_output_format': 'mp4',                # ← Buraya eklendi
            'outtmpl': outtmpl,
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
            'geo_bypass': True,
            'nocheckcertificate': True,
            'user_agent': 'Mozilla/5.0'
        }
        try:
            self.log_signal.emit(f"⬇️ İndiriliyor: {self.url}")
            with YoutubeDL(opts) as ydl:
                ydl.download([self.url])
            self.log_signal.emit(f"✅ İndirildi: {self.url}")
            self.finished_signal.emit(self.url)
        except Exception as e:
            self.log_signal.emit(f"❌ İndirme hatası: {e}")

# ——— Instagram Yükleme Handler —————————————————————————————————————

class VideoHandler(QThread):
    log_signal = pyqtSignal(str)
    finished_signal = pyqtSignal(str)

    def __init__(self, widget: VideoListItemWidget, username: str, password: str, num_tags: int):
        super().__init__()
        self.widget = widget
        self.url = widget.url
        self.username = username
        self.password = password
        self.num_tags = num_tags

    def run(self):
        hash_name = str(abs(hash(self.url)))
        outtmpl = os.path.join(self.widget.download_dir, f"{TEMP_VIDEO_PREFIX}_{hash_name}.%(ext)s")
        opts = {
            'format': 'bestvideo*+bestaudio/best',
            'merge_output_format': 'mp4',                # ← Buraya eklendi
            'outtmpl': outtmpl,
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
            'geo_bypass': True,
            'nocheckcertificate': True,
            'user_agent': 'Mozilla/5.0'
        }
        try:
            # 1) İndir
            self.log_signal.emit(f"⬇️ İndiriliyor: {self.url}")
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(self.url, download=True)
            ext = 'mp4'  # artık kesinlikle .mp4
            video_file = os.path.join(
                self.widget.download_dir,
                f"{TEMP_VIDEO_PREFIX}_{hash_name}.{ext}"
            )
            # 2) Thumbnail
            thumb = os.path.join(self.widget.download_dir, f"{TEMP_VIDEO_PREFIX}_{hash_name}.jpg")
            create_thumbnail(video_file, thumb)
            # 3) Hashtag & açıklama
            title = self.widget.title_edit.text()
            caption = clean_caption(title)
            words = re.findall(r"\w+", title)
            candidates = [w for w in words if len(w) > 3]
            if candidates and self.num_tags > 0:
                sample = random.sample(candidates, min(self.num_tags, len(candidates)))
                tags = " ".join(f"#{w.lower()}" for w in sample)
                caption = f"{caption} {tags}"
                self.log_signal.emit(f"🔖 Hashtaglar: {tags}")
            cta = "daha fazla içerik için bizi takip edebilirsiniz @yzgunlukleri"
            caption = f"{caption} {cta}"
            # 4) Instagram’a yükle
            self.log_signal.emit("🔐 Instagram'a bağlanılıyor...")
            cl = Client()
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, 'r') as f:
                    data = json.load(f)
                    cl.load_settings(data.get("session_file", "session.json"))
            cl.login(self.username, self.password)
            cl.dump_settings("session.json")
            self.log_signal.emit("📤 Yükleme...")
            cl.clip_upload(video_file, caption, thumbnail=thumb)
            self.log_signal.emit(f"✅ Yüklendi: {title}")
            self.finished_signal.emit(self.url)
        except Exception as e:
            self.log_signal.emit(f"❌ Hata: {e}")

# ——— Ana Pencere ——————————————————————————————————————————————

class InstagramUploader(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("📸 Video Uploader Plus")
        self.setGeometry(100, 100, 900, 700)

        self.preview_threads = []
        self.handler_threads = []

        self.setup_ui_components()
        self.setup_layout()
        self.setup_connections()
        self.load_settings()

    def setup_ui_components(self):
        self.dir_label = QLabel()
        self.dir_label.setFixedWidth(400)
        self.dir_btn = QPushButton("📁 İndirme Klasörü")
        self.url_input = QTextEdit()
        self.url_input.setPlaceholderText("Her satıra bir video URL yazıp 'Listeye Ekle' deyiniz...")
        self.add_button = QPushButton("➕ Listeye Ekle")
        self.header_label = QLabel("🎬 Yüklenecek Videolar")
        self.header_label.setFont(QFont("Arial", 12, QFont.Weight.Bold))
        self.list_widget = QListWidget()
        self.tag_count = QSpinBox()
        self.tag_count.setRange(0, 10)
        self.tag_count.setValue(3)
        self.download_button = QPushButton("⬇️ Seçileni İndir")
        self.upload_button = QPushButton("🚀 Seçileni Yükle")
        self.remove_button = QPushButton("🗑️ Seçileni Kaldır")
        self.log_label = QLabel()
        self.log_label.setWordWrap(True)

    def setup_layout(self):
        main_layout = QVBoxLayout(self)
        dir_layout = QHBoxLayout()
        dir_layout.addWidget(QLabel("📂 İndirme Klasörü:"))
        dir_layout.addWidget(self.dir_label)
        dir_layout.addWidget(self.dir_btn)
        main_layout.addLayout(dir_layout)

        form_layout = QFormLayout()
        form_layout.addRow("🎞️ URL Girişi:", self.url_input)
        form_layout.addRow("", self.add_button)
        main_layout.addLayout(form_layout)

        main_layout.addWidget(self.header_label)
        main_layout.addWidget(self.list_widget)

        ctrl_layout = QHBoxLayout()
        ctrl_layout.addWidget(QLabel("🏷️ Tag Sayısı:"))
        ctrl_layout.addWidget(self.tag_count)
        ctrl_layout.addWidget(self.download_button)
        ctrl_layout.addStretch()
        ctrl_layout.addWidget(self.upload_button)
        ctrl_layout.addWidget(self.remove_button)
        main_layout.addLayout(ctrl_layout)

        main_layout.addWidget(self.log_label)

    def setup_connections(self):
        self.dir_btn.clicked.connect(self.choose_directory)
        self.add_button.clicked.connect(self.add_urls)
        self.download_button.clicked.connect(self.download_selected)
        self.upload_button.clicked.connect(self.upload_selected)
        self.remove_button.clicked.connect(self.remove_selected)

    def choose_directory(self):
        dir_path = QFileDialog.getExistingDirectory(self, "Klasör Seç", os.getcwd())
        if dir_path:
            self.download_dir = dir_path
            self.dir_label.setText(dir_path)
            self.save_settings()

    def add_urls(self):
        text = self.url_input.toPlainText().strip()
        if not text:
            return
        urls = [u.strip() for u in text.splitlines() if u.strip()]
        for url in urls:
            item = VideoListItemWidget(url, self.download_dir)
            list_item = QListWidgetItem()
            list_item.setSizeHint(item.sizeHint())
            self.list_widget.addItem(list_item)
            self.list_widget.setItemWidget(list_item, item)
            item.remove_btn.clicked.connect(
                lambda _, it=list_item: self.list_widget.takeItem(self.list_widget.row(it))
            )
            preview = PreviewThread(url)
            preview.preview_ready.connect(lambda u, pm, ti, w=item: w.set_preview(pm, ti))
            preview.log_signal.connect(self.append_log)
            preview.start()
            self.preview_threads.append(preview)
        self.url_input.clear()

    def download_selected(self):
        for i in range(self.list_widget.count()):
            w = self.list_widget.itemWidget(self.list_widget.item(i))
            if w and w.checkbox.isChecked():
                dh = DownloadHandler(w)
                dh.log_signal.connect(self.append_log)
                dh.finished_signal.connect(lambda u: self.append_log(f"✅ İndirildi: {u}"))
                dh.start()
                self.handler_threads.append(dh)

    def upload_selected(self):
        for i in reversed(range(self.list_widget.count())):
            list_item = self.list_widget.item(i)
            w = self.list_widget.itemWidget(list_item)
            if w and w.checkbox.isChecked():
                vh = VideoHandler(w, self.username, self.password, self.tag_count.value())
                vh.log_signal.connect(self.append_log)
                vh.finished_signal.connect(lambda u, it=list_item: self.list_widget.takeItem(self.list_widget.row(it)))
                vh.start()
                self.handler_threads.append(vh)

    def remove_selected(self):
        for i in reversed(range(self.list_widget.count())):
            list_item = self.list_widget.item(i)
            w = self.list_widget.itemWidget(list_item)
            if w and w.checkbox.isChecked():
                self.list_widget.takeItem(self.list_widget.row(list_item))

    def append_log(self, msg: str):
        self.log_label.setText(msg)

    def load_settings(self):
        self.download_dir = os.getcwd()
        self.username = ""
        self.password = ""
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                self.username = data.get("username", "")
                self.password = data.get("password", "")
                self.download_dir = data.get("download_dir", self.download_dir)
        self.dir_label.setText(self.download_dir)

    def save_settings(self):
        data = {
            "username": self.username,
            "password": self.password,
            "download_dir": self.download_dir
        }
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = InstagramUploader()
    window.show()
    sys.exit(app.exec())
