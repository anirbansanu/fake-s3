"""Programmatic icon — no bundled asset files needed."""

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QLinearGradient, QPainter, QPixmap


def make_icon(size: int = 256) -> QIcon:
    icon = QIcon()
    for edge in (16, 32, 48, 64, 128, 256):
        if edge > size:
            continue
        pixmap = QPixmap(edge, edge)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)

        gradient = QLinearGradient(0, 0, 0, edge)
        gradient.setColorAt(0.0, QColor("#2f9e6e"))
        gradient.setColorAt(1.0, QColor("#1c6b4a"))
        painter.setBrush(gradient)
        painter.setPen(Qt.NoPen)
        radius = edge * 0.22
        painter.drawRoundedRect(0, 0, edge, edge, radius, radius)

        painter.setPen(QColor("white"))
        font = QFont("Segoe UI", max(6, int(edge * 0.42)), QFont.Bold)
        painter.setFont(font)
        painter.drawText(QRect(0, 0, edge, edge), Qt.AlignCenter, "S3")
        painter.end()
        icon.addPixmap(pixmap)
    return icon
