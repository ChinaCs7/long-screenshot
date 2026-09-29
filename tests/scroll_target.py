# -*- coding: utf-8 -*-
"""端到端自测用的目标窗口：一个会自己慢慢往下滚的长列表。

启动后把列表区域的屏幕坐标（物理像素）打印成 "REGION x,y,w,h"，
再配合：python long_screenshot.py --region x,y,w,h --save-to out.png --duration 6
"""
import ctypes
import random
import sys
import tkinter as tk

ctypes.windll.shcore.SetProcessDpiAwareness(2)

seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 10
root = tk.Tk()
root.title("scroll target")
root.geometry("460x640+120+120")
root.attributes("-topmost", True)

tk.Label(root, text="吸顶标题栏（不随内容滚动）", bg="#E6F5EC", anchor="w",
         font=("Microsoft YaHei UI", 11), padx=10, pady=8).pack(fill="x")
body = tk.Frame(root)
body.pack(fill="both", expand=True)
bar = tk.Scrollbar(body)  # 真实的原生滚动条：滑块不随内容滚动，用来验证不会被「盖章」进长图
bar.pack(side="right", fill="y")
text = tk.Text(body, font=("Microsoft YaHei UI", 11), bd=0, padx=10,
               spacing1=6, spacing3=6, wrap="word", cursor="arrow", yscrollcommand=bar.set)
text.pack(side="left", fill="both", expand=True)
bar.configure(command=text.yview)
tk.Label(root, text="底部输入框（固定）", bg="#F5F5F5", anchor="w",
         font=("Microsoft YaHei UI", 10), padx=10, pady=12).pack(fill="x")

rnd = random.Random(42)
words = "今天 会议 改到 下午 三点 记得 带上 电脑 文件 已经 发群里 收到 好的 没问题 周报 截图 长图 测试".split()
for i in range(1, 401):
    if i % 37 == 0:
        text.insert("end", "\n\n\n")  # 夹几段空白
    line = " ".join(rnd.choice(words) for _ in range(rnd.randrange(3, 12)))
    text.insert("end", f"{i:03d}  {line}\n")
text.configure(state="disabled")

root.update()
print(f"REGION {body.winfo_rootx()},{body.winfo_rooty()},{body.winfo_width()},{body.winfo_height()}",
      flush=True)


def scroll():
    text.yview_scroll(2, "units")
    root.after(180, scroll)


root.after(2500, scroll)  # 先停一会儿，让截图程序拿到首帧
root.after(int(seconds * 1000), root.destroy)
root.mainloop()
