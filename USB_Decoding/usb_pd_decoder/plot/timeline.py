from __future__ import annotations
import matplotlib.pyplot as plt
from ..models import DecodedMessage
def plot_messages(messages: list[DecodedMessage]) -> None:
    if not messages:
        print("No messages to plot.")
        return
    x = [m.timestamp_us / 1000.0 for m in messages]
    y = list(range(len(messages)))
    labels = [m.message_type for m in messages]
    plt.figure(figsize=(10, 4))
    plt.scatter(x, y, s=20)
    plt.xlabel("Time (ms)")
    plt.ylabel("Message Index")
    plt.title("USB PD Decoded Messages")
    stride = max(1, len(messages) // 15)
    for i, (xx, yy, lbl) in enumerate(zip(x, y, labels)):
        if i % stride == 0:
            plt.text(xx, yy, lbl, fontsize=8)
    plt.tight_layout()
    plt.show()
