#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Асинхронный загрузчик видео и аудио с YouTube.
Используется pytubefix (форк youtube-dl), интерфейс PyQt6 и поддерживается HTTP-прокси.

pip install pyqt6 pytubefix
"""

import sys
import os
import re
import asyncio
from functools import partial

from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QLabel,
    QLineEdit, QPushButton, QTextEdit, QFileDialog,
    QHBoxLayout, QProgressBar
)
from PyQt6.QtCore import Qt, pyqtSignal, QObject, QThread

from pytubefix import YouTube


# ----------------------------------------------------------------------
def is_valid_proxy(proxy: str) -> bool:
    """
    Проверка корректности HTTP‑прокси.
    Ожидаемый формат: http://host:port
    """
    if not proxy:
        return True  # Разрешаем скачивание, если строка HTTP-прокси пустая

    pattern = re.compile(
        r'^https?://'                     # http:// или https://
        r'(?:(?:[a-zA-Z0-9\-\.]+)'       # hostname (поддомены)
        r'(?:\:[0-9]{1,5})?)$'            # :port (необязательно)
    )
    return bool(pattern.match(proxy))


# ----------------------------------------------------------------------
class Worker(QObject):
    """Асинхронный worker‑объект для скачивания видео/аудио."""
    finished = pyqtSignal(str)   # сообщение о завершении
    progress = pyqtSignal(int)   # процент загрузки

    def __init__(self, url: str, out_path: str, proxy: str, fmt: str):
        super().__init__()
        self.url = url
        self.out_path = out_path
        self.proxy = proxy
        self.fmt = fmt          # "video" или "audio"

    async def run(self):
        """Запуск скачивания в отдельном потоке через asyncio.run_in_executor."""
        try:
            yt = YouTube(
                self.url,
                proxies={"http": self.proxy, "https": self.proxy} if self.proxy else None
            )
        except Exception as exc:
            # Ошибка при получении метаданных
            self.finished.emit(f"❌ Ошибка при подключении к YouTube: {exc}")
            return

        try:
            stream = (
                yt.streams.filter(progressive=True).order_by("resolution").desc().first()
                if self.fmt == "video"
                else yt.streams.filter(only_audio=True)
                           .order_by("abr")
                           .desc()
                           .first()
            )
            if not stream:
                raise RuntimeError("Подходящий поток не найден")

            total = stream.filesize
            downloaded = 0

            # Колбэк для обновления прогресса
            def _on_progress(stream_, chunk, bytes_remaining):
                nonlocal downloaded
                downloaded = total - bytes_remaining
                self.progress.emit(int(downloaded / total * 100))

            stream.on_progress = _on_progress

            loop = asyncio.get_running_loop()
            # Запуск синхронного  download в executor (пул потоков)
            await loop.run_in_executor(
                None,
                partial(
                    stream.download,
                    output_path=self.out_path,
                    filename_prefix="",
                ),
            )
            self.finished.emit(f"✅ Сохранено: {self.out_path}")
        except Exception as exc:
            # Любая ошибка во время скачивания
            self.finished.emit(f"❌ Ошибка при скачивании: {exc}")


# ----------------------------------------------------------------------
class DownloadThread(QThread):
    """
    Поток, в котором будет работать asyncio‑loop.
    Поток создаётся один раз и обрабатывает все скачивания последовательно.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.loop = None

    def run(self) -> None:
        # Внутри потока создаём новый event loop
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def schedule_task(self, coro):
        """Запуск coroutine в своём loop‑е."""
        if not self.isRunning():
            raise RuntimeError("DownloadThread не запущен")
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def stop(self) -> None:
        if self.loop and self.isRunning():
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.wait()


# ----------------------------------------------------------------------
class MainWindow(QWidget):
    """Главное окно приложения."""
    def __init__(self):
        super().__init__()
        self.setWindowTitle("YT Downloader (async + proxy)")
        self.resize(500, 300)

        layout = QVBoxLayout(self)

        # --- URL ---------------------------------------------------------
        hurl = QHBoxLayout()
        hurl.addWidget(QLabel("URL:"))
        self.url_edit = QLineEdit()
        hurl.addWidget(self.url_edit)
        layout.addLayout(hurl)

        # --- Папка -------------------------------------------------------
        hout = QHBoxLayout()
        hout.addWidget(QLabel("Folder:"))
        self.folder_edit = QLineEdit()
        hout.addWidget(self.folder_edit)
        btn_folder = QPushButton("Browse")
        btn_folder.clicked.connect(lambda: self.select_folder())
        hout.addWidget(btn_folder)
        layout.addLayout(hout)

        # --- Прокси -------------------------------------------------------
        hproxy = QHBoxLayout()
        hproxy.addWidget(QLabel("HTTP‑Proxy (optional):"))
        self.proxy_edit = QLineEdit()
        self.proxy_edit.setPlaceholderText(
            "http://127.0.0.1:8881"   # пример HTTP‑прокси в placeholder'е
        )
        hproxy.addWidget(self.proxy_edit)
        layout.addLayout(hproxy)

        # --- Кнопки -------------------------------------------------------
        hbttns = QHBoxLayout()
        self.btn_video = QPushButton("Download Video")
        self.btn_audio = QPushButton("Download Audio")
        hbttns.addWidget(self.btn_video)
        hbttns.addWidget(self.btn_audio)
        layout.addLayout(hbttns)

        # --- Прогресс бар -------------------------------------------------
        self.progress_bar = QProgressBar()
        self.progress_bar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                border: 1px solid grey;
                border-radius: 5px;
                text-align: center;
                color: #282828;
            }
            QProgressBar::chunk {
                background-color: #4CAF50;
                width: 10px;
            }
        """)
        layout.addWidget(self.progress_bar)

        # --- Log area ----------------------------------------------------
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(self.log)

        # --- Connections -----------------------------------------------
        self.btn_video.clicked.connect(lambda: self.start_download("video"))
        self.btn_audio.clicked.connect(lambda: self.start_download("audio"))

        # --- Async worker thread ----------------------------------------
        self.thread = DownloadThread()
        self.thread.start()

    def select_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select download folder")
        if folder:
            self.folder_edit.setText(folder)

    def start_download(self, fmt: str) -> None:
        url = self.url_edit.text().strip()
        out_path = self.folder_edit.text().strip() or os.getcwd()
        proxy = self.proxy_edit.text().strip()

        if not url:
            self.log.append("❌ URL is required")
            return

        # ---------- Проверка каталога и прав ----------
        if not os.path.isdir(out_path):
            self.log.append(f"❌ Каталог не существует: {out_path}")
            return
        if not os.access(out_path, os.W_OK):
            self.log.append(f"❌ Нет прав на запись в каталоге: {out_path}")
            return

        # ---------- Валидация прокси ----------
        if proxy and not is_valid_proxy(proxy):
            self.log.append("⚠️ Неверный формат HTTP‑прокси. Ожидается http://host:port")
            return

        worker = Worker(url, out_path, proxy, fmt)
        worker.progress.connect(self.progress_bar.setValue)
        worker.finished.connect(lambda msg: self.log.append(msg))

        # Запуск coroutine в потоке с event‑loop
        self.thread.schedule_task(worker.run())

    def closeEvent(self, event):
        """Корректно завершаем поток при закрытии окна."""
        self.thread.stop()
        super().closeEvent(event)


# ----------------------------------------------------------------------
def main() -> None:
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
